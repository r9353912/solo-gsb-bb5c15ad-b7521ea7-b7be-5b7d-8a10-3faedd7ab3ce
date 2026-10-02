"""现场轨迹点更正测试：版本化、整组原子、不可改履历、幂等重放、冲突整批拒绝。"""
import json

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _points(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


P0 = [
    ("a", "2026-09-30T10:00:00Z", 0.0, 0.0),
    ("b", "2026-09-30T10:01:00Z", 10.0, 10.0),
    ("c", "2026-09-30T10:02:00Z", 20.0, 20.0),
]


def _upload(client, trip="T", points=None):
    return client.post(f"/api/v1/trips/{trip}/points", json=_points(points or P0))


def _correct(client, trip="T", *, base_version, reason="现场误录更正", items, correction_id=None):
    body = {"base_version": base_version, "reason": reason, "items": items}
    if correction_id is not None:
        body["correction_id"] = correction_id
    return client.post(f"/api/v1/trips/{trip}/corrections", json=body)


def test_first_upload_creates_version_1_and_idempotent_does_not_advance(client):
    r = _upload(client, "v1")
    assert r.status_code == 200, r.text
    assert r.json()["accepted_new"] == 3
    assert r.json()["track_version"] == 1

    # 纯幂等重传不推进版本
    r2 = client.post("/api/v1/trips/v1/points", json=_points(P0))
    assert r2.json()["accepted_new"] == 0
    assert r2.json()["track_version"] == 1

    info = client.get("/api/v1/trips/v1/version").json()
    assert info == {"trip_id": "v1", "track_version": 1, "active_point_count": 3}

    # 后续真正新点推进恰好一个版本
    r3 = client.post(
        "/api/v1/trips/v1/points",
        json=_points([("d", "2026-09-30T10:03:00Z", 30.0, 30.0)]),
    )
    assert r3.json()["accepted_new"] == 1
    assert r3.json()["track_version"] == 2


def test_successful_correction_returns_new_version_and_affected(client):
    _upload(client, "T")
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[
            {"point_id": "a", "op": "update", "ts": "2026-09-30T09:59:30Z", "x": -1.0, "y": -1.0},
            {"point_id": "c", "op": "revoke"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track_version"] == 2
    assert body["idempotent_replay"] is False
    assert body["affected_count"] == 2
    affected = {c["point_id"]: c for c in body["affected"]}
    assert affected["a"]["op"] == "update"
    assert affected["a"]["before"] == {"ts": "2026-09-30T10:00:00Z", "x": 0.0, "y": 0.0}
    assert affected["a"]["after"] == {"ts": "2026-09-30T09:59:30Z", "x": -1.0, "y": -1.0}
    assert affected["c"]["op"] == "revoke"
    assert affected["c"]["after"] is None
    assert affected["c"]["before"]["ts"] == "2026-09-30T10:02:00Z"

    # 当前点集立即使用更正后结果
    trip = client.get("/api/v1/trips/T").json()
    assert trip["track_version"] == 2
    assert [p["point_id"] for p in trip["points"]] == ["a", "b"]
    assert trip["points"][0]["ts"] == "2026-09-30T09:59:30Z"
    assert trip["points"][0]["x"] == -1.0


def test_stale_version_rejected_entire_batch(client):
    _upload(client, "T")
    ok = _correct(client, "T", base_version=1, items=[{"point_id": "a", "op": "revoke"}])
    assert ok.status_code == 200 and ok.json()["track_version"] == 2

    stale = _correct(
        client,
        "T",
        base_version=1,
        items=[{"point_id": "b", "op": "revoke"}],
    )
    assert stale.status_code == 409
    assert "版本" in stale.json()["detail"]
    # b 仍在，未留新履历
    assert client.get("/api/v1/trips/T").json()["point_count"] == 2
    assert len(client.get("/api/v1/trips/T/corrections").json()["corrections"]) == 1


def test_missing_point_rejected(client):
    _upload(client, "T")
    r = _correct(client, "T", base_version=1, items=[{"point_id": "ghost", "op": "revoke"}])
    assert r.status_code == 409
    assert "ghost" in r.json()["detail"]
    assert client.get("/api/v1/trips/T/version").json()["track_version"] == 1


def test_already_revoked_point_rejected_and_no_ledger(client):
    _upload(client, "T")
    r1 = _correct(client, "T", base_version=1, items=[{"point_id": "a", "op": "revoke"}])
    assert r1.status_code == 200
    r2 = _correct(client, "T", base_version=2, items=[{"point_id": "a", "op": "revoke"}])
    assert r2.status_code == 409
    assert "撤销" in r2.json()["detail"]
    # 失败不留履历、版本不推进
    hist = client.get("/api/v1/trips/T/corrections").json()
    assert hist["track_version"] == 2
    assert len(hist["corrections"]) == 1


def test_revoked_id_cannot_be_reused_by_upload(client):
    _upload(client, "T")
    _correct(client, "T", base_version=1, items=[{"point_id": "a", "op": "revoke"}])
    # 用同 ID 上传新内容：拒绝
    r = client.post(
        "/api/v1/trips/T/points",
        json=_points([("a", "2026-09-30T11:00:00Z", 99.0, 99.0)]),
    )
    assert r.status_code == 409
    assert "撤销" in r.json()["detail"] or "重用" in r.json()["detail"]
    # 版本不变
    assert client.get("/api/v1/trips/T/version").json()["track_version"] == 2


def test_correction_ts_conflict_with_other_point_rejected(client):
    _upload(client, "T")
    # 把 a 改到 b 的时刻 -> 最终集合同刻冲突，整批拒绝
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[{"point_id": "a", "op": "update", "ts": "2026-09-30T10:01:00Z", "x": 1, "y": 1}],
    )
    assert r.status_code == 409
    assert "同刻" in r.json()["detail"]
    trip = client.get("/api/v1/trips/T").json()
    assert trip["track_version"] == 1
    a = next(p for p in trip["points"] if p["point_id"] == "a")
    assert a["ts"] == "2026-09-30T10:00:00Z"


def test_swap_timestamps_between_two_points_succeeds(client):
    # a、b 互换时刻：中间状态冲突，靠两阶段应用，最终合法
    _upload(client, "T")
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[
            {"point_id": "a", "op": "update", "ts": "2026-09-30T10:01:00Z", "x": 1.0, "y": 1.0},
            {"point_id": "b", "op": "update", "ts": "2026-09-30T10:00:00Z", "x": 2.0, "y": 2.0},
        ],
    )
    assert r.status_code == 200, r.text
    trip = client.get("/api/v1/trips/T").json()
    assert [(p["point_id"], p["ts"]) for p in trip["points"]] == [
        ("b", "2026-09-30T10:00:00Z"),
        ("a", "2026-09-30T10:01:00Z"),
        ("c", "2026-09-30T10:02:00Z"),
    ]


def test_revoke_frees_timestamp_for_update(client):
    # 撤销 b 后，a 可以更正到 b 的原时刻
    _upload(client, "T")
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[
            {"point_id": "b", "op": "revoke"},
            {"point_id": "a", "op": "update", "ts": "2026-09-30T10:01:00Z", "x": 9, "y": 9},
        ],
    )
    assert r.status_code == 200, r.text
    trip = client.get("/api/v1/trips/T").json()
    assert [p["point_id"] for p in trip["points"]] == ["a", "c"]
    assert trip["points"][0]["ts"] == "2026-09-30T10:01:00Z"


def test_invalid_values_rejected(client):
    _upload(client, "T")
    # 非有限坐标
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[{"point_id": "a", "op": "update", "ts": "2026-09-30T10:00:00Z", "x": "NaN", "y": 1}],
    )
    assert r.status_code in (400, 422)
    # 不带时区时刻
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[{"point_id": "a", "op": "update", "ts": "2026-09-30T10:00:00", "x": 1, "y": 1}],
    )
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]
    # update 缺 ts
    r = _correct(
        client, "T", base_version=1,
        items=[{"point_id": "a", "op": "update", "x": 1, "y": 1}],
    )
    assert r.status_code in (400, 422)
    # revoke 带值
    r = _correct(
        client, "T", base_version=1,
        items=[{"point_id": "a", "op": "revoke", "x": 1, "y": 1}],
    )
    assert r.status_code in (400, 422)
    # 空原因
    r = _correct(client, "T", base_version=1, reason="   ", items=[{"point_id": "a", "op": "revoke"}])
    assert r.status_code == 400


def test_duplicate_point_id_in_batch_rejected(client):
    _upload(client, "T")
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[
            {"point_id": "a", "op": "revoke"},
            {"point_id": "a", "op": "update", "ts": "2026-09-30T10:00:30Z", "x": 1, "y": 1},
        ],
    )
    assert r.status_code == 400
    assert "互异" in r.json()["detail"]


def test_idempotent_replay_with_correction_id(client):
    _upload(client, "T")
    items = [{"point_id": "a", "op": "revoke"}]
    r1 = _correct(client, "T", base_version=1, items=items, correction_id="cid-1")
    assert r1.status_code == 200
    v2 = r1.json()["track_version"]
    assert v2 == 2

    # 同令牌同内容：即使此刻版本已过期（base=1 != 2），仍重放首次结果，不推进
    r2 = _correct(client, "T", base_version=1, items=items, correction_id="cid-1")
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["track_version"] == 2
    assert client.get("/api/v1/trips/T/version").json()["track_version"] == 2
    hist = client.get("/api/v1/trips/T/corrections").json()
    assert len(hist["corrections"]) == 1

    # 同令牌异内容：冲突
    r3 = _correct(
        client, "T", base_version=2,
        items=[{"point_id": "b", "op": "revoke"}], correction_id="cid-1",
    )
    assert r3.status_code == 409


def test_failed_batch_leaves_no_ledger_and_no_state_change(client):
    _upload(client, "T")
    # b 撤销合法，但 a 改到 c 时刻非法 -> 整批拒绝，b 不得被撤销
    r = _correct(
        client,
        "T",
        base_version=1,
        items=[
            {"point_id": "b", "op": "revoke"},
            {"point_id": "a", "op": "update", "ts": "2026-09-30T10:02:00Z", "x": 1, "y": 1},
        ],
    )
    assert r.status_code == 409
    trip = client.get("/api/v1/trips/T").json()
    assert trip["track_version"] == 1
    assert [p["point_id"] for p in trip["points"]] == ["a", "b", "c"]
    hist = client.get("/api/v1/trips/T/corrections").json()
    assert hist["corrections"] == []


def test_correction_history_ordered_and_immutable(client):
    _upload(client, "T")
    _correct(client, "T", base_version=1, reason="r1", items=[{"point_id": "a", "op": "revoke"}])
    _correct(client, "T", base_version=2, reason="r2", items=[{"point_id": "b", "op": "revoke"}])
    hist = client.get("/api/v1/trips/T/corrections").json()
    assert hist["track_version"] == 3
    assert [c["new_version"] for c in hist["corrections"]] == [2, 3]
    assert [c["base_version"] for c in hist["corrections"]] == [1, 2]
    assert [c["reason"] for c in hist["corrections"]] == ["r1", "r2"]
    # 履历行含不可改前后值
    first = hist["corrections"][0]["changes"][0]
    assert first["point_id"] == "a" and first["before"]["ts"] == "2026-09-30T10:00:00Z"


def test_correction_on_trip_with_no_points_409(client):
    r = _correct(client, "ghost", base_version=0, items=[{"point_id": "x", "op": "revoke"}])
    assert r.status_code == 409
    # 版本/履历读取接口对无点车次 404
    assert client.get("/api/v1/trips/ghost/version").status_code == 404
    assert client.get("/api/v1/trips/ghost/corrections").status_code == 404


def test_base_version_zero_on_existing_trip_rejected(client):
    _upload(client, "T")
    r = _correct(client, "T", base_version=0, items=[{"point_id": "a", "op": "revoke"}])
    assert r.status_code == 409


def test_baseline_existing_points_correct_at_version_zero(client):
    # 直接构造“版本行尚未建立但已有有效点”的状态（模拟旧库迁移点集）：
    # 以版本 0 为基线可更正一次，成功后版本推进到 1
    from app.main import db as _db
    _upload(client, "base0")
    with _db.transaction() as conn:
        conn.execute("DELETE FROM trip_versions WHERE trip_id = 'base0'")
    info = client.get("/api/v1/trips/base0/version").json()
    assert info["track_version"] == 0 and info["active_point_count"] == 3
    r = _correct(client, "base0", base_version=0, items=[{"point_id": "a", "op": "revoke"}])
    assert r.status_code == 200
    assert r.json()["track_version"] == 1


def test_chained_corrections_advance_one_version_each(client):
    _upload(client, "T")
    for base, pid in ((1, "a"), (2, "b")):
        r = _correct(client, "T", base_version=base, items=[{"point_id": pid, "op": "revoke"}])
        assert r.status_code == 200
        assert r.json()["track_version"] == base + 1
    assert client.get("/api/v1/trips/T").json()["point_count"] == 1


def test_crossings_use_corrected_points_immediately(client):
    assert register_square(client, zone_id="z").status_code == 201
    # 原始轨迹横穿方块（x -50 -> 150，80s），区内 40s
    client.post(
        "/api/v1/trips/TC/points",
        json=_points([
            ("p1", "2026-09-30T10:00:00Z", -50.0, 50.0),
            ("p2", "2026-09-30T10:00:40Z", 50.0, 50.0),
            ("p3", "2026-09-30T10:01:20Z", 150.0, 50.0),
        ]),
    )
    z = client.get("/api/v1/trips/TC/crossings?zone_id=z").json()["zones"][0]
    assert z["total_duration_sec"] == 40.0

    # 更正终点：p3 改到区外同方向更远处但时间不变——结果仍 40s（该段出区点不变）。
    # 改为撤销 p2：轨迹变为 p1->p3 直线，同样 40s（线性几何）；改为撤销 p3：
    # 只剩 p1,p2，覆盖到 x=50，区内 20s。
    r = _correct(
        client,
        "TC",
        base_version=1,
        items=[{"point_id": "p3", "op": "revoke"}],
    )
    assert r.status_code == 200
    z2 = client.get("/api/v1/trips/TC/crossings?zone_id=z").json()["zones"][0]
    assert z2["total_duration_sec"] == 20.0


def test_new_upload_after_correction_advances_version(client):
    _upload(client, "T")
    _correct(client, "T", base_version=1, items=[{"point_id": "a", "op": "revoke"}])
    assert client.get("/api/v1/trips/T/version").json()["track_version"] == 2
    r = client.post(
        "/api/v1/trips/T/points",
        json=_points([("d", "2026-09-30T10:05:00Z", 50.0, 50.0)]),
    )
    assert r.status_code == 200
    assert r.json()["track_version"] == 3
