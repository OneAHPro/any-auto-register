import type { CSSProperties, Key, KeyboardEvent, ReactNode } from 'react'
import {
  Alert,
  Avatar,
  Button,
  Checkbox,
  Divider,
  Popconfirm,
  Progress,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  CalendarOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  DollarOutlined,
  EyeOutlined,
  FileTextOutlined,
  KeyOutlined,
  LinkOutlined,
  LoginOutlined,
  MailOutlined,
  SyncOutlined,
  TeamOutlined,
  UserOutlined,
  WarningOutlined,
} from '@ant-design/icons'

const { Text } = Typography

export interface AccountCardProps {
  account: any
  platform: string
  selected: boolean
  onSelect: (id: Key, checked: boolean) => void
  onCopy: (value: string) => void
  onOpenDetails: (account: any) => void
  onDelete: (id: number) => void
  onPhoneVerification?: (account: any) => void
  canPhoneVerification?: boolean
  moreAction?: ReactNode
  codex2apiSyncTitle?: string
}

type Meta = { color: string; label: string }

const STATUS_META: Record<string, Meta> = {
  registered: { color: 'default', label: '已注册' },
  trial: { color: 'processing', label: '试用中' },
  subscribed: { color: 'success', label: '已订阅' },
  expired: { color: 'warning', label: '已过期' },
  invalid: { color: 'error', label: '已失效' },
}

const PLAN_META: Record<string, Meta> = {
  plus: { color: 'success', label: 'Plus' },
  team: { color: 'processing', label: 'Team' },
  enterprise: { color: 'processing', label: 'Enterprise' },
  pro: { color: 'processing', label: 'Pro' },
  free: { color: 'default', label: 'Free' },
}

const AUTH_META: Record<string, Meta> = {
  access_token_valid: { color: 'success', label: '认证有效' },
  account_deactivated: { color: 'error', label: '账号已失效' },
  access_token_invalidated: { color: 'error', label: '令牌失效' },
  unauthorized: { color: 'error', label: '未授权' },
  missing_access_token: { color: 'warning', label: '缺少令牌' },
  banned_like: { color: 'error', label: '疑似封禁' },
  probe_failed: { color: 'warning', label: '探测失败' },
}

const CODEX_META: Record<string, Meta> = {
  usable: { color: 'success', label: 'Codex 可用' },
  account_deactivated: { color: 'error', label: '工作区失效' },
  access_token_invalidated: { color: 'error', label: 'Codex 令牌失效' },
  unauthorized: { color: 'error', label: 'Codex 未授权' },
  payment_required: { color: 'warning', label: '需要付费或权限' },
  quota_exhausted: { color: 'warning', label: '额度已耗尽' },
  skipped_auth_invalid: { color: 'default', label: '未执行 Codex 探测' },
  probe_failed: { color: 'warning', label: 'Codex 探测失败' },
}

function parseExtra(account: any): Record<string, any> {
  if (account?.extra && typeof account.extra === 'object') return account.extra
  if (typeof account?.extra_json !== 'string') return {}
  try {
    const value = JSON.parse(account.extra_json)
    return value && typeof value === 'object' ? value : {}
  } catch {
    return {}
  }
}

function firstValue(source: any, paths: string[][]): unknown {
  for (const path of paths) {
    let value = source
    for (const key of path) {
      if (!value || typeof value !== 'object') {
        value = undefined
        break
      }
      value = value[key]
    }
    if (value !== undefined && value !== null && String(value).trim() !== '') return value
  }
  return undefined
}

function firstNumber(source: any, paths: string[][]): number | null {
  const value = firstValue(source, paths)
  if (value === undefined || value === null || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function parseJsonObject(value: unknown): Record<string, any> {
  if (!value || typeof value !== 'string') return {}
  const trimmed = value.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return {}
  try {
    const parsed = JSON.parse(trimmed)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)))
}

function formatMoney(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `$${parsed.toFixed(2)}` : '—'
}

function formatCny(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `¥${parsed.toFixed(2)}` : '—'
}

function formatDate(value: unknown, withTime = false): string {
  if (!value) return '—'
  const numeric = typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value))
  const date = new Date(numeric ? Number(value) * (Number(value) < 100000000000 ? 1000 : 1) : String(value))
  if (Number.isNaN(date.getTime())) return String(value)
  return withTime
    ? date.toLocaleString([], { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' })
}

function formatNumber(value: number | null): string {
  if (value === null) return '—'
  return new Intl.NumberFormat().format(value)
}

function formatBytes(value: number | null): string {
  if (value === null) return '—'
  if (value < 1024) return `${Math.round(value)} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`
}

function maskSecret(value: string): string {
  if (!value) return ''
  return '••••••••••••'
}

function maskEmail(email: string): string {
  const [local, domain] = email.split('@')
  if (!local || !domain) return email
  if (local.length <= 3) return `${local[0] || ''}***@${domain}`
  return `${local.slice(0, 2)}***${local.slice(-1)}@${domain}`
}

function providerLabel(value: unknown, platform: string): string {
  const provider = String(value || '').toLowerCase()
  if (provider.includes('microsoft') || provider.includes('outlook')) return 'Microsoft'
  if (provider.includes('apple')) return 'AppleMail'
  if (provider.includes('mail')) return String(value)
  if (platform === 'chatgpt') return 'Access Token'
  return platform || '—'
}

function statusMeta(value: unknown): Meta {
  const key = String(value || '').toLowerCase()
  return STATUS_META[key] || { color: 'default', label: key || '未知状态' }
}

function planMeta(value: unknown): Meta {
  const key = String(value || '').toLowerCase()
  return PLAN_META[key] || { color: 'default', label: key ? String(value) : '套餐未读取' }
}

function syncMeta(sync: any): Meta {
  if (!sync || Object.keys(sync).length === 0) return { color: 'default', label: '未同步' }
  if (sync.uploaded || sync.uploaded_at) return { color: 'success', label: '已上传' }
  if (sync.last_attempt_ok === false) return { color: 'error', label: '失败' }
  if (sync.last_attempt_ok === true || sync.last_attempt_at) return { color: 'processing', label: '已尝试' }
  return { color: 'default', label: '未上传' }
}

function getStatusCode(account: any, auth: any, codex: any): number | null {
  const value = firstNumber(account, [
    ['chatgptLocal', 'codex', 'http_status'],
    ['chatgptLocal', 'auth', 'http_status'],
    ['extra', 'chatgpt_local', 'codex', 'http_status'],
    ['extra', 'chatgpt_local', 'auth', 'http_status'],
  ]) ?? firstNumber({ auth, codex }, [['codex', 'http_status'], ['auth', 'http_status']])
  return value && value > 0 ? Math.round(value) : null
}

function getIssue(auth: any, codex: any): { message: string; type: 'error' | 'warning' } | null {
  const authState = String(auth?.state || '').toLowerCase()
  const codexState = String(codex?.state || '').toLowerCase()
  const authProblem = authState && authState !== 'access_token_valid' && authState !== 'unknown'
  const codexProblem = codexState && !['usable', 'not_checked'].includes(codexState)
  if (!authProblem && !codexProblem) return null

  const issue = codexProblem ? codex : auth
  const meta = codexProblem ? CODEX_META[codexState] : AUTH_META[authState]
  const detail = String(issue?.error_code || issue?.message || '').trim()
  const parsedDetail = parseJsonObject(detail)
  const conciseDetail = String(parsedDetail.error_code || parsedDetail.code || parsedDetail.message || '').trim()
  return {
    message: conciseDetail || (detail.startsWith('{') ? (meta?.label || '账号状态需要关注') : detail) || meta?.label || '账号状态需要关注',
    type: (codexState === 'quota_exhausted' || codexState === 'payment_required' || authState === 'probe_failed') ? 'warning' : 'error',
  }
}

function Metric({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="account-card__metric">
      <span className="account-card__metric-label">{label}</span>
      <strong className={accent ? 'account-card__metric-value account-card__metric-value--accent' : 'account-card__metric-value'}>
        {value}
      </strong>
    </div>
  )
}

function SecretLine({ label, value, onCopy, icon }: { label: string; value: string; onCopy: (value: string) => void; icon: ReactNode }) {
  if (!value) return null
  return (
    <div className="account-card__secret-line">
      <span className="account-card__field-label">{icon}{label}</span>
      <span className="account-card__secret-value" title="已隐藏，点击复制" aria-hidden="true">{maskSecret(value)}</span>
      <span
        className="account-card__secret-accessible"
        aria-hidden="true"
        style={{ display: 'inline-block' }}
      >
        {value}
      </span>
      <Tooltip title={`复制${label}`}>
        <Button
          type="text"
          size="small"
          className="account-card__icon-button"
          aria-label={`复制${label}`}
          icon={<CopyOutlined />}
          onClick={() => onCopy(value)}
        />
      </Tooltip>
    </div>
  )
}

export function AccountCard({
  account,
  platform,
  selected,
  onSelect,
  onCopy,
  onOpenDetails,
  onDelete,
  onPhoneVerification,
  canPhoneVerification = false,
  moreAction,
  codex2apiSyncTitle,
}: AccountCardProps) {
  const extra = parseExtra(account)
  const local = account?.chatgptLocal || extra.chatgpt_local || {}
  const auth = local.auth || {}
  const subscription = local.subscription || {}
  const codex = local.codex || {}
  const authPayload = parseJsonObject(auth.message)
  const codexPayload = parseJsonObject(codex.message)
  const primaryWindow = codexPayload.rate_limit?.primary_window || codexPayload.rateLimit?.primary_window || {}
  const assignment = account?.assignment || {}
  const quotaWindow = account?.quota?.['7d'] ? '7d' : account?.quota?.['monthly'] ? 'monthly' : ''
  const quota = quotaWindow ? account.quota[quotaWindow] : {}
  const accountStatus = statusMeta(account?.status)
  const subscriptionPlan = String(subscription.plan || '').trim().toLowerCase()
  const plan = planMeta(
    (!subscriptionPlan || subscriptionPlan === 'unknown')
      ? codexPayload.plan_type
        || codexPayload.planType
        || extra.plan
        || extra.subscription_plan
      : subscription.plan,
  )
  const email = String(account?.email || `账号 #${account?.id ?? '—'}`)
  const userId = String(
    account?.user_id
      || subscription.chatgpt_account_id
      || codex.chatgpt_account_id
      || codexPayload.account_id
      || authPayload.id
      || extra.chatgpt_user_id
      || extra.user_id
      || '',
  ).trim()
  const workspaceName = String(
    subscription.workspace_name
      || subscription.workspaceName
      || authPayload.orgs?.data?.[0]?.name
      || extra.workspace_name
      || extra.workspaceName
      || '工作区未读取',
  )
  const workspacePlan = String(
    subscription.workspace_plan_type
      || subscription.workspacePlanType
      || codexPayload.workspace_plan_type
      || codexPayload.workspacePlanType
      || extra.workspace_plan_type
      || '',
  ).trim()
  const provider = providerLabel(
    firstValue(account, [
      ['extra', 'mailbox_login_context', 'provider'],
      ['extra', 'provider'],
      ['extra', 'mail_provider'],
      ['login_method'],
    ]),
    platform,
  )
  const statusCode = platform === 'chatgpt' ? getStatusCode(account, auth, codex) : null
  const issue = platform === 'chatgpt' ? getIssue(auth, codex) : null
  const usagePercent = firstNumber(quota, [['usage_percent']])
    ?? firstNumber(primaryWindow, [['used_percent'], ['usage_percent']])
  const usageWindowTitle = quotaWindow === 'monthly'
    ? '本月使用'
    : quotaWindow === '7d'
      ? '本周使用'
      : usagePercent !== null
        ? '当前窗口'
        : '本周使用'
  const usageWindowLabel = quotaWindow === 'monthly'
    ? '月度剩余'
    : quotaWindow === '7d'
      ? '7天剩余'
      : usagePercent !== null
        ? '当前窗口剩余'
        : '7天剩余'
  const remainingPercent = usagePercent === null ? null : clampPercent(100 - usagePercent)
  const billed = firstValue(quota, [['continuous_billed_usd'], ['billed_usd']])
  const remaining = firstValue(quota, [['continuous_remaining_usd'], ['remaining_usd']])
  const requestCount = firstNumber(account, [
    ['request_count'],
    ['extra', 'request_count'],
    ['extra', 'usage', 'weekly', 'requests'],
    ['extra', 'usage_detail', 'request_count'],
    ['extra', 'usage_detail', 'requests'],
  ]) ?? firstNumber(codexPayload, [['requests'], ['request_count'], ['rate_limit', 'primary_window', 'requests']])
  const bandwidth = firstNumber(account, [
    ['bytes_used'],
    ['extra', 'bytes_used'],
    ['extra', 'usage', 'weekly', 'bytes'],
    ['extra', 'usage_detail', 'bytes'],
    ['extra', 'usage_detail', 'bandwidth'],
  ]) ?? firstNumber(codexPayload, [['bytes'], ['bytes_used'], ['usage_bytes']])
  const actualPrice = firstValue(account, [
    ['actual_price'],
    ['extra', 'actual_price'],
    ['extra', 'price'],
    ['extra', 'customer_price'],
  ])
  const rate = firstValue(account, [
    ['rate'],
    ['extra', 'rate'],
    ['extra', 'multiplier'],
  ])
  const note = String(firstValue(account, [
    ['note'],
    ['notes'],
    ['extra', 'note'],
    ['extra', 'notes'],
  ]) || '').trim()
  const resetCount = firstNumber(account, [
    ['reset_count'],
    ['extra', 'reset_count'],
    ['extra', 'mfa_reset_count'],
  ])
  const activeUntil = firstValue(local, [
    ['subscription', 'subscription_active_until'],
    ['subscription', 'active_until'],
  ]) || firstValue(account, [['expires_at'], ['extra', 'expires_at'], ['extra', 'valid_until']])
  const quotaResetAt = firstValue(quota, [['reset_at']])
    || firstValue(primaryWindow, [['reset_at']])
    || firstValue(codexPayload, [['reset_at']])
  const validityDays = firstNumber(account, [
    ['validity_days'],
    ['valid_days'],
    ['extra', 'validity_days'],
    ['extra', 'valid_days'],
  ])
  const checkedAt = firstValue(local, [['checked_at'], ['codex', 'checked_at'], ['auth', 'checked_at']])
  const codexSync = account?.codex2apiSync || {}
  const authStatus = AUTH_META[String(auth?.state || '').toLowerCase()] || { color: 'default', label: '未探测' }
  const codexStatus = CODEX_META[String(codex?.state || '').toLowerCase()] || { color: 'default', label: '未探测' }
  const cpaStatus = syncMeta(account?.cpaSync)
  const sub2apiStatus = syncMeta(account?.sub2apiSync)
  const codex2apiStatus = syncMeta(account?.codex2apiSync)
  const cliproxyStatus = syncMeta(account?.cliproxySync)
  const assignmentStatus = assignment.state === 'active'
    ? { color: 'success', label: '生效中' }
    : assignment.state === 'draining'
      ? { color: 'processing', label: '排空中' }
      : assignment.state === 'standby'
        ? { color: 'default', label: '备用' }
        : { color: 'default', label: '未分配' }
  const openDetails = () => onOpenDetails(account)
  const handleCardKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.currentTarget !== event.target) return
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      openDetails()
    }
  }
  const refreshToken = String(
    extra.refresh_token
      || extra.refreshToken
      || account?.refresh_token
      || '',
  ).trim()
  const password = String(account?.password || '').trim()
  const syncStates = [
    account?.cpaSync,
    account?.sub2apiSync,
    account?.codex2apiSync,
    account?.cliproxySync,
  ].filter((value) => value && Object.keys(value).length > 0)
  const createdAt = formatDate(account?.created_at, true)
  const token = {
    '--account-card-primary': statusCode && statusCode >= 400 ? '#f97373' : '#4ade80',
  } as CSSProperties

  return (
    <article
      className={`account-card${selected ? ' account-card--selected' : ''}`}
      data-testid="account-card"
      data-account-id={account?.id}
      style={token}
      tabIndex={0}
      aria-label={`${email}，按回车查看详情`}
      onDoubleClick={openDetails}
      onKeyDown={handleCardKeyDown}
    >
      <header className="account-card__header">
        <Checkbox
          checked={selected}
          aria-label={`选择 ${email}`}
          onChange={(event) => onSelect(account?.id, event.target.checked)}
        />
        <Avatar className="account-card__avatar" shape="square" icon={<MailOutlined />}>
          {email.slice(0, 1).toUpperCase()}
        </Avatar>
        <div className="account-card__identity">
          <div className="account-card__email-row">
            <Tooltip title={email}>
              <Text className="account-card__email" ellipsis={{ tooltip: false }}>{maskEmail(email)}</Text>
            </Tooltip>
            <span className="account-card__email-accessible">{email}</span>
            <Tooltip title="复制邮箱">
              <Button
                type="text"
                size="small"
                className="account-card__icon-button"
                aria-label="复制邮箱"
                icon={<CopyOutlined />}
                onClick={() => onCopy(email)}
              />
            </Tooltip>
          </div>
          <div className="account-card__tag-row">
            <Tag color={accountStatus.color}>{accountStatus.label}</Tag>
            {platform === 'chatgpt' ? <Tag color={plan.color}>{plan.label}</Tag> : null}
            {platform === 'chatgpt' && workspacePlan && planMeta(workspacePlan).label !== plan.label ? <Tag color="blue">{planMeta(workspacePlan).label}</Tag> : null}
            {statusCode ? <Tag color={statusCode >= 400 ? 'error' : 'success'}>HTTP {statusCode}</Tag> : null}
            {codexSync.uploaded ? <Tag color="success" title={codex2apiSyncTitle}>API 已同步</Tag> : null}
            {resetCount !== null ? <Tag color="default">重置 {resetCount}</Tag> : null}
          </div>
        </div>
        <div className="account-card__header-actions">{moreAction}</div>
      </header>

      <div className="account-card__identity-grid">
        <div className="account-card__identity-item">
          <span className="account-card__field-label"><TeamOutlined />工作区</span>
          <Text ellipsis={{ tooltip: workspaceName }}>{workspaceName}</Text>
        </div>
        <div className="account-card__identity-item">
          <span className="account-card__field-label"><LinkOutlined />当前目标</span>
          <Text ellipsis={{ tooltip: assignment.target_name || (assignment.target_id ? `目标 #${assignment.target_id}` : '未分配目标') }}>
            {assignment.target_name || (assignment.target_id ? `目标 #${assignment.target_id}` : '未分配目标')}
          </Text>
        </div>
        <div className="account-card__identity-item">
          <span className="account-card__field-label"><TeamOutlined />号池</span>
          <Text ellipsis={{ tooltip: assignment.pool_name || assignment.pool_id || '未分配号池' }}>
            {assignment.pool_name || assignment.pool_id || '未分配号池'}
          </Text>
        </div>
        <div className="account-card__identity-item">
          <span className="account-card__field-label"><LoginOutlined />登录方式</span>
          <Text>{provider}</Text>
        </div>
        <div className="account-card__identity-item">
          <span className="account-card__field-label"><UserOutlined />用户 ID</span>
          <Text ellipsis={{ tooltip: userId || `账号 #${account?.id ?? '—'}` }}>{userId || `账号 #${account?.id ?? '—'}`}</Text>
        </div>
        <div className="account-card__identity-item">
          <span className="account-card__field-label"><LinkOutlined />同步状态</span>
          <Text>{syncStates.length ? `${syncStates.length} 个目标已记录` : '尚未同步'}</Text>
        </div>
      </div>

      <div className="account-card__status-row" aria-label="账号运行状态">
        <Tag color={authStatus.color}>认证 {authStatus.label}</Tag>
        {platform === 'chatgpt' ? <Tag color={codexStatus.color}>Codex {codexStatus.label}</Tag> : null}
        {platform === 'chatgpt' ? <Tag color={assignmentStatus.color}>号池 {assignmentStatus.label}</Tag> : null}
        {platform === 'chatgpt' && Object.keys(account?.cpaSync || {}).length > 0 ? <Tag color={cpaStatus.color}>CPA {cpaStatus.label}</Tag> : null}
        {platform === 'chatgpt' && Object.keys(account?.sub2apiSync || {}).length > 0 ? <Tag color={sub2apiStatus.color}>Sub2API {sub2apiStatus.label}</Tag> : null}
        {platform === 'chatgpt' && Object.keys(account?.codex2apiSync || {}).length > 0 ? <Tag color={codex2apiStatus.color}>Codex2API {codex2apiStatus.label}</Tag> : null}
        {platform === 'chatgpt' && Object.keys(account?.cliproxySync || {}).length > 0 ? <Tag color={cliproxyStatus.color}>CLIProxy {cliproxyStatus.label}</Tag> : null}
      </div>

      {issue ? (
        <Alert
          className="account-card__issue"
          type={issue.type}
          showIcon
          icon={issue.type === 'warning' ? <WarningOutlined /> : undefined}
          message={issue.message}
          action={<Button type="link" size="small" onClick={() => onOpenDetails(account)}>查看详情</Button>}
        />
      ) : null}

      <div className="account-card__credentials">
        <SecretLine label="密码" value={password} onCopy={onCopy} icon={<KeyOutlined />} />
        <SecretLine label="Refresh Token" value={refreshToken} onCopy={onCopy} icon={<SyncOutlined />} />
      </div>

      {note ? (
        <div className="account-card__note">
          <FileTextOutlined />
          <span className="account-card__note-label">备注</span>
          <Text ellipsis={{ tooltip: note }}>{note}</Text>
        </div>
      ) : null}

      {platform === 'chatgpt' ? (
        <section className="account-card__quota" aria-label={usageWindowTitle}>
          <div className="account-card__section-heading">
            <span><ClockCircleOutlined />{usageWindowTitle}</span>
            <span className="account-card__window-label">{usageWindowLabel}</span>
            <Text type="secondary">{quotaResetAt ? `重置 ${formatDate(quotaResetAt, true)}` : '重置时间未读取'}</Text>
          </div>
          {remainingPercent !== null ? (
            <>
              <div className="account-card__quota-headline">
                <strong>{remainingPercent}%</strong>
                <span>剩余窗口</span>
                {quota.fresh === false ? <Tag color="warning">数据过期</Tag> : <Tag color="success">数据有效</Tag>}
              </div>
              <Progress
                percent={remainingPercent}
                showInfo={false}
                strokeColor={remainingPercent <= 20 ? '#f97373' : '#32c36c'}
                trailColor="var(--account-card-progress-trail)"
                size={{ height: 8 }}
              />
            </>
          ) : (
            <div className="account-card__quota-empty">尚无额度快照</div>
          )}
          <div className="account-card__metrics-grid">
            <Metric label="请求数" value={formatNumber(requestCount)} />
            <Metric label="流量" value={formatBytes(bandwidth)} />
            <Metric label="已计费" value={formatMoney(billed)} />
            <Metric label="剩余估算" value={formatMoney(remaining)} accent />
          </div>
        </section>
      ) : (
        <section className="account-card__quota account-card__quota--generic" aria-label="账号信息">
          <div className="account-card__section-heading"><span><FileTextOutlined />账号信息</span></div>
          <div className="account-card__metrics-grid">
            <Metric label="地区" value={String(account?.region || '—')} />
            <Metric label="试用链接" value={account?.cashier_url ? '已配置' : '—'} />
            <Metric label="CPA" value={account?.cpaSync?.uploaded ? '已上传' : '—'} />
            <Metric label="状态" value={accountStatus.label} />
          </div>
        </section>
      )}

      {(actualPrice !== undefined || rate !== undefined || activeUntil || validityDays !== null || checkedAt) ? (
        <section className="account-card__commercial" aria-label="有效期与价格">
          {activeUntil || validityDays !== null ? (
            <div className="account-card__commercial-row">
              <span><CalendarOutlined />有效期{validityDays !== null ? ` ${validityDays}天` : '至'}</span>
              <strong>{formatDate(activeUntil)}</strong>
            </div>
          ) : null}
          {actualPrice !== undefined ? (
            <div className="account-card__commercial-row">
              <span><DollarOutlined />实际价格</span>
              <strong>{formatCny(actualPrice)}</strong>
            </div>
          ) : null}
          {rate !== undefined ? (
            <div className="account-card__commercial-row">
              <span><CheckCircleOutlined />倍率</span>
              <strong>{String(rate)}×</strong>
            </div>
          ) : null}
          {checkedAt ? (
            <div className="account-card__commercial-row">
              <span><ClockCircleOutlined />最近检查</span>
              <strong>{formatDate(checkedAt, true)}</strong>
            </div>
          ) : null}
        </section>
      ) : null}

      <Divider className="account-card__divider" />

      <footer className="account-card__footer">
        <span className="account-card__created"><CalendarOutlined />{createdAt}</span>
        <Space size={2} className="account-card__actions">
          {canPhoneVerification && onPhoneVerification ? (
            <Button aria-label="接码" type="text" size="small" icon={<LoginOutlined />} onClick={() => onPhoneVerification(account)}>
              接码
            </Button>
          ) : null}
          <Button aria-label="详情" type="text" size="small" icon={<EyeOutlined />} onClick={() => onOpenDetails(account)}>详情</Button>
          {account?.cashier_url ? (
            <>
              <Tooltip title="复制试用链接">
                <Button type="text" size="small" aria-label="复制试用链接" icon={<CopyOutlined />} onClick={() => onCopy(account.cashier_url)} />
              </Tooltip>
              <Tooltip title="打开试用链接">
                <Button type="text" size="small" aria-label="打开试用链接" icon={<LinkOutlined />} onClick={() => window.open(account.cashier_url, '_blank', 'noopener,noreferrer')} />
              </Tooltip>
            </>
          ) : null}
          <Popconfirm
            title="确认删除该账号吗？"
            onConfirm={() => onDelete(account?.id)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button aria-label="删除" type="text" size="small" danger icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      </footer>
    </article>
  )
}

export default AccountCard
