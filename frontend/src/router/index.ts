import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ReceiptGuardView from '@/views/ReceiptGuardView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径放在动态 :moduleKey 之前，避免被通用工作台抢先匹配。
    { path: '/receipt-guard', name: 'receipt-guard', component: ReceiptGuardView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
