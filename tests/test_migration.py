"""旧库迁移测试：zones / zone_windows 现存区域一次性迁移为基线修订版本。"""
import json
import sqlite3
import tempfile
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.service import AuditService

SQUARE_CLOSED = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]


def _build_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE zones (
            zone_id TEXT PRIMARY KEY, exterior TEXT NOT NULL,
            holes TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE zone_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT NOT NULL,
            start_epoch REAL NOT NULL, end_epoch REAL NOT NULL,
            start_iso TEXT NOT NULL, end_iso TEXT NOT NULL
        );
        CREATE TABLE points (
            trip_id TEXT NOT NULL, point_id TEXT NOT NULL,
            ts_epoch REAL NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
            PRIMARY KEY (trip_id, point_id)
        );
        """
    )
    # 两个旧区，验证基线修订号按 zone_id 顺序分配
    for zone_id in ("zeta", "alpha"):
        conn.execute(
            "INSERT INTO zones(zone_id, exterior, holes, created_at) VALUES (?,?,?,?)",
            (zone_id, json.dumps(SQUARE_CLOSED), json.dumps([]), "2026-09-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso) "
            "VALUES (?,?,?,?,?)",
            (
                zone_id,
                0.0,
                100.0,
                "1970-01-01T00:00:00Z",
                "1970-01-01T00:01:40Z",
            ),
        )
    conn.execute(
        "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
        ("t1", "p1", 10.0, 50.0, 50.0),
    )
    conn.commit()
    conn.close()


def test_legacy_zones_migrate_to_baseline_revisions():
    tmp = Path(tempfile.mkdtemp(prefix="dz-migrate-"))
    db_path = tmp / "old.db"
    _build_legacy_db(db_path)

    settings = Settings()
    settings.db_path = db_path
    db = Database(settings)  # 构造时执行一次性迁移
    try:
        svc = AuditService(db)

        # 旧表已删除，全部进入不可变修订历史
        assert svc.current_revision() == 2
        with db.read_view() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "zones" not in names and "zone_windows" not in names
        assert "zone_revisions" in names

        # 基线版本按 zone_id 排序分配修订号
        alpha = svc.get_zone("alpha")
        zeta = svc.get_zone("zeta")
        assert alpha["revision"] == 1 and zeta["revision"] == 2
        # 迁移沿用旧 created_at，且不推断迁移前历史（registered_at 同 created_at）
        assert alpha["created_at"] == "2026-09-01T00:00:00Z"
        assert alpha["registered_at"] == "2026-09-01T00:00:00Z"
        assert alpha["windows"][0]["start"] == "1970-01-01T00:00:00Z"

        # 基线版本可按修订号查询
        at1 = svc.list_zones(1)
        assert {z["zone_id"] for z in at1["zones"]} == {"alpha"}

        # 迁移后新登记获得 revision=3，历史不受影响
        resp = svc.register_zone(
            {
                "zone_id": "newbie",
                "exterior": [[0, 0], [10, 0], [10, 10]],
                "holes": [],
                "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}],
            }
        )
        assert resp["revision"] == 3
        assert svc.get_zone("alpha")["revision"] == 1

        # 轨迹点数据保留
        assert db.count_trip_points("t1") == 1
    finally:
        db.close()

    # 再次打开：迁移幂等（旧表已不存在，不重复导入）
    db2 = Database(settings)
    try:
        assert db2.current_revision() == 3
    finally:
        db2.close()


def test_legacy_points_table_upgraded_with_status_and_version():
    """旧版 points 表（无 status 列、全量唯一索引）一次性升级为 active 模型与版本基线。"""
    tmp = Path(tempfile.mkdtemp(prefix="dz-pts-migrate-"))
    db_path = tmp / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE points (
            trip_id TEXT NOT NULL, point_id TEXT NOT NULL,
            ts_epoch REAL NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
            PRIMARY KEY (trip_id, point_id)
        );
        CREATE UNIQUE INDEX idx_points_trip_ts ON points(trip_id, ts_epoch);
        """
    )
    conn.execute(
        "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
        ("t1", "p1", 10.0, 1.0, 2.0),
    )
    conn.commit()
    conn.close()

    settings = Settings()
    settings.db_path = db_path
    db = Database(settings)  # 构造时执行 points 升级
    try:
        svc = AuditService(db)
        with db.read_view() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(points)")}
            idx = {
                r["name"]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
        assert "status" in cols
        assert "idx_points_trip_ts" not in idx
        assert "idx_points_active_trip_ts" in idx

        # 旧点全部为 active；既有数据在首次新写入前版本仍为 0（基线即“当前点集”）
        assert db.count_trip_points("t1") == 1
        assert svc.get_trip_version_info("t1")["track_version"] == 0

        # 更正可在旧点上执行：撤销 p1 后版本推进到 1，原时刻被释放
        r = svc.correct_points(
            "t1",
            {"base_version": 0, "reason": "旧库误录", "items": [{"point_id": "p1", "op": "revoke"}]},
        )
        # 版本 0 基线 + 既有现存点：允许更正并推进
        assert r["track_version"] == 1
        assert db.count_trip_points("t1") == 0
    finally:
        db.close()
