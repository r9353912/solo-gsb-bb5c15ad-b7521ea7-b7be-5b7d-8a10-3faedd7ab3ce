"""SQLite 持久层：危险区不可变修订版本、生效时段、轨迹点。

修订模型
--------
- 每次登记（含同 ID 覆盖更新）向 ``zone_revisions`` 追加一行不可变记录，
  ``revision`` 为 SQLite AUTOINCREMENT 分配的全局递增修订号（登记事务内原子产生）。
- 截至修订号 R，每个区取其 ``revision <= R`` 的最后一行；指定版本中尚未登记的区无行、不参与核算。
- 旧库（zones / zone_windows 两张表）首次启动时一次性迁移为基线版本 revision=1..，
  迁移后删除旧表；不推断迁移前历史。

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
    PRIMARY KEY (trip_id, point_id)
);
-- 同车次同刻必须是同一个点：部分索引防止 (trip, ts) 重复
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_trip_ts
    ON points(trip_id, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_points_trip ON points(trip_id, ts_epoch);
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
            self._conn.executescript(_SCHEMA)
            self._migrate_legacy_zones(self._conn)

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

    def find_point_at_ts(self, trip_id: str, ts_epoch: float) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? AND ts_epoch = ?",
                (trip_id, ts_epoch),
            )
            return cur.fetchone()

    def list_trip_points(self, trip_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return self._list_trip_points(self._conn, trip_id)

    @staticmethod
    def _list_trip_points(conn: sqlite3.Connection, trip_id: str) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM points WHERE trip_id = ? ORDER BY ts_epoch, point_id",
                (trip_id,),
            )
        )

    def count_trip_points(self, trip_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM points WHERE trip_id = ?", (trip_id,)
            )
            return int(cur.fetchone()["c"])

    def insert_points(
        self, conn: sqlite3.Connection, rows: List[Tuple[str, str, float, float, float]]
    ) -> None:
        conn.executemany(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
            rows,
        )

    def clear_all_for_tests(self) -> None:
        """测试专用：清空业务数据并重置修订号序列。"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM points")
            conn.execute("DELETE FROM zone_revisions")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='zone_revisions'")
