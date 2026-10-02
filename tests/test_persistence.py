"""持久化测试：重新打开同一数据库文件后，历史记录仍可查询。"""
import os

from app.config import get_settings
from app.db import Database
from app.service import AuditService
from tests.conftest import SQUARE


def test_records_survive_database_reopen():
    settings = get_settings()
    path = settings.db_path
    assert os.path.exists(path)

    db1 = Database(settings)
    svc1 = AuditService(db1)
    svc1.register_zone(
        {
            "zone_id": "persist-z",
            "exterior": SQUARE,
            "holes": [],
            "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}],
        }
    )
    svc1.submit_points(
        "persist-trip",
        {
            "points": [
                {"point_id": "q1", "ts": "2026-09-30T10:00:00Z", "x": -50, "y": 50},
                {"point_id": "q2", "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 50},
                {"point_id": "q3", "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 50},
            ]
        },
    )
    db1.close()

    # 模拟进程重启：用同一文件新建连接
    db2 = Database(settings)
    svc2 = AuditService(db2)
    zone = svc2.get_zone("persist-z")
    assert zone is not None
    result = svc2.query_crossings("persist-trip", zone_id="persist-z")
    z = result["zones"][0]
    assert z["crossed"] is True
    assert z["total_duration_sec"] == 40.0

    # 更正也持久化：撤销中间点后重启，版本/履历/有效点集保持
    corr = svc2.correct_points(
        "persist-trip",
        {
            "base_version": 1,
            "reason": "现场误录",
            "items": [{"point_id": "q2", "op": "revoke"}],
        },
    )
    assert corr["track_version"] == 2
    db2.close()

    db3 = Database(settings)
    svc3 = AuditService(db3)
    assert svc3.get_trip_version_info("persist-trip")["track_version"] == 2
    hist = svc3.list_point_corrections("persist-trip")
    assert len(hist["corrections"]) == 1
    assert hist["corrections"][0]["changes"][0]["point_id"] == "q2"
    assert svc3.get_trip_version_info("persist-trip")["active_point_count"] == 2
    db3.close()
