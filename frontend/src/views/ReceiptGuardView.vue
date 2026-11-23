<template>
  <section class="page receipt-guard" data-testid="receipt-guard">
    <header class="page-head">
      <div>
        <h2>采集回执窗口校验</h2>
        <p class="page-desc">
          按 5 分钟窗口校验采集回执：缺测不补零、迟到归位、重复更正不累计；
          吞吐量告警先消费异常明细再生成补采，每段补采关联实际时间段与履约回执。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="reset-scenario" @click="onReset">
          重置值班场景
        </button>
        <button class="btn ghost" type="button" @click="load">刷新</button>
      </div>
    </header>

    <p v-if="message" class="notice" :class="messageKind" data-testid="notice">{{ message }}</p>

    <!-- 窗口观测总览 -->
    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">缺测（无数字，不补零）</span>
        <strong class="stat-value">{{ overview?.window_summary['缺测'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">时间无效</span>
        <strong class="stat-value">{{ overview?.window_summary['时间无效'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">实采零条（显式 0）</span>
        <strong class="stat-value">{{ overview?.window_summary['实采零条'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">实采正数</span>
        <strong class="stat-value">{{ overview?.window_summary['实采正数'] ?? 0 }}</strong>
      </article>
    </div>

    <div class="rg-grid">
      <!-- 左：窗口观测明细 -->
      <article class="rg-panel">
        <h3>窗口观测明细</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>窗口</th>
              <th>观测结果</th>
              <th>观测数量</th>
              <th>依据回执</th>
              <th>可派补采</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="w in overview?.windows ?? []" :key="w.window_key" :data-testid="`window-${w.window_key}`">
              <td>{{ w.window_start }} ~ {{ w.window_end }}</td>
              <td><span class="badge" :class="`badge-${w.result}`">{{ w.result }}</span></td>
              <td data-testid="observed-count">{{ w.observed_count === null ? '未观测（非 0）' : w.observed_count }}</td>
              <td>{{ w.receipt_ids.length ? w.receipt_ids.join('、') : '—' }}</td>
              <td>{{ dispatchable(w.result) ? '是' : '否（仅可回查）' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="!(overview?.windows ?? []).length" class="empty-state">暂无窗口，请重置值班场景</p>

        <h3>时间无效记录（数量不计入任何窗口）</h3>
        <table class="data-table">
          <thead>
            <tr><th>记录号</th><th>回执号</th><th>原始时间</th><th>原始数量</th><th>原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in overview?.invalid_time_records ?? []" :key="String(r.invalid_id)">
              <td>{{ r.invalid_id }}</td>
              <td>{{ r.receipt_id }}</td>
              <td>{{ r.raw_time }}</td>
              <td>{{ r.raw_count ?? '—' }}</td>
              <td>{{ r.reason }}</td>
            </tr>
            <tr v-if="!(overview?.invalid_time_records ?? []).length">
              <td colspan="5" class="empty-state">暂无时间无效记录</td>
            </tr>
          </tbody>
        </table>
      </article>

      <!-- 右：操作区 -->
      <article class="rg-panel">
        <h3>登记采集回执</h3>
        <form class="rg-form" @submit.prevent="onRegisterReceipt">
          <label><span>回执号</span>
            <input v-model="receiptForm.receipt_id" placeholder="重复回执号即更正，不累计" data-testid="receipt-id" />
          </label>
          <label><span>观测时间（迟到也归位原窗口）</span>
            <input v-model="receiptForm.observed_time" placeholder="2026-11-23 12:07" required data-testid="receipt-time" />
          </label>
          <label><span>采集数量（留空=未观测，绝不补 0）</span>
            <input v-model="receiptForm.observed_count" placeholder="例如 7；留空表示未带数字" data-testid="receipt-count" />
          </label>
          <button class="btn primary" type="submit" data-testid="submit-receipt">登记/更正回执</button>
        </form>

        <h3>登记时间无效记录</h3>
        <form class="rg-form" @submit.prevent="onRegisterInvalid">
          <label><span>回执号</span>
            <input v-model="invalidForm.receipt_id" placeholder="可选" data-testid="invalid-id" />
          </label>
          <label><span>无法解析的时间</span>
            <input v-model="invalidForm.raw_time" placeholder="not-a-time" required data-testid="invalid-time" />
          </label>
          <label><span>原始数量（不计入窗口）</span>
            <input v-model="invalidForm.raw_count" placeholder="可选" data-testid="invalid-count" />
          </label>
          <button class="btn" type="submit" data-testid="submit-invalid">留痕时间无效</button>
        </form>

        <h3>吞吐量告警 → 异常消费 → 补采派单</h3>
        <form class="rg-form" @submit.prevent="onConsume">
          <label><span>告警规则 ID</span>
            <input v-model="alarmId" data-testid="alarm-id" />
          </label>
          <button class="btn primary" type="submit" data-testid="consume-anomalies">
            消费异常明细（留台账）
          </button>
        </form>
        <ul class="ledger" data-testid="anomaly-list">
          <li v-for="a in overview?.anomalies ?? []" :key="a.anomaly_key">
            <span class="badge badge-时间无效">{{ a.kind }}</span>
            {{ a.window_key ?? '无窗口' }} · {{ a.detail }}
            <em>{{ consumed(a.anomaly_key) ? '已消费' : '未消费' }}</em>
          </li>
          <li v-if="!(overview?.anomalies ?? []).length" class="empty-state">当前无异常明细</li>
        </ul>
        <form class="rg-form" @submit.prevent="onCreateBackfill">
          <label><span>补采窗口（逗号分隔窗口起点）</span>
            <input v-model="backfillWindows" data-testid="backfill-windows"
                   placeholder="2026-11-23 12:00,2026-11-23 12:05,2026-11-23 12:10" />
          </label>
          <button class="btn primary" type="submit" data-testid="create-backfill">生成补采请求</button>
        </form>
      </article>
    </div>

    <!-- 补采请求与分段履约 -->
    <article class="rg-panel" v-for="req in overview?.backfill_requests ?? []" :key="req.request_id"
             :data-testid="`backfill-${req.request_id}`">
      <h3>补采请求 {{ req.request_id }}
        <small>告警 {{ req.alarm_id }} · 状态：{{ req.status }}</small>
      </h3>
      <p v-if="req.blocked_windows.length" class="notice warn" data-testid="blocked-windows">
        证据不足被阻断（可回查原因，禁止派出）：
        <span v-for="b in req.blocked_windows" :key="b.window_key">
          【{{ b.window_key }}：{{ b.reason }}】
        </span>
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>分段</th><th>实际补采时间段</th><th>观测依据</th><th>依据回执</th>
            <th>履约状态</th><th>交付/失败回执</th><th>尝试次数</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in req.segments" :key="s.segment_id" :data-testid="`segment-${s.segment_id}`">
            <td>{{ s.segment_id }}</td>
            <td>{{ s.time_range.start }} ~ {{ s.time_range.end }}</td>
            <td><span class="badge" :class="`badge-${s.basis_result}`">{{ s.basis_result }}</span></td>
            <td>{{ s.basis_receipt_ids.join('、') }}</td>
            <td><span class="badge" :class="`seg-${s.status}`">{{ s.status }}</span></td>
            <td class="receipt-cell">{{ s.delivery_receipt ? JSON.stringify(s.delivery_receipt) : '—' }}</td>
            <td>{{ s.attempts }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`deliver-${s.segment_id}`"
                      @click="onDelivery(req.request_id, s.segment_id, true)">登记交付成功</button>
              <button class="link danger" type="button" :data-testid="`fail-${s.segment_id}`"
                      @click="onDelivery(req.request_id, s.segment_id, false)">登记写入失败</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="page-actions">
        <button class="btn" type="button" :data-testid="`redispatch-${req.request_id}`"
                @click="onRedispatch(req.request_id)">
          重派仍欠交付的分段
        </button>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  consumeAnomalies,
  createBackfill,
  fetchOverview,
  redispatch,
  registerInvalidRecord,
  registerReceipt,
  resetScenario,
  updateDelivery,
} from '@/api/receipt-guard'
import type { ScenarioOverview } from '@/api/receipt-guard'

const overview = ref<ScenarioOverview | null>(null)
const message = ref('')
const messageKind = ref('ok')
const alarmId = ref('ALM-1')
const backfillWindows = ref('2026-11-23 12:00,2026-11-23 12:05,2026-11-23 12:10')

const receiptForm = ref({ receipt_id: '', observed_time: '', observed_count: '' })
const invalidForm = ref({ receipt_id: '', raw_time: '', raw_count: '' })

function notify(text: string, ok = true) {
  message.value = text
  messageKind.value = ok ? 'ok' : 'warn'
}

function dispatchable(result: string): boolean {
  return result === '实采零条' || result === '实采正数'
}

function consumed(anomalyKey: string): boolean {
  return (overview.value?.consumption_ledger ?? [])
    .some((item) => item.alarm_id === alarmId.value && item.anomaly_key === anomalyKey)
}

async function load() {
  overview.value = await fetchOverview()
}

async function onReset() {
  const payload = await resetScenario()
  overview.value = payload.scenario
  notify('已重置为值班场景：12:00/12:10 确证零条，12:05 缺测')
}

async function onRegisterReceipt() {
  const rawCount = receiptForm.value.observed_count.trim()
  const result = await registerReceipt({
    source: 'order-stream',
    receipt_id: receiptForm.value.receipt_id.trim() || null,
    observed_time: receiptForm.value.observed_time.trim(),
    observed_count: rawCount === '' ? null : Number(rawCount),
  })
  notify(String(result.message ?? '回执已登记'), Boolean(result.ok))
  receiptForm.value = { receipt_id: '', observed_time: '', observed_count: '' }
  await load()
}

async function onRegisterInvalid() {
  const rawCount = invalidForm.value.raw_count.trim()
  const result = await registerInvalidRecord({
    source: 'order-stream',
    receipt_id: invalidForm.value.receipt_id.trim() || null,
    raw_time: invalidForm.value.raw_time.trim(),
    raw_count: rawCount === '' ? null : Number(rawCount),
  })
  notify(String(result.message ?? '时间无效记录已留痕'), Boolean(result.ok))
  invalidForm.value = { receipt_id: '', raw_time: '', raw_count: '' }
  await load()
}

async function onConsume() {
  const result = await consumeAnomalies(alarmId.value)
  notify(`异常消费完成：本次 ${String(result.consumed)} 条，剩余 ${String(result.remaining)} 条`,
         Boolean(result.all_consumed))
  await load()
}

async function onCreateBackfill() {
  const keys = backfillWindows.value.split(',').map((item) => item.trim()).filter(Boolean)
  const result = await createBackfill({
    alarm_id: alarmId.value,
    window_keys: keys,
    source: 'order-stream',
  })
  if (result.ok) {
    notify(String(result.message))
  } else {
    const blocked = (result.blocked_windows as Array<{ window_key: string; reason: string }> | undefined)
      ?.map((b) => `${b.window_key}(${b.reason})`).join('、') ?? ''
    notify(`${String(result.message)}${blocked ? `；阻断窗口：${blocked}` : ''}`, false)
  }
  await load()
}

async function onDelivery(requestId: string, segmentId: string, success: boolean) {
  const result = await updateDelivery(requestId, segmentId, success
    ? { success: true, delivery_receipt: { receipt_id: `DLV-${segmentId}`, rows_written: 0 } }
    : { success: false, fail_reason: '下游存储写入失败' })
  notify(success ? `分段 ${segmentId} 已登记交付成功` : `分段 ${segmentId} 已登记写入失败`,
         Boolean(result.ok))
  await load()
}

async function onRedispatch(requestId: string) {
  const result = await redispatch(requestId)
  const ids = (result.redispatched as string[] | undefined) ?? []
  notify(ids.length ? `已重派分段：${ids.join('、')}` : '没有欠交付分段需要重派')
  await load()
}

onMounted(load)
</script>

<style scoped>
.receipt-guard { display: flex; flex-direction: column; gap: 12px; }
.notice { padding: 8px 12px; border-radius: 6px; font-size: 13px; margin: 0; }
.notice.ok { background: #ecfdf3; color: #027a48; border: 1px solid #abefc6; }
.notice.warn { background: #fffaeb; color: #b54708; border: 1px solid #fedf89; }
.rg-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 12px; }
.rg-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.rg-panel h3 { font-size: 14px; margin: 8px 0; }
.rg-panel h3 small { color: var(--muted); font-weight: 400; margin-left: 8px; }
.rg-form { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.rg-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.rg-form input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; white-space: nowrap; }
.badge-缺测 { background: #f2f4f7; color: #475467; }
.badge-时间无效 { background: #fef3f2; color: #b42318; }
.badge-实采零条 { background: #fffaeb; color: #b54708; }
.badge-实采正数 { background: #ecfdf3; color: #027a48; }
.seg-待交付 { background: #eff8ff; color: #175cd3; }
.seg-交付成功 { background: #ecfdf3; color: #027a48; }
.seg-写入失败 { background: #fef3f2; color: #b42318; }
.ledger { list-style: none; padding: 0; margin: 0 0 12px; font-size: 12px; }
.ledger li { padding: 4px 0; border-bottom: 1px solid #f0f2f5; display: flex; gap: 6px; align-items: center; }
.ledger em { font-style: normal; color: var(--muted); margin-left: auto; }
.receipt-cell { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.link.danger { color: #b42318; }
@media (max-width: 1100px) { .rg-grid { grid-template-columns: 1fr; } }
</style>
