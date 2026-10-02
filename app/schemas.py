"""HTTP 请求/响应模型（Pydantic；坐标数值校验在服务层做有限性检查）。"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CoordModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 允许任意数值（含 inf/nan 透传），有限性在服务层显式拒绝并给出明确报错
    x: float
    y: float


class WindowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(..., description="生效开始 UTC ISO 8601（含）")
    end: str = Field(..., description="生效结束 UTC ISO 8601（不含）")


class ZoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: str = Field(..., min_length=1)
    # 顶点为 [x, y]，首尾点无需重复
    exterior: List[List[float]] = Field(..., min_length=3)
    holes: List[List[List[float]]] = Field(default_factory=list)
    windows: List[WindowModel] = Field(..., min_length=1)


class TrajectoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(..., min_length=1)
    ts: str = Field(..., description="UTC ISO 8601，必须带时区")
    x: float
    y: float


class PointBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: List[TrajectoryPoint] = Field(..., min_length=1)


class CorrectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(..., min_length=1, description="被更正的点 ID（批内互异）")
    op: Literal["revoke", "update"] = Field(
        ..., description="revoke=撤销该点；update=给出更正后的带时区时刻与有限平面坐标"
    )
    ts: Optional[str] = Field(default=None, description="op=update 时必填：UTC ISO 8601，必须带时区")
    x: Optional[float] = Field(default=None, description="op=update 时必填：有限平面米制坐标")
    y: Optional[float] = Field(default=None, description="op=update 时必填：有限平面米制坐标")


class PointCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(
        ..., ge=0, description="作业人员所见轨迹版本；必须与当前版本一致（首批上传后版本 1 起步）"
    )
    reason: str = Field(..., min_length=1, description="非空更正原因")
    items: List[CorrectionItem] = Field(..., min_length=1, description="一组互异 point_id 的更正项")
    correction_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="可选客户幂等令牌：同车次同令牌须同内容，重放返回首次结果且不推进版本",
    )
