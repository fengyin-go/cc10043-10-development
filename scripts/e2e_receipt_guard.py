"""端到端验证：采集回执窗口校验 + 异常消费 + 补采履约 全链路。

运行：cd backend && .venv/bin/python ../scripts/e2e_receipt_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastapi.testclient import TestClient

from app.main import app
from app.services.receipt_guard import (
    RESULT_MISSING,
    RESULT_POSITIVE,
    RESULT_ZERO,
    SEGMENT_DELIVERED,
    SEGMENT_FAILED,
    SEGMENT_PENDING,
)

client = TestClient(app)
PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def w12(offset: int) -> str:
    return f"2026-11-23 12:{offset:02d}"


print("== 1. 重置值班场景：12:00/12:10 确证零条，12:05 缺测 ==")
r = client.post("/api/receipt-guard/scenario/reset").json()
windows = {w["window_key"]: w for w in r["scenario"]["windows"]}
check("三个窗口就位", set(windows) == {w12(0), w12(5), w12(10)}, str(set(windows)))
check("12:00 实采零条", windows[w12(0)]["result"] == RESULT_ZERO)
check("12:10 实采零条", windows[w12(10)]["result"] == RESULT_ZERO)
check("12:05 缺测", windows[w12(5)]["result"] == RESULT_MISSING)
check("缺测窗口 observed_count 为 null 而非 0",
      windows[w12(5)]["observed_count"] is None,
      str(windows[w12(5)]["observed_count"]))
check("零条窗口 observed_count 显式为 0",
      windows[w12(0)]["observed_count"] == 0)

print("== 2. 未消费异常明细时，补采请求必须被阻断 ==")
r = client.post("/api/receipt-guard/backfills", json={
    "alarm_id": "ALM-1", "window_keys": [w12(0), w12(5), w12(10)],
}).json()
check("未消费即派单被阻断", r.get("ok") is False and r.get("code") == "ANOMALY_NOT_CONSUMED",
      str(r))

print("== 3. 吞吐量告警消费异常明细（留台账） ==")
r = client.post("/api/receipt-guard/anomalies/consume", json={"alarm_id": "ALM-1"}).json()
check("异常全部消费完", r["all_consumed"] is True and r["remaining"] == 0, str(r))
r2 = client.post("/api/receipt-guard/anomalies/consume", json={"alarm_id": "ALM-1"}).json()
check("重复消费不产生重复台账", r2["consumed"] == 0, str(r2))
ledger = client.get("/api/receipt-guard/overview").json()["consumption_ledger"]
check("消费台账留痕", len(ledger) >= 1 and all(x["alarm_id"] == "ALM-1" for x in ledger))

print("== 4. 跨窗补采与缺测范围冲突：零条范围可恢复，缺测窗阻断 ==")
r = client.post("/api/receipt-guard/backfills", json={
    "alarm_id": "ALM-1", "window_keys": [w12(0), w12(5), w12(10)],
}).json()
check("补采请求生成成功", r.get("ok") is True, str(r))
request_id = r["request"]["request_id"]
segments = r["request"]["segments"]
seg_windows = {s["window_key"] for s in segments}
check("只派出两个确证零条窗口", seg_windows == {w12(0), w12(10)}, str(seg_windows))
check("12:05 缺测窗被阻断不可派出",
      any(b["window_key"] == w12(5) for b in r["request"]["blocked_windows"]))
check("每段都关联实际补采时间段",
      all(s["time_range"]["start"] and s["time_range"]["end"] for s in segments))
check("每段都关联依据回执", all(s["basis_receipt_ids"] for s in segments))
check("分段初始状态为待交付", all(s["status"] == SEGMENT_PENDING for s in segments))

print("== 5. 补采一开始即可看到各范围交付与履约情况 ==")
ov = client.get(f"/api/receipt-guard/backfills/{request_id}").json()
check("总览含交付/失败/待交付计数",
      ov["total_segments"] == 2 and ov["delivered"] == 0 and ov["pending"] == 2, str(ov))
check("欠交付清单列出两个分段", len(ov["undelivered_segments"]) == 2)

print("== 6. 首段补采成功、末段写入失败 ==")
first_seg, last_seg = segments[0]["segment_id"], segments[1]["segment_id"]
r = client.post(
    f"/api/receipt-guard/backfills/{request_id}/segments/{first_seg}/delivery",
    json={"success": True, "delivery_receipt": {
        "receipt_id": "DLV-OK-1", "rows_written": 0, "note": "首段零条范围补采交付成功"}},
).json()
check("首段交付成功", r["segment"]["status"] == SEGMENT_DELIVERED)
r = client.post(
    f"/api/receipt-guard/backfills/{request_id}/segments/{last_seg}/delivery",
    json={"success": False, "fail_reason": "下游存储写入失败"},
).json()
check("末段写入失败", r["segment"]["status"] == SEGMENT_FAILED)
check("失败段留有履约回执（含失败原因）",
      r["segment"]["delivery_receipt"]["reason"] == "下游存储写入失败")
ov = client.get(f"/api/receipt-guard/backfills/{request_id}").json()
check("请求状态为部分写入失败", ov["status"] == "部分写入失败")
owed = {s["window_key"] for s in ov["undelivered_segments"]}
check("查出末段仍欠交付，首段不在欠交付清单", owed == {w12(10)}, str(owed))

print("== 7. 对欠交付分段重派（已交付段不重复派出） ==")
r = client.post(f"/api/receipt-guard/backfills/{request_id}/redispatch").json()
check("仅重派失败段", r["redispatched"] == [last_seg], str(r["redispatched"]))
ov = client.get(f"/api/receipt-guard/backfills/{request_id}").json()
seg_map = {s["segment_id"]: s for s in ov["segments"]}
check("重派段回到待交付", seg_map[last_seg]["status"] == SEGMENT_PENDING)
check("已交付段保持交付成功", seg_map[first_seg]["status"] == SEGMENT_DELIVERED)

print("== 8. 迟到观测回到对应窗口：补报 12:05 有 7 条 ==")
r = client.post("/api/receipt-guard/receipts", json={
    "source": "order-stream", "receipt_id": "RCV-LATE-1205",
    "observed_time": w12(7), "observed_count": 7,
}).json()
check("迟到回执归位 12:05 窗口", r.get("window_key") == w12(5), str(r))
detail = client.get(f"/api/receipt-guard/windows/{w12(5)}").json()
check("12:05 更正为实采正数", detail["result"] == RESULT_POSITIVE)
check("观测数量为 7", detail["observed_count"] == 7, str(detail["observed_count"]))

print("== 9. 同窗口补报一条时间无效记录 ==")
r = client.post("/api/receipt-guard/invalid-records", json={
    "source": "order-stream", "receipt_id": "RCV-BAD-TIME",
    "raw_time": "not-a-time", "raw_count": 3,
}).json()
check("时间无效记录被单独留痕", r["result"] == "时间无效")
detail = client.get(f"/api/receipt-guard/windows/{w12(5)}").json()
check("无效记录数量未污染窗口观测，12:05 仍为 7",
      detail["observed_count"] == 7, str(detail["observed_count"]))
invalid = client.get("/api/receipt-guard/overview").json()["invalid_time_records"]
check("时间无效记录可回查", any(x["receipt_id"] == "RCV-BAD-TIME" for x in invalid))

print("== 10. 更正重复送达不能重复累计 ==")
r = client.post("/api/receipt-guard/receipts", json={
    "source": "order-stream", "receipt_id": "RCV-LATE-1205",
    "observed_time": w12(7), "observed_count": 7,
}).json()
check("重复回执被识别更正", r.get("receipt_id") == "RCV-LATE-1205")
detail = client.get(f"/api/receipt-guard/windows/{w12(5)}").json()
check("数量仍是 7 而非 14", detail["observed_count"] == 7, str(detail["observed_count"]))
check("窗口内回执去重后只留一条", detail["receipt_ids"].count("RCV-LATE-1205") == 1)
# 用同回执号更正为其他数量，验证“更正”语义
client.post("/api/receipt-guard/receipts", json={
    "source": "order-stream", "receipt_id": "RCV-LATE-1205",
    "observed_time": w12(7), "observed_count": 9,
})
detail = client.get(f"/api/receipt-guard/windows/{w12(5)}").json()
check("同回执号更正为 9 生效且不累计", detail["observed_count"] == 9,
      str(detail["observed_count"]))

print("== 11. 未携带数字的回执不能补成 0 ==")
client.post("/api/receipt-guard/scenario/reset")
client.post("/api/receipt-guard/receipts", json={
    "source": "order-stream", "receipt_id": "RCV-NO-NUM",
    "observed_time": w12(7), "observed_count": None,
})
detail = client.get(f"/api/receipt-guard/windows/{w12(5)}").json()
check("无数字回执后窗口仍缺测且 observed_count 为 null",
      detail["result"] == RESULT_MISSING and detail["observed_count"] is None,
      str(detail))

print("== 12. 继续操作时真正发出的补采范围 ==")
client.post("/api/receipt-guard/anomalies/consume", json={"alarm_id": "ALM-1"})
# 12:05 现在仍是缺测（reset 后），重新补报 7 条使其具备证据
client.post("/api/receipt-guard/receipts", json={
    "source": "order-stream", "receipt_id": "RCV-LATE-1205",
    "observed_time": w12(7), "observed_count": 7,
})
# 再制造一条时间无效异常并消费，保证消费台账覆盖全部异常
client.post("/api/receipt-guard/invalid-records", json={
    "source": "order-stream", "raw_time": "bad", "raw_count": 1,
})
client.post("/api/receipt-guard/anomalies/consume", json={"alarm_id": "ALM-1"})
r = client.post("/api/receipt-guard/backfills", json={
    "alarm_id": "ALM-1",
    "window_keys": [w12(0), w12(5), w12(10)],
}).json()
check("新补采请求可覆盖全部三个有证据窗口", r.get("ok") is True, str(r))
if r.get("ok"):
    dispatched = [(s["window_key"], s["time_range"], s["basis_result"])
                  for s in r["request"]["segments"]]
    for item in dispatched:
        print(f"     派出分段: {item}")
    check("派出范围恰好 3 段且各带时间段与观测依据",
          len(dispatched) == 3 and all(x[1]["start"] and x[2] for x in dispatched))
    check("12:05 以实采正数依据派出",
          any(x[0] == w12(5) and x[2] == RESULT_POSITIVE for x in dispatched))

print("== 13. 能归窗的时间无效记录，异常明细只产出一次 ==")
client.post("/api/receipt-guard/scenario/reset")
# raw_time 可解析、但业务上标记为时间无效的记录（兜底通道）
r = client.post("/api/receipt-guard/invalid-records", json={
    "source": "order-stream", "receipt_id": "RCV-INV-WINDOWED",
    "raw_time": "2026-11-23 12:07", "raw_count": 5,
}).json()
check("记录挂到 12:05 窗口但数量不计入", r.get("window_key") == w12(5), str(r))
anomalies = client.get("/api/receipt-guard/anomalies").json()["items"]
inv_anomalies = [a for a in anomalies if a["anomaly_key"] == r["invalid_id"]]
check("同一条无效记录在异常清单中只出现一次", len(inv_anomalies) == 1,
      str([a["anomaly_key"] for a in anomalies]))
detail = client.get(f"/api/receipt-guard/windows/{w12(5)}").json()
check("12:05 仍缺测，未被无效记录的数量 5 污染",
      detail["result"] == RESULT_MISSING and detail["observed_count"] is None)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
