type ConfigRecord = Record<string, unknown>

export type ChatGPTSmsMode = 'api_fallback_pool' | 'pool' | 'none'

type ExistingAccountLoginTaskInput = {
  count: number
  concurrency: number
  registerDelaySeconds: number
  executorType: string
  captchaSolver: string
  bindPhoneAndGetRefreshToken: boolean
  rotateMfa?: boolean
  smsMode?: ChatGPTSmsMode
  leadbeeApi?: boolean
  useSmsPool?: boolean
  leadbeeCodes: string[]
  mailProviderPlan?: Array<'microsoft' | 'applemail'>
  config: ConfigRecord
}

const LOGIN_CONFIG_KEYS = [
  'mail_provider',
  'mail_import_source',
  'mailbox_otp_timeout_seconds',
  'email_otp_timeout_seconds',
  'otp_timeout',
  'outlook_backend',
  'outlook_imap_server',
  'outlook_imap_port',
  'outlook_token_endpoint',
  'outlook_graph_api_base',
  'applemail_base_url',
  'applemail_pool_dir',
  'applemail_pool_file',
  'applemail_mailboxes',
] as const

function compactConfig(config: ConfigRecord) {
  const result: ConfigRecord = {}
  for (const key of LOGIN_CONFIG_KEYS) {
    const value = config[key]
    if (value !== undefined && value !== null && value !== '') {
      result[key] = value
    }
  }
  return result
}

export function parseLeadBeeCodes(value: unknown): string[] {
  return String(value || '')
    .split(/\r?\n/)
    .map(code => code.trim())
    .filter(Boolean)
}

export function canStartChatGPTPhoneVerification(account: unknown): boolean {
  const record = account && typeof account === 'object'
    ? account as Record<string, unknown>
    : {}
  const email = String(record.email || '').trim()
  const extra = record.extra && typeof record.extra === 'object'
    ? record.extra as Record<string, unknown>
    : {}
  const accessToken = String(extra.access_token || record.token || '').trim()
  const refreshToken = String(extra.refresh_token || extra.refreshToken || '').trim()
  const mailboxContext = extra.mailbox_login_context
  const hasMailboxContext = Boolean(
    mailboxContext
      && typeof mailboxContext === 'object'
      && Object.keys(mailboxContext as Record<string, unknown>).length > 0,
  )
  return Boolean(email && accessToken && !refreshToken && hasMailboxContext)
}

export function resolveMailboxSnapshotType(config: ConfigRecord): 'microsoft' | 'applemail' | null {
  const provider = String(config.mail_provider || '').trim().toLowerCase()
  if (provider === 'microsoft' || provider === 'outlook') return 'microsoft'
  if (provider === 'applemail') return 'applemail'
  if (provider === 'mail_import') {
    return String(config.mail_import_source || '').trim().toLowerCase() === 'applemail'
      ? 'applemail'
      : 'microsoft'
  }
  return null
}

export function buildExistingAccountLoginTaskPayload(input: ExistingAccountLoginTaskInput) {
  const explicitSmsMode = input.smsMode
  const bindPhoneAndGetRefreshToken = explicitSmsMode
    ? explicitSmsMode !== 'none'
    : Boolean(input.bindPhoneAndGetRefreshToken)
  const leadbeeApi = bindPhoneAndGetRefreshToken && Boolean(input.leadbeeApi)
  const useSmsPool = bindPhoneAndGetRefreshToken && !leadbeeApi && Boolean(input.useSmsPool)
  const extra: ConfigRecord = {
    ...compactConfig(input.config),
    chatgpt_registration_mode: 'refresh_token',
    chatgpt_has_refresh_token_solution: true,
    chatgpt_existing_account_login_only: true,
    chatgpt_existing_account_login_stage: bindPhoneAndGetRefreshToken
      ? 'access_token'
      : 'refresh_token',
    chatgpt_existing_account_allow_phone_verification: false,
    chatgpt_existing_account_rotate_mfa: input.rotateMfa !== false,
  }
  if (explicitSmsMode) {
    extra.chatgpt_existing_account_sms_mode = explicitSmsMode
  } else {
    extra.chatgpt_existing_account_bind_phone_and_get_rt = bindPhoneAndGetRefreshToken
    if (leadbeeApi) {
      extra.chatgpt_existing_account_leadbee_api = true
    } else {
      extra.chatgpt_existing_account_use_sms_pool = useSmsPool
    }
    if (bindPhoneAndGetRefreshToken && !leadbeeApi && !useSmsPool) {
      extra.chatgpt_existing_account_leadbee_codes = input.leadbeeCodes
        .map(code => String(code || '').trim())
        .filter(Boolean)
    }
  }
  const mailProviderPlan = (input.mailProviderPlan || [])
    .map(provider => String(provider || '').trim().toLowerCase())
    .filter(provider => provider === 'microsoft' || provider === 'applemail')
  if (mailProviderPlan.length === Number(input.count)) {
    extra.chatgpt_existing_account_mail_provider_plan = mailProviderPlan
  }

  return {
    platform: 'chatgpt',
    count: Number(input.count),
    concurrency: Number(input.concurrency),
    register_delay_seconds: Number(input.registerDelaySeconds || 0),
    executor_type: input.executorType,
    captcha_solver: input.captchaSolver,
    proxy: null,
    extra,
  }
}
