"""FastAPI 入口：工地作业车危险区穿越审计服务。"""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .schemas import PointBatch, PointCorrectionRequest, ZoneCreate
from .service import AuditService, ConflictError, NotFoundError, ValidationError

settings = get_settings()
db = Database(settings)
service = AuditService(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    db.close()


app = FastAPI(
    title="工地作业车危险区穿越审计服务",
    version="1.2.0",
    lifespan=lifespan,
    description=(
        "登记带洞米制多边形危险区及其 UTC 半开生效时段；每次登记原子产生全局递增修订号，"
        "各区保留不可变历史版本。批量提交车辆轨迹点（同 ID 幂等/异内容冲突），"
        "支持现场误录点的整组版本化更正（撤销或带时区时刻+有限坐标，留不可改前后值履历），"
        "并按最新配置或指定修订号查询各危险区的穿越区间与累计时长。"
    ),
)


@app.exception_handler(ValidationError)
async def _validation_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "validation_error", "detail": str(exc)})


@app.exception_handler(ConflictError)
async def _conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(NotFoundError)
async def _not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {
        "service": "danger-zone-crossing-audit",
        "docs": "/docs",
        "health": "/health",
        "endpoints": [
            "POST /api/v1/zones",
            "GET  /api/v1/zones",
            "GET  /api/v1/zones/{zone_id}",
            "GET  /api/v1/revisions",
            "GET  /api/v1/zones/{zone_id}/revisions",
            "POST /api/v1/trips/{trip_id}/points",
            "POST /api/v1/trips/{trip_id}/corrections",
            "GET  /api/v1/trips/{trip_id}",
            "GET  /api/v1/trips/{trip_id}/version",
            "GET  /api/v1/trips/{trip_id}/corrections",
            "GET  /api/v1/trips/{trip_id}/crossings",
        ],
    }


@app.post("/api/v1/zones", status_code=201)
def create_zone(body: ZoneCreate) -> dict:
    # 响应含本次登记原子产生的全局递增修订号 revision
    return service.register_zone(body.model_dump())


@app.get("/api/v1/zones")
def list_zones(
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description="可选修订号：返回各区截至该修订号的最后版本；省略则返回最新配置",
    ),
) -> dict:
    return service.list_zones(revision)


@app.get("/api/v1/zones/{zone_id}")
def get_zone(
    zone_id: str,
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description="可选修订号：返回该区截至该修订号的版本；省略则返回最新版本",
    ),
) -> JSONResponse:
    try:
        detail = service.get_zone(zone_id, revision)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": str(exc)}
        )
    if detail is None:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"危险区不存在: {zone_id}"}
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/revisions")
def list_revisions(
    limit: int = Query(default=500, ge=1, le=10000, description="最多返回条数（按修订号倒序）"),
) -> dict:
    """全局修订流水：每次区域登记/覆盖更新产生一条。"""
    return service.list_revisions(zone_id=None, limit=limit)


@app.get("/api/v1/zones/{zone_id}/revisions")
def list_zone_revisions(zone_id: str) -> JSONResponse:
    """单个危险区的不可变修订历史（按修订号升序）。"""
    try:
        return JSONResponse(content=service.list_revisions(zone_id=zone_id))
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": str(exc)}
        )


@app.post("/api/v1/trips/{trip_id}/points")
def submit_points(trip_id: str, body: PointBatch) -> dict:
    result = service.submit_points(trip_id, body.model_dump())
    return result


@app.post("/api/v1/trips/{trip_id}/corrections", status_code=200)
def submit_correction(trip_id: str, body: PointCorrectionRequest) -> dict:
    """现场误录点更正：整组原子核对版本/存在性/同刻唯一，写新有效状态与不可改履历。"""
    return service.correct_points(trip_id, body.model_dump())


@app.get("/api/v1/trips/{trip_id}/version")
def get_trip_version(trip_id: str) -> JSONResponse:
    info = service.get_trip_version_info(trip_id)
    if info["track_version"] == 0 and info["active_point_count"] == 0:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"车次无轨迹数据: {trip_id}"}
        )
    return JSONResponse(content=info)


@app.get("/api/v1/trips/{trip_id}/corrections")
def list_corrections(trip_id: str) -> JSONResponse:
    """按车次排列的更正履历（提交顺序升序），含当前轨迹版本。"""
    try:
        return JSONResponse(content=service.list_point_corrections(trip_id))
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": str(exc)}
        )


@app.get("/api/v1/trips/{trip_id}")
def get_trip(trip_id: str) -> JSONResponse:
    from datetime import datetime, timezone

    info = service.get_trip_version_info(trip_id)
    rows = db.list_trip_points(trip_id)
    if not rows:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"车次无轨迹数据: {trip_id}"}
        )

    return JSONResponse(
        content={
            "trip_id": trip_id,
            "track_version": info["track_version"],
            "point_count": len(rows),
            "points": [
                {
                    "point_id": r["point_id"],
                    "ts": datetime.fromtimestamp(r["ts_epoch"], timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "x": r["x"],
                    "y": r["y"],
                }
                for r in rows
            ],
        }
    )


@app.get("/api/v1/trips/{trip_id}/crossings")
def get_crossings(
    trip_id: str,
    zone_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（含）之后"),
    until: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（不含）之前"),
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description=(
            "可选区域配置修订号：按各区截至该修订号的最后版本复现核算，"
            "响应回填该 revision；省略则按最新配置，响应语义不变"
        ),
    ),
) -> dict:
    return service.query_crossings(
        trip_id, zone_id=zone_id, since=since, until=until, revision_raw=revision
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
