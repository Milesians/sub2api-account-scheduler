<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

interface Summary {
  account_count: number
  last_run_id: string | null
  last_decided_at: string | null
  last_run_decision_count: number
  last_run_changed_count: number
  changed_account_count: number
}

interface DashboardAccount {
  account_id: number
  name: string
  last_priority: number | null
  last_current_priority: number | null
  last_target_priority: number | null
  last_current_load_factor: number | null
  last_target_load_factor: number | null
  last_7d_used: number | null
  expected_7d_used: number | null
  expected_7d_gap: number | null
  projected_7d_end: number | null
  required_rate: number | null
  recent_rate: number | null
  remaining_hours: number | null
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
  target_now: number | null
  projected_end: number | null
  required_rate: number | null
  recent_rate: number | null
  remaining_hours: number | null
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
  expires_at?: string
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

const accounts = computed(() => snapshot.value?.accounts ?? [])

const behindAccountCount = computed(() => {
  return accounts.value.filter((account) => (account.expected_7d_gap ?? 0) > 0.5).length
})

const averageExpectedGap = computed(() => {
  const gaps = accounts.value
    .map((account) => account.expected_7d_gap)
    .filter((value): value is number => value !== null && value !== undefined)
  if (!gaps.length) return null
  return gaps.reduce((sum, value) => sum + value, 0) / gaps.length
})

const loadFactorChangedCount = computed(() => {
  return accounts.value.filter((account) => {
    return account.last_current_load_factor !== account.last_target_load_factor
  }).length
})

function fmtPct(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : `${Number(value).toFixed(1)}%`
}

function fmtNum(value: number | null | undefined, digits = 2) {
  return value === null || value === undefined ? '-' : Number(value).toFixed(digits)
}

function fmtRate(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : `${Number(value).toFixed(2)}%/h`
}

function fmtHours(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : `${Number(value).toFixed(1)}h`
}

function fmtGap(value: number | null | undefined) {
  if (value === null || value === undefined) return '-'
  const abs = Math.abs(Number(value))
  if (abs < 0.05) return '持平'
  return value > 0 ? `差 ${abs.toFixed(1)}%` : `超 ${abs.toFixed(1)}%`
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

function progressWidth(value: number | null | undefined) {
  const numeric = value === null || value === undefined ? 0 : Number(value)
  return `${Math.max(0, Math.min(100, numeric)).toFixed(2)}%`
}

function gapClass(value: number | null | undefined) {
  if (value === null || value === undefined) return ''
  if (value > 0.5) return 'behind'
  if (value < -0.5) return 'ahead'
  return 'on-track'
}

function lfClass(account: DashboardAccount) {
  const current = account.last_current_load_factor
  const target = account.last_target_load_factor
  if (current === null || current === undefined || target === null || target === undefined) return ''
  if (target > current) return 'boost'
  if (target < current) return 'protect'
  return ''
}

function priorityText(account: DashboardAccount) {
  const current = account.last_current_priority ?? account.last_priority
  const target = account.last_target_priority ?? account.last_priority
  return `${current ?? '-'} -> ${target ?? '-'}`
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
  <header class="site-head">
    <div class="wrap topbar">
      <div class="title-block">
        <div class="eyebrow">sub2api account scheduler</div>
        <h1>调度看板</h1>
        <div class="hint config-line">
          <span>{{ snapshot?.config.platform || '-' }}</span>
          <span>{{ snapshot?.config.account_name_pattern || '全部账号' }}</span>
          <span>{{ snapshot?.config.db_path || '-' }}</span>
          <span>最近一轮 {{ snapshot?.summary.last_run_id || '-' }}</span>
        </div>
      </div>
      <button type="button" class="primary-action" :disabled="loading" @click="loadSnapshot">
        {{ loading ? '刷新中' : '刷新' }}
      </button>
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
        <div class="label">目标差距</div>
        <div class="value">{{ behindAccountCount }}</div>
        <div class="sub">落后账号 / 平均 {{ fmtGap(averageExpectedGap) }}</div>
      </div>
      <div class="metric">
        <div class="label">LF 调整</div>
        <div class="value">{{ loadFactorChangedCount }}</div>
        <div class="sub">
          本轮 {{ snapshot?.summary.last_run_changed_count ?? '-' }} /
          最新变更 {{ snapshot?.summary.changed_account_count ?? '-' }}
        </div>
      </div>
      <div class="metric">
        <div class="label">心跳</div>
        <div class="value">{{ snapshot?.heartbeat.exists ? '正常' : '缺失' }}</div>
        <div class="sub">{{ snapshot?.heartbeat.modified_at ? fmtTime(snapshot.heartbeat.modified_at) : snapshot?.heartbeat.path }}</div>
      </div>
    </div>

    <section class="panel">
      <div class="section-head">
        <div>
          <h2>账号状态</h2>
          <div class="hint">实际 7d、期望用量与 LF 调整放在同一行对比</div>
        </div>
        <div class="hint">按最近更新时间排序</div>
      </div>
      <div class="table-wrap account-table">
        <table>
          <thead>
            <tr>
              <th>账号</th>
              <th>7d / 期望</th>
              <th>差距</th>
              <th>LF</th>
              <th>Priority</th>
              <th>5h</th>
              <th>预计 7d</th>
              <th>重置 / 冷却</th>
              <th>采样</th>
              <th>最近原因</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="account in accounts" :key="account.account_id">
              <td class="name-cell">
                <div class="name">{{ account.name || '-' }}</div>
                <div class="cell-sub">#{{ account.account_id }}</div>
              </td>
              <td class="usage-cell">
                <div class="usage-top">
                  <strong>{{ fmtPct(account.last_7d_used) }}</strong>
                  <span>期望 {{ fmtPct(account.expected_7d_used) }}</span>
                </div>
                <div class="usage-meter">
                  <span class="usage-fill" :style="{ width: progressWidth(account.last_7d_used) }"></span>
                  <i
                    v-if="account.expected_7d_used !== null && account.expected_7d_used !== undefined"
                    class="usage-marker"
                    :style="{ left: progressWidth(account.expected_7d_used) }"
                  ></i>
                </div>
              </td>
              <td :class="['gap-column', gapClass(account.expected_7d_gap)]">
                <div class="gap-cell">
                  <strong>{{ fmtGap(account.expected_7d_gap) }}</strong>
                  <span>{{ fmtHours(account.remaining_hours) }} 剩余</span>
                </div>
              </td>
              <td :class="['num', 'lf-column', lfClass(account)]">
                <div class="lf-cell">
                  <strong>{{ account.last_current_load_factor ?? '-' }} -> {{ account.last_target_load_factor ?? '-' }}</strong>
                  <span>需 {{ fmtRate(account.required_rate) }} / 近 {{ fmtRate(account.recent_rate) }}</span>
                </div>
              </td>
              <td class="num">{{ priorityText(account) }}</td>
              <td class="num">{{ fmtPct(account.last_5h_used) }}</td>
              <td class="num">{{ fmtPct(account.projected_7d_end) }}</td>
              <td>
                <div>{{ fmtTime(account.last_7d_reset_at) }}</div>
                <div class="cell-sub">冷却 {{ fmtTime(account.cooldown_until) }}</div>
              </td>
              <td>{{ fmtTime(account.last_sampled_at) }}</td>
              <td><span :class="['reason', reasonClass(account.last_reason)]">{{ account.last_reason || '-' }}</span></td>
              <td class="actions">
                <button type="button" class="small" @click="openInvite(account)">邀请管理</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="snapshot && !accounts.length" class="empty">暂无账号状态</div>
    </section>

    <section class="panel">
      <div class="section-head">
        <div>
          <h2>最近决策</h2>
          <div class="hint">最近 80 条调度记录</div>
        </div>
        <div class="hint">{{ fmtTime(snapshot?.summary.last_decided_at) }}</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>账号</th>
              <th>Priority / LF</th>
              <th>7d / 期望</th>
              <th>5h</th>
              <th>速率</th>
              <th>预计 7d</th>
              <th>Catchup</th>
              <th>原因</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="decision in snapshot?.decisions ?? []" :key="`${decision.account_id}-${decision.decided_at}`">
              <td>{{ fmtTime(decision.decided_at) }}</td>
              <td class="name-cell">
                <div class="name">{{ decision.account_name || '-' }}</div>
                <div class="cell-sub">#{{ decision.account_id }}</div>
              </td>
              <td :class="['num', { changed: decision.changed }]">
                {{ decision.current_priority ?? '-' }} -> {{ decision.target_priority ?? '-' }}
                / LF {{ decision.current_load_factor ?? '-' }} -> {{ decision.target_load_factor ?? '-' }}
              </td>
              <td class="num">
                <div>{{ fmtPct(decision.seven_day_used) }} / {{ fmtPct(decision.target_now) }}</div>
                <div class="cell-sub">剩余 {{ fmtHours(decision.remaining_hours) }}</div>
              </td>
              <td class="num">{{ fmtPct(decision.five_hour_used) }}</td>
              <td class="num">
                <div>需 {{ fmtRate(decision.required_rate) }}</div>
                <div class="cell-sub">近 {{ fmtRate(decision.recent_rate) }}</div>
              </td>
              <td class="num">{{ fmtPct(decision.projected_end) }}</td>
              <td class="num">{{ fmtNum(decision.catchup_score) }}</td>
              <td><span :class="['reason', reasonClass(decision.reason)]">{{ decision.reason || '-' }}</span></td>
              <td>{{ decision.usage_source || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="snapshot && !snapshot.decisions.length" class="empty">暂无决策记录</div>
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
                    {{ credit.title || 'Codex 重置机会' }} #{{ index + 1 }} / 过期 {{ fmtTime(credit.expires_at) }}
                  </option>
                </select>
              </label>
              <button type="button" :disabled="inviteLoading || !selectedCreditId" @click="consumeCredit">
                使用重置次数
              </button>
              <ul class="plain-list">
                <li v-for="credit in availableCredits" :key="credit.id">
                  {{ credit.title || 'Codex 重置机会' }}：{{ credit.description || credit.id }} / 过期 {{ fmtTime(credit.expires_at) }}
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
