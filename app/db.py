"""SQLite 持久层：危险区不可变修订版本、生效时段、轨迹点与点更正履历。

修订模型（危险区）
------------------
- 每次登记（含同 ID 覆盖更新）向 ``zone_revisions`` 追加一行不可变记录，
  ``revision`` 为 SQLite AUTOINCREMENT 分配的全局递增修订号（登记事务内原子产生）。
- 截至修订号 R，每个区取其 ``revision <= R`` 的最后一行；指定版本中尚未登记的区无行、不参与核算。
- 旧库（zones / zone_windows 两张表）首次启动时一次性迁移为基线版本 revision=1..，
  迁移后删除旧表；不推断迁移前历史。

轨迹版本与更正模型
------------------
- 每个车次在 ``trip_versions`` 中维护一个 ``track_version``：首批真正录入新点后置 1，
  此后每批真正新点上传或每批成功更正恰好 +1；纯幂等重传不推进。
- ``points.status`` 为 ``active`` / ``revoked``：撤销是软删除，行与主键保留以备履历，
  有效点集只含 active 行；同车次同刻唯一约束只对 active 点生效（可延迟，支持交换时刻）。
- 每次成功更正向 ``point_corrections`` 追加一行不可变履历（前后值、原因、版本），
  失败整批回滚、不留履历；已撤销 point_id 不得再经上传或更正重用。

并发
----
- 服务进程内以一把可重入锁串行化对共享连接的访问；写事务使用 ``BEGIN IMMEDIATE``，
  多进程场景下由 SQLite 写锁保证修订号严格串行。
- 查询通过只读快照（``BEGIN``/``COMMIT``）一次性读完全部版本行，不会读到半次提交。

- 所有写操作在单个事务中完成，批量失败不写入部分数据。
- 重启后数据保留（DZ_DB_PATH 指向持久文件）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import Settings

_SCHEMA = """
-- 危险区不可变修订历史：每次登记/覆盖更新追加一行，永不修改、不删除
CREATE TABLE IF NOT EXISTS zone_revisions (
    revision     INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全局递增修订号
    zone_id      TEXT NOT NULL,
    exterior     TEXT NOT NULL,          -- GeoJSON-like [[x,y],...]（闭合）
    holes        TEXT NOT NULL,          -- [[[x,y],...], ...]（各环闭合）
    windows      TEXT NOT NULL,          -- [{start_epoch,end_epoch,start_iso,end_iso}, ...]
    created_at   TEXT NOT NULL,          -- 该 zone_id 首次登记时间（沿用，不随覆盖更新变化）
    registered_at TEXT NOT NULL          -- 本次登记时间
);
CREATE INDEX IF NOT EXISTS idx_zone_revisions_zone_rev
    ON zone_revisions(zone_id, revision);

CREATE TABLE IF NOT EXISTS points (
    trip_id    TEXT NOT NULL,
    point_id   TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',  -- active=有效；revoked=已更正撤销（行保留备履历）
    PRIMARY KEY (trip_id, point_id)
);
-- 同车次同刻必须是同一个点：仅对有效点唯一（撤销点不占位；DEFERRABLE 允许事务内交换时刻）
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_active_trip_ts
    ON points(trip_id, ts_epoch) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_points_trip ON points(trip_id, ts_epoch);

-- 车次轨迹版本：首批真正录入新点置 1；此后每批真正新点或成功更正 +1，幂等重传不推进
CREATE TABLE IF NOT EXISTS trip_versions (
    trip_id       TEXT PRIMARY KEY,
    track_version INTEGER NOT NULL CHECK (track_version >= 1)
);

-- 点更正履历：只追加、不可改；一次成功更正一行，整组前后值以 JSON 落库
CREATE TABLE IF NOT EXISTS point_corrections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id      TEXT NOT NULL,
    base_version INTEGER NOT NULL,   -- 客户核对时所基于的版本
    new_version  INTEGER NOT NULL,   -- 更正成功后的版本（= base_version + 1）
    reason       TEXT NOT NULL,      -- 非空更正原因
    changes      TEXT NOT NULL,      -- [{point_id, op, before, after}, ...]（升序、不可改）
    affected     INTEGER NOT NULL,   -- 受影响点数（= changes 长度）
    correction_id TEXT,              -- 可选客户幂等令牌
    created_at   TEXT NOT NULL       -- UTC ISO 8601
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_point_corr_client
    ON point_corrections(trip_id, correction_id) WHERE correction_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_point_corr_trip_ver
    ON point_corrections(trip_id, id);

-- 更正请求幂等重放存档：同 (trip, correction_id) 整组内容必须一致
CREATE TABLE IF NOT EXISTS point_correction_requests (
    trip_id       TEXT NOT NULL,
    correction_id TEXT NOT NULL,
    payload       TEXT NOT NULL,     -- 原始请求体（reason + items），用于一致性核对
    correction_pk INTEGER NOT NULL,  -- 对应 point_corrections.id
    PRIMARY KEY (trip_id, correction_id)
);
"""

_LEGACY_TABLES = ("zones", "zone_windows")


def _connect(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30,
        isolation_level=None,  # 显式事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 可重入锁：服务进程内串行化共享连接上的所有访问，杜绝读到半次提交
        self._lock = threading.RLock()
        self._conn = _connect(settings)
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            # 旧库 points 表先升级（补 status 列），再执行建表脚本：
            # 否则脚本中“仅 active”部分索引在旧表（尚无 status 列）上无法创建
            self._migrate_points_status(self._conn)
            self._conn.executescript(_SCHEMA)
            self._migrate_legacy_zones(self._conn)

    def _migrate_points_status(self, conn: sqlite3.Connection) -> None:
        """旧库 points 表一次性升级：补 status 列、用“仅 active”部分唯一索引替换全量索引。

        幂等：status 列已存在则跳过 ALTER；旧索引每次以 IF EXISTS 安全清理。
        """
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(points)")}
        if not cols:
            return  # 全新库（建表语句已含 status）
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE points ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        # 旧版全量唯一索引（已撤销点也参与）会阻止释放时刻，替换为部分索引
        conn.execute("DROP INDEX IF EXISTS idx_points_trip_ts")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_points_active_trip_ts "
            "ON points(trip_id, ts_epoch) WHERE status = 'active'"
        )

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None

    def _migrate_legacy_zones(self, conn: sqlite3.Connection) -> None:
        """旧库现存区域一次性迁移为基线版本（revision 1..N，按 zone_id 排序）。

        幂等：仅当旧 zones 表存在时执行；迁移完成后删除旧表。不推断迁移前历史。
        """
        if not self._table_exists(conn, "zones"):
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            legacy_rows = list(
                conn.execute("SELECT zone_id, exterior, holes, created_at FROM zones ORDER BY zone_id")
            )
            for row in legacy_rows:
                legacy_windows = list(
                    conn.execute(
                        "SELECT start_epoch, end_epoch, start_iso, end_iso "
                        "FROM zone_windows WHERE zone_id = ? ORDER BY start_epoch",
                        (row["zone_id"],),
                    )
                )
                windows = [
                    {
                        "start_epoch": w["start_epoch"],
                        "end_epoch": w["end_epoch"],
                        "start_iso": w["start_iso"],
                        "end_iso": w["end_iso"],
                    }
                    for w in legacy_windows
                ]
                # 基线版本：created_at / registered_at 均沿用旧 created_at
                conn.execute(
                    "INSERT INTO zone_revisions"
                    "(zone_id, exterior, holes, windows, created_at, registered_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        row["zone_id"],
                        row["exterior"],
                        row["holes"],
                        json.dumps(windows),
                        row["created_at"],
                        row["created_at"],
                    ),
                )
            for name in _LEGACY_TABLES:
                conn.execute(f"DROP TABLE {name}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：进程锁 + BEGIN IMMEDIATE（多进程时由 SQLite 写锁串行化）。"""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read_view(self) -> Iterator[sqlite3.Connection]:
        """只读快照事务：块内所有读取自同一快照，并发提交在块内不可见。"""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ----- 修订号 ----------------------------------------------------------

    def current_revision(self) -> int:
        """当前全局修订号（尚无任何登记时为 0）。"""
        with self._lock:
            cur = self._conn.execute("SELECT COALESCE(MAX(revision), 0) AS r FROM zone_revisions")
            return int(cur.fetchone()["r"])

    # ----- 危险区修订版本 --------------------------------------------------

    def insert_zone_revision(
        self,
        conn: sqlite3.Connection,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        windows: List[Dict[str, Any]],
        created_iso: str,
        registered_iso: str,
    ) -> int:
        """在给定写事务内追加一条不可变修订记录，返回由 SQLite 分配的全局修订号。"""
        cur = conn.execute(
            "INSERT INTO zone_revisions"
            "(zone_id, exterior, holes, windows, created_at, registered_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                zone_id,
                json.dumps(exterior),
                json.dumps(holes),
                json.dumps(windows),
                created_iso,
                registered_iso,
            ),
        )
        return int(cur.lastrowid)

    def first_zone_revision(self, conn: sqlite3.Connection, zone_id: str) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT created_at FROM zone_revisions WHERE zone_id = ? ORDER BY revision LIMIT 1",
            (zone_id,),
        )
        return cur.fetchone()

    def latest_zone_row(self, zone_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._latest_zone_row(self._conn, zone_id)

    @staticmethod
    def _latest_zone_row(conn: sqlite3.Connection, zone_id: str) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM zone_revisions WHERE zone_id = ? ORDER BY revision DESC LIMIT 1",
            (zone_id,),
        )
        return cur.fetchone()

    @staticmethod
    def _zone_row_at(conn: sqlite3.Connection, zone_id: str, revision: int) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM zone_revisions WHERE zone_id = ? AND revision <= ? "
            "ORDER BY revision DESC LIMIT 1",
            (zone_id, revision),
        )
        return cur.fetchone()

    def list_latest_zone_rows(self) -> List[sqlite3.Row]:
        with self._lock:
            return self._list_latest_zone_rows(self._conn)

    @staticmethod
    def _list_latest_zone_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT zr.* FROM zone_revisions zr "
                "JOIN (SELECT zone_id, MAX(revision) AS mr FROM zone_revisions GROUP BY zone_id) m "
                "ON zr.zone_id = m.zone_id AND zr.revision = m.mr "
                "ORDER BY zr.zone_id"
            )
        )

    @staticmethod
    def _list_zone_rows_at(conn: sqlite3.Connection, revision: int) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT zr.* FROM zone_revisions zr "
                "JOIN (SELECT zone_id, MAX(revision) AS mr FROM zone_revisions "
                "      WHERE revision <= ? GROUP BY zone_id) m "
                "ON zr.zone_id = m.zone_id AND zr.revision = m.mr "
                "ORDER BY zr.zone_id",
                (revision,),
            )
        )

    def list_revision_rows(self, limit: int = 500) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT revision, zone_id, created_at, registered_at FROM zone_revisions "
                    "ORDER BY revision DESC LIMIT ?",
                    (int(limit),),
                )
            )

    def list_zone_revision_rows(self, zone_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT revision, zone_id, created_at, registered_at FROM zone_revisions "
                    "WHERE zone_id = ? ORDER BY revision",
                    (zone_id,),
                )
            )

    # ----- 轨迹点 ----------------------------------------------------------

    def get_point(self, trip_id: str, point_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? AND point_id = ?",
                (trip_id, point_id),
            )
            return cur.fetchone()

    def find_active_point_at_ts(self, trip_id: str, ts_epoch: float) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? AND ts_epoch = ? AND status = 'active'",
                (trip_id, ts_epoch),
            )
            return cur.fetchone()

    def list_trip_points(self, trip_id: str) -> List[sqlite3.Row]:
        """有效（active）点，按时刻排序；已撤销点不返回。"""
        with self._lock:
            return self._list_trip_points(self._conn, trip_id)

    @staticmethod
    def _list_trip_points(conn: sqlite3.Connection, trip_id: str) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM points "
                "WHERE trip_id = ? AND status = 'active' ORDER BY ts_epoch, point_id",
                (trip_id,),
            )
        )

    def count_trip_points(self, trip_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM points WHERE trip_id = ? AND status = 'active'",
                (trip_id,),
            )
            return int(cur.fetchone()["c"])

    def insert_points(
        self, conn: sqlite3.Connection, rows: List[Tuple[str, str, float, float, float]]
    ) -> None:
        conn.executemany(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y, status) "
            "VALUES (?,?,?,?,?,'active')",
            rows,
        )

    def trip_version(self, conn: sqlite3.Connection, trip_id: str) -> int:
        """车次当前轨迹版本：从未有真正新点入库（即无版本行）时为 0。"""
        cur = conn.execute(
            "SELECT track_version FROM trip_versions WHERE trip_id = ?", (trip_id,)
        )
        row = cur.fetchone()
        return int(row["track_version"]) if row is not None else 0

    def bump_trip_version(
        self, conn: sqlite3.Connection, trip_id: str, has_existing_version: bool
    ) -> int:
        """真正录入新点 / 成功更正后推进恰好一个版本，返回新版本号。

        首批真正新点（版本行不存在）置 1；之后 UPSERT 自增。
        """
        if has_existing_version:
            conn.execute(
                "UPDATE trip_versions SET track_version = track_version + 1 WHERE trip_id = ?",
                (trip_id,),
            )
        else:
            conn.execute(
                "INSERT INTO trip_versions(trip_id, track_version) VALUES (?, 1)", (trip_id,)
            )
        return self.trip_version(conn, trip_id)

    def get_point_rows(
        self, conn: sqlite3.Connection, trip_id: str, point_ids: List[str]
    ) -> Dict[str, sqlite3.Row]:
        if not point_ids:
            return {}
        placeholders = ",".join("?" for _ in point_ids)
        rows = conn.execute(
            f"SELECT * FROM points WHERE trip_id = ? AND point_id IN ({placeholders})",
            [trip_id] + list(point_ids),
        )
        return {r["point_id"]: r for r in rows}

    @staticmethod
    def _make_scratch_ts(existing_ts: List[float], seq: int) -> float:
        """基于全量已用时刻生成一个保证未被占用的临时时刻（仅更正事务内瞬时存在）。"""
        used = set(existing_ts)
        candidate = (min(used) if used else 0.0) - 1e9 - seq - 1.0
        while candidate in used:
            candidate -= 1.0
        used.add(candidate)
        return candidate

    def apply_point_correction(
        self,
        conn: sqlite3.Connection,
        trip_id: str,
        updates: List[Tuple[str, float, float, float]],
        revokes: List[str],
        inserts: List[Tuple[str, float, float, float]],
    ) -> None:
        """在调用方已校验最终状态后，分阶段应用更正，规避同刻唯一索引的中间冲突。

        1) 撤销点置 revoked（先释放其原时刻）；
        2) 待更新点改写到彼此不同且远离真实时刻的临时时刻（坐标此时即改为新值）；
        3) 更新点写入最终时刻；
        4) 插入新点。
        """
        existing_ts = [
            r[0]
            for r in conn.execute(
                "SELECT ts_epoch FROM points WHERE trip_id = ?", (trip_id,)
            ).fetchall()
        ]
        if revokes:
            placeholders = ",".join("?" for _ in revokes)
            conn.execute(
                f"UPDATE points SET status = 'revoked' "
                f"WHERE trip_id = ? AND point_id IN ({placeholders}) AND status = 'active'",
                [trip_id] + list(revokes),
            )
        scratch = [self._make_scratch_ts(existing_ts, seq) for seq in range(len(updates))]
        for (point_id, ts_epoch, x, y), tmp in zip(updates, scratch):
            conn.execute(
                "UPDATE points SET ts_epoch = ?, x = ?, y = ? "
                "WHERE trip_id = ? AND point_id = ? AND status = 'active'",
                (tmp, x, y, trip_id, point_id),
            )
        for point_id, ts_epoch, x, y in updates:
            conn.execute(
                "UPDATE points SET ts_epoch = ? WHERE trip_id = ? AND point_id = ?",
                (ts_epoch, trip_id, point_id),
            )
        if inserts:
            conn.executemany(
                "INSERT INTO points(trip_id, point_id, ts_epoch, x, y, status) "
                "VALUES (?,?,?,?,?,'active')",
                [(trip_id, pid, ts, x, y) for pid, ts, x, y in inserts],
            )

    def insert_point_correction(
        self,
        conn: sqlite3.Connection,
        trip_id: str,
        base_version: int,
        new_version: int,
        reason: str,
        changes: List[Dict[str, Any]],
        created_iso: str,
        correction_id: Optional[str],
        payload_json: str,
    ) -> int:
        """追加不可变更正履历及可选幂等请求存档，返回履历主键。"""
        cur = conn.execute(
            "INSERT INTO point_corrections"
            "(trip_id, base_version, new_version, reason, changes, affected, "
            " correction_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                trip_id,
                base_version,
                new_version,
                reason,
                json.dumps(changes, ensure_ascii=False),
                len(changes),
                correction_id,
                created_iso,
            ),
        )
        pk = int(cur.lastrowid)
        if correction_id is not None:
            conn.execute(
                "INSERT INTO point_correction_requests"
                "(trip_id, correction_id, payload, correction_pk) VALUES (?,?,?,?)",
                (trip_id, correction_id, payload_json, pk),
            )
        return pk

    def get_idempotent_request(
        self, conn: sqlite3.Connection, trip_id: str, correction_id: str
    ) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM point_correction_requests WHERE trip_id = ? AND correction_id = ?",
            (trip_id, correction_id),
        )
        return cur.fetchone()

    def list_point_correction_rows(self, trip_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM point_corrections WHERE trip_id = ? ORDER BY id",
                    (trip_id,),
                )
            )

    def clear_all_for_tests(self) -> None:
        """测试专用：清空业务数据并重置修订号序列。"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM points")
            conn.execute("DELETE FROM zone_revisions")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='zone_revisions'")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='point_corrections'")
            conn.execute("DELETE FROM point_corrections")
            conn.execute("DELETE FROM point_correction_requests")
            conn.execute("DELETE FROM trip_versions")
