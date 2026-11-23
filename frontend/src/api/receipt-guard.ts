/** 采集回执窗口校验 API：窗口观测、异常消费、补采请求与分段履约。 */
import { request } from './client'

export type WindowObservation = {
  window_key: string
  window_start: string
  window_end: string
  result: '缺测' | '时间无效' | '实采零条' | '实采正数'
  observed_count: number | null
  receipt_ids: string[]
  invalid_records: Array<{ invalid_id: string; receipt_id: string; raw_time: string; reason: string }>
  updated_at: string | null
  note: string
}

export type Anomaly = {
  anomaly_key: string
  kind: string
  window_key: string | null
  source: string
  detail: string
  dispatchable: boolean
}

export type BackfillSegment = {
  segment_id: string
  window_key: string
  time_range: { start: string; end: string }
  basis_result: string
  basis_receipt_ids: string[]
  status: '待交付' | '交付成功' | '写入失败'
  delivery_receipt: Record<string, unknown> | null
  attempts: number
}

export type BackfillRequest = {
  request_id: string
  alarm_id: string
  source: string
  status: string
  segments: BackfillSegment[]
  blocked_windows: Array<{ window_key: string; reason: string; detail?: string }>
  created_at: string
}

export type ScenarioOverview = {
  window_minutes: number
  window_summary: Record<string, number>
  windows: WindowObservation[]
  invalid_time_records: Array<Record<string, unknown>>
  anomalies: Anomaly[]
  consumption_ledger: Array<{ alarm_id: string; anomaly_key: string; consumed_at: string }>
  backfill_requests: BackfillRequest[]
}

export type GenericResult = Record<string, unknown>

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export function resetScenario(): Promise<{ ok: boolean; scenario: ScenarioOverview }> {
  return request('/api/receipt-guard/scenario/reset', { method: 'POST' }).then(
    (response) => asJson<{ ok: boolean; scenario: ScenarioOverview }>(response),
  )
}

export function fetchOverview(): Promise<ScenarioOverview> {
  return request('/api/receipt-guard/overview').then(
    (response) => asJson<ScenarioOverview>(response),
  )
}

export function registerReceipt(payload: {
  source: string
  receipt_id: string | null
  observed_time: string
  observed_count: number | null
}): Promise<GenericResult> {
  return request('/api/receipt-guard/receipts', {
    method: 'POST',
    body: JSON.stringify(payload),
  }).then((response) => asJson<GenericResult>(response))
}

export function registerInvalidRecord(payload: {
  source: string
  receipt_id: string | null
  raw_time: string
  raw_count: number | null
}): Promise<GenericResult> {
  return request('/api/receipt-guard/invalid-records', {
    method: 'POST',
    body: JSON.stringify(payload),
  }).then((response) => asJson<GenericResult>(response))
}

export function consumeAnomalies(alarmId: string): Promise<GenericResult> {
  return request('/api/receipt-guard/anomalies/consume', {
    method: 'POST',
    body: JSON.stringify({ alarm_id: alarmId }),
  }).then((response) => asJson<GenericResult>(response))
}

export function createBackfill(payload: {
  alarm_id: string
  window_keys: string[]
  source: string
}): Promise<GenericResult> {
  return request('/api/receipt-guard/backfills', {
    method: 'POST',
    body: JSON.stringify(payload),
  }).then((response) => asJson<GenericResult>(response))
}

export function updateDelivery(
  requestId: string,
  segmentId: string,
  payload: { success: boolean; delivery_receipt?: Record<string, unknown>; fail_reason?: string },
): Promise<GenericResult> {
  return request(
    `/api/receipt-guard/backfills/${requestId}/segments/${segmentId}/delivery`,
    { method: 'POST', body: JSON.stringify(payload) },
  ).then((response) => asJson<GenericResult>(response))
}

export function redispatch(requestId: string): Promise<GenericResult> {
  return request(`/api/receipt-guard/backfills/${requestId}/redispatch`, {
    method: 'POST',
  }).then((response) => asJson<GenericResult>(response))
}
