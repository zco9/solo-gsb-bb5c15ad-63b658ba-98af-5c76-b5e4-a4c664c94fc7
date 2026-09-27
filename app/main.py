"""FastAPI 入口：启动即建库，并暴露样本 / 读数 / 状态 / 循环 / 告警接口。"""

from __future__ import annotations

from typing import Optional

import sqlite3
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from . import __version__, services
from .database import init_db
from .schemas import (
    AckRequest,
    AlertOut,
    BatchReadingsResponse,
    CycleOut,
    LoggerBindingIn,
    LoggerBindingOut,
    LoggerBindingUpdate,
    LoggerConsistencyResponse,
    LoggerCreate,
    LoggerInvalidPeriodIn,
    LoggerInvalidPeriodOut,
    LoggerInvalidPeriodUpdate,
    LoggerOut,
    LoggerReadingOut,
    LoggerReadingsResponse,
    LoggerUpdate,
    ReadingOut,
    RecomputeResponse,
    ReadingIn,
    ReadingsBatch,
    SampleCreate,
    SampleOut,
    SampleStateOut,
    SampleUpdate,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动即初始化数据库（幂等建表、索引）
    init_db()
    yield


app = FastAPI(
    title="实验室样本冻融追踪 API",
    description=(
        "接收带事件时间的样本温度读数，按时间顺序重排迟到数据，"
        "以可配置的冻结/解冻阈值和滞回规则识别完整冻融循环，"
        "提供幂等去重、当前状态、循环次数与超限告警查询。"
        "支持共享温度记录仪：登记记录仪与读数，并以左闭右开时段绑定到样本，"
        "记录仪读数按发生时间归入绑定窗口内的样本（一读多样本），"
        "与样本直接读数一起重算冻融循环、暴露剂量与告警。"
        "记录仪可登记校准无效时段（左闭右开、互不重叠）：时段内读数照常入库可查，"
        "但不归属样本、不参与冻融/剂量/告警与一致性核查，时段删除后有效读数恢复归属。"
        "另提供样本 × 记录仪温度一致性核查：对样本直接读数与当前归属的记录仪读数"
        "做不交叉一对一最优配对（配对数最多、总时间差最小、读数 ID 序列稳定取舍），"
        "返回温差超限配对明细与配对/未配对数量。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def _require_sample(conn: sqlite3.Connection, sample_id: str) -> sqlite3.Row:
    sample = services.get_sample(conn, sample_id)
    if sample is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="样本不存在")
    return sample


def _require_logger(conn: sqlite3.Connection, logger_id: str) -> sqlite3.Row:
    logger = services.get_logger(conn, logger_id)
    if logger is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录仪不存在")
    return logger


# ---------------- 健康检查 / 文档 ----------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


# ---------------- 样本管理 ----------------


@app.post("/samples", response_model=SampleOut, status_code=201, tags=["samples"])
def create_sample(payload: SampleCreate, conn=Depends(services.get_db_connection)):
    data = payload.model_dump()
    if data.get("id"):
        if services.get_sample(conn, data["id"]) is not None:
            raise HTTPException(status_code=409, detail="样本 ID 已存在")
    return services.create_sample(conn, data)


@app.get("/samples", response_model=list[SampleOut], tags=["samples"])
def list_samples(conn=Depends(services.get_db_connection)):
    return services.list_samples(conn)


@app.get("/samples/{sample_id}", response_model=SampleOut, tags=["samples"])
def get_sample(sample_id: str, conn=Depends(services.get_db_connection)):
    return _require_sample(conn, sample_id)


@app.patch("/samples/{sample_id}", response_model=SampleOut, tags=["samples"])
def update_sample(
    sample_id: str, payload: SampleUpdate, conn=Depends(services.get_db_connection)
):
    _require_sample(conn, sample_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        # name 为显式 null 时拒绝（其余可空配置字段允许显式 null 清空）
        raise HTTPException(status_code=422, detail="name 不能为空")
    existing = services.get_sample(conn, sample_id)
    merged = {
        "freeze_threshold_c": existing["freeze_threshold_c"],
        "thaw_threshold_c": existing["thaw_threshold_c"],
        "min_allowed_c": existing["min_allowed_c"],
        "max_allowed_c": existing["max_allowed_c"],
        "cold_dose_budget_c_min": existing["cold_dose_budget_c_min"],
        "heat_dose_budget_c_min": existing["heat_dose_budget_c_min"],
    }
    merged.update(changes)  # 显式 null 也参与合并，用于清空边界/预算
    if merged["freeze_threshold_c"] >= merged["thaw_threshold_c"]:
        raise HTTPException(
            status_code=422,
            detail="freeze_threshold_c 必须严格小于 thaw_threshold_c",
        )
    if (
        merged.get("cold_dose_budget_c_min") is not None
        and merged.get("min_allowed_c") is None
    ):
        raise HTTPException(
            status_code=422,
            detail="配置 cold_dose_budget_c_min 时必须同时提供 min_allowed_c",
        )
    if (
        merged.get("heat_dose_budget_c_min") is not None
        and merged.get("max_allowed_c") is None
    ):
        raise HTTPException(
            status_code=422,
            detail="配置 heat_dose_budget_c_min 时必须同时提供 max_allowed_c",
        )
    services.update_sample(conn, sample_id, changes)
    # 阈值/区间/循环上限/暴露剂量预算或积分间隔变化后按新配置重算
    # （告警确认状态通过 dedup_key 保留）
    if any(
        k in changes
        for k in (
            "freeze_threshold_c",
            "thaw_threshold_c",
            "min_allowed_c",
            "max_allowed_c",
            "max_freeze_thaw_cycles",
            "cold_dose_budget_c_min",
            "heat_dose_budget_c_min",
            "max_integration_interval_minutes",
        )
    ):
        services.recompute(conn, sample_id)
    return services.get_sample(conn, sample_id)


@app.delete("/samples/{sample_id}", status_code=204, tags=["samples"])
def delete_sample(sample_id: str, conn=Depends(services.get_db_connection)):
    if not services.delete_sample(conn, sample_id):
        raise HTTPException(status_code=404, detail="样本不存在")


# ---------------- 读数提交（幂等） ----------------


@app.post(
    "/samples/{sample_id}/readings",
    response_model=BatchReadingsResponse,
    status_code=200,
    tags=["readings"],
)
def submit_readings(
    sample_id: str,
    payload: ReadingsBatch,
    conn=Depends(services.get_db_connection),
):
    _require_sample(conn, sample_id)
    accepted, duplicates, new_alerts = services.add_readings(
        conn,
        sample_id,
        [r.model_dump() for r in payload.readings],
    )
    sample = services.get_sample(conn, sample_id)
    return BatchReadingsResponse(
        sample_id=sample_id,
        accepted=accepted,
        duplicates=duplicates,
        current_state=sample["current_state"],
        cycle_count=sample["cycle_count"],
        new_alerts=new_alerts,
    )


@app.get(
    "/samples/{sample_id}/readings",
    response_model=list[ReadingOut],
    tags=["readings"],
)
def get_readings(
    sample_id: str,
    source: str = Query(
        "all",
        pattern="^(all|direct|logger)$",
        description="all=直接读数与记录仪归属读数合并时间轴；direct=仅直接读数；logger=仅记录仪归属读数",
    ),
    conn=Depends(services.get_db_connection),
):
    _require_sample(conn, sample_id)
    return services.list_readings(conn, sample_id, source=source)


# ---------------- 状态与循环 ----------------


@app.get(
    "/samples/{sample_id}/state",
    response_model=SampleStateOut,
    tags=["state"],
)
def get_sample_state(sample_id: str, conn=Depends(services.get_db_connection)):
    _require_sample(conn, sample_id)
    return services.get_state(conn, sample_id)


@app.get(
    "/samples/{sample_id}/cycles",
    response_model=list[CycleOut],
    tags=["state"],
)
def get_cycles(sample_id: str, conn=Depends(services.get_db_connection)):
    _require_sample(conn, sample_id)
    return services.list_cycles(conn, sample_id)


@app.post(
    "/samples/{sample_id}/recompute",
    response_model=RecomputeResponse,
    tags=["state"],
)
def recompute_sample(sample_id: str, conn=Depends(services.get_db_connection)):
    """手动触发重算（正常提交读数后会自动重算，此接口用于兜底/运维）。"""

    _require_sample(conn, sample_id)
    before = {
        row["dedup_key"]
        for row in conn.execute(
            "SELECT dedup_key FROM alerts WHERE sample_id = ?", (sample_id,)
        )
    }
    new_rows = services.recompute(conn, sample_id)
    sample = services.get_sample(conn, sample_id)
    alert_count = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE sample_id = ?", (sample_id,)
    ).fetchone()["n"]
    new_count = sum(1 for row in new_rows if row["dedup_key"] not in before)
    return RecomputeResponse(
        sample_id=sample_id,
        cycle_count=sample["cycle_count"],
        current_state=sample["current_state"],
        alert_count=alert_count,
        new_alert_count=new_count,
    )


# ---------------- 告警 ----------------


@app.get("/alerts", response_model=list[AlertOut], tags=["alerts"])
def query_alerts(
    sample_id: Optional[str] = None,
    acknowledged: Optional[bool] = Query(None, description="true/false 过滤确认状态"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    conn=Depends(services.get_db_connection),
):
    return services.list_alerts(
        conn,
        sample_id=sample_id,
        acknowledged=acknowledged,
        limit=limit,
        offset=offset,
    )


@app.get("/alerts/{alert_id}", response_model=AlertOut, tags=["alerts"])
def get_alert(alert_id: int, conn=Depends(services.get_db_connection)):
    alert = services.get_alert(conn, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return alert


@app.post("/alerts/{alert_id}/ack", response_model=AlertOut, tags=["alerts"])
def acknowledge_alert(
    alert_id: int,
    payload: AckRequest,
    conn=Depends(services.get_db_connection),
):
    alert = services.acknowledge_alert(conn, alert_id, payload.acknowledged)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return alert


@app.post("/samples/{sample_id}/alerts/ack", tags=["alerts"])
def acknowledge_sample_alerts(
    sample_id: str,
    payload: AckRequest,
    conn=Depends(services.get_db_connection),
):
    _require_sample(conn, sample_id)
    count = services.acknowledge_sample_alerts(conn, sample_id, payload.acknowledged)
    return {"sample_id": sample_id, "updated": count, "acknowledged": payload.acknowledged}


# ---------------- 共享温度记录仪 ----------------


@app.post("/loggers", response_model=LoggerOut, status_code=201, tags=["loggers"])
def create_logger(payload: LoggerCreate, conn=Depends(services.get_db_connection)):
    data = payload.model_dump()
    if data.get("id"):
        if services.get_logger(conn, data["id"]) is not None:
            raise HTTPException(status_code=409, detail="记录仪 ID 已存在")
    return services.create_logger(conn, data)


@app.get("/loggers", response_model=list[LoggerOut], tags=["loggers"])
def list_loggers(conn=Depends(services.get_db_connection)):
    return services.list_loggers(conn)


@app.get("/loggers/{logger_id}", response_model=LoggerOut, tags=["loggers"])
def get_logger(logger_id: str, conn=Depends(services.get_db_connection)):
    return _require_logger(conn, logger_id)


@app.patch("/loggers/{logger_id}", response_model=LoggerOut, tags=["loggers"])
def update_logger(
    logger_id: str, payload: LoggerUpdate, conn=Depends(services.get_db_connection)
):
    _require_logger(conn, logger_id)
    return services.update_logger(conn, logger_id, payload.name)


@app.delete("/loggers/{logger_id}", status_code=204, tags=["loggers"])
def delete_logger(logger_id: str, conn=Depends(services.get_db_connection)):
    if not services.delete_logger(conn, logger_id):
        raise HTTPException(status_code=404, detail="记录仪不存在")


@app.post(
    "/loggers/{logger_id}/readings",
    response_model=LoggerReadingsResponse,
    status_code=200,
    tags=["loggers"],
)
def submit_logger_readings(
    logger_id: str,
    payload: ReadingsBatch,
    conn=Depends(services.get_db_connection),
):
    _require_logger(conn, logger_id)
    accepted, duplicates, new_alerts = services.add_logger_readings(
        conn, logger_id, [r.model_dump() for r in payload.readings]
    )
    affected = services.samples_bound_to_logger(conn, logger_id)
    return LoggerReadingsResponse(
        logger_id=logger_id,
        accepted=accepted,
        duplicates=duplicates,
        affected_samples=affected,
        new_alerts=new_alerts,
    )


@app.get(
    "/loggers/{logger_id}/readings",
    response_model=list[LoggerReadingOut],
    tags=["loggers"],
)
def get_logger_readings(logger_id: str, conn=Depends(services.get_db_connection)):
    _require_logger(conn, logger_id)
    return services.list_logger_readings(conn, logger_id)


# ---------------- 记录仪 ↔ 样本绑定（左闭右开时段） ----------------


@app.get(
    "/samples/{sample_id}/logger-bindings",
    response_model=list[LoggerBindingOut],
    tags=["loggers"],
)
def list_sample_bindings(sample_id: str, conn=Depends(services.get_db_connection)):
    _require_sample(conn, sample_id)
    return services.list_bindings_for_sample(conn, sample_id)


@app.get(
    "/loggers/{logger_id}/bindings",
    response_model=list[LoggerBindingOut],
    tags=["loggers"],
)
def list_logger_bindings(logger_id: str, conn=Depends(services.get_db_connection)):
    _require_logger(conn, logger_id)
    return services.list_bindings_for_logger(conn, logger_id)


@app.post(
    "/samples/{sample_id}/logger-bindings",
    response_model=LoggerBindingOut,
    status_code=201,
    tags=["loggers"],
)
def create_binding(
    sample_id: str,
    logger_id: str,
    payload: LoggerBindingIn,
    conn=Depends(services.get_db_connection),
):
    _require_sample(conn, sample_id)
    _require_logger(conn, logger_id)
    try:
        return services.create_binding(
            conn, sample_id, logger_id, payload.start_time, payload.end_time
        )
    except services.ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.patch(
    "/logger-bindings/{binding_id}",
    response_model=LoggerBindingOut,
    tags=["loggers"],
)
def update_binding(
    binding_id: int,
    payload: LoggerBindingUpdate,
    conn=Depends(services.get_db_connection),
):
    if services.get_binding(conn, binding_id) is None:
        raise HTTPException(status_code=404, detail="绑定不存在")
    changes = payload.model_dump(exclude_unset=True)
    try:
        return services.update_binding(conn, binding_id, changes)
    except services.ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.delete("/logger-bindings/{binding_id}", status_code=204, tags=["loggers"])
def delete_binding(binding_id: int, conn=Depends(services.get_db_connection)):
    if not services.delete_binding(conn, binding_id):
        raise HTTPException(status_code=404, detail="绑定不存在")


# ---------------- 记录仪校准无效时段 ----------------


@app.post(
    "/loggers/{logger_id}/invalid-periods",
    response_model=LoggerInvalidPeriodOut,
    status_code=201,
    tags=["loggers"],
)
def create_invalid_period(
    logger_id: str,
    payload: LoggerInvalidPeriodIn,
    conn=Depends(services.get_db_connection),
):
    """登记校准无效时段（左闭右开；同一记录仪时段不重叠）。

    落在时段内的记录仪读数仍照常幂等入库、可经原始读数接口查询，
    但不归属样本，不参与冻融循环、暴露剂量、告警与一致性核查；
    登记后自动对绑定该记录仪的样本重新对账并回算。
    """

    _require_logger(conn, logger_id)
    try:
        return services.create_invalid_period(
            conn, logger_id, payload.start_time, payload.end_time
        )
    except services.ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get(
    "/loggers/{logger_id}/invalid-periods",
    response_model=list[LoggerInvalidPeriodOut],
    tags=["loggers"],
)
def list_invalid_periods(logger_id: str, conn=Depends(services.get_db_connection)):
    _require_logger(conn, logger_id)
    return services.list_invalid_periods(conn, logger_id)


@app.get(
    "/invalid-periods/{period_id}",
    response_model=LoggerInvalidPeriodOut,
    tags=["loggers"],
)
def get_invalid_period(period_id: int, conn=Depends(services.get_db_connection)):
    period = services.get_invalid_period(conn, period_id)
    if period is None:
        raise HTTPException(status_code=404, detail="校准无效时段不存在")
    return period


@app.patch(
    "/invalid-periods/{period_id}",
    response_model=LoggerInvalidPeriodOut,
    tags=["loggers"],
)
def update_invalid_period(
    period_id: int,
    payload: LoggerInvalidPeriodUpdate,
    conn=Depends(services.get_db_connection),
):
    if services.get_invalid_period(conn, period_id) is None:
        raise HTTPException(status_code=404, detail="校准无效时段不存在")
    changes = payload.model_dump(exclude_unset=True)
    try:
        return services.update_invalid_period(conn, period_id, changes)
    except services.ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.delete("/invalid-periods/{period_id}", status_code=204, tags=["loggers"])
def delete_invalid_period(period_id: int, conn=Depends(services.get_db_connection)):
    """删除校准无效时段：原时段内的有效读数恢复归属，受影响样本自动回算。"""

    if not services.delete_invalid_period(conn, period_id):
        raise HTTPException(status_code=404, detail="校准无效时段不存在")


# ---------------- 样本 × 记录仪温度一致性核查 ----------------


@app.get(
    "/samples/{sample_id}/logger-consistency/{logger_id}",
    response_model=LoggerConsistencyResponse,
    tags=["loggers"],
)
def check_logger_consistency(
    sample_id: str,
    logger_id: str,
    max_time_diff_seconds: float = Query(
        ..., ge=0, description="允许配对的最大事件时间差（秒，非负；恰好等于也可配对）"
    ),
    temp_diff_threshold_c: float = Query(
        ..., ge=0, description="温差异常阈值（°C，非负）；配对温差严格大于该值才计入异常"
    ),
    conn=Depends(services.get_db_connection),
):
    """样本直接读数 vs 当前有效归属的该记录仪读数：不交叉一对一最优配对。

    配对目标按字典序：配对数最多 → 总时间差最小 → 配对读数 ID 序列稳定取舍。
    只读接口：迟到读数 / 绑定调整后的查询反映最新归属，
    不改动原读数与既有冻融、剂量、告警结果；未配对读数不算温差异常。
    """

    _require_sample(conn, sample_id)
    _require_logger(conn, logger_id)
    return services.logger_consistency_check(
        conn, sample_id, logger_id, max_time_diff_seconds, temp_diff_threshold_c
    )
