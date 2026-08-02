import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Skeleton,
  Switch,
  Tag,
  Typography,
  message,
  theme,
} from 'antd'

import { apiFetch } from '@/lib/utils'
import { normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'
import {
  buildExistingAccountLoginTaskPayload,
  parseLeadBeeCodes,
  resolveMailboxSnapshotType,
} from '@/lib/chatgptStagedLogin'
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
  bind_phone_and_get_rt: boolean
  use_sms_pool: boolean
  leadbee_codes: string
}

type ImportedMailProvider = 'microsoft' | 'applemail'

function errorMessage(error: unknown, fallback: string) {
  if (error && typeof error === 'object' && 'message' in error) {
    const messageText = String(error.message || '').trim()
    if (messageText) return messageText
  }
  return fallback
}

export function ChatGPTExistingAccountLoginModal({ open, onClose, onDone }: Props) {
  const [form] = Form.useForm<LoginFormValues>()
  const { token } = theme.useToken()
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [poolCount, setPoolCount] = useState<number | null>(null)
  const [mailProviderPlan, setMailProviderPlan] = useState<ImportedMailProvider[]>([])
  const [smsPoolAvailable, setSmsPoolAvailable] = useState<number | null>(null)
  const [loadingConfig, setLoadingConfig] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const bindPhoneAndGetRefreshToken = Boolean(
    Form.useWatch('bind_phone_and_get_rt', form),
  )
  const useSmsPool = Boolean(Form.useWatch('use_sms_pool', form))
  const watchedCount = Math.max(1, Number(Form.useWatch('count', form) || 1))
  const leadbeeCodes = parseLeadBeeCodes(Form.useWatch('leadbee_codes', form))

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setTaskId(null)
    setPoolCount(null)
    setMailProviderPlan([])
    setSmsPoolAvailable(null)
    setConfig(null)
    form.resetFields()
    setLoadingConfig(true)

    const loadContext = async () => {
      try {
        const [nextConfig, smsStats] = await Promise.all([
          apiFetch('/config'),
          apiFetch('/sms-pool/stats').catch(() => null),
        ])
        if (cancelled) return
        setConfig(nextConfig)
        setSmsPoolAvailable(smsStats === null ? null : Math.max(0, Number(smsStats?.unused || 0)))
        const snapshotType = resolveMailboxSnapshotType(nextConfig)
        if (!snapshotType) {
          form.setFieldsValue({
            count: 1,
            concurrency: 1,
            register_delay_seconds: 0,
            bind_phone_and_get_rt: false,
            use_sms_pool: false,
            leadbee_codes: '',
          })
          return
        }
        const snapshotTypes: ImportedMailProvider[] = ['microsoft', 'applemail']
        const snapshots = await Promise.all(snapshotTypes.map(async (type) => {
          const params = new URLSearchParams({ type, preview_limit: '1' })
          if (type === 'applemail') {
            if (nextConfig.applemail_pool_dir) params.set('pool_dir', String(nextConfig.applemail_pool_dir))
            if (nextConfig.applemail_pool_file) params.set('pool_file', String(nextConfig.applemail_pool_file))
          }
          try {
            const snapshot = await apiFetch(`/mail-imports/snapshot?${params.toString()}`)
            return { type, count: Math.max(0, Number(snapshot?.count || 0)) }
          } catch {
            return { type, count: 0 }
          }
        }))
        if (cancelled) return
        const providerPlan = snapshots.flatMap(({ type, count }) =>
          Array.from({ length: count }, () => type))
        const count = providerPlan.length
        setMailProviderPlan(providerPlan)
        setPoolCount(count)
        form.setFieldsValue({
          count: Math.max(1, count),
          concurrency: 1,
          register_delay_seconds: 0,
          bind_phone_and_get_rt: false,
          use_sms_pool: false,
          leadbee_codes: '',
        })
      } catch (error) {
        if (!cancelled) {
          message.error(`读取邮箱池失败: ${errorMessage(error, '请求失败')}`)
          form.setFieldsValue({
            count: 1,
            concurrency: 1,
            register_delay_seconds: 0,
            bind_phone_and_get_rt: false,
            use_sms_pool: false,
            leadbee_codes: '',
          })
        }
      } finally {
        if (!cancelled) setLoadingConfig(false)
      }
    }

    loadContext()
    return () => {
      cancelled = true
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
        executorType: normalizeExecutorForPlatform('chatgpt', String(config.default_executor || 'protocol')),
        captchaSolver: String(config.default_captcha_solver || 'yescaptcha'),
        bindPhoneAndGetRefreshToken: values.bind_phone_and_get_rt,
        useSmsPool: values.use_sms_pool,
        leadbeeCodes: parseLeadBeeCodes(values.leadbee_codes),
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

  return (
    <Modal
      title="登录已有 ChatGPT 账号"
      open={open}
      onCancel={close}
      footer={null}
      width={600}
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
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : taskId ? (
        <TaskLogPanel
          taskId={taskId}
          mode="login"
          onDone={() => {
            onDone()
          }}
        />
      ) : (
        <>
          <Alert
            type={poolCount === 0 ? 'warning' : 'info'}
            showIcon
            message={poolCount === null ? '当前邮箱来源不提供池数量' : `可用邮箱 ${poolCount} 个`}
            description={bindPhoneAndGetRefreshToken
              ? '仅未绑定手机号的账号需要此模式：系统会先保存 AT，再使用 LeadBee 完成手机验证并获取 RT。'
              : '系统会直接完成已有账号 OAuth 登录并保存 AT + RT；已绑定手机号的账号无需卡密。'}
            style={{ marginBottom: 16 }}
          />
          <Form
            form={form}
            layout="vertical"
            initialValues={{
              count: 1,
              concurrency: 1,
              register_delay_seconds: 0,
              bind_phone_and_get_rt: false,
              use_sms_pool: false,
              leadbee_codes: '',
            }}
            onFinish={handleStart}
          >
            <Row gutter={12}>
              <Col span={8}>
                <Form.Item
                  name="count"
                  label="登录数量"
                  rules={[{ required: true, message: '请输入登录数量' }]}
                >
                  <InputNumber min={1} max={poolCount || undefined} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item
                  name="concurrency"
                  label="并发数"
                  rules={[{ required: true, message: '请输入并发数' }]}
                >
                  <InputNumber min={1} max={watchedCount} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item name="register_delay_seconds" label="启动间隔（秒）">
                  <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            </Row>

            <div
              style={{
                border: `1px solid ${bindPhoneAndGetRefreshToken ? token.colorPrimaryBorder : token.colorBorderSecondary}`,
                borderRadius: token.borderRadiusLG,
                background: bindPhoneAndGetRefreshToken
                  ? token.colorPrimaryBg
                  : token.colorFillAlter,
                padding: 16,
                marginBottom: bindPhoneAndGetRefreshToken ? 16 : 20,
                transition: 'border-color 160ms ease, background 160ms ease',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  justifyContent: 'space-between',
                  gap: 16,
                }}
              >
                <div>
                  <Typography.Text strong>
                    未绑定手机号时自动接码
                  </Typography.Text>
                  <Typography.Paragraph
                    type="secondary"
                    style={{ margin: '4px 0 0', fontSize: 13, lineHeight: 1.55 }}
                  >
                    仅用于未绑定手机号的账号；已绑定手机号请保持关闭，普通登录会直接获取 RT。
                  </Typography.Paragraph>
                </div>
                <Form.Item
                  name="bind_phone_and_get_rt"
                  valuePropName="checked"
                  noStyle
                >
                  <Switch aria-label="未绑定手机号时自动接码" />
                </Form.Item>
              </div>

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
                {bindPhoneAndGetRefreshToken ? (
                  <>
                    <Tag color="purple" bordered={false}>手机验证</Tag>
                    <Typography.Text type="secondary">→</Typography.Text>
                    <Tag color="green" bordered={false}>保存 AT + RT</Tag>
                  </>
                ) : (
                  <Tag color="green" bordered={false}>保存 AT + RT</Tag>
                )}
              </div>

              {bindPhoneAndGetRefreshToken && (
                <div
                  style={{
                    borderTop: `1px solid ${token.colorBorderSecondary}`,
                    marginTop: 14,
                    paddingTop: 14,
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      gap: 16,
                    }}
                  >
                    <div>
                      <Typography.Text strong>使用 SMS 接码池</Typography.Text>
                      <Typography.Paragraph
                        type="secondary"
                        style={{ margin: '3px 0 0', fontSize: 12 }}
                      >
                        后端自动领取未使用卡密，并使用卡片绑定的接码地址。
                      </Typography.Paragraph>
                    </div>
                    <Form.Item name="use_sms_pool" valuePropName="checked" noStyle>
                      <Switch aria-label="使用 SMS 接码池" />
                    </Form.Item>
                  </div>
                  {useSmsPool && (
                    <Alert
                      type={smsPoolAvailable !== null && smsPoolAvailable < watchedCount ? 'warning' : 'success'}
                      showIcon
                      message={smsPoolAvailable === null ? '正在读取接码池余量' : `可用卡密 ${smsPoolAvailable} 个`}
                      description={smsPoolAvailable !== null && smsPoolAvailable < watchedCount
                        ? `当前登录需要 ${watchedCount} 张卡密，请先到 SMS接码池补充。`
                        : '卡密仅在后端分配，登录任务不会在浏览器中暴露卡密原文。'}
                      style={{ marginTop: 12 }}
                    />
                  )}
                </div>
              )}
            </div>

            {bindPhoneAndGetRefreshToken && !useSmsPool && (
              <Form.Item
                name="leadbee_codes"
                preserve={false}
                label="LeadBee 接码卡密"
                dependencies={['count']}
                extra={(
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      gap: 12,
                      marginTop: 4,
                    }}
                  >
                    <span>一行一个卡密，按登录顺序分配</span>
                    <span
                      style={{
                        color: leadbeeCodes.length === watchedCount
                          ? token.colorSuccess
                          : token.colorTextSecondary,
                        whiteSpace: 'nowrap',
                      }}
                    >
                      已填写 {leadbeeCodes.length} / 需要 {watchedCount}
                    </span>
                  </div>
                )}
                rules={[
                  {
                    validator: (_, value) => {
                      const codes = parseLeadBeeCodes(value)
                      const count = Math.max(1, Number(form.getFieldValue('count') || 1))
                      if (codes.length !== count) {
                        return Promise.reject(new Error(
                          `卡密数量需与登录数量一致（需要 ${count} 个，当前 ${codes.length} 个）`,
                        ))
                      }
                      return Promise.resolve()
                    },
                  },
                ]}
              >
                <Input.TextArea
                  aria-label="LeadBee 接码卡密"
                  placeholder={'例如：\nbei-sms-xxxx-xxxx\nbei-sms-yyyy-yyyy'}
                  autoSize={{ minRows: 3, maxRows: 7 }}
                  autoComplete="off"
                  spellCheck={false}
                />
              </Form.Item>
            )}

            <Button
              type="primary"
              htmlType="submit"
              block
              loading={submitting}
              disabled={
                poolCount === 0
                || (
                  bindPhoneAndGetRefreshToken
                  && useSmsPool
                  && smsPoolAvailable !== null
                  && smsPoolAvailable < watchedCount
                )
              }
            >
              {bindPhoneAndGetRefreshToken ? '开始登录并接码' : '开始登录'}
            </Button>
          </Form>
        </>
      )}
    </Modal>
  )
}
