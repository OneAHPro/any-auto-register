import { useCallback, useEffect, useRef, useState } from 'react'
import { App, Card, Form, Input, Select, Button, message, Tabs, Space, Tag, Typography, QRCode, Switch, Alert } from 'antd'
import {
  SaveOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  MailOutlined,
  SafetyOutlined,
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
  PlusOutlined,
  LockOutlined,
} from '@ant-design/icons'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import MailImportPanel from '@/components/settings/MailImportPanel'
import ChatGPTAutoReloginSection from '@/components/settings/ChatGPTAutoReloginSection'
import { apiFetch } from '@/lib/utils'
import { ConsolePageHeader } from '@/components/console/ConsolePageHeader'
import './management-workspace.css'

function resolveEffectiveMailProvider(mailProvider: string, mailImportSource: string) {
  if (mailProvider !== 'mail_import') return mailProvider
  return mailImportSource === 'applemail' ? 'applemail' : 'microsoft'
}

const SELECT_FIELDS: Record<string, { label: string; value: string }[]> = {
  mail_provider: [
    { label: 'LuckMail（订单接码 / 已购邮箱）', value: 'luckmail' },
    { label: '邮箱导入', value: 'mail_import' },
    { label: 'Laoudo（固定邮箱）', value: 'laoudo' },
    { label: 'TempMail.lol（自动生成）', value: 'tempmail_lol' },
    { label: 'SkyMail（CloudMail 接口）', value: 'skymail' },
    { label: 'CloudMail（genToken 口令模式）', value: 'cloudmail' },
    { label: 'DuckMail（自动生成）', value: 'duckmail' },
    { label: 'MoeMail (sall.cc)', value: 'moemail' },
    { label: 'YYDS Mail / MaliAPI', value: 'maliapi' },
    { label: 'GPTMail', value: 'gptmail' },
    { label: 'OpenTrashMail', value: 'opentrashmail' },
    { label: 'Freemail（自建 CF Worker）', value: 'freemail' },
    { label: 'CF Worker（自建域名）', value: 'cfworker' },
  ],
  maliapi_auto_domain_strategy: [
    { label: 'balanced', value: 'balanced' },
    { label: 'prefer_owned', value: 'prefer_owned' },
    { label: 'prefer_public', value: 'prefer_public' },
  ],
  default_executor: [
    { label: 'API 协议（无浏览器）', value: 'protocol' },
    { label: '无头浏览器', value: 'headless' },
    { label: '有头浏览器', value: 'headed' },
  ],
  default_captcha_solver: [
    { label: 'YesCaptcha', value: 'yescaptcha' },
    { label: '本地 Solver (Camoufox)', value: 'local_solver' },
    { label: '手动', value: 'manual' },
  ],
  outlook_backend: [
    { label: 'Graph（默认）', value: 'graph' },
    { label: 'IMAP', value: 'imap' },
  ],
  luckmail_email_type: [
    { label: '自动 / 留空', value: '' },
    { label: '微软邮箱 - Graph', value: 'ms_graph' },
    { label: '微软邮箱 - IMAP', value: 'ms_imap' },
    { label: '自建邮箱', value: 'self_built' },
  ],

}

const TAB_ITEMS = [
  { key: 'recovery', label: '恢复与通知', icon: <SyncOutlined />, sections: [] },
  {
    key: 'codex2api',
    label: '连接与联动',
    icon: <ApiOutlined />,
    sections: [
      {
        title: '默认连接',
        desc: '已有账号登录后使用的默认上传连接；多个实例在实例管理中维护',
        fields: [
          { key: 'codex2api_enabled', label: '启用自动上传', type: 'boolean' },
          { key: 'codex2api_api_url', label: 'API URL', placeholder: 'http://127.0.0.1:8080' },
          { key: 'codex2api_admin_key', label: 'Admin Key', secret: true },
        ],
      },
      {
        title: '删除联动',
        fields: [
          {
            key: 'codex2api_delete_on_account_remove_enabled',
            label: '删除本地 ChatGPT 账号时，同步删除 Codex2API 认证',
            type: 'boolean',
            help: '自动清理、单个删除和批量删除均生效；远端删除失败时保留本地账号。',
          },
        ],
      },
      {
        title: '账号池观测与计划',
        desc: '定时采集目标健康度和额度，只生成预览计划；扩容、缩容均需在调度页人工确认。',
        fields: [
          {
            key: 'codex2api_scheduler_enabled',
            label: '启用额度采集与预览计划',
            type: 'boolean',
            help: '启用后不会自动迁移账号。',
          },
          { key: 'codex2api_scheduler_interval_minutes', label: '计划周期（分钟）', placeholder: '15' },
          { key: 'codex2api_scheduler_min_lease_hours', label: '最小租约（小时）', placeholder: '6' },
          { key: 'codex2api_scheduler_scale_up_threshold_usd', label: '扩容安全余量（美元）', placeholder: '0.00' },
          { key: 'codex2api_scheduler_scale_down_utilization_percent', label: '缩容利用率阈值（%）', placeholder: '60' },
          { key: 'codex2api_scheduler_quota_freshness_minutes', label: '额度有效期（分钟）', placeholder: '15' },
        ],
      },
    ],
  },
  {
    key: 'mailbox',
    label: '邮箱取码',
    icon: <MailOutlined />,
    sections: [
      {
        title: '默认邮箱服务',
        desc: '选择已有账号登录时的收件来源',
        fields: [
          { key: 'mail_provider', label: '邮箱服务', type: 'select' },
          { key: 'mailbox_otp_timeout_seconds', label: '邮箱验证码等待秒数', placeholder: '例如 60 / 90 / 120' },
        ],
      },
      {
        title: 'Laoudo',
        desc: '固定邮箱，手动配置',
        fields: [
          { key: 'laoudo_email', label: '邮箱地址', placeholder: 'xxx@laoudo.com' },
          { key: 'laoudo_account_id', label: 'Account ID', placeholder: '563' },
          { key: 'laoudo_auth', label: 'JWT Token', placeholder: 'eyJ...', secret: true },
        ],
      },
      {
        title: 'Freemail',
        desc: '基于 Cloudflare Worker 的自建邮箱，支持管理员令牌或账号密码认证',
        fields: [
          { key: 'freemail_api_url', label: 'API URL', placeholder: 'https://mail.example.com' },
          { key: 'freemail_admin_token', label: '管理员令牌', secret: true },
          { key: 'freemail_username', label: '用户名（可选）' },
          { key: 'freemail_password', label: '密码（可选）', secret: true },
          { key: 'freemail_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
        ],
      },
      {
        title: 'MoeMail',
        desc: 'MoeMail 邮箱服务连接',
        fields: [
          { key: 'moemail_api_url', label: 'API URL', placeholder: 'https://sall.cc' },
          { key: 'moemail_api_key', label: 'API Key', secret: true },
        ],
      },
      {
        title: 'SkyMail',
        desc: 'CloudMail 兼容接口（addUser / emailList）',
        fields: [
          { key: 'skymail_api_base', label: 'API Base', placeholder: 'https://api.skymail.ink' },
          { key: 'skymail_token', label: 'Authorization Token', secret: true },
          { key: 'skymail_domain', label: '邮箱域名', placeholder: 'mail.example.com' },
        ],
      },
      {
        title: 'CloudMail',
        desc: 'CloudMail 口令模式（genToken + emailList）',
        fields: [
          { key: 'cloudmail_api_base', label: 'API Base', placeholder: 'https://cloudmail.example.com' },
          { key: 'cloudmail_admin_email', label: '管理员邮箱（可选）', placeholder: 'admin@example.com' },
          { key: 'cloudmail_admin_password', label: '管理员密码', secret: true },
          { key: 'cloudmail_domain', label: '邮箱域名（可选）', placeholder: 'mail.example.com,mail2.example.com' },
          { key: 'cloudmail_subdomain', label: '子域名（可选）', placeholder: 'pool-a' },
          { key: 'cloudmail_timeout', label: '请求超时秒数', placeholder: '30' },
        ],
      },
      {
        title: 'YYDS Mail / MaliAPI',
        desc: '基于 API Key 创建临时邮箱并轮询收件箱消息',
        fields: [
          { key: 'maliapi_base_url', label: 'API URL', placeholder: 'https://maliapi.215.im/v1' },
          { key: 'maliapi_api_key', label: 'API Key', secret: true },
          { key: 'maliapi_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
          { key: 'maliapi_auto_domain_strategy', label: '自动域名策略', type: 'select' },
        ],
      },
      {
        title: '邮箱导入（微软 / Outlook / Hotmail）',
        desc: '使用本地导入的微软账号池，运行时支持 Graph / IMAP 轮询（默认 Graph）',
        fields: [
          { key: 'outlook_backend', label: '微软收信方式', type: 'select' },
        ],
      },
      {
        title: '邮箱导入（iCloud MFA / AppleMail / 小苹果）',
        desc: 'iCloud 支持“邮箱----密码----MFA 秘钥”直连取码；原 refresh_token + client_id 小苹果格式继续兼容',
        fields: [
          { key: 'applemail_base_url', label: 'API URL', placeholder: 'https://www.appleemail.top' },
          { key: 'applemail_pool_dir', label: '邮箱池目录', placeholder: 'mail' },
          { key: 'applemail_pool_file', label: '当前邮箱池文件（可选）', placeholder: '留空则自动读取目录中最新文件' },
          { key: 'applemail_mailboxes', label: '轮询文件夹', placeholder: 'INBOX,Junk' },
        ],
      },
      {
        title: 'GPTMail',
        desc: '基于 GPTMail API 生成临时邮箱并轮询邮件；若已知本站可用域名，也可本地拼装随机地址',
        fields: [
          { key: 'gptmail_base_url', label: 'API URL', placeholder: 'https://mail.chatgpt.org.uk' },
          { key: 'gptmail_api_key', label: 'API Key', secret: true, placeholder: 'gpt-test' },
          { key: 'gptmail_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
        ],
      },
      {
        title: 'OpenTrashMail',
        desc: '对接 opentrashmail 服务；可直接轮询 /json/<email>，也支持已知域名时本地拼装随机地址',
        fields: [
          { key: 'opentrashmail_api_url', label: 'API URL', placeholder: 'http://mail.example.com:8085' },
          { key: 'opentrashmail_domain', label: '邮箱域名（可选）', placeholder: 'xiyoufm.com' },
          { key: 'opentrashmail_password', label: '站点密码（可选）', secret: true, placeholder: '启用 PASSWORD 时填写' },
        ],
      },
      {
        title: 'TempMail.lol',
        desc: '自动生成邮箱，无需配置，需要代理访问（CN IP 被封）',
        fields: [],
      },
      {
        title: 'DuckMail',
        desc: '自动生成邮箱，随机创建账号',
        fields: [
          { key: 'duckmail_api_url', label: 'Web URL', placeholder: 'https://www.duckmail.sbs' },
          { key: 'duckmail_provider_url', label: 'Provider URL', placeholder: 'https://api.duckmail.sbs' },
          { key: 'duckmail_bearer', label: 'Bearer Token', placeholder: 'kevin273945', secret: true },
          { key: 'duckmail_domain', label: '自定义域名', placeholder: '留空则从 Provider URL 推导' },
          { key: 'duckmail_api_key', label: 'API Key（私有域名）', placeholder: 'dk_xxx（domain.duckmail.sbs 获取）', secret: true },
        ],
      },
      {
        title: 'CF Worker 自建邮箱',
        desc: '基于 Cloudflare Worker 的自建临时邮箱服务',
        fields: [
          { key: 'cfworker_api_url', label: 'API URL', placeholder: 'https://apimail.example.com' },
          { key: 'cfworker_admin_token', label: '管理员 Token', secret: true },
          { key: 'cfworker_custom_auth', label: '站点密码', secret: true },
          { key: 'cfworker_subdomain', label: '固定子域名', placeholder: 'mail / pool-a' },
          { key: 'email_domain_rule_enabled', label: '启用域名规则', type: 'boolean' },
          { key: 'email_domain_level_count', label: '域名级数（N 级）', placeholder: '例如 2 / 3 / 4' },
          { key: 'cfworker_random_subdomain', label: '随机子域名', type: 'boolean' },
          { key: 'cfworker_random_name_subdomain', label: '随机姓名子域名', type: 'boolean' },
          { key: 'cfworker_fingerprint', label: 'Fingerprint', placeholder: '6703363b...' },
        ],
      },
      {
        title: 'LuckMail',
        desc: 'ChatGPT 走购买邮箱，其他平台继续走订单接码老逻辑',
        fields: [
          { key: 'luckmail_base_url', label: '平台地址', placeholder: 'https://mails.luckyous.com' },
          { key: 'luckmail_api_key', label: 'API Key', secret: true },
          { key: 'luckmail_email_type', label: '邮箱类型（可选）', type: 'select' },
          { key: 'luckmail_domain', label: '邮箱域名（可选）', placeholder: 'outlook.com / gmail.com' },
        ],
      },
    ],
  },
  {
    key: 'runtime', label: '运行配置', icon: <ApiOutlined />,
    sections: [
      { title: '登录执行方式', desc: '选择已有账号登录使用的运行方式。', fields: [
        { key: 'default_executor', label: '执行器类型', type: 'select' },
      ] },
      { title: '人机验证', desc: '登录流程遇到验证时使用的服务。', fields: [
        { key: 'default_captcha_solver', label: '默认服务', type: 'select' },
        { key: 'yescaptcha_key', label: 'YesCaptcha Key', secret: true },
      ] },
    ],
  },
  { key: 'security', label: '登录保护', icon: <SafetyOutlined />, sections: [] },
]

interface FieldConfig {
  key: string
  label: string
  placeholder?: string
  help?: string
  type?: 'select' | 'input' | 'boolean'
  secret?: boolean
}

interface SectionConfig {
  title: string
  desc?: string
  fields: FieldConfig[]
}

interface TabConfig {
  key: string
  label: string
  icon: React.ReactNode
  sections: SectionConfig[]
}

type SettingsPageMode = 'settings' | 'codex2api' | 'mail-import'

interface SettingsProps {
  page?: SettingsPageMode
}

const MAILBOX_SECTION_FIELD_KEY_BY_PROVIDER: Record<string, string> = {
  laoudo: 'laoudo_email',
  freemail: 'freemail_api_url',
  moemail: 'moemail_api_url',
  skymail: 'skymail_api_base',
  cloudmail: 'cloudmail_api_base',
  maliapi: 'maliapi_base_url',
  microsoft: 'outlook_backend',
  applemail: 'applemail_base_url',
  gptmail: 'gptmail_base_url',
  opentrashmail: 'opentrashmail_api_url',
  duckmail: 'duckmail_api_url',
  cfworker: 'cfworker_api_url',
  luckmail: 'luckmail_base_url',
}

const MAILBOX_SECTION_INDEX_BY_PROVIDER: Record<string, number> = {
  tempmail_lol: 10,
}

function splitMailboxSections(sections: SectionConfig[], mailProvider: string) {
  const defaultSection = sections[0] || null
  let selectedSection: SectionConfig | null = null

  const byIndex = MAILBOX_SECTION_INDEX_BY_PROVIDER[mailProvider]
  if (Number.isInteger(byIndex)) {
    selectedSection = sections[byIndex] || null
  } else {
    const fieldKey = MAILBOX_SECTION_FIELD_KEY_BY_PROVIDER[mailProvider]
    if (fieldKey) {
      selectedSection = sections.find((section) => section.fields.some((field) => field.key === fieldKey)) || null
    }
  }

  if (selectedSection === defaultSection) {
    selectedSection = null
  }

  const remainingSections = sections.filter((section) => section !== defaultSection && section !== selectedSection)

  return {
    defaultSection,
    selectedSection,
    remainingSections,
  }
}

function normalizeDomainList(input: unknown): string[] {
  const items = Array.isArray(input) ? input : []
  const seen = new Set<string>()
  const domains: string[] = []
  for (const item of items) {
    const domain = String(item || '').trim().toLowerCase().replace(/^@/, '')
    if (!domain || seen.has(domain)) continue
    seen.add(domain)
    domains.push(domain)
  }
  return domains
}

function parseStoredDomainList(value: unknown): string[] {
  if (Array.isArray(value)) return normalizeDomainList(value)
  if (typeof value !== 'string') return []

  const text = value.trim()
  if (!text) return []

  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) {
      return normalizeDomainList(parsed)
    }
  } catch {
    // Fall back to the delimiter parser for legacy plain-text values.
  }

  return normalizeDomainList(
    text
      .split('\n')
      .flatMap((line) => line.split(','))
      .map((item) => item.trim()),
  )
}

function normalizeBoundedInteger(value: unknown, fallback: number, min: number, max: number): number {
  const normalized = String(value ?? '').trim()
  if (!normalized) return fallback
  const parsed = typeof value === 'number' ? value : Number(normalized)
  if (!Number.isInteger(parsed)) return fallback
  return Math.min(max, Math.max(min, parsed))
}

function normalizeBoundedDecimal(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
  precision: number,
): number {
  const normalized = String(value ?? '').trim()
  if (!normalized) return fallback
  const parsed = typeof value === 'number' ? value : Number(normalized)
  if (!Number.isFinite(parsed)) return fallback
  const factor = 10 ** precision
  const rounded = Math.round(parsed * factor) / factor
  return Math.min(max, Math.max(min, rounded))
}

function errorDetail(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim()
  if (typeof error === 'object' && error !== null) {
    const detail = (error as Record<string, unknown>).detail
    if (typeof detail === 'string' && detail.trim()) return detail.trim()
  }
  return fallback
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function ConfigField({ field }: { field: FieldConfig }) {
  const [showSecret, setShowSecret] = useState(false)
  const options = SELECT_FIELDS[field.key]
  const isBooleanField = field.type === 'boolean'
  const helpText = field.help || (
    field.key === 'default_executor'
      ? '用于已有 ChatGPT 账号登录。协议模式适合常规批量任务，浏览器模式用于需要交互的登录。'
      : field.key === 'email_domain_rule_enabled'
      ? '仅 CF Worker 生效：开启后会校验域名级数，以及域名至少包含 2 个字母和 2 个数字。'
      : field.key === 'email_domain_level_count'
      ? '例如 2=example.com，3=a.example.com，4=a.b.example.com。'
      : undefined
  )

  return (
    <Form.Item
      label={field.label}
      name={field.key}
      extra={helpText}
      valuePropName={isBooleanField ? 'checked' : undefined}
    >
      {options ? (
        <Select options={options} style={{ width: '100%' }} />
      ) : isBooleanField ? (
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      ) : field.secret ? (
        <Input.Password
          placeholder={field.placeholder}
          visibilityToggle={{
            visible: !showSecret,
            onVisibleChange: setShowSecret,
          }}
          iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
        />
      ) : (
        <Input placeholder={field.placeholder} />
      )}
    </Form.Item>
  )
}

function ConfigSection({ section }: { section: SectionConfig }) {
  return (
    <section className="console-panel management-setting-section">
      <div className="management-setting-intro"><h2>{section.title}</h2>{section.desc && <p>{section.desc}</p>}</div>
      <div className="management-setting-fields">{section.fields.map((field) => <ConfigField key={field.key} field={field} />)}</div>
    </section>
  )
}

function CFWorkerDomainPoolSection({ form }: { form: any }) {
  const watchedDomains = Form.useWatch('cfworker_domains', form) || []
  const watchedEnabledDomains = Form.useWatch('cfworker_enabled_domains', form) || []
  const normalizedDomains = normalizeDomainList(watchedDomains)
  const enabledDomains = normalizeDomainList(watchedEnabledDomains).filter((domain) => normalizedDomains.includes(domain))

  const updateEnabledDomains = (nextDomains: string[]) => {
    form.setFieldValue('cfworker_enabled_domains', normalizeDomainList(nextDomains))
  }

  const toggleEnabledDomain = (domain: string, checked: boolean) => {
    if (checked) {
      updateEnabledDomains([...enabledDomains, domain])
      return
    }
    updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
  }

  return (
    <Card
      title="CF Worker 域名池"
      extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>维护当前邮箱服务使用的域名范围</span>}
      style={{ marginBottom: 16 }}
    >
      <Form.List name="cfworker_domains">
        {(fields, { add, remove }) => (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {fields.map((field) => {
              const { key, ...restField } = field
              return (
                <Space key={key} align="start" style={{ display: 'flex' }}>
                  <Form.Item
                    {...restField}
                    label={field.name === 0 ? '全部域名' : ''}
                    style={{ flex: 1, marginBottom: 0 }}
                    rules={[
                      {
                        validator: async (_, value) => {
                          if (!String(value || '').trim()) {
                            throw new Error('请输入域名')
                          }
                        },
                      },
                    ]}
                  >
                  <Input placeholder="example.com" />
                </Form.Item>
                <Button
                  danger
                  onClick={() => {
                    const currentDomains = Array.isArray(form.getFieldValue('cfworker_domains'))
                      ? [...form.getFieldValue('cfworker_domains')]
                      : []
                    const removedDomain = String(currentDomains[field.name] || '').trim().toLowerCase().replace(/^@/, '')
                    remove(field.name)
                    if (!removedDomain) return
                    const enabledDomains = normalizeDomainList(form.getFieldValue('cfworker_enabled_domains'))
                    form.setFieldValue(
                      'cfworker_enabled_domains',
                      enabledDomains.filter((domain) => domain !== removedDomain),
                    )
                  }}
                >
                  删除
                </Button>
              </Space>
            )})}
            {fields.length === 0 ? (
              <Typography.Text type="secondary">还没有配置域名。添加后即可在下方选择启用项。</Typography.Text>
            ) : null}
            <Button type="dashed" onClick={() => add('')} icon={<PlusOutlined />} block>
              添加域名
            </Button>
          </div>
        )}
      </Form.List>

      <Form.Item name="cfworker_enabled_domains" hidden>
        <Select mode="multiple" options={normalizedDomains.map((domain) => ({ label: domain, value: domain }))} />
      </Form.Item>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>已启用域名</div>
        {enabledDomains.length > 0 ? (
          <Space wrap>
            {enabledDomains.map((domain) => (
              <Tag
                key={domain}
                color="blue"
                closable
                onClose={(event) => {
                  event.preventDefault()
                  updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
                }}
              >
                {domain}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">暂无启用域名，点击下方域名即可启用。</Typography.Text>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>点击切换启用状态</div>
        {normalizedDomains.length > 0 ? (
          <Space wrap>
            {normalizedDomains.map((domain) => (
              <Tag.CheckableTag
                key={domain}
                checked={enabledDomains.includes(domain)}
                onChange={(checked) => toggleEnabledDomain(domain, checked)}
              >
                {domain}
              </Tag.CheckableTag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">请先在上方添加域名。</Typography.Text>
        )}
      </div>
      <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
        点击已启用标签可将域名移出当前范围。
      </Typography.Text>
    </Card>
  )
}

function SolverStatus() {
  const [running, setRunning] = useState<boolean | null>(null)

  const checkSolver = async () => {
    try {
      const d = await apiFetch('/solver/status')
      setRunning(d.running)
    } catch {
      setRunning(false)
    }
  }

  const restartSolver = async () => {
    await apiFetch('/solver/restart', { method: 'POST' })
    setRunning(null)
    setTimeout(checkSolver, 2000)
  }

  useEffect(() => {
    checkSolver()
    const timer = window.setInterval(checkSolver, 5000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <Card title="Turnstile Solver" size="small" style={{ marginBottom: 16 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <Space size={8}>
          {running === null ? (
            <SyncOutlined spin style={{ color: '#7a8ba3' }} />
          ) : running ? (
            <CheckCircleOutlined style={{ color: '#10b981' }} />
          ) : (
            <CloseCircleOutlined style={{ color: '#ef4444' }} />
          )}
          <span style={{ color: running ? '#10b981' : '#7a8ba3', fontWeight: 500 }}>
            {running === null ? '检测中' : running ? '运行中' : '未运行'}
          </span>
        </Space>
        <Button size="small" onClick={restartSolver}>
          重启 Solver
        </Button>
      </div>
    </Card>
  )
}

type TotpSetupState = 'idle' | 'setup'

function SecurityPanel() {
  const { message: msg } = App.useApp()
  const [status, setStatus] = useState<{ has_password: boolean; has_totp: boolean } | null>(null)
  const [loading, setLoading] = useState(false)

  const [enableForm] = Form.useForm()
  const [pwForm] = Form.useForm()
  const [codeForm] = Form.useForm()

  const [totpSetupState, setTotpSetupState] = useState<TotpSetupState>('idle')
  const [totpSecret, setTotpSecret] = useState('')
  const [totpUri, setTotpUri] = useState('')

  const loadStatus = async () => {
    try {
      const s = await apiFetch('/auth/status')
      setStatus(s)
    } catch {
      // The security status is optional; the panel remains usable if it fails.
    }
  }

  useEffect(() => { loadStatus() }, [])

  const handleEnable = async (values: { password: string; confirm: string }) => {
    if (values.password !== values.confirm) {
      msg.error('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      const d = await apiFetch('/auth/setup', {
        method: 'POST',
        body: JSON.stringify({ password: values.password }),
      })
      localStorage.setItem('auth_token', d.access_token)
      msg.success('密码保护已启用')
      enableForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableAuth = async () => {
    setLoading(true)
    try {
      await apiFetch('/auth/disable', { method: 'POST' })
      localStorage.removeItem('auth_token')
      msg.success('密码保护已关闭')
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleChangePassword = async (values: { current_password: string; new_password: string; confirm: string }) => {
    if (values.new_password !== values.confirm) {
      msg.error('两次输入的新密码不一致')
      return
    }
    setLoading(true)
    try {
      await apiFetch('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password: values.current_password, new_password: values.new_password }),
      })
      msg.success('密码已更新')
      pwForm.resetFields()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleSetupTotp = async () => {
    setLoading(true)
    try {
      const d = await apiFetch('/auth/2fa/setup')
      setTotpSecret(d.secret)
      setTotpUri(d.uri)
      setTotpSetupState('setup')
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleEnableTotp = async (values: { code: string }) => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/enable', {
        method: 'POST',
        body: JSON.stringify({ secret: totpSecret, code: values.code }),
      })
      msg.success('双因素认证已启用')
      setTotpSetupState('idle')
      codeForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableTotp = async () => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/disable', { method: 'POST' })
      msg.success('双因素认证已关闭')
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="访问密码保护"
        extra={
          status?.has_password
            ? <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
            : <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
        }
      >
        {!status?.has_password ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text type="secondary">
              启用后，访问页面需要输入密码。默认不开启，任何能访问此地址的人均可使用。
            </Typography.Text>
            <Form form={enableForm} layout="vertical" onFinish={handleEnable} requiredMark={false} style={{ maxWidth: 360, marginTop: 8 }}>
              <Form.Item name="password" label="设置访问密码" rules={[{ required: true, message: '请输入密码' }, { min: 6, message: '至少 6 位' }]}>
                <Input.Password placeholder="至少 6 位" />
              </Form.Item>
              <Form.Item name="confirm" label="确认密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入密码" />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<LockOutlined />}>
                  启用密码保护
                </Button>
              </Form.Item>
            </Form>
          </Space>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text type="secondary">当前已启用密码保护，关闭后任何人无需密码即可访问。</Typography.Text>
            <Button danger loading={loading} onClick={handleDisableAuth}>
              关闭密码保护
            </Button>
          </Space>
        )}
      </Card>

      {status?.has_password && (
        <>
          <Card title="修改密码">
            <Form form={pwForm} layout="vertical" onFinish={handleChangePassword} requiredMark={false} style={{ maxWidth: 360 }}>
              <Form.Item name="current_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
                <Input.Password placeholder="当前密码" />
              </Form.Item>
              <Form.Item name="new_password" label="新密码" rules={[{ required: true, message: '请输入新密码' }, { min: 6, message: '至少 6 位' }]}>
                <Input.Password placeholder="新密码（至少 6 位）" />
              </Form.Item>
              <Form.Item name="confirm" label="确认新密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入新密码" />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<SaveOutlined />}>
                  更新密码
                </Button>
              </Form.Item>
            </Form>
          </Card>

          <Card
            title="双因素认证 (2FA)"
            extra={
              status?.has_totp
                ? <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
                : <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
            }
          >
            {status?.has_totp ? (
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  登录时需输入 Google Authenticator / Authy 等 App 中的 6 位验证码。
                </Typography.Text>
                <Button danger loading={loading} onClick={handleDisableTotp}>
                  关闭双因素认证
                </Button>
              </Space>
            ) : totpSetupState === 'idle' ? (
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  启用后，登录时除密码外还需输入验证器 App 中的 6 位验证码，大幅提升安全性。
                </Typography.Text>
                <Button type="primary" loading={loading} onClick={handleSetupTotp} icon={<SafetyOutlined />}>
                  开启双因素认证
                </Button>
              </Space>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Typography.Text strong>1. 用验证器 App 扫描下方二维码</Typography.Text>
                <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                  <QRCode value={totpUri} size={180} />
                  <div style={{ flex: 1 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>无法扫码？手动输入密钥：</Typography.Text>
                    <Typography.Paragraph copyable style={{ fontFamily: 'var(--font-family-mono)', fontSize: 13, marginTop: 4 }}>
                      {totpSecret}
                    </Typography.Paragraph>
                  </div>
                </div>
                <Typography.Text strong>2. 输入 App 中显示的 6 位验证码以确认绑定</Typography.Text>
                <Form form={codeForm} layout="inline" onFinish={handleEnableTotp}>
                  <Form.Item name="code" rules={[{ required: true, message: '请输入验证码' }, { len: 6, message: '6 位数字' }]}>
                    <Input placeholder="000000" maxLength={6} style={{ width: 140, letterSpacing: 4, textAlign: 'center' }} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit" loading={loading}>确认启用</Button>
                  </Form.Item>
                  <Form.Item>
                    <Button onClick={() => setTotpSetupState('idle')}>取消</Button>
                  </Form.Item>
                </Form>
              </Space>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

export default function Settings({ page = 'settings' }: SettingsProps) {
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [configLoadState, setConfigLoadState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [configLoadError, setConfigLoadError] = useState('')
  const [activeTab, setActiveTab] = useState('recovery')
  const effectiveTab = page === 'codex2api' ? 'codex2api' : page === 'mail-import' ? 'mail-import' : activeTab
  const currentMailProviderRaw = String(Form.useWatch('mail_provider', { form, preserve: true }) || '')
  const currentMailImportSource = String(Form.useWatch('mail_import_source', { form, preserve: true }) || 'microsoft')
  const currentMailProvider = resolveEffectiveMailProvider(currentMailProviderRaw, currentMailImportSource)
  const configLoadRequestIdRef = useRef(0)
  const configBaselineRef = useRef<Record<string, unknown>>({})

  const loadConfig = useCallback(async () => {
    const requestId = ++configLoadRequestIdRef.current
    setConfigLoadState('loading')
    setConfigLoadError('')
    try {
      const response = await apiFetch('/config')
      if (requestId !== configLoadRequestIdRef.current) return
      const data = asRecord(response)
      if (!data) throw new Error('配置响应格式无效')
      const configMailProvider = String(data.mail_provider || 'luckmail')
      const isMailImportProvider = configMailProvider === 'microsoft' || configMailProvider === 'outlook' || configMailProvider === 'applemail'
      if (!data.mail_provider) {
        data.mail_provider = 'luckmail'
      }
      if (!data.applemail_base_url) {
        data.applemail_base_url = 'https://www.appleemail.top'
      }
      if (!data.applemail_pool_dir) {
        data.applemail_pool_dir = 'mail'
      }
      if (!data.applemail_mailboxes) {
        data.applemail_mailboxes = 'INBOX,Junk'
      }
      if (!data.outlook_backend) {
        data.outlook_backend = 'graph'
      }
      if (!data.gptmail_base_url) {
        data.gptmail_base_url = 'https://mail.chatgpt.org.uk'
      }
      if (!data.maliapi_base_url) {
        data.maliapi_base_url = 'https://maliapi.215.im/v1'
      }
      if (!data.luckmail_base_url) {
        data.luckmail_base_url = 'https://mails.luckyous.com/'
      }
      if (!data.cloudmail_timeout) {
        data.cloudmail_timeout = 30
      }
      data.codex2api_enabled = parseBooleanConfigValue(data.codex2api_enabled)
      data.codex2api_delete_on_account_remove_enabled = parseBooleanConfigValue(
        data.codex2api_delete_on_account_remove_enabled,
      )
      delete data.leadbee_api_enabled
      delete data.leadbee_api_key
      delete data.leadbee_api_secret
      delete data.leadbee_api_product_id
      data.chatgpt_auto_relogin_enabled = parseBooleanConfigValue(data.chatgpt_auto_relogin_enabled)
      data.chatgpt_auto_relogin_interval_minutes = normalizeBoundedInteger(
        data.chatgpt_auto_relogin_interval_minutes,
        2,
        2,
        1440,
      )
      data.chatgpt_auto_relogin_concurrency = normalizeBoundedInteger(
        data.chatgpt_auto_relogin_concurrency,
        3,
        1,
        3,
      )
      data.chatgpt_auto_relogin_alert_threshold = normalizeBoundedInteger(
        data.chatgpt_auto_relogin_alert_threshold,
        20,
        1,
        10000,
      )
      data.chatgpt_auto_relogin_quota_alert_threshold_usd = normalizeBoundedDecimal(
        data.chatgpt_auto_relogin_quota_alert_threshold_usd,
        0,
        0,
        10000000,
        2,
      )
      data.smtp_port = normalizeBoundedInteger(data.smtp_port, 587, 1, 65535)
      data.smtp_use_ssl = parseBooleanConfigValue(data.smtp_use_ssl)
      data.smtp_force_auth_login = parseBooleanConfigValue(data.smtp_force_auth_login)
      data.smtp_password = ''
      data.bark_enabled = parseBooleanConfigValue(data.bark_enabled)
      data.bark_endpoint = ''
      data.cfworker_domains = parseStoredDomainList(data.cfworker_domains)
      data.cfworker_enabled_domains = parseStoredDomainList(data.cfworker_enabled_domains)
      data.cfworker_random_subdomain = parseBooleanConfigValue(data.cfworker_random_subdomain)
      data.cfworker_random_name_subdomain = parseBooleanConfigValue(data.cfworker_random_name_subdomain)
      data.email_domain_rule_enabled = parseBooleanConfigValue(data.email_domain_rule_enabled)
      if (!String(data.email_domain_level_count ?? '').trim()) {
        data.email_domain_level_count = 2
      }
      data.mail_import_source = configMailProvider === 'applemail' ? 'applemail' : 'microsoft'
      data.mail_provider = isMailImportProvider ? 'mail_import' : configMailProvider
      configBaselineRef.current = structuredClone(data)
      form.setFieldsValue(data)
      setConfigLoadState('ready')
    } catch (error: unknown) {
      if (requestId !== configLoadRequestIdRef.current) return
      setConfigLoadError(errorDetail(error, '读取配置失败，请重试'))
      setConfigLoadState('error')
    }
  }, [form])

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadConfig() }, 0)
    return () => {
      window.clearTimeout(timer)
      configLoadRequestIdRef.current += 1
    }
  }, [loadConfig])

  const save = async () => {
    if (configLoadState !== 'ready') return
    setSaving(true)
    setSaved(false)
    setSaveError('')
    try {
      const values = { ...form.getFieldsValue(true) }
      const changedKeys = supportedFieldKeys.filter(
        key => JSON.stringify(values[key]) !== JSON.stringify(configBaselineRef.current[key]),
      )
      values.mail_provider = resolveEffectiveMailProvider(values.mail_provider, values.mail_import_source)
      delete values.mail_import_source
      const domains = normalizeDomainList(values.cfworker_domains)
      const enabledDomains = normalizeDomainList(values.cfworker_enabled_domains).filter((domain) => domains.includes(domain))

      if (values.mail_provider === 'cfworker' && domains.length > 0 && enabledDomains.length === 0) {
        setActiveTab('mailbox')
        message.error('CF Worker 至少需要启用一个域名')
        return
      }

      values.cfworker_domains = JSON.stringify(domains)
      values.cfworker_enabled_domains = JSON.stringify(enabledDomains)
      if (domains.length > 0) {
        values.cfworker_domain = ''
      }
      values.codex2api_enabled = parseBooleanConfigValue(values.codex2api_enabled)
      values.codex2api_delete_on_account_remove_enabled = parseBooleanConfigValue(
        values.codex2api_delete_on_account_remove_enabled,
      )
      delete values.leadbee_api_enabled
      delete values.leadbee_api_key
      delete values.leadbee_api_secret
      delete values.leadbee_api_product_id
      values.chatgpt_auto_relogin_enabled = parseBooleanConfigValue(values.chatgpt_auto_relogin_enabled)
      values.chatgpt_auto_relogin_interval_minutes = normalizeBoundedInteger(
        values.chatgpt_auto_relogin_interval_minutes,
        2,
        2,
        1440,
      )
      values.chatgpt_auto_relogin_concurrency = normalizeBoundedInteger(
        values.chatgpt_auto_relogin_concurrency,
        3,
        1,
        3,
      )
      values.chatgpt_auto_relogin_alert_threshold = normalizeBoundedInteger(
        values.chatgpt_auto_relogin_alert_threshold,
        20,
        1,
        10000,
      )
      values.chatgpt_auto_relogin_quota_alert_threshold_usd = normalizeBoundedDecimal(
        values.chatgpt_auto_relogin_quota_alert_threshold_usd,
        0,
        0,
        10000000,
        2,
      )
      values.smtp_port = normalizeBoundedInteger(values.smtp_port, 587, 1, 65535)
      values.smtp_use_ssl = parseBooleanConfigValue(values.smtp_use_ssl)
      values.smtp_force_auth_login = parseBooleanConfigValue(values.smtp_force_auth_login)
      values.bark_enabled = parseBooleanConfigValue(values.bark_enabled)
      values.cfworker_random_subdomain = parseBooleanConfigValue(values.cfworker_random_subdomain)
      values.cfworker_random_name_subdomain = parseBooleanConfigValue(values.cfworker_random_name_subdomain)
      values.email_domain_rule_enabled = parseBooleanConfigValue(values.email_domain_rule_enabled)
      const rawDomainLevelCount = Number.parseInt(String(values.email_domain_level_count ?? '').trim(), 10)
      if (values.mail_provider === 'cfworker' && values.email_domain_rule_enabled) {
        if (!Number.isInteger(rawDomainLevelCount) || rawDomainLevelCount < 2) {
          setActiveTab('mailbox')
          message.error('域名级数必须是大于等于 2 的整数')
          return
        }
      }
      values.email_domain_level_count =
        Number.isInteger(rawDomainLevelCount) && rawDomainLevelCount >= 2
          ? String(rawDomainLevelCount)
          : '2'

      // Only visible or explicitly edited fields participate in this update.
      // The backend merges this subset, preserving removed integrations and other providers.
      const editableKeys = new Set([
        ...visibleFieldKeys,
        ...changedKeys,
      ])
      if (editableKeys.has('mail_import_source')) editableKeys.add('mail_provider')
      const data = Object.fromEntries(Object.entries(values).filter(([key]) => editableKeys.has(key)))
      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data }) })
      form.setFieldsValue({
        mail_provider: values.mail_provider === 'microsoft' || values.mail_provider === 'applemail' ? 'mail_import' : values.mail_provider,
        mail_import_source: values.mail_provider === 'applemail' ? 'applemail' : 'microsoft',
        codex2api_enabled: values.codex2api_enabled,
        codex2api_delete_on_account_remove_enabled: values.codex2api_delete_on_account_remove_enabled,
        chatgpt_auto_relogin_enabled: values.chatgpt_auto_relogin_enabled,
        chatgpt_auto_relogin_interval_minutes: values.chatgpt_auto_relogin_interval_minutes,
        chatgpt_auto_relogin_concurrency: values.chatgpt_auto_relogin_concurrency,
        chatgpt_auto_relogin_alert_threshold: values.chatgpt_auto_relogin_alert_threshold,
        chatgpt_auto_relogin_quota_alert_threshold_usd: values.chatgpt_auto_relogin_quota_alert_threshold_usd,
        smtp_port: values.smtp_port,
        smtp_use_ssl: values.smtp_use_ssl,
        smtp_force_auth_login: values.smtp_force_auth_login,
        smtp_password: '',
        bark_enabled: values.bark_enabled,
        bark_endpoint: '',
        cfworker_domains: domains,
        cfworker_enabled_domains: enabledDomains,
        cfworker_domain: domains.length > 0 ? '' : values.cfworker_domain,
        cfworker_random_subdomain: values.cfworker_random_subdomain,
        cfworker_random_name_subdomain: values.cfworker_random_name_subdomain,
        email_domain_rule_enabled: values.email_domain_rule_enabled,
        email_domain_level_count: values.email_domain_level_count,
      })
      const savedFormValues = form.getFieldsValue(true)
      for (const key of editableKeys) {
        configBaselineRef.current[key] = structuredClone(savedFormValues[key])
      }
      message.success('保存成功')
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (error: unknown) {
      setSaved(false)
      setSaveError(errorDetail(error, '保存配置失败，请稍后重试'))
    } finally {
      setSaving(false)
    }
  }

  const currentTab = (TAB_ITEMS.find((t) => t.key === effectiveTab) ?? TAB_ITEMS[0]) as TabConfig
  const saveDisabled = configLoadState !== 'ready'
  const mailboxSections =
    effectiveTab === 'mailbox'
      ? splitMailboxSections(currentTab.sections, currentMailProvider)
      : { defaultSection: null, selectedSection: null, remainingSections: currentTab.sections }
  const automationKeys = [
    'chatgpt_auto_relogin_enabled', 'chatgpt_auto_relogin_interval_minutes', 'chatgpt_auto_relogin_concurrency',
    'chatgpt_auto_relogin_alert_threshold', 'chatgpt_auto_relogin_quota_alert_threshold_usd',
    'smtp_host', 'smtp_port', 'smtp_username', 'smtp_password', 'smtp_sender_email', 'smtp_recipient_email',
    'smtp_use_ssl', 'smtp_force_auth_login', 'bark_enabled', 'bark_endpoint',
  ]
  const mailboxExtraKeys = ['mail_import_source', 'cfworker_domains', 'cfworker_enabled_domains', 'cfworker_domain']
  const supportedFieldKeys = [...TAB_ITEMS.flatMap(tab => tab.sections.flatMap(section => section.fields.map(field => field.key))), ...automationKeys, ...mailboxExtraKeys]
  const visibleFieldKeys = page === 'mail-import'
    ? ['mail_provider', 'mail_import_source', 'applemail_pool_dir', 'applemail_pool_file']
    : effectiveTab === 'recovery'
      ? automationKeys
      : effectiveTab === 'mailbox'
        ? [
          ...[mailboxSections.defaultSection, mailboxSections.selectedSection].flatMap(section => section?.fields.map(field => field.key) || []),
          'mail_import_source',
          ...(currentMailProvider === 'cfworker' ? mailboxExtraKeys : []),
        ]
        : [
          ...currentTab.sections.flatMap(section => section.fields.map(field => field.key)),
          ...(page === 'codex2api' ? automationKeys : []),
        ]

  return (
    <div className="console-page management-workspace management-settings">
      <ConsolePageHeader
        title={page === 'codex2api' ? 'Codex2API' : page === 'mail-import' ? '邮箱导入' : '设置'}
        description={page === 'mail-import' ? '维护已有账号的邮箱登录资料与收件来源。' : '管理账号恢复、告警通知和运行连接。'}
        actions={effectiveTab !== 'security' ? <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} disabled={saveDisabled}>{saved ? '已保存 ✓' : '保存配置'}</Button> : undefined}
      />
      {configLoadState === 'loading' && <Typography.Text role="status" aria-live="polite">正在加载配置…</Typography.Text>}
      {configLoadState === 'error' && <Alert type="error" showIcon message="配置加载失败" description={configLoadError} action={<Button onClick={() => { void loadConfig() }}>重试加载</Button>} />}
      {saveError && <Alert type="error" showIcon message={saveError} />}
      {saved && <Typography.Text role="status" aria-live="polite">配置已保存。</Typography.Text>}
      {page === 'settings' && <Tabs className="management-settings-tabs" activeKey={activeTab} onChange={setActiveTab}
        items={TAB_ITEMS.map(tab => ({ key: tab.key, label: tab.label }))} />}
      {effectiveTab === 'security' ? <SecurityPanel /> : (
        <Form form={form} layout="vertical" className="management-settings-form">
          {page === 'mail-import' ? <MailImportPanel form={form} /> : effectiveTab === 'recovery' ? (
            <div className="management-recovery"><ChatGPTAutoReloginSection /></div>
          ) : effectiveTab === 'mailbox' ? (
            <>
              {mailboxSections.defaultSection && <ConfigSection section={mailboxSections.defaultSection} />}
              {mailboxSections.selectedSection && <ConfigSection section={mailboxSections.selectedSection} />}
              {currentMailProvider === 'cfworker' && <CFWorkerDomainPoolSection form={form} />}
            </>
          ) : (
            <>
              {currentTab.sections.map(section => <ConfigSection key={section.title} section={section} />)}
              {effectiveTab === 'runtime' && <SolverStatus />}
              {page === 'codex2api' && <div className="management-recovery"><ChatGPTAutoReloginSection /></div>}
            </>
          )}
        </Form>
      )}
    </div>
  )
}
