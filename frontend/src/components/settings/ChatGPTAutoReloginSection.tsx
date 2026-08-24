import { useEffect, useState } from 'react'
import { Button, Card, Descriptions, Divider, Form, Input, InputNumber, Switch, Tag, Typography } from 'antd'

import { apiFetch } from '@/lib/utils'

interface ChatGPTAutoReloginStatus {
  state?: string
  eligible_accounts?: number
  last_task_id?: string | null
  last_started_at?: string | null
  next_run_at?: string | null
}

const SMTP_FORM_FIELDS = [
  'smtp_host',
  'smtp_port',
  'smtp_username',
  'smtp_password',
  'smtp_sender_email',
  'smtp_recipient_email',
  'smtp_use_ssl',
  'smtp_force_auth_login',
] as const

const BARK_FORM_FIELDS = [
  'bark_enabled',
  'bark_endpoint',
] as const

function requestError(error: unknown, fallback: string) {
  if (error instanceof Error && error.message.trim()) return error.message.trim()
  return fallback
}

function formatTime(value: string | null | undefined) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(date)
}

function statusLabel(status: string | undefined) {
  switch (status) {
    case 'disabled':
      return '已关闭'
    case 'idle':
      return '空闲'
    case 'running':
      return '运行中'
    case 'paused_no_accounts':
      return '已暂停：没有可登录账号'
    default:
      return status || '未知'
  }
}

export default function ChatGPTAutoReloginSection() {
  const form = Form.useFormInstance()
  const [status, setStatus] = useState<ChatGPTAutoReloginStatus | null>(null)
  const [statusError, setStatusError] = useState(false)
  const [smtpTestSending, setSmtpTestSending] = useState(false)
  const [smtpTestResult, setSmtpTestResult] = useState<{
    type: 'success' | 'error'
    message: string
  } | null>(null)
  const [barkTestSending, setBarkTestSending] = useState(false)
  const [barkTestResult, setBarkTestResult] = useState<{
    type: 'success' | 'error'
    message: string
  } | null>(null)

  useEffect(() => {
    let mounted = true
    let refreshInFlight = false

    const refresh = async () => {
      if (!mounted || refreshInFlight) return
      refreshInFlight = true
      try {
        const data = await apiFetch('/automations/chatgpt-relogin') as ChatGPTAutoReloginStatus
        if (!mounted) return
        setStatus(data)
        setStatusError(false)
      } catch {
        if (mounted) setStatusError(true)
      } finally {
        refreshInFlight = false
      }
    }

    void refresh()
    const timer = window.setInterval(() => void refresh(), 5_000)
    return () => {
      mounted = false
      window.clearInterval(timer)
    }
  }, [])

  const sendSmtpTest = async () => {
    if (smtpTestSending) return
    setSmtpTestSending(true)
    setSmtpTestResult(null)
    try {
      const data = form.getFieldsValue([...SMTP_FORM_FIELDS])
      const response = await apiFetch('/config/smtp/test', {
        method: 'POST',
        body: JSON.stringify({ data }),
      }) as { message?: string }
      setSmtpTestResult({
        type: 'success',
        message: response.message || '测试邮件已发送',
      })
    } catch (error: unknown) {
      setSmtpTestResult({
        type: 'error',
        message: requestError(error, 'SMTP 测试邮件发送失败'),
      })
    } finally {
      setSmtpTestSending(false)
    }
  }

  const sendBarkTest = async () => {
    if (barkTestSending) return
    setBarkTestSending(true)
    setBarkTestResult(null)
    try {
      const data = form.getFieldsValue([...BARK_FORM_FIELDS])
      const response = await apiFetch('/config/bark/test', {
        method: 'POST',
        body: JSON.stringify({ data }),
      }) as { message?: string }
      setBarkTestResult({
        type: 'success',
        message: response.message || '测试 Bark 强提醒已发送',
      })
    } catch (error: unknown) {
      setBarkTestResult({
        type: 'error',
        message: requestError(error, 'Bark 测试通知发送失败'),
      })
    } finally {
      setBarkTestSending(false)
    }
  }

  return (
    <Card
      title="ChatGPT 自动重登"
      extra={<Typography.Text type="secondary">前台任务优先</Typography.Text>}
      style={{ marginBottom: 16 }}
    >
      <Form.Item
        name="chatgpt_auto_relogin_enabled"
        label="启用自动重登"
        valuePropName="checked"
        initialValue={false}
      >
        <Switch aria-label="启用 ChatGPT 自动重登" checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
      <Form.Item
        name="chatgpt_auto_relogin_interval_minutes"
        label="Codex2API 鉴权巡检间隔（分钟）"
        initialValue={2}
      >
        <InputNumber aria-label="Codex2API 鉴权巡检间隔（分钟）" min={2} max={1440} precision={0} style={{ width: '100%' }} />
      </Form.Item>
      <Form.Item
        name="chatgpt_auto_relogin_concurrency"
        label="异常账号重登并发数"
        initialValue={3}
      >
        <InputNumber aria-label="异常账号重登并发数" min={1} max={3} precision={0} style={{ width: '100%' }} />
      </Form.Item>

      <Typography.Paragraph type="secondary">
        每轮主动触发 Codex2API 的 wham-only 轻量鉴权探针，正常与限流账号不会刷新本地 RT。发现 401 后先让 Codex2API 用自身 RT 自刷新；仍明确鉴权失效时才获取验证码完整登录并覆盖同步。前台新增邮箱、注册、登录和接码任务优先执行。
      </Typography.Paragraph>

      <Divider orientation="left">告警通知</Divider>
      <Form.Item
        name="chatgpt_auto_relogin_alert_threshold"
        label="重登失败告警阈值（账号数）"
        initialValue={20}
        extra="每轮自动鉴权完成后，重登失败账号数达到或超过此值时通过已启用的通知渠道发送提醒；鉴权失败数仅展示，不触发告警。"
      >
        <InputNumber aria-label="重登失败告警阈值（账号数）" min={1} max={10000} precision={0} style={{ width: '100%' }} />
      </Form.Item>
      <Form.Item
        name="chatgpt_auto_relogin_quota_alert_threshold_usd"
        label="Codex2API 剩余额度告警阈值（美元）"
        initialValue={0}
        extra="设置为 0 时关闭额度不足告警；每轮自动鉴权结束后，额度低于此值都会通过已启用的通知渠道发送提醒。"
      >
        <InputNumber
          aria-label="Codex2API 剩余额度告警阈值（美元）"
          min={0}
          max={10000000}
          precision={2}
          step={0.01}
          prefix="$"
          style={{ width: '100%' }}
        />
      </Form.Item>
      <Divider orientation="left" plain>Bark 强提醒</Divider>
      <Form.Item
        name="bark_enabled"
        label="启用 Bark 强提醒"
        valuePropName="checked"
        initialValue={false}
      >
        <Switch aria-label="启用 Bark 强提醒" checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
      <Form.Item
        name="bark_endpoint"
        label="Bark 推送地址"
        extra="粘贴 Bark App 提供的完整地址；留空保留已保存地址。两类业务告警固定使用 critical + call=1 强提醒，并持续响铃约 30 秒。"
      >
        <Input.Password
          aria-label="Bark 推送地址"
          autoComplete="new-password"
          placeholder="https://api.day.app/你的Key"
        />
      </Form.Item>
      <Button onClick={() => void sendBarkTest()} loading={barkTestSending}>
        发送测试 Bark 通知
      </Button>
      {barkTestResult ? (
        <Typography.Text
          role={barkTestResult.type === 'error' ? 'alert' : 'status'}
          aria-live={barkTestResult.type === 'error' ? 'assertive' : 'polite'}
          type={barkTestResult.type === 'error' ? 'danger' : 'success'}
          style={{ display: 'block', marginTop: 10 }}
        >
          {barkTestResult.message}
        </Typography.Text>
      ) : null}

      <Divider orientation="left" plain>邮件通知</Divider>
      <Form.Item name="smtp_host" label="SMTP 服务器地址">
        <Input aria-label="SMTP 服务器地址" placeholder="smtp.example.com" />
      </Form.Item>
      <Form.Item name="smtp_port" label="SMTP 端口" initialValue={587}>
        <InputNumber aria-label="SMTP 端口" min={1} max={65535} precision={0} style={{ width: '100%' }} />
      </Form.Item>
      <Form.Item name="smtp_username" label="SMTP 账号">
        <Input aria-label="SMTP 账号" autoComplete="username" />
      </Form.Item>
      <Form.Item name="smtp_password" label="SMTP 访问凭证">
        <Input.Password
          aria-label="SMTP 访问凭证"
          autoComplete="new-password"
          placeholder="留空保留已配置凭证；敏感信息不会回显"
        />
      </Form.Item>
      <Form.Item name="smtp_sender_email" label="SMTP 发送者邮箱">
        <Input aria-label="SMTP 发送者邮箱" type="email" />
      </Form.Item>
      <Form.Item name="smtp_recipient_email" label="告警接收邮箱">
        <Input aria-label="告警接收邮箱" type="email" placeholder="留空时使用 SMTP 账号" />
      </Form.Item>
      <Form.Item name="smtp_use_ssl" label="启用 SMTP 加密" valuePropName="checked" initialValue>
        <Switch aria-label="启用 SMTP 加密" checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
      <Form.Item name="smtp_force_auth_login" label="强制使用 AUTH LOGIN" valuePropName="checked" initialValue={false}>
        <Switch aria-label="强制使用 AUTH LOGIN" checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
      <Button onClick={() => void sendSmtpTest()} loading={smtpTestSending}>
        发送测试邮件
      </Button>
      {smtpTestResult ? (
        <Typography.Text
          role={smtpTestResult.type === 'error' ? 'alert' : 'status'}
          aria-live={smtpTestResult.type === 'error' ? 'assertive' : 'polite'}
          type={smtpTestResult.type === 'error' ? 'danger' : 'success'}
          style={{ display: 'block', marginTop: 10 }}
        >
          {smtpTestResult.message}
        </Typography.Text>
      ) : null}

      <div role="status" aria-live="polite" aria-atomic="true">
        <Descriptions size="small" column={1} bordered>
          <Descriptions.Item label="当前状态">
            <Tag color={status?.state === 'running' ? 'processing' : status?.state === 'paused_no_accounts' ? 'warning' : 'default'}>
              {statusLabel(status?.state)}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="可登录账号">{status?.eligible_accounts ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="最近任务 ID">{status?.last_task_id || '-'}</Descriptions.Item>
          <Descriptions.Item label="最近启动时间">{formatTime(status?.last_started_at)}</Descriptions.Item>
          <Descriptions.Item label="下次运行时间">{formatTime(status?.next_run_at)}</Descriptions.Item>
        </Descriptions>
      </div>
      {statusError ? (
        <Typography.Text role="alert" aria-live="assertive" type="secondary" style={{ display: 'block', marginTop: 12 }}>
          状态暂时不可用，将自动重试。
        </Typography.Text>
      ) : null}
    </Card>
  )
}
