"""采集回执窗口校验路由：窗口观测、异常消费、补采请求与履约回执。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.receipt_guard import receipt_guard

router = APIRouter(prefix="/api/receipt-guard", tags=["采集回执窗口校验"])


class ReceiptPayload(BaseModel):
    source: str = "order-stream"
    receipt_id: str | None = None
    observed_time: str
    # None 表示回执未携带数量：保持未观测，后端绝不补 0。
    observed_count: int | None = None


class InvalidRecordPayload(BaseModel):
    source: str = "order-stream"
    receipt_id: str | None = None
    raw_time: str
    raw_count: Any = None


class ConsumePayload(BaseModel):
    alarm_id: str


class BackfillPayload(BaseModel):
    alarm_id: str
    window_keys: list[str] = Field(default_factory=list)
    source: str = "order-stream"


class DeliveryPayload(BaseModel):
    success: bool
    delivery_receipt: dict[str, Any] | None = None
    fail_reason: str = ""


@router.post("/scenario/reset")
def reset_scenario() -> dict[str, Any]:
    """重置为值班初始场景（12:00/12:10 零条，12:05 缺测）。"""
    receipt_guard.__init__()
    receipt_guard.seed_duty_scenario()
    return {"ok": True, "scenario": receipt_guard.scenario_overview()}


@router.get("/overview")
def overview() -> dict[str, Any]:
    return receipt_guard.scenario_overview()


@router.get("/windows")
def list_windows() -> dict[str, Any]:
    return {"items": receipt_guard.list_windows()}


@router.get("/windows/{window_key:path}")
def window_detail(window_key: str) -> dict[str, Any]:
    detail = receipt_guard.window_detail(window_key)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"窗口 {window_key} 不存在")
    return detail


@router.post("/receipts")
def register_receipt(payload: ReceiptPayload) -> dict[str, Any]:
    return receipt_guard.register_receipt(
        source=payload.source,
        receipt_id=payload.receipt_id,
        observed_time=payload.observed_time,
        observed_count=payload.observed_count,
    )


@router.post("/invalid-records")
def register_invalid_record(payload: InvalidRecordPayload) -> dict[str, Any]:
    return receipt_guard.register_invalid_record(
        source=payload.source,
        receipt_id=payload.receipt_id,
        raw_time=payload.raw_time,
        raw_count=payload.raw_count,
    )


@router.get("/anomalies")
def list_anomalies() -> dict[str, Any]:
    return {"items": receipt_guard.list_anomalies()}


@router.post("/anomalies/consume")
def consume_anomalies(payload: ConsumePayload) -> dict[str, Any]:
    return receipt_guard.consume_anomalies(alarm_id=payload.alarm_id)


@router.post("/backfills")
def create_backfill(payload: BackfillPayload) -> dict[str, Any]:
    result = receipt_guard.create_backfill_request(
        alarm_id=payload.alarm_id,
        window_keys=payload.window_keys,
        source=payload.source,
    )
    if not result.get("ok"):
        # 业务阻断（未消费异常 / 证据不足）仍以 200 返回结构化原因，便于前端展示。
        return result
    return result


@router.get("/backfills")
def list_backfills() -> dict[str, Any]:
    return {"items": receipt_guard.list_backfill_requests()}


@router.get("/backfills/{request_id}")
def backfill_detail(request_id: str) -> dict[str, Any]:
    detail = receipt_guard.backfill_overview(request_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"补采请求 {request_id} 不存在")
    return detail


@router.post("/backfills/{request_id}/segments/{segment_id}/delivery")
def update_delivery(request_id: str, segment_id: str, payload: DeliveryPayload) -> dict[str, Any]:
    result = receipt_guard.update_segment_delivery(
        request_id=request_id,
        segment_id=segment_id,
        success=payload.success,
        delivery_receipt=payload.delivery_receipt,
        fail_reason=payload.fail_reason,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/backfills/{request_id}/redispatch")
def redispatch(request_id: str) -> dict[str, Any]:
    result = receipt_guard.redispatch_failed(request_id=request_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result
