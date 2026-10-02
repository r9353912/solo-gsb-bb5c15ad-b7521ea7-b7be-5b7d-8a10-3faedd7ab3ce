"""业务服务：危险区登记（全局修订号）、轨迹点批量提交、穿越审计计算。"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import Polygon

from .db import Database
from .geometry import GeometryError, build_polygon, merge_intervals, segment_inside_params
from .timeutils import TimeError, clip_intervals_halfopen, merge_windows, parse_utc, validate_window


class ConflictError(Exception):
    """内容冲突（HTTP 409）。"""


class ValidationError(Exception):
    """语义校验失败（HTTP 400）。"""


class NotFoundError(Exception):
    """资源不存在或修订号超出范围（HTTP 404）。"""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_revision_param(raw: Any) -> int:
    """校验查询参数 revision：必须是 >=1 的整数。"""
    if isinstance(raw, bool):
        raise ValidationError(f"revision 必须是正整数，收到: {raw!r}")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise ValidationError(f"revision 必须是正整数，收到: {raw!r}")
    if value < 1:
        raise ValidationError(f"revision 必须是正整数（>=1），收到: {value}")
    return value


def _row_to_zone(row: Any, *, include_revision: bool = True) -> Dict[str, Any]:
    windows_raw = json.loads(row["windows"])
    body: Dict[str, Any] = {
        "zone_id": row["zone_id"],
        "exterior": json.loads(row["exterior"]),
        "holes": json.loads(row["holes"]),
        "windows": [
            {"start": w["start_iso"], "end": w["end_iso"]} for w in windows_raw
        ],
        "created_at": row["created_at"],
        "registered_at": row["registered_at"],
    }
    if include_revision:
        body["revision"] = int(row["revision"])
    return body


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.min_duration = db.settings.min_duration

    # ------------------------------------------------------------------
    # 修订号
    # ------------------------------------------------------------------

    def current_revision(self) -> int:
        return self.db.current_revision()

    def _resolve_revision(self, raw: Any) -> int:
        """校验并解析调用方指定的修订号；超出当前范围明确报错 404。"""
        revision = _validate_revision_param(raw)
        head = self.db.current_revision()
        if revision > head:
            raise NotFoundError(
                f"修订号 {revision} 超出当前范围：当前最新修订号为 {head}"
                + ("（尚无任何区域登记）" if head == 0 else "")
            )
        return revision

    # ------------------------------------------------------------------
    # 危险区登记
    # ------------------------------------------------------------------

    def register_zone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        exterior = payload.get("exterior")
        holes = payload.get("holes") or []
        if not isinstance(holes, list):
            raise ValidationError("holes 必须是环列表")
        try:
            polygon = build_polygon(exterior, holes)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc

        raw_windows = payload.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValidationError("windows 必须是非空左闭右开时段列表 [{start, end}]")
        windows: List[Dict[str, Any]] = []
        for idx, w in enumerate(raw_windows):
            if not isinstance(w, dict):
                raise ValidationError(f"windows[{idx}] 必须是对象")
            try:
                start = parse_utc(w.get("start"), field=f"windows[{idx}].start")
                end = parse_utc(w.get("end"), field=f"windows[{idx}].end")
                validate_window(start, end)
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc
            windows.append(
                {
                    "start_epoch": start.timestamp(),
                    "end_epoch": end.timestamp(),
                    "start_iso": _iso_z(start),
                    "end_iso": _iso_z(end),
                }
            )

        now_iso = _iso_z(datetime.now(timezone.utc))
        # 单事务：读取既有首版 created_at + 追加不可变版本 + 分配全局修订号，原子提交
        with self.db.transaction() as conn:
            first = self.db.first_zone_revision(conn, zone_id)
            created = first is None
            created_iso = now_iso if created else first["created_at"]
            revision = self.db.insert_zone_revision(
                conn,
                zone_id,
                [list(c) for c in polygon.exterior.coords],
                [[list(c) for c in ring.coords] for ring in polygon.interiors],
                windows,
                created_iso,
                now_iso,
            )

        return {
            "zone_id": zone_id,
            "revision": revision,
            "status": "created" if created else "updated",
            "num_holes": len(polygon.interiors),
            "num_windows": len(windows),
        }

    def get_zone(self, zone_id: str, revision_raw: Any = None) -> Optional[Dict[str, Any]]:
        if revision_raw is None:
            row = self.db.latest_zone_row(zone_id)
            if row is None:
                return None
            return _row_to_zone(row)

        revision = self._resolve_revision(revision_raw)
        with self.db.read_view() as conn:
            row = self.db._zone_row_at(conn, zone_id, revision)
        if row is None:
            # 指定版本中该区尚未登记
            raise NotFoundError(
                f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
            )
        return _row_to_zone(row)

    def list_zones(self, revision_raw: Any = None) -> Dict[str, Any]:
        if revision_raw is None:
            rows = self.db.list_latest_zone_rows()
            return {"zones": [_row_to_zone(r) for r in rows]}

        revision = self._resolve_revision(revision_raw)
        with self.db.read_view() as conn:
            rows = self.db._list_zone_rows_at(conn, revision)
        return {
            "revision": revision,
            "zones": [_row_to_zone(r) for r in rows],
        }

    def list_revisions(self, zone_id: Optional[str] = None, limit: int = 500) -> Dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10000:
            raise ValidationError("limit 必须是 1..10000 的整数")
        if zone_id is None:
            rows = self.db.list_revision_rows(limit=limit)
            payload = {
                "current_revision": self.db.current_revision(),
                "revisions": [self._revision_brief(r) for r in rows],
            }
            return payload
        # 按区查看：区当前必须存在（历史曾登记即视为存在，版本不可删除）
        existing = self.db.latest_zone_row(zone_id)
        if existing is None:
            raise NotFoundError(f"危险区不存在: {zone_id}")
        rows = self.db.list_zone_revision_rows(zone_id)
        return {
            "zone_id": zone_id,
            "revisions": [self._revision_brief(r) for r in rows],
        }

    @staticmethod
    def _revision_brief(row: Any) -> Dict[str, Any]:
        return {
            "revision": int(row["revision"]),
            "zone_id": row["zone_id"],
            "created_at": row["created_at"],
            "registered_at": row["registered_at"],
        }

    # ------------------------------------------------------------------
    # 轨迹点批量提交（幂等 / 冲突）
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_coord(item: Dict[str, Any], idx_label: str) -> Tuple[float, float]:
        x_raw, y_raw = item.get("x"), item.get("y")
        if isinstance(x_raw, bool) or isinstance(y_raw, bool):
            raise ValidationError(f"{idx_label} 坐标必须是数值")
        try:
            x, y = float(x_raw), float(y_raw)
        except (TypeError, ValueError):
            raise ValidationError(
                f"{idx_label} 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
            )
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValidationError(
                f"{idx_label} 坐标必须有限，禁止 NaN/Infinity: x={x_raw!r}, y={y_raw!r}"
            )
        return x, y

    def submit_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        points = payload.get("points")
        if not isinstance(points, list) or not points:
            raise ValidationError("points 必须是非空轨迹点列表")

        # ---- 批内解析与校验 ----
        parsed: List[Tuple[str, float, float, float, str]] = []
        # point_id -> (ts,x,y,ts_iso)；ts -> point_id，用于批内一致性检查
        seen_ids: Dict[str, Tuple[float, float, float, str]] = {}
        seen_ts: Dict[float, str] = {}

        for idx, item in enumerate(points):
            if not isinstance(item, dict):
                raise ValidationError(f"points[{idx}] 必须是对象")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"points[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()

            try:
                dt = parse_utc(item.get("ts"), field=f"points[{idx}].ts")
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc

            x, y = self._parse_coord(item, f"points[{idx}]")

            ts_epoch = dt.timestamp()
            sig = (ts_epoch, x, y)

            # 同 point_id 批内重复：内容一致幂等，不一致冲突
            if point_id in seen_ids:
                if seen_ids[point_id][:3] != sig:
                    raise ConflictError(
                        f"批内冲突: point_id={point_id!r} 出现了不同内容（时刻或坐标不一致）"
                    )
                continue

            # 同车次同刻不同点
            if ts_epoch in seen_ts:
                raise ConflictError(
                    f"批内冲突: 时刻 {_iso_z(dt)} 同时关联了 point_id="
                    f"{seen_ts[ts_epoch]!r} 与 {point_id!r}"
                )

            seen_ids[point_id] = (ts_epoch, x, y, _iso_z(dt))
            seen_ts[ts_epoch] = point_id
            parsed.append((point_id, ts_epoch, x, y, _iso_z(dt)))

        # ---- 与已存数据比对（只读快照，先全部收集错误） ----
        stored_ids: Dict[str, Tuple[float, float, float]] = {}
        with self.db.read_view() as conn:
            stored = self.db.get_point_rows(conn, trip_id, [p[0] for p in parsed])
            for point_id, ts_epoch, x, y, _ in parsed:
                row = stored.get(point_id)
                if row is not None:
                    if row["status"] == "revoked":
                        # 已撤销 ID 永久退役，不得经上传重用（即使内容相同）
                        raise ConflictError(
                            f"point_id={point_id!r} 已被更正撤销，不得经上传重用；"
                            f"请使用新的 point_id"
                        )
                    stored_sig = (row["ts_epoch"], row["x"], row["y"])
                    incoming = (ts_epoch, x, y)
                    if stored_sig != incoming:
                        raise ConflictError(
                            f"point_id={point_id!r} 已存在且内容不同："
                            f"已存 (ts_epoch={stored_sig[0]}, x={stored_sig[1]}, y={stored_sig[2]})，"
                            f"新传 (ts_epoch={ts_epoch}, x={x}, y={y})"
                        )
                    stored_ids[point_id] = incoming
                    continue
                # 新点：检查是否与已有有效点同刻
                other = self.db.find_active_point_at_ts(trip_id, ts_epoch)
                if other is not None:
                    raise ConflictError(
                        f"时刻冲突: point_id={point_id!r} 的时刻与已存 point_id="
                        f"{other['point_id']!r} 相同（同车次同刻只能有一个点）"
                    )

        # ---- 单事务写入新点；只有真正录入新点才推进恰好一个轨迹版本 ----
        new_rows = [
            (trip_id, point_id, ts_epoch, x, y)
            for point_id, ts_epoch, x, y, _ in parsed
            if point_id not in stored_ids
        ]
        if new_rows:
            with self.db.transaction() as conn:
                had_version = self.db.trip_version(conn, trip_id) > 0
                self.db.insert_points(conn, new_rows)
                track_version = self.db.bump_trip_version(conn, trip_id, had_version)
        else:
            with self.db.read_view() as conn:
                track_version = self.db.trip_version(conn, trip_id)

        return {
            "trip_id": trip_id,
            "received": len(points),
            "accepted_new": len(new_rows),
            "idempotent_retransmit": len(points) - len(new_rows),
            "track_version": track_version,
        }

    # ------------------------------------------------------------------
    # 轨迹点更正（版本化、整组原子、不可改履历）
    # ------------------------------------------------------------------

    def get_trip_version_info(self, trip_id: str) -> Dict[str, Any]:
        with self.db.read_view() as conn:
            version = self.db.trip_version(conn, trip_id)
            active = self.db._list_trip_points(conn, trip_id)
        return {
            "trip_id": trip_id,
            "track_version": version,
            "active_point_count": len(active),
        }

    def list_point_corrections(self, trip_id: str) -> Dict[str, Any]:
        """按车次排列的更正履历（提交顺序升序）；车次从未有过任何点则 404。"""
        with self.db.read_view() as conn:
            version = self.db.trip_version(conn, trip_id)
            rows = self.db.list_point_correction_rows(trip_id)
        if version == 0:
            raise NotFoundError(f"车次无轨迹数据: {trip_id}")
        return {
            "trip_id": trip_id,
            "track_version": version,
            "corrections": [self._correction_brief(r) for r in rows],
        }

    @staticmethod
    def _point_value(ts_epoch: float, x: float, y: float) -> Dict[str, Any]:
        return {
            "ts": _iso_z(datetime.fromtimestamp(ts_epoch, timezone.utc)),
            "x": x,
            "y": y,
        }

    def _correction_brief(self, row: Any) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "base_version": int(row["base_version"]),
            "new_version": int(row["new_version"]),
            "reason": row["reason"],
            "affected": int(row["affected"]),
            "changes": json.loads(row["changes"]),
            "correction_id": row["correction_id"],
            "created_at": row["created_at"],
        }

    def _build_replay_response(self, trip_id: str, row: Any) -> Dict[str, Any]:
        brief = self._correction_brief(row)
        return {
            "trip_id": trip_id,
            "track_version": brief["new_version"],
            "idempotent_replay": True,
            "affected_count": brief["affected"],
            "affected": brief["changes"],
            "reason": brief["reason"],
            "base_version": brief["base_version"],
            "correction_id": brief["correction_id"],
            "created_at": brief["created_at"],
        }

    def correct_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        base_version_raw = payload.get("base_version")
        if isinstance(base_version_raw, bool) or not isinstance(base_version_raw, int):
            raise ValidationError("base_version 必须是非负整数")
        base_version = int(base_version_raw)
        if base_version < 0:
            raise ValidationError("base_version 必须是非负整数（已有上传的车次从 1 开始）")

        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason 必须是非空更正原因")
        reason = reason.strip()

        correction_id = payload.get("correction_id")
        if correction_id is not None:
            if not isinstance(correction_id, str) or not correction_id.strip():
                raise ValidationError("correction_id 必须是非空字符串")
            correction_id = correction_id.strip()

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须是非空更正项列表")

        # ---- 解析更正项：point_id 互异；revoke 无值，update 必须带带时区时刻与有限坐标 ----
        parsed: List[Dict[str, Any]] = []
        seen: set = set()
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"items[{idx}] 必须是对象")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"items[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()
            if point_id in seen:
                raise ValidationError(f"items[{idx}].point_id={point_id!r} 在本批中重复：更正项 point_id 必须互异")
            seen.add(point_id)

            op = item.get("op")
            if op not in ("revoke", "update"):
                raise ValidationError(f"items[{idx}].op 只能是 'revoke' 或 'update'，收到: {op!r}")

            if op == "revoke":
                if item.get("ts") is not None or item.get("x") is not None or item.get("y") is not None:
                    raise ValidationError(
                        f"items[{idx}] op=revoke 仅需 point_id，不得附带 ts/x/y"
                    )
                parsed.append({"point_id": point_id, "op": "revoke"})
                continue

            try:
                dt = parse_utc(item.get("ts"), field=f"items[{idx}].ts")
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc
            x, y = self._parse_coord(item, f"items[{idx}]")
            parsed.append(
                {
                    "point_id": point_id,
                    "op": "update",
                    "ts_epoch": dt.timestamp(),
                    "ts_iso": _iso_z(dt),
                    "x": x,
                    "y": y,
                }
            )

        # 批内最终时刻不得冲突（允许点保持原时刻；交换时刻在两阶段应用中处理）
        final_ts: Dict[float, str] = {}
        for item in parsed:
            if item["op"] != "update":
                continue
            t = item["ts_epoch"]
            if t in final_ts:
                raise ConflictError(
                    f"批内冲突: 更正后时刻 {item['ts_iso']} 同时关联 point_id="
                    f"{final_ts[t]!r} 与 {item['point_id']!r}"
                )
            final_ts[t] = item["point_id"]

        payload_json = json.dumps(
            {"reason": reason, "items": items}, ensure_ascii=False, sort_keys=True, default=str
        )

        # ---- 单事务：幂等重放 -> 版本/存在性/同刻核对 -> 写新有效状态 + 不可改履历 ----
        with self.db.transaction() as conn:
            if correction_id is not None:
                prior = self.db.get_idempotent_request(conn, trip_id, correction_id)
                if prior is not None:
                    if prior["payload"] != payload_json:
                        raise ConflictError(
                            f"correction_id={correction_id!r} 已使用但更正内容不同："
                            f"同一令牌只能以相同请求体重放"
                        )
                    row = conn.execute(
                        "SELECT * FROM point_corrections WHERE id = ?",
                        (prior["correction_pk"],),
                    ).fetchone()
                    return self._build_replay_response(trip_id, row)

            current_version = self.db.trip_version(conn, trip_id)
            active_snapshot = self.db._list_trip_points(conn, trip_id)
            if not active_snapshot:
                # 无任何有效点（含全新车次）：版本 0 为空基线，无点可改
                raise ConflictError(
                    f"车次 {trip_id!r} 尚无有效点（版本 0 为基线），不能更正；请先上传"
                )
            if base_version != current_version:
                raise ConflictError(
                    f"轨迹版本过期或非法：提交基于版本 {base_version}，"
                    f"车次 {trip_id!r} 当前版本为 {current_version}（请读取最新版本后重试）"
                )

            rows_by_id = self.db.get_point_rows(conn, trip_id, list(seen))
            active_rows: Dict[str, Any] = {}
            for point_id in seen:
                row = rows_by_id.get(point_id)
                if row is None:
                    raise ConflictError(f"point_id={point_id!r} 在车次 {trip_id!r} 中不存在，不能更正")
                if row["status"] == "revoked":
                    raise ConflictError(
                        f"point_id={point_id!r} 已被更正撤销，不能再次更正；已撤销 ID 不得重用"
                    )
                active_rows[point_id] = row

            # 最终有效点集 = 该车次全部当前有效点应用本批；整组核对同车次同刻唯一
            all_active = {r["point_id"]: r for r in active_snapshot}
            updates: List[Tuple[str, float, float, float]] = []
            revokes: List[str] = []
            final: Dict[str, Tuple[float, float, float]] = {
                pid: (r["ts_epoch"], r["x"], r["y"]) for pid, r in all_active.items()
            }
            for item in sorted(parsed, key=lambda i: i["point_id"]):
                point_id = item["point_id"]
                if item["op"] == "revoke":
                    revokes.append(point_id)
                    del final[point_id]
                else:
                    updates.append((point_id, item["ts_epoch"], item["x"], item["y"]))
                    final[point_id] = (item["ts_epoch"], item["x"], item["y"])

            ts_owner: Dict[float, str] = {}
            for point_id, (t, _x, _y) in final.items():
                owner = ts_owner.get(t)
                if owner is not None:
                    raise ConflictError(
                        f"更正后存在同车次同刻冲突: 时刻 epoch={t} 同时属于 point_id="
                        f"{owner!r} 与 {point_id!r}"
                    )
                ts_owner[t] = point_id

            # ---- 组装不可改前后值履历（point_id 升序，结果确定） ----
            changes: List[Dict[str, Any]] = []
            for item in sorted(parsed, key=lambda i: i["point_id"]):
                point_id = item["point_id"]
                row = active_rows[point_id]
                before = self._point_value(row["ts_epoch"], row["x"], row["y"])
                if item["op"] == "revoke":
                    changes.append({"point_id": point_id, "op": "revoke", "before": before, "after": None})
                else:
                    after = self._point_value(item["ts_epoch"], item["x"], item["y"])
                    changes.append({"point_id": point_id, "op": "update", "before": before, "after": after})

            self.db.apply_point_correction(
                conn,
                trip_id,
                updates=updates,
                revokes=revokes,
                inserts=[],
            )
            new_version = self.db.bump_trip_version(
                conn, trip_id, has_existing_version=current_version > 0
            )
            now_iso = _iso_z(datetime.now(timezone.utc))
            self.db.insert_point_correction(
                conn,
                trip_id,
                base_version=current_version,
                new_version=new_version,
                reason=reason,
                changes=changes,
                created_iso=now_iso,
                correction_id=correction_id,
                payload_json=payload_json,
            )

        return {
            "trip_id": trip_id,
            "track_version": new_version,
            "idempotent_replay": False,
            "affected_count": len(changes),
            "affected": changes,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # 穿越审计
    # ------------------------------------------------------------------

    def query_crossings(
        self,
        trip_id: str,
        zone_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        revision_raw: Any = None,
    ) -> Dict[str, Any]:
        try:
            lo = parse_utc(since, field="since").timestamp() if since else None
            hi = parse_utc(until, field="until").timestamp() if until else None
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo is not None and hi is not None and lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        pinned = revision_raw is not None
        revision = self._resolve_revision(revision_raw) if pinned else None

        with self.db.read_view() as conn:
            points = self.db._list_trip_points(conn, trip_id)
            if len(points) < 2:
                result: Dict[str, Any] = {
                    "trip_id": trip_id,
                    "point_count": len(points),
                    "zones": [],
                }
                if pinned:
                    result["revision"] = revision
                return result

            if pinned:
                zone_rows = self.db._list_zone_rows_at(conn, revision)
            else:
                zone_rows = self.db._list_latest_zone_rows(conn)
            if zone_id is not None:
                zone_rows = [r for r in zone_rows if r["zone_id"] == zone_id]
                if not zone_rows:
                    if pinned:
                        # 指定版本中尚未登记的区域不参与核算
                        raise NotFoundError(
                            f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
                        )
                    raise ValidationError(f"危险区不存在: {zone_id}")

            zone_results: List[Dict[str, Any]] = []
            for zrow in zone_rows:
                zone_result = self._crossing_for_zone(zrow, points, lo, hi)
                zone_results.append(zone_result)

        result = {
            "trip_id": trip_id,
            "point_count": len(points),
            "zones": zone_results,
        }
        if pinned:
            result["revision"] = revision
        return result

    def _crossing_for_zone(
        self,
        zrow: Any,
        points: List[Any],
        lo: Optional[float],
        hi: Optional[float],
    ) -> Dict[str, Any]:
        polygon = Polygon(
            json.loads(zrow["exterior"]),
            json.loads(zrow["holes"]),
        )
        windows_raw = json.loads(zrow["windows"])
        windows = merge_windows([(w["start_epoch"], w["end_epoch"]) for w in windows_raw])

        # 覆盖参数（时间）区间：遍历相邻点，乱序已在 SQL 层按时刻排序重建
        cover: List[Tuple[float, float]] = []
        for a, b in zip(points, points[1:]):
            t0, t1 = a["ts_epoch"], b["ts_epoch"]
            if t1 <= t0:
                # 同刻点不应出现（唯一约束），稳妥跳过
                continue
            params = segment_inside_params(
                polygon, (a["x"], a["y"]), (b["x"], b["y"])
            )
            for pa, pb in params:
                # 参数 0/1 直接取端点时刻，保证相邻段在公共采样点精确相接
                s = t0 if pa == 0.0 else t0 + pa * (t1 - t0)
                e = t1 if pb == 1.0 else t0 + pb * (t1 - t0)
                if e - s > self.min_duration:
                    cover.append((s, e))

        # 时间域合并留 1e-6 秒容差，吸收不同时刻解析路径的浮点误差
        cover = merge_intervals(cover, eps=1e-6)
        intervals = clip_intervals_halfopen(cover, windows)
        if lo is not None or hi is not None:
            bounds = [(-float("inf") if lo is None else lo, float("inf") if hi is None else hi)]
            intervals = clip_intervals_halfopen(intervals, bounds)
        intervals = merge_intervals(intervals, eps=1e-6)
        intervals = [(s, e) for s, e in intervals if e - s > self.min_duration]

        segments = [
            {
                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                "duration_sec": round(e - s, 6),
            }
            for s, e in intervals
        ]
        total = round(sum(e - s for s, e in intervals), 6)

        return {
            "zone_id": zrow["zone_id"],
            "crossed": bool(intervals),
            "total_duration_sec": total,
            "segments": segments,
        }
