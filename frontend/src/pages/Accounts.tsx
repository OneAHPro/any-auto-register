import { useEffect, useRef, useState, useCallback } from 'react'
import { useParams } from 'react-router-dom'
import {
  Table,
  Button,
  Input,
  InputNumber,
  Select,
  Tag,
  Space,
  Modal,
  Form,
  message,
  Popconfirm,
  Dropdown,
  Typography,
  Alert,
  Descriptions,
  DatePicker,
  Timeline,
  theme,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  ReloadOutlined,
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  DownloadOutlined,
  UploadOutlined,
  MoreOutlined,
  DeleteOutlined,
  SyncOutlined,
  LoginOutlined,
  RedoOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { ChatGPTExistingAccountLoginModal } from '@/components/ChatGPTExistingAccountLoginModal'
import {
  ChatGPTPhoneVerificationModal,
  type ChatGPTPhoneVerificationAccount,
} from '@/components/ChatGPTPhoneVerificationModal'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { canStartChatGPTPhoneVerification } from '@/lib/chatgptStagedLogin'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { apiFetch } from '@/lib/utils'
import { normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'
import {
  formatAutoReloginCountdown,
  type ChatGPTAutoReloginStatus,
} from '@/lib/chatgptAutoReloginStatus'

const { Text } = Typography
const CHATGPT_RELOGIN_MAX_CONCURRENCY = 10
const CHATGPT_MFA_RESET_CONCURRENCY = 5

const STATUS_COLORS: Record<string, string> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'error',
}

function parseExtraJson(raw: string | undefined) {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function normalizeAccount(account: any) {
  const extra = parseExtraJson(account.extra_json)
  const syncStatuses = extra.sync_statuses && typeof extra.sync_statuses === 'object' ? extra.sync_statuses : {}
  const cpaSync = syncStatuses.cpa && typeof syncStatuses.cpa === 'object' ? syncStatuses.cpa : {}
  const sub2apiSync = syncStatuses.sub2api && typeof syncStatuses.sub2api === 'object' ? syncStatuses.sub2api : {}
  const codex2apiSync = syncStatuses.codex2api && typeof syncStatuses.codex2api === 'object' ? syncStatuses.codex2api : {}
  const cliproxySync = syncStatuses.cliproxyapi && typeof syncStatuses.cliproxyapi === 'object' ? syncStatuses.cliproxyapi : {}
  const chatgptLocal = extra.chatgpt_local && typeof extra.chatgpt_local === 'object' ? extra.chatgpt_local : {}
  return { ...account, extra, cpaSync, sub2apiSync, codex2apiSync, cliproxySync, chatgptLocal }
}

interface Codex2APIDeleteResult {
  enabled?: boolean
  status?: string
  remote_id?: number | null
}

interface AccountDeleteResponse {
  ok?: boolean
  account_id?: number
  local_deleted?: boolean
  detail?: string
  message?: string
  codex2api?: Codex2APIDeleteResult
}

interface BatchDeleteItem {
  account_id: number
  ok?: boolean
  status?: string
  error_code?: string
  message?: string
  codex2api?: Codex2APIDeleteResult
}

interface BatchDeleteResponse {
  total_requested?: number
  total_unique?: number
  deleted?: number | string | null
  failed?: number | string | null
  not_found?: Array<number | string>
  remote_deleted?: number
  remote_already_absent?: number
  remote_skipped?: number
  items?: BatchDeleteItem[]
}

function normalizeBatchDeleteCount(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  const text = String(value).trim()
  if (!text) return null
  const parsed = Number(text)
  if (!Number.isSafeInteger(parsed) || parsed < 0) return null
  return parsed
}

function deletionErrorDetail(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim()
  if (error && typeof error === 'object') {
    const detail = String((error as Record<string, unknown>).detail ?? '').trim()
    if (detail) return detail
    const errorMessage = String((error as Record<string, unknown>).message ?? '').trim()
    if (errorMessage) return errorMessage
  }
  const text = String(error ?? '').trim()
  return text && text !== '[object Object]' ? text : fallback
}

function singleDeleteSuccessMessage(result: AccountDeleteResponse): string {
  const remoteStatus = String(result?.codex2api?.status ?? '').trim().toLowerCase()
  switch (remoteStatus) {
    case 'deleted':
      return '本地账号与 Codex2API 认证已删除'
    case 'already_absent':
      return '本地账号已删除，Codex2API 认证已不存在'
    case 'skipped_disabled':
      return '本地账号已删除，Codex2API 删除联动未启用'
    case 'not_applicable':
      return '账号已删除，无需同步 Codex2API'
    case 'skipped':
      return '本地账号已删除，Codex2API 删除已跳过'
    default:
      return String(result?.message ?? '').trim() || '删除成功'
  }
}

function batchFailureDetails(items: BatchDeleteItem[]): string {
  const seen = new Set<string>()
  const details: string[] = []
  for (const item of items) {
    if (item?.ok === true || String(item?.status ?? '') === 'not_found') continue
    const detail = String(item?.message ?? item?.error_code ?? '').trim()
    if (!detail || seen.has(detail)) continue
    seen.add(detail)
    details.push(detail)
    if (details.length === 2) break
  }
  return details.join('；')
}

function formatSyncTime(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatCreatedAt(value?: string) {
  if (!value) return { date: '-', time: '' }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return { date: value, time: '' }
  }
  return {
    date: date.toLocaleDateString(),
    time: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }
}

function formatUsd(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `$${parsed.toFixed(2)}` : '-'
}

function assignmentStateMeta(value?: string) {
  switch (value) {
    case 'active':
      return { color: 'success', label: '生效中' }
    case 'draining':
      return { color: 'processing', label: '排空中' }
    case 'standby':
      return { color: 'default', label: '备用' }
    default:
      return { color: 'default', label: '未分配' }
  }
}

function migrationStateMeta(value?: string) {
  switch (value) {
    case 'committed':
      return { color: 'green', label: '迁移完成' }
    case 'rolled_back':
      return { color: 'gray', label: '已回滚' }
    case 'rollback_required':
      return { color: 'red', label: '需人工回滚' }
    case 'cleanup_pending':
      return { color: 'orange', label: '待清理源副本' }
    default:
      return { color: 'blue', label: '迁移中' }
  }
}

interface ControlTargetOption {
  id: number
  name: string
  health_status: string
  enabled: boolean
}

interface ControlPoolOption {
  id: string
  name: string
  target_id?: number | null
}

interface MigrationRecord {
  id: string
  state: string
  step: string
  source_target_id: number
  destination_target_id: number
  error?: Record<string, unknown>
  created_at: string
  updated_at: string
}

interface AccountQuotaView {
  continuous_remaining_usd?: unknown
  remaining_usd?: unknown
  reset_at?: string | null
  captured_at?: string | null
  fresh?: boolean
}

interface AccountControlRow {
  assignment?: {
    target_id?: number
    target_name?: string
    pool_id?: string
    pool_name?: string
    state?: string
  } | null
  quota?: Record<string, AccountQuotaView>
}

function authStateMeta(state?: string) {
  switch (state) {
    case 'access_token_valid':
      return { color: 'success', label: 'AT有效' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'missing_access_token':
      return { color: 'default', label: '缺少AT' }
    case 'banned_like':
      return { color: 'error', label: '疑似封禁' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

function codexStateMeta(state?: string) {
  switch (state) {
    case 'usable':
      return { color: 'success', label: '可用' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'payment_required':
      return { color: 'warning', label: '需付费/权限' }
    case 'quota_exhausted':
      return { color: 'warning', label: '额度耗尽' }
    case 'skipped_auth_invalid':
      return { color: 'default', label: '未测' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

function planMeta(plan?: string) {
  switch ((plan || '').toLowerCase()) {
    case 'plus':
      return { color: 'success', label: 'Plus' }
    case 'team':
      return { color: 'processing', label: 'Team' }
    case 'enterprise':
      return { color: 'processing', label: 'Enterprise' }
    case 'pro':
      return { color: 'processing', label: 'Pro' }
    case 'free':
      return { color: 'default', label: 'Free' }
    default:
      return { color: 'default', label: '未知' }
  }
}

function formatStructuredText(value?: string) {
  if (!value) return ''
  const trimmed = String(value).trim()
  if (!trimmed) return ''
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2)
    } catch {
      return trimmed
    }
  }
  return trimmed
}

function SummaryField({
  label,
  value,
  code = false,
}: {
  label: string
  value?: string
  code?: boolean
}) {
  const { token } = theme.useToken()
  if (!value) return null

  const content = code ? formatStructuredText(value) : value
  const isBlock = code || content.length > 96 || content.includes('\n')

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '104px minmax(0, 1fr)',
        gap: 12,
        alignItems: 'start',
      }}
    >
      <Text type="secondary" style={{ fontSize: 12, lineHeight: '20px' }}>
        {label}
      </Text>
      {isBlock ? (
        <pre
          style={{
            margin: 0,
            padding: code ? '8px 10px' : 0,
            borderRadius: code ? token.borderRadius : 0,
            border: code ? `1px solid ${token.colorBorder}` : 'none',
            background: code ? token.colorBgElevated : 'transparent',
            color: code ? token.colorText : token.colorTextSecondary,
            fontFamily: code ? 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace' : 'inherit',
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowWrap: 'anywhere',
            maxHeight: code ? 160 : 'none',
            overflow: code ? 'auto' : 'visible',
          }}
        >
          {content}
        </pre>
      ) : (
        <Text style={{ display: 'block', color: token.colorTextSecondary, lineHeight: '20px' }}>
          {content}
        </Text>
      )}
    </div>
  )
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  const { token } = theme.useToken()

  return (
    <div
      style={{
        marginTop: 16,
        padding: 14,
        borderRadius: token.borderRadiusLG,
        border: `1px solid ${token.colorBorder}`,
        background: token.colorFillAlter,
      }}
    >
      <div style={{ marginBottom: 10, fontWeight: 600, color: token.colorText }}>{title}</div>
      {children}
    </div>
  )
}

function LocalProbeSummary({ probe }: { probe: any }) {
  const checkedAt = probe?.checked_at || probe?.auth?.checked_at || probe?.subscription?.checked_at || probe?.codex?.checked_at
  const auth = probe?.auth || {}
  const subscription = probe?.subscription || {}
  const codex = probe?.codex || {}

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={authStateMeta(auth.state).color}>认证: {authStateMeta(auth.state).label}</Tag>
        <Tag color={planMeta(subscription.plan).color}>订阅: {planMeta(subscription.plan).label}</Tag>
        <Tag color={codexStateMeta(codex.state).color}>Codex: {codexStateMeta(codex.state).label}</Tag>
      </div>
      <SummaryField label="探测时间" value={checkedAt ? formatSyncTime(checkedAt) : ''} />
      <SummaryField label="认证信息" value={auth.message} code />
      <SummaryField label="工作区套餐" value={subscription.workspace_plan_type} />
      <SummaryField label="Codex 信息" value={codex.message} code />
    </div>
  )
}

function cliproxyStateMeta(sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return { color: 'default', label: '未同步' }
  }
  if (sync.remote_state === 'unreachable') {
    return { color: 'error', label: '不可连接' }
  }
  if (sync.remote_state === 'not_found') {
    return { color: 'default', label: '远端未发现' }
  }
  if (!sync.uploaded) {
    return { color: 'default', label: '未发现' }
  }
  if (sync.remote_state === 'usable') {
    return { color: 'success', label: '远端可用' }
  }
  if (sync.remote_state === 'account_deactivated') {
    return { color: 'error', label: '远端已失效' }
  }
  if (sync.remote_state === 'access_token_invalidated') {
    return { color: 'error', label: '远端AT失效' }
  }
  if (sync.remote_state === 'unauthorized') {
    return { color: 'error', label: '远端未授权' }
  }
  if (sync.remote_state === 'payment_required') {
    return { color: 'warning', label: '远端需付费/权限' }
  }
  if (sync.remote_state === 'quota_exhausted') {
    return { color: 'warning', label: '远端额度耗尽' }
  }
  if (sync.status === 'active') {
    return { color: 'processing', label: '远端Active' }
  }
  if (sync.status === 'refreshing') {
    return { color: 'processing', label: '远端刷新中' }
  }
  if (sync.status === 'pending') {
    return { color: 'default', label: '远端待处理' }
  }
  if (sync.status === 'error') {
    return { color: 'error', label: '远端错误' }
  }
  if (sync.status === 'disabled') {
    return { color: 'default', label: '远端禁用' }
  }
  return { color: 'default', label: '未同步' }
}

function uploadSyncMeta(sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return { color: 'default', label: '未上传' }
  }
  if (sync.uploaded || sync.uploaded_at) {
    return { color: 'success', label: '已上传' }
  }
  if (sync.last_attempt_ok === false) {
    return { color: 'error', label: '失败' }
  }
  if (sync.last_attempt_ok === true || sync.last_attempt_at) {
    return { color: 'processing', label: '已尝试' }
  }
  return { color: 'default', label: '未上传' }
}

function uploadSyncTitle(name: string, sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return `${name} 未上传`
  }

  const parts: string[] = []
  if (sync.uploaded_at) {
    parts.push(`成功时间: ${formatSyncTime(sync.uploaded_at)}`)
  }
  if (sync.last_attempt_at) {
    parts.push(`最近尝试: ${formatSyncTime(sync.last_attempt_at)}`)
  }
  if (sync.last_message) {
    parts.push(`结果: ${sync.last_message}`)
  }
  return parts.join('\n') || `${name} 已记录状态`
}

function CliproxySyncSummary({ sync }: { sync: any }) {
  const meta = cliproxyStateMeta(sync)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={meta.color}>{meta.label}</Tag>
        {sync?.status ? <Tag>{`status: ${sync.status}`}</Tag> : null}
      </div>
      <SummaryField label="状态信息" value={sync?.status_message} code />
      <SummaryField label="auth-file" value={sync?.name} />
      <SummaryField label="API URL" value={sync?.base_url} />
      <SummaryField label="同步时间" value={sync?.last_synced_at ? formatSyncTime(sync.last_synced_at) : ''} />
      <SummaryField label="远端刷新时间" value={sync?.last_refresh ? formatSyncTime(sync.last_refresh) : ''} />
      <SummaryField label="下次重试时间" value={sync?.next_retry_after ? formatSyncTime(sync.next_retry_after) : ''} />
      <SummaryField label="探测信息" value={sync?.last_probe_message} code />
    </div>
  )
}

function ActionMenu({ acc, onRefresh, actions }: { acc: any; onRefresh: () => void; actions: any[] }) {
  const [resultOpen, setResultOpen] = useState(false)
  const [resultTitle, setResultTitle] = useState('')
  const [resultStatus, setResultStatus] = useState<'success' | 'error'>('success')
  const [resultText, setResultText] = useState('')
  const [resultUrl, setResultUrl] = useState('')
  const [resultProbe, setResultProbe] = useState<any>(null)
  const [resultCliproxySync, setResultCliproxySync] = useState<any>(null)
  const [runningActionId, setRunningActionId] = useState<string | null>(null)

  const showResult = (title: string, status: 'success' | 'error', text: string, url = '', probe: any = null, cliproxySync: any = null) => {
    setResultTitle(title)
    setResultStatus(status)
    setResultText(text)
    setResultUrl(url)
    setResultProbe(probe)
    setResultCliproxySync(cliproxySync)
    setResultOpen(true)
  }

  const copyResultUrl = async () => {
    if (!resultUrl) return
    try {
      await navigator.clipboard.writeText(resultUrl)
      message.success('链接已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const handleAction = async (actionId: string) => {
    if (runningActionId) return
    const actionLabel = actions.find((item) => item.id === actionId)?.label || actionId
    const toastKey = `account-action:${acc?.id}:${actionId}`
    setRunningActionId(actionId)
    message.loading({ content: `${actionLabel}运行中...`, key: toastKey, duration: 0 })

    try {
      const r = await apiFetch(`/actions/${acc.platform}/${acc.id}/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({ params: {} }),
      })
      if (!r.ok) {
        const data = r.data || {}
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
        message.error({ content: `${actionLabel}失败`, key: toastKey })
        showResult(actionLabel, 'error', r.error || data.message || '操作失败', '', probe, cliproxySync)
        onRefresh()
        return
      }
      const data = r.data || {}
      if (data.url || data.checkout_url || data.cashier_url) {
        const targetUrl = data.url || data.checkout_url || data.cashier_url
        message.success({ content: `${actionLabel}完成`, key: toastKey })
        showResult(actionLabel, 'success', '操作成功，请在弹窗中打开或复制链接。', targetUrl)
      } else {
        message.success({ content: data.message || `${actionLabel}完成`, key: toastKey })
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
        const text =
          probe
            ? String(data.message || '操作成功')
            : cliproxySync
            ? String(data.message || '操作成功')
            : typeof data === 'string'
            ? data
            : Object.keys(data).length > 0
              ? JSON.stringify(data, null, 2)
              : '操作成功'
        showResult(actionLabel, 'success', text, '', probe, cliproxySync)
      }
      onRefresh()
    } catch (e: any) {
      const detail = e?.message ? String(e.message) : '请求失败'
      message.error({ content: detail, key: toastKey })
      showResult(actionLabel, 'error', detail)
    } finally {
      setRunningActionId(null)
    }
  }

  const menuItems: MenuProps['items'] = actions.map((a) => ({
    key: a.id,
    label: runningActionId === a.id ? `${a.label}（运行中）` : a.label,
    disabled: Boolean(runningActionId),
  }))

  if (actions.length === 0) return null

  return (
    <>
      <Dropdown
        menu={{
          items: menuItems,
          onClick: ({ key }) => handleAction(String(key)),
        }}
      >
        <Button
          type="link"
          size="small"
          icon={<MoreOutlined />}
          loading={Boolean(runningActionId)}
        />
      </Dropdown>
      <Modal
        title={resultTitle}
        open={resultOpen}
        onCancel={() => setResultOpen(false)}
        footer={[
          resultUrl ? (
            <Button key="copy" onClick={copyResultUrl}>
              复制链接
            </Button>
          ) : null,
          resultUrl ? (
            <Button
              key="open"
              type="primary"
              onClick={() => window.open(resultUrl, '_blank', 'noopener,noreferrer')}
            >
              打开链接
            </Button>
          ) : null,
          <Button key="ok" type={resultUrl ? 'default' : 'primary'} onClick={() => setResultOpen(false)}>
            确定
          </Button>,
        ].filter(Boolean)}
        maskClosable={false}
      >
        <Alert
          type={resultStatus}
          showIcon
          message={resultStatus === 'success' ? '操作完成' : '操作失败'}
          style={{ marginBottom: 12 }}
        />
        {resultProbe ? (
          <div style={{ marginBottom: 12 }}>
            <LocalProbeSummary probe={resultProbe} />
          </div>
        ) : null}
        {resultCliproxySync ? (
          <div style={{ marginBottom: 12 }}>
            <CliproxySyncSummary sync={resultCliproxySync} />
          </div>
        ) : null}
        {resultUrl ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Text copyable={{ text: resultUrl }} style={{ wordBreak: 'break-all' }}>
              {resultUrl}
            </Text>
          </Space>
        ) : null}
        {resultText ? (
          <pre
            style={{
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {resultText}
          </pre>
        ) : null}
      </Modal>
    </>
  )
}

export default function Accounts() {
  const { platform } = useParams<{ platform: string }>()
  const { token } = theme.useToken()
  const [currentPlatform, setCurrentPlatform] = useState(platform || 'chatgpt')
  const [accounts, setAccounts] = useState<any[]>([])
  const [platformActions, setPlatformActions] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [createdAtStart, setCreatedAtStart] = useState('')
  const [createdAtEnd, setCreatedAtEnd] = useState('')
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [currentAccount, setCurrentAccount] = useState<any>(null)
  const [controlQuota, setControlQuota] = useState<Record<string, AccountQuotaView>>({})
  const [quotaHistory, setQuotaHistory] = useState<AccountQuotaView[]>([])
  const [migrations, setMigrations] = useState<MigrationRecord[]>([])
  const [controlDetailLoading, setControlDetailLoading] = useState(false)
  const [assignmentModalOpen, setAssignmentModalOpen] = useState(false)
  const [assignmentLoading, setAssignmentLoading] = useState(false)
  const [controlTargets, setControlTargets] = useState<ControlTargetOption[]>([])
  const [controlPools, setControlPools] = useState<ControlPoolOption[]>([])
  const [existingAccountLoginModalOpen, setExistingAccountLoginModalOpen] = useState(false)
  const [phoneVerificationAccount, setPhoneVerificationAccount] =
    useState<ChatGPTPhoneVerificationAccount | null>(null)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const [assignmentForm] = Form.useForm()
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [reloginTaskId, setReloginTaskId] = useState<string | null>(null)
  const [reloginTaskMode, setReloginTaskMode] = useState<'relogin' | 'mfa'>('relogin')
  const [reloginLoading, setReloginLoading] = useState(false)
  const [mfaResetLoading, setMfaResetLoading] = useState(false)
  const [reloginStartError, setReloginStartError] = useState('')
  const [reloginConcurrency, setReloginConcurrency] = useState(1)
  const reloginRequestEpochRef = useRef(0)
  const [cpaSyncLoading, setCpaSyncLoading] = useState<'pending' | 'selected' | ''>('')
  const [cpaUploadLoading, setCpaUploadLoading] = useState<'all' | 'selected' | ''>('')
  const [codex2apiUploadLoading, setCodex2APIUploadLoading] = useState<'all' | 'selected' | ''>('')
  const [statusSyncLoading, setStatusSyncLoading] = useState<'probe_selected' | 'probe_all' | 'remote_selected' | 'remote_all' | ''>('')
  const [autoReloginStatus, setAutoReloginStatus] =
    useState<ChatGPTAutoReloginStatus | null>(null)
  const [autoReloginNow, setAutoReloginNow] = useState(() => Date.now())
  const [autoReloginRunNowLoading, setAutoReloginRunNowLoading] = useState(false)
  const autoReloginRunNowEpochRef = useRef(0)

  useEffect(() => {
    reloginRequestEpochRef.current += 1
    autoReloginRunNowEpochRef.current += 1
    setCurrentPlatform(platform || 'chatgpt')
    setSelectedRowKeys([])
    setReloginStartError('')
    setReloginTaskId(null)
    setReloginTaskMode('relogin')
    setReloginLoading(false)
    setMfaResetLoading(false)
    setReloginConcurrency(1)
    setAutoReloginStatus(null)
    setAutoReloginRunNowLoading(false)
  }, [platform])

  useEffect(() => {
    if (!detailModalOpen || !currentAccount) return
    detailForm.setFieldsValue({
      status: currentAccount.status,
      token: currentAccount.token,
    })
  }, [detailModalOpen, currentAccount, detailForm])

  useEffect(() => {
    let cancelled = false
    const timer = window.setTimeout(() => {
      if (!detailModalOpen || !currentAccount || currentPlatform !== 'chatgpt') {
        setControlQuota({})
        setQuotaHistory([])
        setMigrations([])
        return
      }
      setControlDetailLoading(true)
      Promise.all([
        apiFetch(`/accounts/${currentAccount.id}/quota`),
        apiFetch(`/accounts/${currentAccount.id}/quota/history?window=7d&limit=50`),
        apiFetch(`/accounts/${currentAccount.id}/migrations`),
      ])
        .then(([quotaData, historyData, migrationData]) => {
          if (cancelled) return
          setControlQuota(quotaData?.windows || {})
          setQuotaHistory(Array.isArray(historyData?.items) ? historyData.items : [])
          setMigrations(Array.isArray(migrationData?.migrations) ? migrationData.migrations : [])
        })
        .catch(() => {
          if (cancelled) return
          setControlQuota(currentAccount.quota || {})
          setQuotaHistory([])
          setMigrations([])
        })
        .finally(() => {
          if (!cancelled) setControlDetailLoading(false)
        })
    }, 0)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [detailModalOpen, currentAccount, currentPlatform])

  const load = useCallback(async () => {
    if (createdAtStart && createdAtEnd && new Date(createdAtStart).getTime() > new Date(createdAtEnd).getTime()) {
      message.warning('开始时间不能晚于结束时间')
      setAccounts([])
      setTotal(0)
      return
    }

    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: currentPlatform, page: String(page), page_size: String(pageSize) })
      if (search) params.set('email', search)
      if (filterStatus) params.set('status', filterStatus)
      if (createdAtStart) params.set('created_at_start', createdAtStart)
      if (createdAtEnd) params.set('created_at_end', createdAtEnd)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts((data.items || []).map(normalizeAccount))
      setTotal(data.total)
    } finally {
      setLoading(false)
    }
  }, [currentPlatform, search, filterStatus, createdAtStart, createdAtEnd, page, pageSize])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    apiFetch(`/actions/${currentPlatform}`)
      .then((data) => setPlatformActions(data.actions || []))
      .catch(() => setPlatformActions([]))
  }, [currentPlatform])

  const handleAutoReloginRunNow = async () => {
    if (currentPlatform !== 'chatgpt' || autoReloginRunNowLoading) return
    const requestEpoch = ++autoReloginRunNowEpochRef.current
    setAutoReloginRunNowLoading(true)
    try {
      const result = await apiFetch('/automations/chatgpt-relogin/run-now', {
        method: 'POST',
      })
      if (requestEpoch !== autoReloginRunNowEpochRef.current) return
      if (result?.status) {
        setAutoReloginStatus(result.status as ChatGPTAutoReloginStatus)
      }
      setAutoReloginNow(Date.now())
      message.success('自动化流程已立即启动')
    } catch (error: unknown) {
      if (requestEpoch !== autoReloginRunNowEpochRef.current) return
      const detail = error instanceof Error ? error.message : String(error || '请求失败')
      message.error(`立即执行失败：${detail}`)
    } finally {
      if (requestEpoch === autoReloginRunNowEpochRef.current) {
        setAutoReloginRunNowLoading(false)
      }
    }
  }

  useEffect(() => {
    if (currentPlatform !== 'chatgpt') {
      return
    }

    let cancelled = false
    const loadAutoReloginStatus = async () => {
      try {
        const status = await apiFetch('/automations/chatgpt-relogin')
        if (!cancelled) setAutoReloginStatus(status as ChatGPTAutoReloginStatus)
      } catch {
        if (!cancelled) setAutoReloginStatus(null)
      }
    }

    loadAutoReloginStatus()
    const statusPoll = window.setInterval(loadAutoReloginStatus, 5000)
    const countdownTick = window.setInterval(() => setAutoReloginNow(Date.now()), 1000)
    return () => {
      cancelled = true
      window.clearInterval(statusPoll)
      window.clearInterval(countdownTick)
    }
  }, [currentPlatform])

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text)
    message.success('已复制')
  }

  const getRefreshToken = (record: any): string => {
    try {
      const extra = JSON.parse(record.extra_json || '{}')
      return extra.refresh_token || extra.refreshToken || ''
    } catch {
      return ''
    }
  }

  const exportCsv = () => {
    const quoteCsv = (value: any) => {
      const text = value == null ? '' : String(value)
      return `"${text.replace(/"/g, '""')}"`
    }

    const downloadCsv = (content: string) => {
      const blob = new Blob([`\uFEFF${content}`], { type: 'text/csv;charset=utf-8;' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${currentPlatform}_accounts.csv`
      a.click()
      URL.revokeObjectURL(url)
    }

    if (currentPlatform === 'kiro') {
      const header = ['邮箱', '昵称', '登录方式', 'RefreshToken', 'ClientId', 'ClientSecret', 'Region']
      const rows = accounts.map((a) => {
        const nickname = a.extra?.name || String(a.email || '').split('@')[0] || ''
        const provider = a.extra?.provider || 'BuilderId'
        const refreshToken = a.extra?.refreshToken || ''
        const clientId = a.extra?.clientId || ''
        const clientSecret = a.extra?.clientSecret || ''
        const region = a.extra?.region || 'us-east-1'

        return [
          a.email || '',
          nickname,
          provider,
          refreshToken,
          clientId,
          clientSecret,
          region,
        ].map(quoteCsv).join(',')
      })

      downloadCsv([header.map(quoteCsv).join(','), ...rows].join('\r\n'))
      return
    }

    const header = ['email', 'password', 'status', 'region', 'cashier_url', 'created_at']
    if (currentPlatform === 'kiro') {
      header.push('accessToken', 'refreshToken', 'clientId', 'clientSecret')
    } else if (currentPlatform === 'chatgpt') {
      header.push('token', 'refresh_token')
    } else {
      header.push('token')
    }

    const rows = accounts.map((a) => {
      const baseRow = [a.email, a.password, a.status, a.region, a.cashier_url, a.created_at].map(quoteCsv)
      if (currentPlatform === 'kiro') {
        baseRow.push(quoteCsv(a.extra?.accessToken || a.extra?.webAccessToken || a.token))
        baseRow.push(quoteCsv(a.extra?.refreshToken))
        baseRow.push(quoteCsv(a.extra?.clientId))
        baseRow.push(quoteCsv(a.extra?.clientSecret))
      } else if (currentPlatform === 'chatgpt') {
        baseRow.push(quoteCsv(a.token))
        baseRow.push(quoteCsv(getRefreshToken(a)))
      } else {
        baseRow.push(quoteCsv(a.token))
      }
      return baseRow.join(',')
    })

    downloadCsv([header.map(quoteCsv).join(','), ...rows].join('\r\n'))
  }

  const refreshAccountListAfterDelete = async () => {
    try {
      await load()
    } catch (error: unknown) {
      message.error(`刷新账号列表失败：${deletionErrorDetail(error, '请求失败')}`)
    }
  }

  const handleDelete = async (id: number) => {
    try {
      const result = await apiFetch(`/accounts/${id}`, { method: 'DELETE' }) as AccountDeleteResponse
      if (result?.ok === false || result?.local_deleted === false) {
        message.error(`删除失败：${deletionErrorDetail(result, '账号未删除')}`)
        return
      }
      message.success(singleDeleteSuccessMessage(result))
      setSelectedRowKeys((current) => current.filter((key) => String(key) !== String(id)))
    } catch (error: unknown) {
      message.error(`删除失败：${deletionErrorDetail(error, '请求失败')}`)
    } finally {
      await refreshAccountListAfterDelete()
    }
  }

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return
    const requestedKeys = Array.from(selectedRowKeys)
    try {
      const result = await apiFetch('/accounts/batch-delete', {
        method: 'POST',
        body: JSON.stringify({ ids: requestedKeys }),
      }) as BatchDeleteResponse
      const items: BatchDeleteItem[] = Array.isArray(result.items) ? result.items : []
      const requestedIds = new Set(requestedKeys.map((key) => String(key)))
      const completedIds = new Set<string>()
      for (const item of items) {
        if (item?.ok === true || String(item?.status ?? '') === 'not_found') {
          completedIds.add(String(item.account_id))
        }
      }
      const topLevelNotFoundIds = new Set<string>()
      if (Array.isArray(result.not_found)) {
        for (const accountId of result.not_found) {
          const normalizedId = String(accountId).trim()
          if (!requestedIds.has(normalizedId)) continue
          topLevelNotFoundIds.add(normalizedId)
          completedIds.add(normalizedId)
        }
      }

      const normalizedDeleted = normalizeBatchDeleteCount(result.deleted)
      const normalizedFailed = normalizeBatchDeleteCount(result.failed)
      const failedCountMissing = result.failed === undefined || result.failed === null
      const legacyResponseProvesCompletion =
        items.length === 0
        && (failedCountMissing || normalizedFailed === 0)
        && (normalizedDeleted ?? 0) + topLevelNotFoundIds.size >= requestedIds.size
      if (legacyResponseProvesCompletion) {
        for (const requestedId of requestedIds) completedIds.add(requestedId)
      }
      setSelectedRowKeys((current) => current.filter((key) => !completedIds.has(String(key))))

      const deleted = normalizedDeleted ?? 0
      const failed = normalizedFailed !== null
        ? normalizedFailed
        : items.filter((item) => item.ok !== true && String(item.status ?? '') !== 'not_found').length
      const detail = batchFailureDetails(items)
      if (failed === 0) {
        message.success(`批量删除完成：删除 ${deleted} 个`)
      } else if (deleted === 0) {
        message.error(`批量删除失败：失败 ${failed} 个${detail ? `；${detail}` : ''}`)
      } else {
        message.warning(`批量删除部分完成：删除 ${deleted} 个，失败 ${failed} 个${detail ? `；${detail}` : ''}`)
      }
    } catch (error: unknown) {
      message.error(`批量删除失败：${deletionErrorDetail(error, '请求失败')}`)
    } finally {
      await refreshAccountListAfterDelete()
    }
  }

  const handleChatgptRelogin = async () => {
    const accountIds = Array.from(selectedRowKeys)
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value > 0)

    if (currentPlatform !== 'chatgpt' || accountIds.length === 0) return
    const effectiveConcurrency = Math.min(
      Math.max(Math.trunc(Number(reloginConcurrency) || 1), 1),
      CHATGPT_RELOGIN_MAX_CONCURRENCY,
      accountIds.length,
    )

    const requestEpoch = ++reloginRequestEpochRef.current
    setReloginLoading(true)
    setReloginStartError('')
    try {
      const result = await apiFetch('/tasks/chatgpt-relogin', {
        method: 'POST',
        body: JSON.stringify({
          account_ids: accountIds,
          concurrency: effectiveConcurrency,
        }),
      })
      if (requestEpoch !== reloginRequestEpochRef.current) return
      const nextTaskId = String(result.task_id || '').trim()
      if (!nextTaskId) throw new Error('服务端未返回任务 ID')

      setSelectedRowKeys([])
      setReloginConcurrency(1)
      setReloginTaskMode('relogin')
      setReloginTaskId(nextTaskId)
      message.success(
        `已启动 ${accountIds.length} 个账号重登（并发 ${effectiveConcurrency}）`,
      )
    } catch (error: unknown) {
      if (requestEpoch !== reloginRequestEpochRef.current) return
      const detail = error instanceof Error ? error.message : String(error || '请求失败')
      const errorMessage = `启动重登失败: ${detail}`
      setReloginStartError(errorMessage)
      message.error(errorMessage)
    } finally {
      if (requestEpoch === reloginRequestEpochRef.current) {
        setReloginLoading(false)
      }
    }
  }

  const handleResetAllChatgptMfa = async () => {
    if (currentPlatform !== 'chatgpt' || total === 0 || mfaResetLoading) return

    const requestEpoch = ++reloginRequestEpochRef.current
    setMfaResetLoading(true)
    setReloginStartError('')
    try {
      const result = await apiFetch('/tasks/chatgpt-relogin', {
        method: 'POST',
        body: JSON.stringify({
          all_eligible: true,
          rotate_mfa: true,
          concurrency: CHATGPT_MFA_RESET_CONCURRENCY,
        }),
      })
      if (requestEpoch !== reloginRequestEpochRef.current) return
      const nextTaskId = String(result.task_id || '').trim()
      if (!nextTaskId) throw new Error('服务端未返回任务 ID')

      const taskCount = Math.max(Number(result.count) || 0, 0)
      setReloginTaskMode('mfa')
      setReloginTaskId(nextTaskId)
      message.success(
        taskCount > 0
          ? `已启动 ${taskCount} 个账号的 MFA 重设`
          : '已启动全部账号的 MFA 重设',
      )
    } catch (error: unknown) {
      if (requestEpoch !== reloginRequestEpochRef.current) return
      const detail = error instanceof Error ? error.message : String(error || '请求失败')
      const errorMessage = `启动 MFA 重设失败: ${detail}`
      setReloginStartError(errorMessage)
      message.error(errorMessage)
    } finally {
      if (requestEpoch === reloginRequestEpochRef.current) {
        setMfaResetLoading(false)
      }
    }
  }

  const handleAdd = async () => {
    const values = await addForm.validateFields()
    await apiFetch('/accounts', {
      method: 'POST',
      body: JSON.stringify({ ...values, platform: currentPlatform }),
    })
    message.success('添加成功')
    setAddModalOpen(false)
    addForm.resetFields()
    load()
  }

  const handleImport = async () => {
    if (!importText.trim()) return
    setImportLoading(true)
    try {
      const lines = importText.trim().split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
      const res = await apiFetch('/accounts/import', {
        method: 'POST',
        body: JSON.stringify({ platform: currentPlatform, lines }),
      })
      const created = Math.max(Number(res.created) || 0, 0)
      const skipped = Math.max(lines.length - created, 0)
      message.success(
        skipped > 0
          ? `导入成功 ${created} 个，跳过 ${skipped} 行`
          : `导入成功 ${created} 个`,
      )
      setImportModalOpen(false)
      setImportText('')
      load()
    } catch (e: any) {
      message.error(`导入失败: ${e.message}`)
    } finally {
      setImportLoading(false)
    }
  }

  const handleRegister = async () => {
    const values = await registerForm.validateFields()
    setRegisterLoading(true)
    try {
      const cfg = await apiFetch('/config')
      const executorType = normalizeExecutorForPlatform(currentPlatform, cfg.default_executor)
      const registerExtra = {
        mail_provider: cfg.mail_provider || 'luckmail',
        applemail_base_url: cfg.applemail_base_url,
        applemail_pool_dir: cfg.applemail_pool_dir,
        applemail_pool_file: cfg.applemail_pool_file,
        applemail_mailboxes: cfg.applemail_mailboxes,
        laoudo_auth: cfg.laoudo_auth,
        laoudo_email: cfg.laoudo_email,
        laoudo_account_id: cfg.laoudo_account_id,
        gptmail_base_url: cfg.gptmail_base_url,
        gptmail_api_key: cfg.gptmail_api_key,
        gptmail_domain: cfg.gptmail_domain,
        maliapi_base_url: cfg.maliapi_base_url,
        maliapi_api_key: cfg.maliapi_api_key,
        maliapi_domain: cfg.maliapi_domain,
        maliapi_auto_domain_strategy: cfg.maliapi_auto_domain_strategy,
        yescaptcha_key: cfg.yescaptcha_key,
        moemail_api_url: cfg.moemail_api_url,
        moemail_api_key: cfg.moemail_api_key,
        skymail_api_base: cfg.skymail_api_base,
        skymail_token: cfg.skymail_token,
        skymail_domain: cfg.skymail_domain,
        cloudmail_api_base: cfg.cloudmail_api_base,
        cloudmail_admin_email: cfg.cloudmail_admin_email,
        cloudmail_admin_password: cfg.cloudmail_admin_password,
        cloudmail_domain: cfg.cloudmail_domain,
        cloudmail_subdomain: cfg.cloudmail_subdomain,
        cloudmail_timeout: cfg.cloudmail_timeout,
        duckmail_address: cfg.duckmail_address,
        duckmail_password: cfg.duckmail_password,
        duckmail_api_url: cfg.duckmail_api_url,
        duckmail_provider_url: cfg.duckmail_provider_url,
        duckmail_bearer: cfg.duckmail_bearer,
        freemail_api_url: cfg.freemail_api_url,
        freemail_admin_token: cfg.freemail_admin_token,
        freemail_username: cfg.freemail_username,
        freemail_password: cfg.freemail_password,
        freemail_domain: cfg.freemail_domain,
        cfworker_api_url: cfg.cfworker_api_url,
        cfworker_admin_token: cfg.cfworker_admin_token,
        cfworker_custom_auth: cfg.cfworker_custom_auth,
        cfworker_domain: cfg.cfworker_domain,
        cfworker_subdomain: cfg.cfworker_subdomain,
        cfworker_random_subdomain: parseBooleanConfigValue(cfg.cfworker_random_subdomain),
        cfworker_random_name_subdomain: parseBooleanConfigValue(cfg.cfworker_random_name_subdomain),
        cfworker_fingerprint: cfg.cfworker_fingerprint,
        smstome_cookie: cfg.smstome_cookie,
        smstome_country_slugs: cfg.smstome_country_slugs,
        smstome_phone_attempts: cfg.smstome_phone_attempts,
        smstome_otp_timeout_seconds: cfg.smstome_otp_timeout_seconds,
        smstome_poll_interval_seconds: cfg.smstome_poll_interval_seconds,
        smstome_sync_max_pages_per_country: cfg.smstome_sync_max_pages_per_country,
        luckmail_base_url: cfg.luckmail_base_url,
        luckmail_api_key: cfg.luckmail_api_key,
        luckmail_email_type: cfg.luckmail_email_type,
        luckmail_domain: cfg.luckmail_domain,
      }
      const chatgptRegistrationRequestAdapter =
        buildChatGPTRegistrationRequestAdapter(
          currentPlatform,
          chatgptRegistrationMode,
        )
      const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
        ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
        : registerExtra

      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: currentPlatform,
          count: values.count,
          concurrency: values.concurrency,
          register_delay_seconds: values.register_delay_seconds || 0,
          executor_type: executorType,
          captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
          proxy: null,
          extra: adaptedRegisterExtra,
        }),
      })
      setTaskId(res.task_id)
    } finally {
      setRegisterLoading(false)
    }
  }

  const handleDetailSave = async () => {
    const values = await detailForm.validateFields()
    await apiFetch(`/accounts/${currentAccount.id}`, {
      method: 'PATCH',
      body: JSON.stringify(values),
    })
    message.success('保存成功')
    setDetailModalOpen(false)
    load()
  }

  const openAssignmentEditor = async () => {
    if (!currentAccount) return
    setAssignmentLoading(true)
    try {
      const [targetData, poolData] = await Promise.all([
        apiFetch('/codex2api/targets'),
        apiFetch('/codex2api/pools'),
      ])
      const targetRows = Array.isArray(targetData?.targets) ? targetData.targets : []
      const poolRows = Array.isArray(poolData?.pools) ? poolData.pools : []
      setControlTargets(targetRows)
      setControlPools(poolRows)
      assignmentForm.setFieldsValue({
        target_id: currentAccount.assignment?.target_id,
        pool_id: currentAccount.assignment?.pool_id,
        reason: 'manual_assignment',
      })
      setAssignmentModalOpen(true)
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error || '请求失败')
      message.error(`加载可选归属失败：${detail}`)
    } finally {
      setAssignmentLoading(false)
    }
  }

  const saveAssignment = async () => {
    if (!currentAccount) return
    const values = await assignmentForm.validateFields()
    setAssignmentLoading(true)
    try {
      const result = await apiFetch(`/accounts/${currentAccount.id}/assignment`, {
        method: 'POST',
        body: JSON.stringify(values),
      })
      setAssignmentModalOpen(false)
      message.success(result?.operation_id ? '迁移已入队' : '号池归属已更新')
      setDetailModalOpen(false)
      await load()
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error || '请求失败')
      message.error(`调整归属失败：${detail}`)
    } finally {
      setAssignmentLoading(false)
    }
  }

  const showCpaSyncResult = (title: string, result: any) => {
    const lines = (result.items || [])
      .flatMap((item: any) =>
        (item.results || []).map((syncResult: any) => ({
          email: item.email,
          platform: item.platform,
          ok: Boolean(syncResult.ok),
          name: syncResult.name || 'CPA',
          msg: syncResult.msg || '',
        })),
      )
      .filter((item: any) => !item.ok)
      .map((item: any) => `[${item.platform}] ${item.email || '-'} / ${item.name}: ${item.msg || '失败'}`)

    if (lines.length === 0) return

    Modal.info({
      title,
      width: 760,
      content: (
        <pre
          style={{
            margin: 0,
            maxHeight: 360,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </pre>
      ),
    })
  }

  const showBatchActionResult = (title: string, result: any) => {
    const lines = (result.items || [])
      .filter((item: any) => !item.ok)
      .map((item: any) => `[${item.id || '-'}] ${item.email || '-'}: ${item.message || '失败'}`)

    if (lines.length === 0) return

    Modal.info({
      title,
      width: 760,
      content: (
        <pre
          style={{
            margin: 0,
            maxHeight: 360,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </pre>
      ),
    })
  }

  const handleCpaBackfill = async (mode: 'pending' | 'selected') => {
    if (currentPlatform !== 'chatgpt') return

    const body: Record<string, unknown> = {
      platforms: ['chatgpt'],
    }

    if (mode === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要上传的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.pending_only = true
      if (filterStatus) body.status = filterStatus
      if (search) body.email = search
    }

    setCpaSyncLoading(mode)
    try {
      const result = await apiFetch('/integrations/backfill', {
        method: 'POST',
        body: JSON.stringify(body),
      })

      const actionLabel = mode === 'selected' ? '所选账号远端补传' : '远端未发现账号补传'
      if (!result.total) {
        message.info('没有可处理的账号')
      } else if (!result.failed && !result.skipped) {
        message.success(`${actionLabel}完成：成功 ${result.success} / ${result.total}`)
      } else if (!result.failed) {
        message.success(`${actionLabel}完成：成功 ${result.success}，跳过 ${result.skipped} / ${result.total}`)
      } else if (!result.success) {
        message.error(`${actionLabel}失败：成功 ${result.success}，跳过 ${result.skipped} / ${result.total}`)
      } else {
        message.warning(`${actionLabel}部分完成：成功 ${result.success}，跳过 ${result.skipped} / ${result.total}`)
      }

      showCpaSyncResult(`${actionLabel}结果`, result)
      await load()
    } catch (e: any) {
      message.error(`CPA 上传失败: ${e.message}`)
    } finally {
      setCpaSyncLoading('')
    }
  }

  const handleBatchStatusSync = async (kind: 'probe' | 'remote', scope: 'selected' | 'all') => {
    if (currentPlatform !== 'chatgpt') return

    const loadingKey = `${kind}_${scope}` as typeof statusSyncLoading
    const actionId = kind === 'probe' ? 'probe_local_status' : 'sync_cliproxyapi_status'
    const actionLabel = kind === 'probe' ? '本地状态同步' : 'CLIProxyAPI 状态同步'
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'
    const toastKey = `status-sync:${loadingKey}`

    const body: Record<string, unknown> = {
      params: {},
    }

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要同步的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
    }

    setStatusSyncLoading(loadingKey)
    message.loading({ content: `${scopeLabel}${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch(`/actions/${currentPlatform}/${actionId}/batch`, {
        method: 'POST',
        body: JSON.stringify(body),
      })

      if (!result.total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (!result.failed) {
        message.success({ content: `${scopeLabel}${actionLabel}完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else if (!result.success) {
        message.error({ content: `${scopeLabel}${actionLabel}失败：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else {
        message.warning({ content: `${scopeLabel}${actionLabel}部分完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      }

      showBatchActionResult(`${scopeLabel}${actionLabel}结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `${actionLabel}失败: ${e.message}`, key: toastKey })
    } finally {
      setStatusSyncLoading('')
    }
  }

  const handleBatchUploadCpa = async (scope: 'selected' | 'all') => {
    const toastKey = `batch-upload-cpa:${scope}`
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'

    const body: Record<string, unknown> = {
      params: {},
    }

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要导入 CPA 的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
    }

    setCpaUploadLoading(scope)
    message.loading({ content: `${scopeLabel}导入 CPA 进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch(`/actions/${currentPlatform}/upload_cpa/batch`, {
        method: 'POST',
        body: JSON.stringify(body),
      })

      if (!result.total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (!result.failed) {
        message.success({ content: `${scopeLabel}导入 CPA 完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else if (!result.success) {
        message.error({ content: `${scopeLabel}导入 CPA 失败：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else {
        message.warning({ content: `${scopeLabel}导入 CPA 部分完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      }

      showBatchActionResult(`${scopeLabel}导入 CPA 结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `导入 CPA 失败: ${e.message}`, key: toastKey })
    } finally {
      setCpaUploadLoading('')
    }
  }

  const handleBatchUploadCodex2API = async (scope: 'selected' | 'all') => {
    const toastKey = `batch-upload-codex2api:${scope}`
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'

    const body: Record<string, unknown> = {
      params: {},
    }

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要导入 Codex2API 的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (createdAtStart) body.created_at_start = createdAtStart
      if (createdAtEnd) body.created_at_end = createdAtEnd
    }

    setCodex2APIUploadLoading(scope)
    message.loading({ content: `${scopeLabel}导入 Codex2API 进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch(`/actions/${currentPlatform}/upload_codex2api/batch`, {
        method: 'POST',
        body: JSON.stringify(body),
      })

      if (!result.total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (!result.failed) {
        message.success({ content: `${scopeLabel}导入 Codex2API 完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else if (!result.success) {
        message.error({ content: `${scopeLabel}导入 Codex2API 失败：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else {
        message.warning({ content: `${scopeLabel}导入 Codex2API 部分完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      }

      showBatchActionResult(`${scopeLabel}导入 Codex2API 结果`, result)
      await load()
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error || '请求失败')
      message.error({ content: `导入 Codex2API 失败: ${detail}`, key: toastKey })
    } finally {
      setCodex2APIUploadLoading('')
    }
  }

  const getStatusSyncScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  const getBackfillScope = (): 'selected' | 'pending' => (selectedRowKeys.length > 0 ? 'selected' : 'pending')

  const getUploadCpaScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  const getUploadCodex2APIScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  const backfillButtonLabel = () => {
    const scope = getBackfillScope()
    const count = scope === 'selected' ? selectedRowKeys.length : total
    return scope === 'selected' ? `补传所选远端未发现 (${count})` : `补传远端未发现 (${count})`
  }

  const uploadCpaButtonLabel = () => {
    const scope = getUploadCpaScope()
    const count = scope === 'selected' ? selectedRowKeys.length : total
    return scope === 'selected' ? `导入所选 CPA (${count})` : `导入筛选 CPA (${count})`
  }

  const uploadCodex2APIButtonLabel = () => {
    const scope = getUploadCodex2APIScope()
    const count = scope === 'selected' ? selectedRowKeys.length : total
    return scope === 'selected' ? `导入所选 Codex2API (${count})` : `导入筛选 Codex2API (${count})`
  }

  const isChatgptPlatform = currentPlatform === 'chatgpt'
  const hasUploadCpaAction = platformActions.some((item) => item?.id === 'upload_cpa')
  const hasUploadCodex2APIAction = platformActions.some((item) => item?.id === 'upload_codex2api')
  const monospaceStyle: React.CSSProperties = {
    fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    fontSize: 12,
  }
  const secondaryTextStyle: React.CSSProperties = {
    fontSize: 12,
    color: token.colorTextSecondary,
  }
  const cellStackStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    minWidth: 0,
  }
  const secretPreviewStyle: React.CSSProperties = {
    ...monospaceStyle,
    display: 'inline-block',
    filter: 'blur(4px)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    maxWidth: '100%',
    opacity: 0.9,
  }
  const compactPanelStyle: React.CSSProperties = {
    padding: '8px 10px',
    borderRadius: token.borderRadiusLG,
    border: `1px solid ${token.colorBorder}`,
    background: token.colorFillAlter,
  }

  const columns: any[] = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 260,
      fixed: isChatgptPlatform ? 'left' : undefined,
      render: (text: string, record: any) => (
        <div style={cellStackStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            <Text
              style={{ ...monospaceStyle, flex: 1, minWidth: 0, whiteSpace: 'nowrap' }}
              ellipsis={{ tooltip: text }}
            >
              {text}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
          </div>
          <Text type="secondary" style={secondaryTextStyle} ellipsis={{ tooltip: record.user_id || `账号 #${record.id}` }}>
            {record.user_id ? `UID: ${record.user_id}` : `账号 #${record.id}`}
          </Text>
        </div>
      ),
    },
    {
      title: '密码',
      dataIndex: 'password',
      key: 'password',
      width: 150,
      render: (text: string) => (
        <Space size={6} style={{ width: '100%', justifyContent: 'space-between' }}>
          <Text style={{ ...secretPreviewStyle, maxWidth: 90 }} title={text}>
            {text}
          </Text>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
        </Space>
      ),
    },
    {
      title: 'RT',
      key: 'refresh_token',
      width: 120,
      render: (_: any, record: any) => {
        const rt = getRefreshToken(record)
        if (!rt) return <span style={{ color: '#ccc' }}>-</span>
        return (
          <Space size={6} style={{ width: '100%', justifyContent: 'space-between' }}>
            <Text style={{ ...secretPreviewStyle, fontSize: 11, maxWidth: 58 }} title={rt}>
              {rt}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(rt)} />
          </Space>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string) => <Tag color={STATUS_COLORS[status] || 'default'}>{status}</Tag>,
    },
  ]

  if (isChatgptPlatform) {
    columns.push(
      {
        title: '当前目标',
        key: 'assignment',
        width: 210,
        render: (_: unknown, record: AccountControlRow) => {
          const assignment = record.assignment || {}
          const meta = assignmentStateMeta(assignment.state)
          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <Space size={6} wrap>
                <Tag color={assignment.target_id ? 'blue' : 'default'}>
                  {assignment.target_id
                    ? assignment.target_name || `目标 #${assignment.target_id}`
                    : '未分配'}
                </Tag>
                <Tag color={meta.color}>{meta.label}</Tag>
              </Space>
              <Text
                type="secondary"
                style={secondaryTextStyle}
                ellipsis={{ tooltip: assignment.pool_name || assignment.pool_id || '未分配号池' }}
              >
                {assignment.pool_name || assignment.pool_id || '未分配号池'}
              </Text>
            </div>
          )
        },
      },
      {
        title: '7天剩余',
        key: 'quota_7d',
        width: 180,
        render: (_: unknown, record: AccountControlRow) => {
          const quota = record.quota?.['7d'] || {}
          const remaining = quota.continuous_remaining_usd ?? quota.remaining_usd
          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <Space size={6}>
                <Text strong>{formatUsd(remaining)}</Text>
                {quota.fresh === false ? <Tag color="warning">已过期</Tag> : null}
              </Space>
              <Text type="secondary" style={secondaryTextStyle}>
                {quota.reset_at ? `重置 ${formatSyncTime(quota.reset_at)}` : '尚无额度快照'}
              </Text>
            </div>
          )
        },
      },
      {
        title: '本地状态',
        key: 'chatgpt_local_state',
        width: 320,
        render: (_: any, record: any) => {
          const auth = record.chatgptLocal?.auth || {}
          const subscription = record.chatgptLocal?.subscription || {}
          const codex = record.chatgptLocal?.codex || {}
          const cpaSync = record.cpaSync || {}
          const sub2apiSync = record.sub2apiSync || {}
          const codex2apiSync = record.codex2apiSync || {}
          const authMeta = authStateMeta(auth.state)
          const planTag = planMeta(subscription.plan)
          const codexMeta = codexStateMeta(codex.state)
          const cpaMeta = uploadSyncMeta(cpaSync)
          const sub2apiMeta = uploadSyncMeta(sub2apiSync)
          const codex2apiMeta = uploadSyncMeta(codex2apiSync)

          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                <Tag color={authMeta.color}>{authMeta.label}</Tag>
                <Tag color={planTag.color}>{planTag.label}</Tag>
                <Tag color={codexMeta.color}>Codex {codexMeta.label}</Tag>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                <Tag color={cpaMeta.color} title={uploadSyncTitle('CPA', cpaSync)}>
                  CPA {cpaMeta.label}
                </Tag>
                <Tag color={sub2apiMeta.color} title={uploadSyncTitle('Sub2API', sub2apiSync)}>
                  Sub2API {sub2apiMeta.label}
                </Tag>
                <Tag color={codex2apiMeta.color} title={uploadSyncTitle('Codex2API', codex2apiSync)}>
                  Codex2API {codex2apiMeta.label}
                </Tag>
              </div>
            </div>
          )
        },
      },
      {
        title: 'CLIProxyAPI',
        key: 'cliproxy_sync',
        width: 170,
        render: (_: any, record: any) => {
          const sync = record.cliproxySync || {}
          const meta = cliproxyStateMeta(sync)

          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <Tag color={meta.color}>{meta.label}</Tag>
            </div>
          )
        },
      },
    )
  } else {
    if (hasUploadCpaAction) {
      columns.push({
        title: 'CPA',
        key: 'cpa_sync',
        width: 120,
        render: (_: any, record: any) => {
          const cpaMeta = uploadSyncMeta(record.cpaSync || {})
          return (
            <Tag color={cpaMeta.color} title={uploadSyncTitle('CPA', record.cpaSync || {})}>
              {cpaMeta.label}
            </Tag>
          )
        },
      })
    }

    columns.push(
      {
        title: '地区',
        dataIndex: 'region',
        key: 'region',
        width: 100,
        render: (text: string) => text || '-',
      },
      {
        title: '试用链接',
        dataIndex: 'cashier_url',
        key: 'cashier_url',
        width: 120,
        render: (url: string) =>
          url ? (
            <Space size={0}>
              <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(url)} />
              <Button type="text" size="small" icon={<LinkOutlined />} onClick={() => window.open(url, '_blank')} />
            </Space>
          ) : (
            '-'
          ),
      },
    )
  }

  columns.push(
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 132,
      render: (text: string) => {
        const formatted = formatCreatedAt(text)
        return (
          <div style={cellStackStyle}>
            <Text style={{ fontSize: 13 }}>{formatted.date}</Text>
            {formatted.time ? <Text type="secondary" style={secondaryTextStyle}>{formatted.time}</Text> : null}
          </div>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      fixed: isChatgptPlatform ? 'right' : undefined,
      render: (_: any, record: any) => (
        <Space size={4} wrap>
          {isChatgptPlatform && canStartChatGPTPhoneVerification(record) ? (
            <Button type="link" size="small" onClick={() => setPhoneVerificationAccount(record)}>
              接码
            </Button>
          ) : null}
          <Button type="link" size="small" onClick={() => { setCurrentAccount(record); setDetailModalOpen(true); }}>
            详情
          </Button>
          <Popconfirm
            title="确认删除该账号吗？"
            onConfirm={() => handleDelete(record.id)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
          <ActionMenu acc={record} onRefresh={load} actions={platformActions} />
        </Space>
      ),
    },
  )

  const statusSyncMenuItems: MenuProps['items'] = [
    {
      key: `probe:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选本地状态 (${selectedRowKeys.length})`
          : `同步当前筛选本地状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
    {
      key: `remote:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选 CLIProxyAPI 状态 (${selectedRowKeys.length})`
          : `同步当前筛选 CLIProxyAPI 状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <Space>
          <Input.Search
            placeholder="搜索邮箱..."
            allowClear
            onSearch={(v) => { setPage(1); setSearch(v) }}
            style={{ width: 200 }}
          />
          <Select
            placeholder="状态筛选"
            allowClear
            style={{ width: 120 }}
            onChange={(v) => { setPage(1); setFilterStatus(v) }}
            options={[
              { value: 'registered', label: '已注册' },
              { value: 'trial', label: '试用中' },
              { value: 'subscribed', label: '已订阅' },
              { value: 'expired', label: '已过期' },
              { value: 'invalid', label: '已失效' },
            ]}
          />
          <DatePicker
            showTime
            allowClear
            placeholder="开始时间"
            onChange={(value) => { setPage(1); setCreatedAtStart(value ? value.toISOString() : '') }}
          />
          <DatePicker
            showTime
            allowClear
            placeholder="结束时间"
            onChange={(value) => { setPage(1); setCreatedAtEnd(value ? value.toISOString() : '') }}
          />
          <Text type="secondary">{total} 个账号</Text>
          {currentPlatform === 'chatgpt' && (
            <Text type="secondary">
              下次执行：{formatAutoReloginCountdown(autoReloginStatus, autoReloginNow)}
            </Text>
          )}
          {currentPlatform === 'chatgpt' && (
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              loading={autoReloginRunNowLoading}
              disabled={
                autoReloginRunNowLoading
                || !autoReloginStatus
                || !autoReloginStatus.enabled
                || ['running', 'stopping'].includes(autoReloginStatus.state)
                || Number(autoReloginStatus.eligible_accounts || 0) <= 0
              }
              onClick={handleAutoReloginRunNow}
            >
              立即执行
            </Button>
          )}
          {selectedRowKeys.length > 0 && (
            <Text type="success">已选 {selectedRowKeys.length} 个</Text>
          )}
        </Space>
        <Space>
          {currentPlatform === 'chatgpt' && (
            <Button icon={<LoginOutlined />} onClick={() => setExistingAccountLoginModalOpen(true)}>
              登录
            </Button>
          )}
          {currentPlatform === 'chatgpt' && (
            <Dropdown
              trigger={['click']}
              menu={{
                items: statusSyncMenuItems,
                onClick: ({ key }) => {
                  const [kind, scope] = String(key).split(':') as ['probe' | 'remote', 'selected' | 'all']
                  handleBatchStatusSync(kind, scope)
                },
              }}
            >
              <Button
                icon={<SyncOutlined />}
                loading={statusSyncLoading !== ''}
                disabled={total === 0}
              >
                状态同步
              </Button>
            </Dropdown>
          )}
          {currentPlatform === 'chatgpt' && (
            <Popconfirm
              title="确认强制重设全部 ChatGPT 账号的 MFA？"
              description="所有具备登录凭据的账号都会新增或替换 MFA；没有邮箱接码地址也会继续执行。"
              onConfirm={handleResetAllChatgptMfa}
              disabled={total === 0}
              okText="确认重设"
              cancelText="取消"
              okButtonProps={{ danger: true, autoInsertSpace: false }}
            >
              <Button
                icon={<SyncOutlined />}
                loading={mfaResetLoading}
                disabled={total === 0 || reloginLoading}
              >
                重设全部 MFA
              </Button>
            </Popconfirm>
          )}
          {currentPlatform === 'chatgpt' && (
            <Popconfirm
              title={
                getBackfillScope() === 'selected'
                  ? `确认补传所选 ${selectedRowKeys.length} 个账号中远端未发现的 auth-file？`
                  : '确认补传当前筛选范围内远端未发现且本地状态有效的账号？'
              }
              onConfirm={() => handleCpaBackfill(getBackfillScope())}
              okText="确认"
              cancelText="取消"
            >
              <Button
                loading={cpaSyncLoading === 'pending' || cpaSyncLoading === 'selected'}
                icon={<UploadOutlined />}
                disabled={getBackfillScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0}
              >
                {backfillButtonLabel()}
              </Button>
            </Popconfirm>
          )}
          {currentPlatform === 'chatgpt' && hasUploadCodex2APIAction && (
            <Popconfirm
              title={
                getUploadCodex2APIScope() === 'selected'
                  ? `确认导入所选 ${selectedRowKeys.length} 个账号到 Codex2API？`
                  : `确认导入当前筛选范围内 ${total} 个账号到 Codex2API？`
              }
              onConfirm={() => handleBatchUploadCodex2API(getUploadCodex2APIScope())}
              okText="确认"
              cancelText="取消"
            >
              <Button
                loading={codex2apiUploadLoading === 'selected' || codex2apiUploadLoading === 'all'}
                icon={<UploadOutlined />}
                disabled={getUploadCodex2APIScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0}
              >
                {uploadCodex2APIButtonLabel()}
              </Button>
            </Popconfirm>
          )}
          {currentPlatform !== 'chatgpt' && hasUploadCpaAction && (
            <Popconfirm
              title={
                getUploadCpaScope() === 'selected'
                  ? `确认导入所选 ${selectedRowKeys.length} 个账号到 CPA？`
                  : `确认导入当前筛选范围内 ${total} 个账号到 CPA？`
              }
              onConfirm={() => handleBatchUploadCpa(getUploadCpaScope())}
              okText="确认"
              cancelText="取消"
            >
              <Button
                loading={cpaUploadLoading === 'selected' || cpaUploadLoading === 'all'}
                icon={<UploadOutlined />}
                disabled={getUploadCpaScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0}
              >
                {uploadCpaButtonLabel()}
              </Button>
            </Popconfirm>
          )}
          {currentPlatform === 'chatgpt' && (
            <Popconfirm
              title={`确认重新登录所选 ${selectedRowKeys.length} 个账号、获取新令牌并覆盖同步到 Codex2API？`}
              description={(
                <Space size={8}>
                  <Text type="secondary">
                    并发数（最多 {Math.min(selectedRowKeys.length, CHATGPT_RELOGIN_MAX_CONCURRENCY)}）
                  </Text>
                  <InputNumber
                    aria-label="重登并发数"
                    min={1}
                    max={Math.max(
                      1,
                      Math.min(selectedRowKeys.length, CHATGPT_RELOGIN_MAX_CONCURRENCY),
                    )}
                    precision={0}
                    size="small"
                    value={reloginConcurrency}
                    onChange={(value) => {
                      const maxConcurrency = Math.max(
                        1,
                        Math.min(selectedRowKeys.length, CHATGPT_RELOGIN_MAX_CONCURRENCY),
                      )
                      setReloginConcurrency(
                        Math.min(Math.max(Math.trunc(Number(value) || 1), 1), maxConcurrency),
                      )
                    }}
                    style={{ width: 72 }}
                  />
                </Space>
              )}
              onConfirm={handleChatgptRelogin}
              disabled={selectedRowKeys.length === 0}
              okText="确认"
              cancelText="取消"
              okButtonProps={{ autoInsertSpace: false }}
              onOpenChange={(open) => {
                if (open) {
                  setReloginStartError('')
                  setReloginConcurrency((current) => Math.min(
                    Math.max(Math.trunc(Number(current) || 1), 1),
                    Math.max(
                      1,
                      Math.min(selectedRowKeys.length, CHATGPT_RELOGIN_MAX_CONCURRENCY),
                    ),
                  ))
                }
              }}
            >
              <Button
                icon={<RedoOutlined />}
                loading={reloginLoading}
                disabled={selectedRowKeys.length === 0 || mfaResetLoading}
              >
                重登所选 ({selectedRowKeys.length})
              </Button>
            </Popconfirm>
          )}
          {selectedRowKeys.length > 0 && (
            <Popconfirm
              title={`确认删除选中的 ${selectedRowKeys.length} 个账号？`}
              onConfirm={handleBatchDelete}
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
            >
              <Button danger icon={<DeleteOutlined />}>删除 {selectedRowKeys.length} 个</Button>
            </Popconfirm>
          )}
          <Button icon={<UploadOutlined />} onClick={() => setImportModalOpen(true)}>导入</Button>
          <Button icon={<DownloadOutlined />} onClick={exportCsv} disabled={accounts.length === 0}>导出</Button>
          <Button icon={<PlusOutlined />} onClick={() => setAddModalOpen(true)}>新增</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setRegisterModalOpen(true)}>注册</Button>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} />
        </Space>
      </div>

      {reloginStartError ? (
        <Alert
          type="error"
          showIcon
          closable
          message={reloginStartError}
          onClose={() => setReloginStartError('')}
          style={{ marginBottom: 16 }}
        />
      ) : null}

      <Table
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        loading={loading}
        size="middle"
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
        }}
        pagination={{ total, current: page, pageSize, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'], onChange: (p, ps) => { setPage(p); setPageSize(ps) } }}
        scroll={{ x: isChatgptPlatform ? 1830 : 980 }}
        onRow={(record) => ({
          onDoubleClick: () => {
            setCurrentAccount(record)
            setDetailModalOpen(true)
          },
        })}
      />

      <ChatGPTExistingAccountLoginModal
        open={existingAccountLoginModalOpen && currentPlatform === 'chatgpt'}
        onClose={() => setExistingAccountLoginModalOpen(false)}
        onDone={() => {
          load()
        }}
      />

      <ChatGPTPhoneVerificationModal
        open={Boolean(phoneVerificationAccount) && currentPlatform === 'chatgpt'}
        account={phoneVerificationAccount}
        onClose={() => setPhoneVerificationAccount(null)}
        onSuccess={() => {
          load()
        }}
      />

      {reloginTaskId && currentPlatform === 'chatgpt' ? (
        <Modal
          title={reloginTaskMode === 'mfa' ? '重设全部 ChatGPT MFA' : '重登并同步 Codex2API'}
          open
          onCancel={() => setReloginTaskId(null)}
          footer={null}
          width={600}
          maskClosable={false}
          destroyOnHidden
        >
          <TaskLogPanel
            taskId={reloginTaskId}
            mode="relogin"
            onDone={() => { load() }}
          />
        </Modal>
      ) : null}

      <Modal
        title={`注册 ${currentPlatform}`}
        open={registerModalOpen}
        onCancel={() => { setRegisterModalOpen(false); setTaskId(null); registerForm.resetFields(); }}
        footer={null}
        width={500}
        maskClosable={false}
      >
        {!taskId ? (
          <Form form={registerForm} layout="vertical" onFinish={handleRegister}>
            <Form.Item name="count" label="注册数量" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} />
            </Form.Item>
            <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" initialValue={0}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
            </Form.Item>
            {currentPlatform === 'chatgpt' && (
              <Form.Item label="ChatGPT Token 方案">
                <ChatGPTRegistrationModeSwitch
                  mode={chatgptRegistrationMode}
                  onChange={setChatgptRegistrationMode}
                />
              </Form.Item>
            )}
            <Form.Item>
              <Button type="primary" htmlType="submit" block loading={registerLoading}>
                开始注册
              </Button>
            </Form.Item>
          </Form>
        ) : (
          <TaskLogPanel taskId={taskId} onDone={() => { load(); }} />
        )}
      </Modal>

      <Modal
        title="手动新增账号"
        open={addModalOpen}
        onCancel={() => { setAddModalOpen(false); addForm.resetFields(); }}
        onOk={handleAdd}
        okText="确定"
        cancelText="取消"
        maskClosable={false}
      >
        <Form form={addForm} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="token" label="Token">
            <Input />
          </Form.Item>
          <Form.Item name="cashier_url" label="试用链接">
            <Input />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue="registered">
            <Select
              options={[
                { value: 'registered', label: '已注册' },
                { value: 'trial', label: '试用中' },
                { value: 'subscribed', label: '已订阅' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="批量导入"
        open={importModalOpen}
        onCancel={() => { setImportModalOpen(false); setImportText(''); }}
        onOk={handleImport}
        okText="确定"
        cancelText="取消"
        confirmLoading={importLoading}
        maskClosable={false}
      >
        <p style={{ marginBottom: 8, fontSize: 12, color: '#7a8ba3' }}>
          每行格式: <code style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 4px', borderRadius: 4 }}>email----password [JSON元数据]</code>；也兼容空格分隔。
        </p>
        <Input.TextArea
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          rows={8}
          style={{ fontFamily: 'monospace' }}
        />
      </Modal>

      <Modal
        title="账号详情"
        open={detailModalOpen}
        onCancel={() => setDetailModalOpen(false)}
        onOk={handleDetailSave}
        okText="保存"
        cancelText="取消"
        maskClosable={false}
        width={760}
        styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
      >
        {currentAccount && (
          <>
            <Form form={detailForm} layout="vertical" initialValues={currentAccount}>
              <Form.Item name="status" label="状态">
                <Select
                  options={[
                    { value: 'registered', label: '已注册' },
                    { value: 'trial', label: '试用中' },
                    { value: 'subscribed', label: '已订阅' },
                    { value: 'expired', label: '已过期' },
                    { value: 'invalid', label: '已失效' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="token" label="Access Token">
                <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
              </Form.Item>
            </Form>
            {(() => {
              const rt = getRefreshToken(currentAccount)
              if (!rt) return null
              return (
                <div style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>Refresh Token</div>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'flex-start',
                      gap: 8,
                      background: token.colorFillAlter,
                      border: `1px solid ${token.colorBorder}`,
                      borderRadius: token.borderRadius,
                      padding: '8px 10px',
                    }}
                  >
                    <Text
                      style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                      copyable={{ text: rt, tooltips: ['复制 RT', '已复制'] }}
                    >
                      {rt}
                    </Text>
                  </div>
                </div>
              )
            })()}
            {currentPlatform === 'kiro' && currentAccount?.extra ? (
              <DetailSection title="Kiro 客户端信息">
                <SummaryField label="Client ID" value={currentAccount.extra?.clientId} code />
                <SummaryField label="Client Secret" value={currentAccount.extra?.clientSecret} code />
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="号池归属与连续额度">
                {(() => {
                  const assignment = currentAccount.assignment || {}
                  const binding = currentAccount.binding || {}
                  const windows = Object.keys(controlQuota).length ? controlQuota : (currentAccount.quota || {})
                  const fiveHour = windows['5h'] || {}
                  const sevenDay = windows['7d'] || {}
                  const assignmentMeta = assignmentStateMeta(assignment.state)
                  return (
                    <>
                      <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
                        <Descriptions.Item label="当前目标">
                          {assignment.target_id
                            ? assignment.target_name || `目标 #${assignment.target_id}`
                            : '未分配'}
                        </Descriptions.Item>
                        <Descriptions.Item label="当前号池">
                          {assignment.pool_name || assignment.pool_id || '未分配'}
                        </Descriptions.Item>
                        <Descriptions.Item label="租约状态"><Tag color={assignmentMeta.color}>{assignmentMeta.label}</Tag></Descriptions.Item>
                        <Descriptions.Item label="绑定状态">{binding.sync_status || '尚未同步'}</Descriptions.Item>
                        <Descriptions.Item label="5 小时剩余">
                          {formatUsd(fiveHour.continuous_remaining_usd ?? fiveHour.remaining_usd)}
                        </Descriptions.Item>
                        <Descriptions.Item label="7 天剩余">
                          {formatUsd(sevenDay.continuous_remaining_usd ?? sevenDay.remaining_usd)}
                        </Descriptions.Item>
                        <Descriptions.Item label="7 天重置时间" span={2}>
                          {sevenDay.reset_at ? formatSyncTime(sevenDay.reset_at) : '尚无数据'}
                        </Descriptions.Item>
                      </Descriptions>
                      <Button
                        size="small"
                        loading={assignmentLoading}
                        onClick={openAssignmentEditor}
                        style={{ marginTop: 10 }}
                      >
                        调整目标与号池
                      </Button>
                      {controlDetailLoading ? <Text type="secondary" style={{ marginLeft: 10 }}>正在读取最新账本…</Text> : null}
                    </>
                  )
                })()}

                {quotaHistory.length ? (
                  <div className="quota-history-list">
                    <Text strong>7 天额度采样</Text>
                    {quotaHistory.slice(0, 6).map((item, index) => (
                      <div className="quota-history-row" key={`${item.captured_at || index}:${index}`}>
                        <Text type="secondary">{formatSyncTime(item.captured_at)}</Text>
                        <Text>{formatUsd(item.continuous_remaining_usd ?? item.remaining_usd)} 剩余</Text>
                        <Tag color={item.fresh === false ? 'warning' : 'success'}>{item.fresh === false ? '过期' : '有效'}</Tag>
                      </div>
                    ))}
                  </div>
                ) : null}
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' && migrations.length ? (
              <DetailSection title="迁移时间线">
                <Timeline
                  items={migrations.map((item) => {
                    const meta = migrationStateMeta(item.state)
                    const errorMessage = item.error && typeof item.error === 'object'
                      ? String(item.error.message || item.error.detail || '')
                      : ''
                    return {
                      color: meta.color,
                      children: (
                        <div>
                          <Space wrap>
                            <Tag color={meta.color}>{meta.label}</Tag>
                            <Text>目标 #{item.source_target_id} → 目标 #{item.destination_target_id}</Text>
                            <Text type="secondary">{formatSyncTime(item.updated_at || item.created_at)}</Text>
                          </Space>
                          <div><Text type="secondary">当前步骤：{item.step || item.state}</Text></div>
                          {errorMessage ? <Alert type="warning" showIcon message={errorMessage} style={{ marginTop: 8 }} /> : null}
                        </div>
                      ),
                    }
                  })}
                />
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="本地真实状态">
                {currentAccount.chatgptLocal && Object.keys(currentAccount.chatgptLocal).length > 0 ? (
                  <LocalProbeSummary probe={currentAccount.chatgptLocal} />
                ) : (
                  <Text type="secondary">尚未探测。可在操作菜单中点击“探测本地状态”。</Text>
                )}
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="CLIProxyAPI 状态">
                {currentAccount.cliproxySync && Object.keys(currentAccount.cliproxySync).length > 0 ? (
                  <CliproxySyncSummary sync={currentAccount.cliproxySync} />
                ) : (
                  <Text type="secondary">尚未同步。可在操作菜单中点击“同步 CLIProxyAPI 状态”。</Text>
                )}
              </DetailSection>
            ) : null}
          </>
        )}
      </Modal>

      <Modal
        title={`调整账号归属${currentAccount?.email ? ` · ${currentAccount.email}` : ''}`}
        open={assignmentModalOpen}
        onCancel={() => setAssignmentModalOpen(false)}
        onOk={saveAssignment}
        okText="确认调整"
        cancelText="取消"
        confirmLoading={assignmentLoading}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          message="跨目标调整会启动可恢复迁移"
          description="源账号会先排空，目标端验证成功后才提交归属。"
          style={{ marginBottom: 16 }}
        />
        <Form form={assignmentForm} layout="vertical">
          <Form.Item name="target_id" label="目标节点" rules={[{ required: true, message: '请选择目标节点' }]}>
            <Select
              options={controlTargets.map(item => ({
                value: item.id,
                label: `${item.name} · #${item.id}${item.health_status === 'healthy' ? '' : ' · 未就绪'}`,
                disabled: !item.enabled || item.health_status !== 'healthy',
              }))}
            />
          </Form.Item>
          <Form.Item name="pool_id" label="逻辑号池" rules={[{ required: true, message: '请选择号池' }]}>
            <Select options={controlPools.map(item => ({ value: item.id, label: `${item.name} · ${item.id}` }))} />
          </Form.Item>
          <Form.Item name="reason" label="调整原因" rules={[{ required: true }]}>
            <Input maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
