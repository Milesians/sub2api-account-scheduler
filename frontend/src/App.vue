<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

interface Summary {
  account_count: number
  last_run_id: string | null
  last_decided_at: string | null
  last_run_decision_count: number
  last_run_changed_count: number
}

interface DashboardAccount {
  account_id: number
  name: string
  last_priority: number | null
  last_7d_used: number | null
  last_7d_reset_at: string | null
  last_5h_used: number | null
  last_sampled_at: string | null
  hourly_burn_ewma: number | null
  cooldown_until: string | null
  last_reason: string | null
}

interface Decision {
  decided_at: string | null
  account_id: number
  account_name: string
  current_priority: number | null
  target_priority: number | null
  current_load_factor: number | null
  target_load_factor: number | null
  reason: string | null
  seven_day_used: number | null
  seven_day_reset_at: string | null
  five_hour_used: number | null
  catchup_score: number | null
  recent_hour_burn: number | null
  usage_source: string | null
  changed: boolean
}

interface Snapshot {
  generated_at: string
  config: {
    platform: string
    account_name_pattern: string
    db_path: string
    heartbeat_file: string
  }
  heartbeat: {
    exists: boolean
    modified_at: string | null
    path: string
  }
  summary: Summary
  accounts: DashboardAccount[]
  decisions: Decision[]
}

interface InviteCredit {
  id: string
  status?: string
  title?: string
  description?: string
}

interface InviteStatus {
  requires_consent?: boolean
  available_count?: number
  credits?: InviteCredit[]
  eligibility_rules?: string[]
}

const snapshot = ref<Snapshot | null>(null)
const error = ref('')
const loading = ref(false)
const inviteOpen = ref(false)
const inviteAccount = ref<DashboardAccount | null>(null)
const inviteStatus = ref<InviteStatus | null>(null)
const inviteLoading = ref(false)
const inviteMessage = ref('')
const inviteMessageType = ref<'success' | 'error' | ''>('')
const selectedCreditId = ref('')
const emailInput = ref('')
const consentConfirmed = ref(false)

const availableCredits = computed(() => {
  return (inviteStatus.value?.credits ?? []).filter((credit) => {
    const status = credit.status?.toLowerCase()
    return !status || status === 'available'
  })
})

const availableCount = computed(() => inviteStatus.value?.available_count ?? availableCredits.value.length)

function fmtPct(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : `${Number(value).toFixed(1)}%`
}

function fmtNum(value: number | null | undefined, digits = 2) {
  return value === null || value === undefined ? '-' : Number(value).toFixed(digits)
}

function fmtTime(value: string | null | undefined) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function reasonClass(reason: string | null | undefined) {
  if (!reason) return ''
  if (reason.includes('boost') || reason === 'behind') return 'boost'
  if (reason.includes('protect') || reason.includes('cap')) return 'protect'
  if (reason.includes('cooldown') || reason.includes('hot')) return 'hot'
  return ''
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    cache: 'no-store',
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(data?.error || `HTTP ${response.status}`)
  }
  return data as T
}

async function loadSnapshot() {
  loading.value = true
  error.value = ''
  try {
    snapshot.value = await requestJson<Snapshot>('/api/snapshot')
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败'
  } finally {
    loading.value = false
  }
}

function openInvite(account: DashboardAccount) {
  inviteOpen.value = true
  inviteAccount.value = account
  inviteStatus.value = null
  inviteMessage.value = ''
  inviteMessageType.value = ''
  selectedCreditId.value = ''
  emailInput.value = ''
  consentConfirmed.value = false
  loadInviteStatus()
}

function closeInvite() {
  inviteOpen.value = false
  inviteAccount.value = null
}

function setInviteMessage(type: 'success' | 'error', text: string) {
  inviteMessageType.value = type
  inviteMessage.value = text
}

async function loadInviteStatus(clearMessage = true) {
  if (!inviteAccount.value) return
  inviteLoading.value = true
  if (clearMessage) {
    inviteMessage.value = ''
    inviteMessageType.value = ''
  }
  try {
    inviteStatus.value = await requestJson<InviteStatus>(
      `/api/accounts/${inviteAccount.value.account_id}/codex/invite-reset/status`
    )
    const first = availableCredits.value[0]?.id ?? ''
    if (!availableCredits.value.some((credit) => credit.id === selectedCreditId.value)) {
      selectedCreditId.value = first
    }
  } catch (e) {
    setInviteMessage('error', e instanceof Error ? e.message : '加载邀请状态失败')
  } finally {
    inviteLoading.value = false
  }
}

function parseEmails() {
  const emails = emailInput.value
    .split(/[,\s;]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  const unique = [...new Map(emails.map((email) => [email.toLowerCase(), email])).values()]
  if (unique.length === 0) throw new Error('请输入至少一个邮箱')
  if (unique.length > 5) throw new Error('一次最多邀请 5 个邮箱')
  const invalid = unique.find((email) => !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
  if (invalid) throw new Error(`邮箱格式不正确：${invalid}`)
  return unique
}

async function sendInvite() {
  if (!inviteAccount.value || inviteLoading.value) return
  try {
    const emails = parseEmails()
    if ((inviteStatus.value?.requires_consent ?? true) && !consentConfirmed.value) {
      throw new Error('请先确认已获得收件人同意')
    }
    inviteLoading.value = true
    const result = await requestJson<{ failed_emails?: string[]; message?: string }>(
      `/api/accounts/${inviteAccount.value.account_id}/codex/invite-reset/invite`,
      { method: 'POST', body: JSON.stringify({ emails }) }
    )
    const failed = result.failed_emails?.filter(Boolean) ?? []
    if (failed.length) {
      setInviteMessage('error', `以下邮箱邀请失败：${failed.join(', ')}`)
      return
    }
    emailInput.value = ''
    setInviteMessage('success', result.message || '邀请已发送')
  } catch (e) {
    setInviteMessage('error', e instanceof Error ? e.message : '发送邀请失败')
  } finally {
    inviteLoading.value = false
  }
}

async function consumeCredit() {
  if (!inviteAccount.value || !selectedCreditId.value || inviteLoading.value) return
  inviteLoading.value = true
  try {
    const result = await requestJson<{ code?: string }>(
      `/api/accounts/${inviteAccount.value.account_id}/codex/invite-reset/consume`,
      { method: 'POST', body: JSON.stringify({ credit_id: selectedCreditId.value }) }
    )
    const ok = !result.code || result.code === 'reset'
    setInviteMessage(ok ? 'success' : 'error', inviteConsumeMessage(result.code))
    await loadInviteStatus(false)
    await loadSnapshot()
  } catch (e) {
    setInviteMessage('error', e instanceof Error ? e.message : '使用重置次数失败')
  } finally {
    inviteLoading.value = false
  }
}

function inviteConsumeMessage(code?: string) {
  if (code === 'nothing_to_reset') return '当前没有需要重置的用量窗口'
  if (code === 'already_redeemed') return '该重置机会已经被使用'
  if (code === 'no_credit') return '没有可用的重置机会'
  return 'Codex 用量已重置'
}

onMounted(() => {
  loadSnapshot()
  window.setInterval(loadSnapshot, 60000)
})
</script>

<template>
  <header>
    <div class="wrap topbar">
      <div>
        <h1>sub2api 调度看板</h1>
        <div class="hint">
          {{ snapshot?.config.platform || '-' }} /
          {{ snapshot?.config.account_name_pattern || '全部账号' }} /
          {{ snapshot?.config.db_path || '-' }}
        </div>
      </div>
      <button type="button" :disabled="loading" @click="loadSnapshot">刷新</button>
    </div>
  </header>

  <main class="wrap">
    <div v-if="error" class="error">{{ error }}</div>

    <div class="status">
      <div class="metric">
        <div class="label">受控账号</div>
        <div class="value">{{ snapshot?.summary.account_count ?? '-' }}</div>
        <div class="sub">页面刷新 {{ fmtTime(snapshot?.generated_at) }}</div>
      </div>
      <div class="metric">
        <div class="label">最近一轮</div>
        <div class="value">{{ snapshot?.summary.last_run_id || '-' }}</div>
        <div class="sub">{{ fmtTime(snapshot?.summary.last_decided_at) }}</div>
      </div>
      <div class="metric">
        <div class="label">本轮调整</div>
        <div class="value">{{ snapshot?.summary.last_run_changed_count ?? '-' }}</div>
        <div class="sub">
          {{ snapshot?.summary.last_run_decision_count ? `共 ${snapshot.summary.last_run_decision_count} 条决策` : '-' }}
        </div>
      </div>
      <div class="metric">
        <div class="label">心跳</div>
        <div class="value">{{ snapshot?.heartbeat.exists ? '正常' : '缺失' }}</div>
        <div class="sub">{{ snapshot?.heartbeat.modified_at ? fmtTime(snapshot.heartbeat.modified_at) : snapshot?.heartbeat.path }}</div>
      </div>
    </div>

    <section>
      <div class="section-head">
        <h2>账号状态</h2>
        <div class="hint">按最近更新时间排序</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>账号</th>
              <th>Priority</th>
              <th>7d</th>
              <th>7d 刷新</th>
              <th>5h</th>
              <th>EWMA/h</th>
              <th>Cooldown</th>
              <th>采样</th>
              <th>最近原因</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="account in snapshot?.accounts ?? []" :key="account.account_id">
              <td class="name">{{ account.name || '-' }} #{{ account.account_id }}</td>
              <td class="num">{{ account.last_priority ?? '-' }}</td>
              <td class="num">{{ fmtPct(account.last_7d_used) }}</td>
              <td>{{ fmtTime(account.last_7d_reset_at) }}</td>
              <td class="num">{{ fmtPct(account.last_5h_used) }}</td>
              <td class="num">{{ fmtNum(account.hourly_burn_ewma) }}</td>
              <td>{{ fmtTime(account.cooldown_until) }}</td>
              <td>{{ fmtTime(account.last_sampled_at) }}</td>
              <td :class="reasonClass(account.last_reason)">{{ account.last_reason || '-' }}</td>
              <td class="actions">
                <button type="button" class="small" @click="openInvite(account)">邀请管理</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!snapshot?.accounts.length" class="empty">暂无账号状态</div>
    </section>

    <section>
      <div class="section-head">
        <h2>最近决策</h2>
        <div class="hint">最多 80 条</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>账号</th>
              <th>Priority / LF</th>
              <th>原因</th>
              <th>7d</th>
              <th>7d 刷新</th>
              <th>5h</th>
              <th>Catchup</th>
              <th>Burn/h</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="decision in snapshot?.decisions ?? []" :key="`${decision.account_id}-${decision.decided_at}`">
              <td>{{ fmtTime(decision.decided_at) }}</td>
              <td class="name">{{ decision.account_name || '-' }} #{{ decision.account_id }}</td>
              <td :class="['num', { changed: decision.changed }]">
                {{ decision.current_priority ?? '-' }} -> {{ decision.target_priority ?? '-' }}
                / LF {{ decision.current_load_factor ?? '-' }} -> {{ decision.target_load_factor ?? '-' }}
              </td>
              <td><span :class="['reason', reasonClass(decision.reason)]">{{ decision.reason || '-' }}</span></td>
              <td class="num">{{ fmtPct(decision.seven_day_used) }}</td>
              <td>{{ fmtTime(decision.seven_day_reset_at) }}</td>
              <td class="num">{{ fmtPct(decision.five_hour_used) }}</td>
              <td class="num">{{ fmtNum(decision.catchup_score) }}</td>
              <td class="num">{{ fmtNum(decision.recent_hour_burn) }}</td>
              <td>{{ decision.usage_source || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!snapshot?.decisions.length" class="empty">暂无决策记录</div>
    </section>
  </main>

  <div v-if="inviteOpen" class="modal-backdrop" @click.self="closeInvite">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="invite-title">
      <div class="modal-head">
        <div>
          <h2 id="invite-title">Codex 邀请管理</h2>
          <div class="hint">{{ inviteAccount?.name || '-' }} #{{ inviteAccount?.account_id }}</div>
        </div>
        <button type="button" @click="closeInvite">关闭</button>
      </div>
      <div class="modal-body">
        <div v-if="inviteMessage" :class="['notice', inviteMessageType]">{{ inviteMessage }}</div>
        <div class="modal-grid">
          <section class="modal-section">
            <div class="section-head tight">
              <h2>重置次数</h2>
              <button type="button" class="small" :disabled="inviteLoading" @click="loadInviteStatus()">刷新</button>
            </div>
            <div class="stack">
              <div class="mini-metric">
                <div class="label">可用次数</div>
                <div class="value">{{ inviteLoading && !inviteStatus ? '读取中' : availableCount }}</div>
              </div>
              <label>
                <span class="field-label">选择重置机会</span>
                <select v-model="selectedCreditId" :disabled="inviteLoading || !availableCredits.length">
                  <option v-if="!availableCredits.length" value="">暂无可用机会</option>
                  <option v-for="(credit, index) in availableCredits" :key="credit.id" :value="credit.id">
                    {{ credit.title || 'Codex 重置机会' }} #{{ index + 1 }}
                  </option>
                </select>
              </label>
              <button type="button" :disabled="inviteLoading || !selectedCreditId" @click="consumeCredit">
                使用重置次数
              </button>
              <ul class="plain-list">
                <li v-for="credit in availableCredits" :key="credit.id">
                  {{ credit.title || 'Codex 重置机会' }}：{{ credit.description || credit.id }}
                </li>
              </ul>
            </div>
          </section>

          <section class="modal-section">
            <div class="section-head tight">
              <h2>发送邀请</h2>
              <div class="hint">最多 5 个邮箱</div>
            </div>
            <div class="stack">
              <label>
                <span class="field-label">邀请邮箱</span>
                <textarea v-model="emailInput" placeholder="支持逗号、空格或换行分隔"></textarea>
              </label>
              <label class="inline-check">
                <input v-model="consentConfirmed" type="checkbox">
                <span>确认已获得收件人同意，可以发送 Codex 邀请邮件</span>
              </label>
              <button type="button" :disabled="inviteLoading" @click="sendInvite">发送邀请</button>
              <div>
                <div class="field-label">邀请规则</div>
                <ul class="plain-list">
                  <li v-for="rule in inviteStatus?.eligibility_rules ?? []" :key="rule">{{ rule }}</li>
                  <li v-if="!(inviteStatus?.eligibility_rules ?? []).length">暂无可展示的规则</li>
                </ul>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>
