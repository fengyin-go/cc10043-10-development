"""吞吐量告警领域服务：监控接入链路的吞吐量波动并在低于阈值时触发告警。

数据链路位置：监控告警层，对整个数据链路的吞吐量进行持续监控和告警触发。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.receipt_guard import receipt_guard
from app.store import store


class ThroughputAlarmService(BaseModuleService):
    MODULE = "throughput_alarm"
    ENTITY = "告警规则"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["规则名称", "监控数据源", "吞吐量下限", "连续超时阈值", "通知方式"]
    REQUIRED_FIELDS = ["规则名称", "监控数据源", "吞吐量下限"]
    STATUS_ORDER = ["待生效", "已生效", "已关闭"]
    ACTION_RULES = {"启用告警": "已生效", "关闭告警": "已关闭"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：吞吐量监控与告警触发 ----

    def check_throughput(self, entry_id: int, current_rate: float) -> dict[str, Any]:
        """检查当前吞吐量是否触达告警阈值。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生效":
            return {"triggered": False, "message": "告警规则未生效"}
        try:
            floor = float(str(entry.get("吞吐量下限", "0")))
            timeout_threshold = int(str(entry.get("连续超时阈值", "3")))
        except (ValueError, TypeError):
            return {"triggered": False, "message": "告警阈值配置错误"}
        if current_rate < floor:
            entry["abnormal"] = True
            notify = entry.get("通知方式", "无")
            return {"triggered": True, "current": current_rate, "floor": floor,
                    "notify": notify, "timeout_threshold": timeout_threshold}
        return {"triggered": False, "current": current_rate, "floor": floor}

    def validate_data_source(self, entry_id: int) -> dict[str, Any]:
        """校验监控数据源是否在系统中已注册。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"found": False, "message": "告警规则不存在"}
        source = entry.get("监控数据源", "")
        sources_found = []
        for module_key in ("stream_source", "api_connector", "api_scheduler"):
            for row in store.rows(module_key):
                if source in str(row):
                    sources_found.append(module_key)
                    break
        return {"found": len(sources_found) > 0, "source": source, "module_types": sources_found}

    def create_incident_from_alarm(self, entry_id: int, detail: str) -> dict[str, Any]:
        """告警触发时生成隔离事件和审计记录。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "规则不存在"}
        source = entry.get("监控数据源", "")
        now = datetime.now(timezone.utc).isoformat()
        isolation_rows = store.rows("failure_isolation")
        fid = max((int(r.get("id", 0)) for r in isolation_rows), default=0) + 1
        isolation_rows.append({
            "id": fid, "失败消息ID": f"throughput-alert-{fid}",
            "失败原因": f"吞吐量偏低: {detail}",
            "原始Topic": source, "隔离时间": now, "重试次数": "0",
            "status": "待处理", "pending": True, "abnormal": False,
        })
        audit_rows = store.rows("stream_audit")
        aid = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        audit_rows.append({
            "id": aid, "处理时间": now, "数据源": source,
            "消息总数": "1", "成功数": "0", "失败原因分布": f"吞吐量偏低: {detail}",
            "status": "数据断流", "pending": True, "abnormal": True,
        })
        return {"ok": True, "isolation_id": fid, "audit_id": aid}

    # ---- 业务扩展：基于采集回执窗口校验的异常消费与补采编排 ----

    def consume_receipt_anomalies(self, entry_id: int) -> dict[str, Any]:
        """吞吐量告警先消费采集回执异常明细，留下消费台账。

        未完成消费之前不允许生成补采请求，避免把“缺测”误判成真实断流后盲目派单。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "告警规则不存在"}
        if entry.get("status") != "已生效":
            return {"ok": False, "message": "告警规则未生效，不能消费异常明细"}
        return receipt_guard.consume_anomalies(alarm_id=str(entry_id))

    def raise_backfill_request(self, entry_id: int, window_keys: list[str]) -> dict[str, Any]:
        """消费异常明细之后才允许生成补采请求；证据不足的窗口由回执守卫阻断。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "blocked": True, "message": "告警规则不存在"}
        return receipt_guard.create_backfill_request(
            alarm_id=str(entry_id),
            window_keys=window_keys,
            source=str(entry.get("监控数据源", "order-stream")),
        )

    def suggest_resume_policy(self, entry_id: int) -> str | None:
        """告警触发时推荐续采策略。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        source = entry.get("监控数据源", "")
        policies = store.rows("resume_policy")
        for policy in policies:
            if policy.get("status") == "已启用" and source in str(policy.get("规则名称", "")):
                return policy.get("规则名称", "")
        return None