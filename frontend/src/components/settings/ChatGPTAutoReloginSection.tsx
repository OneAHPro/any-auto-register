import { useEffect, useState } from 'react'
import { Card, Descriptions, Divider, Form, Input, InputNumber, Switch, Tag, Typography } from 'antd'

import { apiFetch } from '@/lib/utils'

interface ChatGPTAutoReloginStatus {
  state?: string
  eligible_accounts?: number
  last_task_id?: string | null
  last_started_at?: string | null
  next_run_at?: string | null
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
  const [status, setStatus] = useState<ChatGPTAutoReloginStatus | null>(null)
  const [statusError, setStatusError] = useState(false)

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
        label="自动重登间隔（分钟）"
        initialValue={10}
      >
        <InputNumber aria-label="自动重登间隔（分钟）" min={10} max={1440} precision={0} style={{ width: '100%' }} />
      </Form.Item>
      <Form.Item
        name="chatgpt_auto_relogin_concurrency"
        label="自动重登并发数"
        initialValue={10}
      >
        <InputNumber aria-label="自动重登并发数" min={1} max={10} precision={0} style={{ width: '100%' }} />
      </Form.Item>

      <Typography.Paragraph type="secondary">
        每轮优先刷新全部账号 RT；仅服务端明确判定 RT 失效时才获取验证码完整登录。前台新增邮箱、注册、登录和接码任务优先执行。
      </Typography.Paragraph>

      <Divider orientation="left">邮件告警</Divider>
      <Form.Item
        name="chatgpt_auto_relogin_alert_threshold"
        label="邮件告警阈值（账号数）"
        initialValue={5}
        extra="RT 明确失效数或完整重登失败数达到此值时，每轮最多发送一封。"
      >
        <InputNumber aria-label="邮件告警阈值（账号数）" min={1} max={10000} precision={0} style={{ width: '100%' }} />
      </Form.Item>
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
