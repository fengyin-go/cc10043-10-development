"""采集回执窗口校验领域服务。

背景：处理进度在“没有收到可用采集回执”时会和真实断流一样触发停滞，
而说明原因的异常明细缺少可信观测。本服务按固定时间窗口校验采集回执，
为缺测、时间无效、实际采到零条、采到正数分别记录可追溯的观测结果。

核心不变量：
1. 未观测到数字时 observed_count 保持 None，绝不补成 0；
   只有回执显式给出 0 才记为“实采零条”。
2. 迟到观测按回执里的窗口时间归位到它对应的窗口，而不是落到当前窗口。
3. 同一回执重复送达时按 receipt_id 去重并更正窗口结果，不重复累计。
4. 吞吐量告警必须先消费异常明细（留消费台账），才能生成补采请求。
5. 证据不足（缺测/时间无效）的窗口仍可回查异常原因，但不能派出补采；
   每个补采分段都必须关联实际补采时间段及其交付/履约回执。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# 窗口大小（分钟）：值班场景使用 12:00 / 12:05 / 12:10 三个相邻窗口。
WINDOW_MINUTES = 5

# 窗口观测结果枚举。
RESULT_MISSING = "缺测"          # 窗口已过但未收到任何可用回执（observed_count 为 None）
RESULT_INVALID_TIME = "时间无效"  # 收到记录但时间无法归入任何有效窗口
RESULT_ZERO = "实采零条"          # 回执显式确认采集到 0 条
RESULT_POSITIVE = "实采正数"      # 回执显式确认采集到正数条

# 补采分段履约状态。
SEGMENT_PENDING = "待交付"
SEGMENT_DELIVERED = "交付成功"
SEGMENT_FAILED = "写入失败"

# 缺测/时间无效属于证据不足：可以回查原因，但禁止派出补采。
UNDISPATCHABLE_RESULTS = {RESULT_MISSING, RESULT_INVALID_TIME}


def _parse_time(value: str) -> datetime | None:
    """尽量宽容地解析时间字符串；无法解析时返回 None（调用方记时间无效）。"""
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    # 允许 "2026-11-23 12:03" 这类以空格分隔的本地时间写法。
    candidates = [text]
    if " " in text:
        candidates.append(text.replace(" ", "T"))
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def window_start_for(dt: datetime) -> datetime:
    """把任意时刻向下取整到窗口起点。"""
    minute = (dt.minute // WINDOW_MINUTES) * WINDOW_MINUTES
    return dt.replace(minute=minute, second=0, microsecond=0)


def window_key(start: datetime) -> str:
    """窗口对外标识，使用窗口起点的本地样式字符串（分钟对齐）。"""
    return start.strftime("%Y-%m-%d %H:%M")


def window_end_for(start: datetime) -> datetime:
    return start + timedelta(minutes=WINDOW_MINUTES)


class ReceiptGuardService:
    """采集回执窗口校验 + 异常消费 + 补采履约 的内存领域服务。"""

    def __init__(self) -> None:
        # key: 窗口起点字符串 -> 窗口观测记录
        self.windows: dict[str, dict[str, Any]] = {}
        # 回执登记流水（含时间无效记录），key 为 receipt_id
        self.receipts: dict[str, dict[str, Any]] = {}
        # 异常明细消费台账：(alarm_id, window_key) -> 消费记录
        self.consumptions: dict[tuple[str, str], dict[str, Any]] = {}
        # 补采请求
        self.backfill_requests: list[dict[str, Any]] = []
        self._backfill_seq = 0
        self._receipt_seq = 0
        self._invalid_seq = 0
        self._seeded = False

    # ---------- 初始化：值班场景 ----------

    def seed_duty_scenario(self) -> None:
        """初始化一次值班发现：12:00 与 12:10 确证零条，12:05 尚未收到回执。"""
        if self._seeded:
            return
        self._seeded = True
        base = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        # 12:00 与 12:10 分别有显式回执确认零条。
        for offset in (0, 10):
            start = base + timedelta(minutes=offset)
            self._ensure_window(start)
            self.register_receipt(
                source="order-stream",
                receipt_id=f"duty-zero-{offset // WINDOW_MINUTES}",
                observed_time=window_key(start + timedelta(minutes=1)),
                observed_count=0,
            )
        # 12:05 没有任何回执：显式建窗并标记为缺测。
        missing_start = base + timedelta(minutes=WINDOW_MINUTES)
        window = self._ensure_window(missing_start)
        window["result"] = RESULT_MISSING
        window["observed_count"] = None
        window["note"] = "窗口已过，尚未收到可用采集回执"

    # ---------- 窗口与回执 ----------

    def _ensure_window(self, start: datetime) -> dict[str, Any]:
        key = window_key(start)
        window = self.windows.get(key)
        if window is None:
            window = {
                "window_key": key,
                "window_start": key,
                "window_end": window_key(window_end_for(start)),
                "result": RESULT_MISSING,
                # None 表示“未观测到数字”，与显式 0 严格区分，绝不补零。
                "observed_count": None,
                "receipt_ids": [],
                "invalid_records": [],
                "updated_at": None,
                "note": "",
            }
            self.windows[key] = window
        return window

    def register_receipt(
        self,
        *,
        source: str,
        receipt_id: str | None,
        observed_time: str,
        observed_count: int | None,
    ) -> dict[str, Any]:
        """登记一条采集回执。

        - 时间无法解析：记一条“时间无效”观测，不挂到任何窗口、不产生数字。
        - 时间可解析：归位到对应窗口（迟到也回到原窗口）。
        - receipt_id 重复送达：更正原窗口结果，不重复累计。
        - observed_count 为 None（回执未带数字）：保持未观测，不补 0。
        """
        now = datetime.now(timezone.utc).isoformat()
        dt = _parse_time(observed_time)

        # 时间无效：回执本身不可信，单独留痕，禁止用来推断窗口数字。
        if dt is None:
            self._invalid_seq += 1
            record = {
                "invalid_id": f"INV-{self._invalid_seq:04d}",
                "source": source,
                "receipt_id": receipt_id or f"(无回执号)-{self._invalid_seq}",
                "raw_time": observed_time,
                "raw_count": observed_count,
                "recorded_at": now,
                "reason": "时间字段无法解析，不能归入任何采集窗口",
            }
            self.receipts[record["receipt_id"]] = {
                **record, "kind": "invalid_time", "window_key": None,
            }
            return {"ok": True, "accepted": False, "result": RESULT_INVALID_TIME,
                    "invalid_id": record["invalid_id"],
                    "message": "回执时间无效，已登记时间无效观测，不计入任何窗口"}

        start = window_start_for(dt)
        window = self._ensure_window(start)
        rid = receipt_id or f"RCV-AUTO-{self._next_receipt_seq()}"

        # 重复送达：先从旧窗口摘除，再按新观测更正，保证不重复累计。
        previous = self.receipts.get(rid)
        if previous is not None and previous.get("kind") == "windowed":
            old_window = self.windows.get(previous["window_key"])
            if old_window is not None and rid in old_window["receipt_ids"]:
                old_window["receipt_ids"].remove(rid)
            self._recompute_window(old_window)

        # 未带数字的回执：只证明“回执到达”，不产生 0；窗口维持缺测/原观测。
        if observed_count is None:
            window["receipt_ids"].append(rid)
            window["updated_at"] = now
            window["note"] = "回执到达但未携带采集数量，保持未观测"
            self.receipts[rid] = {
                "receipt_id": rid, "kind": "windowed", "source": source,
                "window_key": window["window_key"], "observed_time": observed_time,
                "observed_count": None, "recorded_at": now,
            }
            self._recompute_window(window)
            return {"ok": True, "accepted": True, "result": window["result"],
                    "window_key": window["window_key"], "receipt_id": rid,
                    "message": "回执已归位窗口，但未观测到数字，未补零"}

        count = int(observed_count)
        self.receipts[rid] = {
            "receipt_id": rid, "kind": "windowed", "source": source,
            "window_key": window["window_key"], "observed_time": observed_time,
            "observed_count": count, "recorded_at": now,
        }
        if rid not in window["receipt_ids"]:
            window["receipt_ids"].append(rid)
        window["updated_at"] = now
        window["note"] = ""
        self._recompute_window(window)
        return {"ok": True, "accepted": True, "result": window["result"],
                "window_key": window["window_key"], "receipt_id": rid,
                "observed_count": count,
                "message": f"回执已归位窗口 {window['window_key']}"}

    def register_invalid_record(
        self, *, source: str, raw_time: str, raw_count: Any, receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """显式补报一条时间无效的采集记录（可附带它原本想表达的数量）。"""
        now = datetime.now(timezone.utc).isoformat()
        self._invalid_seq += 1
        rid = receipt_id or f"INV-RCV-{self._invalid_seq:04d}"
        record = {
            "invalid_id": f"INV-{self._invalid_seq:04d}",
            "source": source,
            "receipt_id": rid,
            "raw_time": raw_time,
            "raw_count": raw_count,
            "recorded_at": now,
            "reason": "时间字段无法解析，不能归入任何采集窗口",
        }
        self.receipts[rid] = {**record, "kind": "invalid_time"}

        # 若能从 raw_time 看出目标窗口（例如只缺秒、但 fromisoformat 失败的兜底），
        # 仍不把数字计入，只在窗口上挂一条无效记录便于回查。
        dt = _parse_time(raw_time)
        target_window = None
        attached_window_key = None
        if dt is not None:
            target_window = self._ensure_window(window_start_for(dt))
            target_window["invalid_records"].append(record)
            attached_window_key = target_window["window_key"]
        # window_key 非空表示该无效记录已挂到具体窗口，异常清单只由窗口侧产出一次。
        self.receipts[rid] = {**record, "kind": "invalid_time",
                              "window_key": attached_window_key}
        return {"ok": True, "result": RESULT_INVALID_TIME,
                "invalid_id": record["invalid_id"],
                "window_key": attached_window_key,
                "message": "时间无效记录已留痕，其数量未计入任何窗口"}

    def _recompute_window(self, window: dict[str, Any]) -> None:
        """根据窗口内当前有效回执重算观测结果（更正重复送达后的收口逻辑）。"""
        counts = [
            self.receipts[rid]["observed_count"]
            for rid in window["receipt_ids"]
            if rid in self.receipts
            and self.receipts[rid].get("kind") == "windowed"
            and self.receipts[rid].get("observed_count") is not None
        ]
        if not counts:
            # 没有任何带数字的回执：保持缺测，绝不补 0。
            window["observed_count"] = None
            window["result"] = RESULT_MISSING
            return
        # 同一窗口多张回执按最后观测更正口径求和会重复；这里以回执为粒度，
        # 重复 receipt_id 已在登记时去重，故对去重后的回执数量求和。
        total = sum(counts)
        window["observed_count"] = total
        window["result"] = RESULT_ZERO if total == 0 else RESULT_POSITIVE

    def _next_receipt_seq(self) -> int:
        self._receipt_seq += 1
        return self._receipt_seq

    # ---------- 查询 ----------

    def list_windows(self) -> list[dict[str, Any]]:
        return [self.windows[key] for key in sorted(self.windows)]

    def window_detail(self, window_key: str) -> dict[str, Any] | None:
        window = self.windows.get(window_key)
        if window is None:
            return None
        detail = dict(window)
        detail["receipts"] = [
            self.receipts[rid] for rid in window["receipt_ids"] if rid in self.receipts
        ]
        detail["dispatchable"] = window["result"] not in UNDISPATCHABLE_RESULTS
        return detail

    def list_anomalies(self) -> list[dict[str, Any]]:
        """异常明细：缺测窗口 + 时间无效记录，供吞吐量告警消费。"""
        items: list[dict[str, Any]] = []
        for key in sorted(self.windows):
            window = self.windows[key]
            if window["result"] in UNDISPATCHABLE_RESULTS:
                items.append({
                    "anomaly_key": f"W:{key}",
                    "kind": window["result"],
                    "window_key": key,
                    "source": "order-stream",
                    "detail": window["note"] or "窗口缺少可信采集回执",
                    "dispatchable": False,
                })
            for record in window["invalid_records"]:
                items.append({
                    "anomaly_key": record["invalid_id"],
                    "kind": RESULT_INVALID_TIME,
                    "window_key": key,
                    "source": record["source"],
                    "detail": record["reason"],
                    "dispatchable": False,
                })
        for rid, receipt in self.receipts.items():
            if receipt.get("kind") == "invalid_time" and receipt.get("window_key") is None:
                items.append({
                    "anomaly_key": receipt["invalid_id"],
                    "kind": RESULT_INVALID_TIME,
                    "window_key": None,
                    "source": receipt["source"],
                    "detail": receipt["reason"],
                    "dispatchable": False,
                })
        return items

    # ---------- 异常消费（告警侧台账） ----------

    def consume_anomalies(self, *, alarm_id: str) -> dict[str, Any]:
        """吞吐量告警消费异常明细：逐条留台账，未消费完不允许生成补采请求。"""
        now = datetime.now(timezone.utc).isoformat()
        consumed: list[dict[str, Any]] = []
        for anomaly in self.list_anomalies():
            ledger_key = (str(alarm_id), anomaly["anomaly_key"])
            if ledger_key not in self.consumptions:
                self.consumptions[ledger_key] = {
                    "alarm_id": str(alarm_id),
                    "anomaly_key": anomaly["anomaly_key"],
                    "consumed_at": now,
                }
                consumed.append(anomaly)
        remaining = [
            anomaly for anomaly in self.list_anomalies()
            if (str(alarm_id), anomaly["anomaly_key"]) not in self.consumptions
        ]
        return {"ok": True, "alarm_id": str(alarm_id),
                "consumed": len(consumed), "remaining": len(remaining),
                "all_consumed": len(remaining) == 0}

    def _anomalies_consumed(self, alarm_id: str) -> bool:
        anomalies = self.list_anomalies()
        return all(
            (str(alarm_id), anomaly["anomaly_key"]) in self.consumptions
            for anomaly in anomalies
        ) and bool(anomalies)

    # ---------- 补采请求 ----------

    def create_backfill_request(
        self, *, alarm_id: str, window_keys: list[str], source: str = "order-stream",
    ) -> dict[str, Any]:
        """生成补采请求：按窗口证据分段，证据不足的窗口整段阻断派出。

        每个分段关联实际补采时间段；零条/正数窗口可派出，缺测/时间无效窗口
        只能回查异常原因，不能派出。
        """
        # 规则 1：告警必须先消费异常明细。
        if not self._anomalies_consumed(alarm_id):
            return {"ok": False, "blocked": True,
                    "code": "ANOMALY_NOT_CONSUMED",
                    "message": "吞吐量告警尚未消费完异常明细，不能生成补采请求"}

        segments: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for key in window_keys:
            window = self.windows.get(key)
            if window is None:
                blocked.append({"window_key": key, "reason": "窗口不存在，证据不足"})
                continue
            if window["result"] in UNDISPATCHABLE_RESULTS:
                blocked.append({
                    "window_key": key, "reason": window["result"],
                    "detail": window["note"] or "缺少可信采集回执，可回查异常原因但禁止派出",
                })
                continue
            segments.append({
                "segment_id": "",  # 建请求时统一编号
                "window_key": key,
                "time_range": {"start": window["window_start"], "end": window["window_end"]},
                "basis_result": window["result"],
                "basis_receipt_ids": list(window["receipt_ids"]),
                "status": SEGMENT_PENDING,
                "delivery_receipt": None,
                "attempts": 0,
            })

        if not segments:
            return {"ok": False, "blocked": True, "code": "NO_DISPATCHABLE_WINDOW",
                    "blocked_windows": blocked,
                    "message": "所选窗口均证据不足，没有可派出的补采分段"}

        self._backfill_seq += 1
        request_id = f"BF-{self._backfill_seq:04d}"
        for index, segment in enumerate(segments, start=1):
            segment["segment_id"] = f"{request_id}-S{index}"
        request = {
            "request_id": request_id,
            "alarm_id": str(alarm_id),
            "source": source,
            "status": "待履约",
            "segments": segments,
            "blocked_windows": blocked,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.backfill_requests.append(request)
        return {"ok": True, "request": request,
                "message": f"补采请求 {request_id} 已生成，含 {len(segments)} 个可派分段"
                           + (f"，{len(blocked)} 个窗口证据不足被阻断" if blocked else "")}

    def list_backfill_requests(self) -> list[dict[str, Any]]:
        return self.backfill_requests

    def backfill_overview(self, request_id: str) -> dict[str, Any] | None:
        """补采一开始就能看到各范围（分段）的交付与履约情况。"""
        request = self._find_request(request_id)
        if request is None:
            return None
        delivered = [s for s in request["segments"] if s["status"] == SEGMENT_DELIVERED]
        failed = [s for s in request["segments"] if s["status"] == SEGMENT_FAILED]
        pending = [s for s in request["segments"] if s["status"] == SEGMENT_PENDING]
        return {
            "request_id": request_id,
            "status": request["status"],
            "total_segments": len(request["segments"]),
            "delivered": len(delivered),
            "failed": len(failed),
            "pending": len(pending),
            "segments": request["segments"],
            "blocked_windows": request["blocked_windows"],
            "undelivered_segments": [
                {"segment_id": s["segment_id"], "window_key": s["window_key"],
                 "time_range": s["time_range"], "status": s["status"]}
                for s in request["segments"] if s["status"] != SEGMENT_DELIVERED
            ],
        }

    def update_segment_delivery(
        self, *, request_id: str, segment_id: str, success: bool,
        delivery_receipt: dict[str, Any] | None = None, fail_reason: str = "",
    ) -> dict[str, Any]:
        """登记某补采分段的交付/履约回执（成功交付或写入失败）。"""
        request = self._find_request(request_id)
        if request is None:
            return {"ok": False, "message": "补采请求不存在"}
        segment = next((s for s in request["segments"] if s["segment_id"] == segment_id), None)
        if segment is None:
            return {"ok": False, "message": f"分段 {segment_id} 不存在"}
        segment["attempts"] += 1
        now = datetime.now(timezone.utc).isoformat()
        if success:
            segment["status"] = SEGMENT_DELIVERED
            segment["delivery_receipt"] = delivery_receipt or {
                "receipt_id": f"DLV-{segment_id}", "delivered_at": now, "rows_written": 0,
            }
        else:
            segment["status"] = SEGMENT_FAILED
            segment["delivery_receipt"] = {
                "receipt_id": None, "failed_at": now, "reason": fail_reason or "写入失败",
            }
        self._refresh_request_status(request)
        return {"ok": True, "segment": segment,
                "overview": self.backfill_overview(request_id)}

    def redispatch_failed(self, *, request_id: str) -> dict[str, Any]:
        """对仍欠交付（写入失败）的分段重新派出，已交付分段不重复派出。"""
        request = self._find_request(request_id)
        if request is None:
            return {"ok": False, "message": "补采请求不存在"}
        redispatched: list[str] = []
        for segment in request["segments"]:
            if segment["status"] == SEGMENT_FAILED:
                segment["status"] = SEGMENT_PENDING
                segment["delivery_receipt"] = None
                redispatched.append(segment["segment_id"])
        self._refresh_request_status(request)
        return {"ok": True, "redispatched": redispatched,
                "overview": self.backfill_overview(request_id)}

    def _refresh_request_status(self, request: dict[str, Any]) -> None:
        statuses = {s["status"] for s in request["segments"]}
        if statuses == {SEGMENT_DELIVERED}:
            request["status"] = "全部交付"
        elif SEGMENT_FAILED in statuses:
            request["status"] = "部分写入失败"
        elif SEGMENT_PENDING in statuses:
            request["status"] = "履约中"

    def _find_request(self, request_id: str) -> dict[str, Any] | None:
        return next(
            (r for r in self.backfill_requests if r["request_id"] == request_id), None,
        )

    # ---------- 总览 ----------

    def scenario_overview(self) -> dict[str, Any]:
        windows = self.list_windows()
        summary = {result: 0 for result in
                   (RESULT_MISSING, RESULT_INVALID_TIME, RESULT_ZERO, RESULT_POSITIVE)}
        for window in windows:
            summary[window["result"]] += 1
        invalid_count = sum(
            1 for receipt in self.receipts.values()
            if receipt.get("kind") == "invalid_time"
        )
        return {
            "window_minutes": WINDOW_MINUTES,
            "window_summary": summary,
            "windows": windows,
            "invalid_time_records": [
                receipt for receipt in self.receipts.values()
                if receipt.get("kind") == "invalid_time"
            ],
            "anomalies": self.list_anomalies(),
            "consumption_ledger": list(self.consumptions.values()),
            "backfill_requests": self.backfill_requests,
        }


# 单例：与 SERVICES 中的模块服务一样，进程内共享一份状态。
receipt_guard = ReceiptGuardService()
