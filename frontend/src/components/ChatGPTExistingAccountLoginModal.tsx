import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Col,
  Form,
  InputNumber,
  Modal,
  Radio,
  Row,
  Skeleton,
  Space,
  Switch,
  Tag,
  Typography,
  message,
  theme,
} from 'antd'

import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'
import {
  buildExistingAccountLoginTaskPayload,
  resolveMailboxSnapshotType,
} from '@/lib/chatgptStagedLogin'
import type { ChatGPTSmsMode } from '@/lib/chatgptStagedLogin'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from './TaskLogPanel'

type Props = {
  open: boolean
  onClose: () => void
  onDone: () => void
}

type LoginFormValues = {
  count: number
  concurrency: number
  register_delay_seconds: number
  sms_mode: ChatGPTSmsMode
  rotate_mfa: boolean
}

type ImportedMailProvider = 'microsoft' | 'applemail'

type CapacitySummary = {
  balanceAvailable: number | null
  unitPrice: number | null
  estimatedOrderCapacity: number | null
  currency: string
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function errorMessage(error: unknown, fallback: string) {
  if (error && typeof error === 'object' && 'message' in error) {
    const messageText = String(error.message || '').trim()
    if (messageText) return messageText
  }
  return fallback
}

function safeDecimal(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  const text = String(value).trim()
  if (!/^\d+(?:\.\d{1,8})?$/.test(text)) return null
  const parsed = Number(text)
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1_000_000_000
    ? parsed
    : null
}

function safeCapacity(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(String(value ?? '').trim())
  return Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= 1_000_000_000
    ? parsed
    : null
}

function sanitizeCapacity(value: unknown): CapacitySummary | null {
  const data = asRecord(value)
  if (!data || data.ok !== true) return null
  const currencyText = String(data.currency ?? '').trim().toUpperCase()
  return {
    balanceAvailable: safeDecimal(data.balance_available),
    unitPrice: safeDecimal(data.unit_price),
    estimatedOrderCapacity: safeCapacity(data.estimated_order_capacity),
    currency: /^[A-Z]{3}$/.test(currencyText) ? currencyText : '',
  }
}

function moneySymbol(currency: string): string {
  if (currency === 'CNY') return '¥'
  if (currency === 'USD') return '$'
  if (currency === 'EUR') return '€'
  return currency ? `${currency} ` : ''
}

function formatMoney(value: number | null, currency: string): string {
  if (value === null) return '暂未获取'
  return `${moneySymbol(currency)}${value.toFixed(2)}`
}

function sanitizeLoginConfig(value: unknown): Record<string, unknown> {
  const config = { ...(asRecord(value) ?? {}) }
  delete config.leadbee_api_key
  delete config.leadbee_api_secret
  delete config.leadbee_api_client_order_id
  delete config.leadbee_api_client_order_ids
  return config
}

export function ChatGPTExistingAccountLoginModal({ open, onClose, onDone }: Props) {
  const [form] = Form.useForm<LoginFormValues>()
  const { token } = theme.useToken()
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [poolCount, setPoolCount] = useState<number | null>(null)
  const [mailProviderPlan, setMailProviderPlan] = useState<ImportedMailProvider[]>([])
  const [smsPoolAvailable, setSmsPoolAvailable] = useState<number | null>(null)
  const [apiConfigured, setApiConfigured] = useState(false)
  const [capacity, setCapacity] = useState<CapacitySummary | null>(null)
  const [loadingConfig, setLoadingConfig] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const smsMode = Form.useWatch('sms_mode', form) || 'none'
  const rotateMfa = Form.useWatch('rotate_mfa', form) !== false
  const watchedCount = Math.max(1, Number(Form.useWatch('count', form) || 1))

  useEffect(() => {
    if (!open) return
    let cancelled = false

    const loadContext = async () => {
      try {
        const rawConfig = await apiFetch('/config')
        const rawConfigRecord = asRecord(rawConfig) ?? {}
        const nextApiConfigured = Boolean(
          parseBooleanConfigValue(rawConfigRecord.leadbee_api_enabled)
          && String(rawConfigRecord.leadbee_api_product_id ?? '').trim(),
        )
        const nextConfig = sanitizeLoginConfig(rawConfigRecord)
        const snapshotType = resolveMailboxSnapshotType(nextConfig)
        const snapshotTypes: ImportedMailProvider[] = snapshotType
          ? ['microsoft', 'applemail']
          : []

        const [smsStats, apiCapacityResponse, snapshots] = await Promise.all([
          apiFetch('/sms-pool/stats').catch(() => null),
          nextApiConfigured
            ? apiFetch('/config/leadbee/test', {
              method: 'POST',
              body: JSON.stringify({ data: {} }),
            }).catch(() => null)
            : Promise.resolve(null),
          Promise.all(snapshotTypes.map(async (type) => {
            const params = new URLSearchParams({ type, preview_limit: '1' })
            if (type === 'applemail') {
              if (nextConfig.applemail_pool_dir) {
                params.set('pool_dir', String(nextConfig.applemail_pool_dir))
              }
              if (nextConfig.applemail_pool_file) {
                params.set('pool_file', String(nextConfig.applemail_pool_file))
              }
            }
            try {
              const snapshot = await apiFetch(`/mail-imports/snapshot?${params.toString()}`)
              return { type, count: Math.max(0, Number(snapshot?.count || 0)) }
            } catch {
              return { type, count: 0 }
            }
          })),
        ])
        if (cancelled) return

        const providerPlan = snapshots.flatMap(({ type, count }) =>
          Array.from({ length: count }, () => type))
        const count = snapshotType ? providerPlan.length : 1
        const nextSmsPoolAvailable = smsStats === null
          ? null
          : Math.max(0, Number(smsStats?.unused || 0))
        const defaultSmsMode: ChatGPTSmsMode = nextApiConfigured
          ? 'api_fallback_pool'
          : nextSmsPoolAvailable && nextSmsPoolAvailable > 0
            ? 'pool'
            : 'none'

        setConfig(nextConfig)
        setApiConfigured(nextApiConfigured)
        setCapacity(sanitizeCapacity(apiCapacityResponse))
        setSmsPoolAvailable(nextSmsPoolAvailable)
        setMailProviderPlan(providerPlan)
        setPoolCount(snapshotType ? count : null)
        form.setFieldsValue({
          count: Math.max(1, count),
          concurrency: 1,
          register_delay_seconds: 0,
          sms_mode: defaultSmsMode,
          rotate_mfa: true,
        })
      } catch (error) {
        if (!cancelled) {
          message.error(`读取邮箱池失败: ${errorMessage(error, '请求失败')}`)
          form.setFieldsValue({
            count: 1,
            concurrency: 1,
            register_delay_seconds: 0,
            sms_mode: 'none',
            rotate_mfa: true,
          })
        }
      } finally {
        if (!cancelled) setLoadingConfig(false)
      }
    }

    const startTimer = window.setTimeout(() => {
      setTaskId(null)
      setPoolCount(null)
      setMailProviderPlan([])
      setSmsPoolAvailable(null)
      setApiConfigured(false)
      setCapacity(null)
      setConfig(null)
      form.resetFields()
      setLoadingConfig(true)
      void loadContext()
    }, 0)
    return () => {
      cancelled = true
      window.clearTimeout(startTimer)
    }
  }, [form, open])

  const handleStart = async (values: LoginFormValues) => {
    if (!config) {
      message.error('登录配置尚未加载完成')
      return
    }
    setSubmitting(true)
    try {
      const payload = buildExistingAccountLoginTaskPayload({
        count: values.count,
        concurrency: values.concurrency,
        registerDelaySeconds: values.register_delay_seconds || 0,
        executorType: normalizeExecutorForPlatform(
          'chatgpt',
          String(config.default_executor || 'protocol'),
        ),
        captchaSolver: String(config.default_captcha_solver || 'yescaptcha'),
        bindPhoneAndGetRefreshToken: values.sms_mode !== 'none',
        rotateMfa: values.rotate_mfa,
        smsMode: values.sms_mode,
        leadbeeCodes: [],
        mailProviderPlan: mailProviderPlan.slice(0, values.count),
        config,
      })
      const result = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      setTaskId(String(result.task_id || ''))
    } catch (error) {
      message.error(`启动登录失败: ${errorMessage(error, '请求失败')}`)
    } finally {
      setSubmitting(false)
    }
  }

  const close = () => {
    if (submitting) return
    setTaskId(null)
    form.resetFields()
    onClose()
  }

  const apiSummary = capacity
    ? `API 可用余额 ${formatMoney(capacity.balanceAvailable, capacity.currency)} · 单价 ${formatMoney(capacity.unitPrice, capacity.currency)}/次 · 预计可接 ${capacity.estimatedOrderCapacity ?? '暂未获取'} 次`
    : '余额暂未获取'
  const usesPhoneVerification = smsMode !== 'none'

  return (
    <Modal
      title="登录已有 ChatGPT 账号"
      open={open}
      onCancel={close}
      footer={null}
      width={640}
      style={{ top: 48 }}
      styles={{
        body: {
          maxHeight: 'calc(100vh - 128px)',
          overflowY: 'auto',
          paddingRight: 4,
        },
      }}
      maskClosable={false}
      destroyOnHidden
    >
      {loadingConfig ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : taskId ? (
        <TaskLogPanel taskId={taskId} mode="login" onDone={onDone} />
      ) : (
        <>
          <Alert
            type={poolCount === 0 ? 'warning' : 'info'}
            showIcon
            message={poolCount === null ? '当前邮箱来源不提供池数量' : `可用邮箱 ${poolCount} 个`}
            description={usesPhoneVerification
              ? '系统会先保存 AT，再按所选接码方式完成手机验证并获取 RT。'
              : '适用于已绑定手机号的账号，系统会直接完成 OAuth 登录并保存 AT + RT。'}
            style={{ marginBottom: 16 }}
          />
          <Form
            form={form}
            layout="vertical"
            initialValues={{
              count: 1,
              concurrency: 1,
              register_delay_seconds: 0,
              sms_mode: 'none',
              rotate_mfa: true,
            }}
            onFinish={handleStart}
          >
            <Row gutter={12}>
              <Col xs={24} sm={8}>
                <Form.Item
                  name="count"
                  label="登录数量"
                  rules={[{ required: true, message: '请输入登录数量' }]}
                >
                  <InputNumber min={1} max={poolCount || undefined} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col xs={24} sm={8}>
                <Form.Item
                  name="concurrency"
                  label="并发数"
                  rules={[{ required: true, message: '请输入并发数' }]}
                >
                  <InputNumber
                    min={1}
                    max={Math.min(50, watchedCount)}
                    style={{ width: '100%' }}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} sm={8}>
                <Form.Item name="register_delay_seconds" label="启动间隔（秒）">
                  <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            </Row>

            <div
              style={{
                border: `1px solid ${token.colorBorderSecondary}`,
                borderRadius: token.borderRadiusLG,
                background: token.colorFillAlter,
                padding: 16,
                marginBottom: 16,
              }}
            >
              <Form.Item
                name="rotate_mfa"
                valuePropName="checked"
                style={{ marginBottom: 10 }}
              >
                <Switch aria-label="登录后新增或轮换 MFA" />
              </Form.Item>
              <Typography.Text strong>登录后新增/轮换 MFA</Typography.Text>
              <Typography.Paragraph
                type="secondary"
                style={{ margin: '4px 0 12px', fontSize: 13 }}
              >
                无 MFA 自动新增；已有 MFA 自动废弃旧密钥并保存项目生成的新密钥。
              </Typography.Paragraph>
              <Alert
                type="warning"
                showIcon
                message="MFA 轮换不等于邮箱接管"
                description="共享接码地址仍可能被供货商访问；系统会保存风险标记，不会把此类账号显示为已完全接管。"
              />
            </div>

            <div
              style={{
                border: `1px solid ${token.colorBorderSecondary}`,
                borderRadius: token.borderRadiusLG,
                background: token.colorFillAlter,
                padding: 16,
                marginBottom: 20,
              }}
            >
              <Typography.Title level={5} style={{ margin: 0 }}>
                接码方式
              </Typography.Title>
              <Typography.Paragraph
                type="secondary"
                style={{ margin: '4px 0 14px', fontSize: 13 }}
              >
                一般使用 API 优先。余额不足时只切换该账号，卡密队列不会阻塞其他 API 订单。
              </Typography.Paragraph>

              <Space direction="vertical" size={6} style={{ width: '100%', marginBottom: 14 }}>
                {apiConfigured ? (
                  <Typography.Text>{apiSummary}</Typography.Text>
                ) : (
                  <Typography.Text type="secondary">LeadBee API 尚未启用</Typography.Text>
                )}
                <Typography.Text>
                  {smsPoolAvailable === null
                    ? '卡密池余量暂未获取'
                    : `卡密池可用 ${smsPoolAvailable} 张`}
                </Typography.Text>
              </Space>

              <Form.Item name="sms_mode" style={{ marginBottom: 0 }}>
                <Radio.Group
                  style={{ display: 'flex', flexDirection: 'column', gap: 10, width: '100%' }}
                >
                  <Radio
                    value="api_fallback_pool"
                    aria-label="API优先"
                    disabled={!apiConfigured}
                  >
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>API优先</Typography.Text>
                      <Typography.Text type="secondary">
                        API 余额不足时，自动改用卡密池。
                      </Typography.Text>
                    </Space>
                  </Radio>
                  <Radio
                    value="pool"
                    aria-label="仅卡密池"
                    disabled={smsPoolAvailable === 0}
                  >
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>仅卡密池</Typography.Text>
                      <Typography.Text type="secondary">
                        跳过 API，直接从已导入卡密中分配。
                      </Typography.Text>
                    </Space>
                  </Radio>
                  <Radio value="none" aria-label="无需接码">
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>无需接码</Typography.Text>
                      <Typography.Text type="secondary">
                        已绑定手机号的账号直接登录并获取 RT。
                      </Typography.Text>
                    </Space>
                  </Radio>
                </Radio.Group>
              </Form.Item>

              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  flexWrap: 'wrap',
                  gap: 6,
                  marginTop: 14,
                }}
              >
                <Tag color="blue" bordered={false}>邮箱登录</Tag>
                <Typography.Text type="secondary">→</Typography.Text>
                {rotateMfa ? (
                  <>
                    <Tag color="gold" bordered={false}>MFA 新增/轮换</Tag>
                    <Typography.Text type="secondary">→</Typography.Text>
                  </>
                ) : null}
                {usesPhoneVerification ? (
                  <>
                    <Tag color="purple" bordered={false}>手机验证</Tag>
                    <Typography.Text type="secondary">→</Typography.Text>
                  </>
                ) : null}
                <Tag color="green" bordered={false}>保存 AT + RT</Tag>
              </div>
            </div>

            <div
              style={{
                position: 'sticky',
                bottom: 0,
                zIndex: 1,
                padding: '12px 0 2px',
                background: token.colorBgElevated,
              }}
            >
              <Button
                type="primary"
                htmlType="submit"
                block
                loading={submitting}
                disabled={poolCount === 0}
              >
                {usesPhoneVerification ? '开始登录并接码' : '开始登录'}
              </Button>
            </div>
          </Form>
        </>
      )}
    </Modal>
  )
}
