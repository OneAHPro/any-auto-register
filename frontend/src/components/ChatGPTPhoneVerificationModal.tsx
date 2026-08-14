import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Form, Input, Modal, Radio, Space, Typography, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'

import { apiFetch } from '@/lib/utils'

export type ChatGPTPhoneVerificationAccount = {
  id: number
  email?: string
  token?: string
  extra?: Record<string, unknown>
}

type Props = {
  open: boolean
  account: ChatGPTPhoneVerificationAccount | null
  onClose: () => void
  onSuccess: () => void
}

type PhoneSession = {
  session_id: string
  phone?: string
  provider?: 'manual' | 'leadbee'
  provider_mode?: string
  leadbee_api?: boolean
  automatic?: boolean
  status: 'starting' | 'code_sent' | 'verifying' | 'resending' | 'persisting' | 'completed' | 'failed' | 'expired'
  message: string
  resend_after: number
  expires_in: number
  phone_verified?: boolean
  exchange_code_consumed?: boolean
  reused?: boolean
  logs?: string[]
}

function normalizePhone(value: unknown) {
  return String(value || '').replace(/[\s()-]+/g, '')
}

function normalizeTruthy(value: unknown) {
  if (value === true) return true
  if (typeof value !== 'string' && typeof value !== 'number') return false
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase())
}

const exchangeLifecyclePattern = /卡密|兑换码|exchange[\s_-]*(?:code|card)|redemption[\s_-]*(?:code|card)|leadbee[\s_-]*code/i

function isLeadBeeApiSession(session: PhoneSession | null, configuredApiMode = false) {
  return configuredApiMode
    || normalizeTruthy(session?.leadbee_api)
    || String(session?.provider_mode || '').trim().toLowerCase() === 'api'
}

function leadBeeApiStatusMessage(session: PhoneSession) {
  switch (session.status) {
    case 'starting':
      return 'LeadBee API 正在自动接码'
    case 'code_sent':
      return 'LeadBee API 已收到短信验证码，正在继续验证'
    case 'verifying':
      return 'LeadBee API 正在提交短信验证码'
    case 'resending':
      return 'LeadBee API 正在重新获取短信验证码'
    case 'persisting':
      return 'LeadBee API 手机验证已完成，正在保存 Refresh Token'
    case 'completed':
      return session.phone_verified
        ? 'LeadBee API 手机验证完成，Refresh Token 已保存'
        : 'LeadBee API 流程完成，Refresh Token 已保存'
    case 'expired':
      return 'LeadBee API 自动接码会话已过期'
    default:
      return 'LeadBee API 自动接码失败'
  }
}

function visibleSessionMessage(session: PhoneSession, leadBeeApiSession: boolean) {
  const rawMessage = String(session.message || '').trim()
  if (!leadBeeApiSession) return rawMessage
  return rawMessage && !exchangeLifecyclePattern.test(rawMessage)
    ? rawMessage
    : leadBeeApiStatusMessage(session)
}

function visibleSessionLogs(session: PhoneSession | null, leadBeeApiSession: boolean) {
  const logs = Array.isArray(session?.logs) ? session.logs : []
  if (!leadBeeApiSession) return logs
  return logs.map((line) => {
    if (!exchangeLifecyclePattern.test(line)) return line
    const timestamp = line.match(/^\[[^\]]+\]\s*/)?.[0] || ''
    return `${timestamp}LeadBee API 手机验证进度已更新`
  })
}

function errorMessage(error: unknown, fallback: string) {
  if (error && typeof error === 'object' && 'message' in error) {
    const messageText = String(error.message || '').trim()
    if (messageText) return messageText
  }
  return fallback
}

function shouldPollSession(status: PhoneSession['status']) {
  return status === 'starting'
    || status === 'verifying'
    || status === 'resending'
    || status === 'persisting'
}

export function ChatGPTPhoneVerificationModal({
  open,
  account,
  onClose,
  onSuccess,
}: Props) {
  const [form] = Form.useForm()
  const [mode, setMode] = useState<'manual' | 'leadbee'>('manual')
  const [leadBeeApiEnabled, setLeadBeeApiEnabled] = useState<boolean | null>(null)
  const [session, setSession] = useState<PhoneSession | null>(null)
  const [sending, setSending] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [resending, setResending] = useState(false)
  const [countdown, setCountdown] = useState(0)
  const [requestError, setRequestError] = useState('')
  const pollTimer = useRef<number | null>(null)
  const logBox = useRef<HTMLDivElement | null>(null)
  const completedSession = useRef('')
  const lifecycleGeneration = useRef(0)

  const stopPolling = () => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current)
      pollTimer.current = null
    }
  }

  useEffect(() => {
    const generation = lifecycleGeneration.current + 1
    lifecycleGeneration.current = generation
    stopPolling()
    if (!open) return
    form.resetFields()
    setMode('manual')
    setLeadBeeApiEnabled(null)
    setSession(null)
    setSending(false)
    setSubmitting(false)
    setResending(false)
    setCountdown(0)
    setRequestError('')
    completedSession.current = ''

    const loadConfig = async () => {
      try {
        const config = await apiFetch('/config') as Record<string, unknown> | null
        if (lifecycleGeneration.current === generation) {
          setLeadBeeApiEnabled(normalizeTruthy(config?.leadbee_api_enabled))
        }
      } catch {
        if (lifecycleGeneration.current === generation) setLeadBeeApiEnabled(false)
      }
    }
    void loadConfig()

    return () => {
      if (lifecycleGeneration.current === generation) {
        lifecycleGeneration.current += 1
      }
      stopPolling()
    }
  }, [account?.id, form, open])

  useEffect(() => {
    if (!open || countdown <= 0) return
    const timer = window.setInterval(() => {
      setCountdown((value) => Math.max(0, value - 1))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [countdown, open])

  useEffect(() => {
    if (!logBox.current) return
    logBox.current.scrollTop = logBox.current.scrollHeight
  }, [session?.logs])

  const finishSuccess = (result: PhoneSession) => {
    stopPolling()
    setSession(result)
    if (completedSession.current !== result.session_id) {
      completedSession.current = result.session_id
      const apiSession = isLeadBeeApiSession(
        result,
        mode === 'leadbee' && leadBeeApiEnabled === true,
      )
      message.success(visibleSessionMessage(result, apiSession) || 'Refresh Token 已保存')
      onSuccess()
    }
  }

  const applySession = (result: PhoneSession) => {
    if (result.phone) form.setFieldValue('phone', result.phone)
    setRequestError('')
    setSession(result)
    setCountdown(Math.max(0, Number(result.resend_after || 0)))
    if (result.status === 'completed') {
      finishSuccess(result)
      return
    }
    if (result.status === 'failed' || result.status === 'expired') {
      stopPolling()
      const apiSession = isLeadBeeApiSession(
        result,
        mode === 'leadbee' && leadBeeApiEnabled === true,
      )
      message.error(visibleSessionMessage(result, apiSession) || '手机验证失败')
    }
  }

  const pollSession = (
    sessionId: string,
    generation = lifecycleGeneration.current,
    leadBeeApiMode = mode === 'leadbee' && leadBeeApiEnabled === true,
  ) => {
    stopPolling()
    pollTimer.current = window.setTimeout(async () => {
      if (generation !== lifecycleGeneration.current || !account?.id) return
      try {
        const result = await apiFetch(
          `/chatgpt/${account.id}/phone-verification/${sessionId}`,
        ) as PhoneSession
        if (generation !== lifecycleGeneration.current) return
        applySession(result)
        if (shouldPollSession(result.status)) {
          pollSession(
            sessionId,
            generation,
            isLeadBeeApiSession(result, leadBeeApiMode),
          )
        }
      } catch (error) {
        if (generation !== lifecycleGeneration.current) return
        const detail = leadBeeApiMode
          ? '读取 LeadBee API 手机验证状态失败'
          : errorMessage(error, '读取手机验证状态失败')
        setRequestError(detail)
        message.error(detail)
      }
    }, 1200)
  }

  const handleSend = async () => {
    if (!account?.id) return
    if (mode === 'leadbee' && leadBeeApiEnabled === null) return
    const generation = lifecycleGeneration.current
    const leadBeeApiMode = mode === 'leadbee' && leadBeeApiEnabled === true
    let values: Record<string, unknown> = {}
    try {
      if (mode === 'manual') {
        values = await form.validateFields(['phone'])
      } else if (!leadBeeApiMode) {
        values = await form.validateFields(['leadbee_code'])
      }
    } catch {
      return
    }
    if (generation !== lifecycleGeneration.current) return
    const body = mode === 'leadbee'
      ? leadBeeApiMode
        ? { leadbee_api: true }
        : { leadbee_code: String(values.leadbee_code || '').trim() }
      : { phone: normalizePhone(values.phone) }
    setRequestError('')
    setSending(true)
    try {
      const result = await apiFetch(
        `/chatgpt/${account.id}/phone-verification/start`,
        {
          method: 'POST',
          body: JSON.stringify(body),
        },
      ) as PhoneSession
      if (generation !== lifecycleGeneration.current) return
      applySession(result)
      if (shouldPollSession(result.status)) {
        pollSession(
          result.session_id,
          generation,
          isLeadBeeApiSession(result, leadBeeApiMode),
        )
      }
    } catch (error) {
      if (generation !== lifecycleGeneration.current) return
      const detail = leadBeeApiMode
        ? 'LeadBee API 自动接码启动失败'
        : errorMessage(error, '短信验证码发送失败')
      setRequestError(detail)
      message.error(detail)
    } finally {
      if (generation === lifecycleGeneration.current) setSending(false)
    }
  }

  const handleSubmit = async () => {
    if (!account?.id || !session?.session_id) return
    const generation = lifecycleGeneration.current
    let values
    try {
      values = await form.validateFields(['code'])
    } catch {
      return
    }
    if (generation !== lifecycleGeneration.current) return
    setSubmitting(true)
    try {
      const result = await apiFetch(
        `/chatgpt/${account.id}/phone-verification/${session.session_id}/submit`,
        {
          method: 'POST',
          body: JSON.stringify({ code: String(values.code || '').trim() }),
        },
      ) as PhoneSession
      if (generation !== lifecycleGeneration.current) return
      applySession(result)
      if (result.status === 'code_sent' && result.message) {
        message.error(result.message)
      } else if (shouldPollSession(result.status)) {
        pollSession(
          result.session_id,
          generation,
          isLeadBeeApiSession(result, leadBeeApiSession),
        )
      }
    } catch (error) {
      if (generation !== lifecycleGeneration.current) return
      const detail = errorMessage(error, '短信验证码提交失败')
      setRequestError(detail)
      message.error(detail)
    } finally {
      if (generation === lifecycleGeneration.current) setSubmitting(false)
    }
  }

  const handleResend = async () => {
    if (!account?.id || !session?.session_id || countdown > 0) return
    const generation = lifecycleGeneration.current
    setResending(true)
    try {
      const result = await apiFetch(
        `/chatgpt/${account.id}/phone-verification/${session.session_id}/resend`,
        { method: 'POST', body: JSON.stringify({}) },
      ) as PhoneSession
      if (generation !== lifecycleGeneration.current) return
      applySession(result)
      if (result.status === 'code_sent') message.success(result.message || '验证码已重新发送')
    } catch (error) {
      if (generation !== lifecycleGeneration.current) return
      const detail = errorMessage(error, '重新发送失败')
      setRequestError(detail)
      message.error(detail)
    } finally {
      if (generation === lifecycleGeneration.current) setResending(false)
    }
  }

  const close = () => {
    if (sending || submitting || resending) return
    lifecycleGeneration.current += 1
    stopPolling()
    form.resetFields()
    setSession(null)
    completedSession.current = ''
    onClose()
  }

  const copyLogs = async () => {
    const apiSession = isLeadBeeApiSession(
      session,
      mode === 'leadbee' && leadBeeApiEnabled === true,
    )
    const logs = visibleSessionLogs(session, apiSession)
    if (!logs.length) return
    try {
      await navigator.clipboard.writeText(logs.join('\n'))
      message.success('接码日志已复制')
    } catch {
      message.error('复制日志失败')
    }
  }

  const codeReady = session?.status === 'code_sent'
  const leadBeeApiSession = isLeadBeeApiSession(
    session,
    mode === 'leadbee' && leadBeeApiEnabled === true,
  )
  const sessionMessage = session
    ? visibleSessionMessage(session, leadBeeApiSession)
    : ''
  const sessionLogs = visibleSessionLogs(session, leadBeeApiSession)
  const automatic = Boolean(
    session?.automatic
      || session?.provider === 'leadbee'
      || mode === 'leadbee'
      || leadBeeApiSession,
  )
  const resumeContext = account?.extra?.oauth_resume_context
  const resumeRecord = resumeContext && typeof resumeContext === 'object'
    ? resumeContext as Record<string, unknown>
    : null
  const resumeFlowState = resumeRecord?.flow_state && typeof resumeRecord.flow_state === 'object'
    ? resumeRecord.flow_state as Record<string, unknown>
    : null
  const hasPreparedSecrets = Boolean(
    String(resumeRecord?.code_verifier || '').trim()
      && String(resumeRecord?.oauth_state || '').trim(),
  )
  const resumeReady = Boolean(
    Number(resumeRecord?.version || 0) === 2
      && Number(resumeRecord?.expires_at || 0) > Date.now() / 1000
      && (resumeRecord?.ready === true || hasPreparedSecrets)
      && String(resumeFlowState?.page_type || '').trim(),
  )

  return (
    <Modal
      title={`手机验证${account?.email ? ` · ${account.email}` : ''}`}
      open={open}
      onCancel={close}
      footer={null}
      width={720}
      maskClosable={false}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        message="AT 已安全保存"
        description="手机验证仅用于补齐 Refresh Token；发送或校验失败不会覆盖当前 Access Token。"
        style={{ marginBottom: 16 }}
      />
      {requestError ? (
        <Alert
          type="error"
          showIcon
          message={requestError}
          style={{ marginBottom: 16 }}
        />
      ) : null}
      {!resumeReady ? (
        <Alert
          type="warning"
          showIcon
          message="当前账号缺少可续接的手机授权事务"
          description={leadBeeApiEnabled === false
            ? '请先关闭窗口，点击页面顶部“登录”重新登录该邮箱。登录成功后会同时准备手机授权事务；准备完成前系统不会获取手机号、使用 LeadBee 兑换码或发送短信。'
            : '请先关闭窗口，点击页面顶部“登录”重新登录该邮箱。登录成功后会同时准备手机授权事务；准备完成前系统不会启动 LeadBee API 自动接码或发送短信。'}
        />
      ) : (
      <Form form={form} layout="vertical">
        <Form.Item label="接码方式">
          <Radio.Group
            value={mode}
            disabled={Boolean(session) || sending}
            onChange={(event) => {
              setMode(event.target.value)
              form.resetFields(['phone', 'leadbee_code', 'code'])
              setRequestError('')
            }}
          >
            <Radio value="manual">手动填写手机号</Radio>
            <Radio
              value="leadbee"
              disabled={leadBeeApiEnabled === null || Boolean(session) || sending}
            >
              {leadBeeApiEnabled ? 'LeadBee API 自动接码' : 'LeadBee 自动接码'}
            </Radio>
          </Radio.Group>
        </Form.Item>

        {mode === 'manual' ? (
          <Form.Item
            name="phone"
            label="手机号码"
            normalize={normalizePhone}
            rules={[
              { required: true, message: '请输入手机号码' },
              {
                validator: async (_, value) => {
                  if (/^\+[1-9]\d{7,14}$/.test(normalizePhone(value))) return
                  throw new Error('请输入 E.164 国际格式手机号，例如 +447456344799')
                },
              },
            ]}
          >
            <Input
              placeholder="+447456344799"
              disabled={Boolean(session) || sending}
              autoComplete="tel"
            />
          </Form.Item>
        ) : null}

        {!session && mode === 'leadbee' && leadBeeApiEnabled === false ? (
          <Form.Item
            name="leadbee_code"
            label="LeadBee 兑换码"
            extra="系统会自动取号、等待短信并提交验证码；每次只使用一个兑换码。"
            rules={[
              { required: true, whitespace: true, message: '请输入 LeadBee 兑换码' },
            ]}
          >
            <Input.Password
              placeholder="bei-sms-XXXX-XXXX"
              autoComplete="off"
              disabled={sending}
            />
          </Form.Item>
        ) : null}

        {session?.status === 'completed' ? (
          <>
            <Alert
              type={session.phone_verified ? 'success' : 'warning'}
              showIcon
              message={sessionMessage || 'Refresh Token 已保存'}
              description={
                session.phone_verified
                  ? '本次已实际提交并通过手机号验证码。'
                  : '本次未新增或验证手机号。该账号可能此前已绑定手机号，或 OpenAI 当前未要求绑定。'
              }
              style={{ marginBottom: 16 }}
            />
            {session.phone_verified && session.phone ? (
              <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 16 }}>
                已验证号码：{session.phone}
              </Typography.Text>
            ) : null}
            <Button type="primary" block onClick={close}>
              关闭
            </Button>
          </>
        ) : !session ? (
          <Button type="primary" block loading={sending} onClick={handleSend}>
            {mode === 'leadbee' ? '开始自动接码' : '获取验证码'}
          </Button>
        ) : automatic ? (
          <>
            <Alert
              type={session.status === 'failed' || session.status === 'expired' ? 'error' : 'info'}
              showIcon
              message={sessionMessage || '正在自动接码'}
              style={{ marginBottom: 16 }}
            />
            {session.phone ? (
              <Typography.Text type="secondary">
                当前号码：{session.phone}
              </Typography.Text>
            ) : null}
          </>
        ) : (
          <>
            <Alert
              type={session.status === 'failed' || session.status === 'expired' ? 'error' : 'success'}
              showIcon
              message={sessionMessage || '正在处理手机验证'}
              style={{ marginBottom: 16 }}
            />
            <Form.Item
              name="code"
              label="短信验证码"
              rules={[
                { required: true, message: '请输入短信验证码' },
                { pattern: /^\d{4,8}$/, message: '请输入 4 至 8 位数字验证码' },
              ]}
            >
              <Input
                inputMode="numeric"
                maxLength={8}
                placeholder="请输入短信验证码"
                disabled={!codeReady}
              />
            </Form.Item>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Button
                type="primary"
                block
                loading={submitting}
                disabled={!codeReady}
                onClick={handleSubmit}
              >
                提交验证码
              </Button>
              <Button
                type="link"
                block
                loading={resending}
                disabled={!codeReady || countdown > 0}
                onClick={handleResend}
              >
                {countdown > 0 ? `${countdown} 秒后可重新发送` : '重新发送'}
              </Button>
              {session.expires_in > 0 ? (
                <Typography.Text type="secondary" style={{ textAlign: 'center' }}>
                  当前验证会话约 {Math.ceil(session.expires_in / 60)} 分钟后过期
                </Typography.Text>
              ) : null}
            </Space>
          </>
        )}
        {sessionLogs.length > 0 ? (
          <div style={{ marginTop: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <Typography.Text strong>接码日志</Typography.Text>
              <Button aria-label="复制日志" size="small" icon={<CopyOutlined />} onClick={copyLogs}>
                复制日志
              </Button>
            </div>
            <div
              ref={logBox}
              style={{
                minHeight: 150,
                maxHeight: 280,
                overflowY: 'auto',
                padding: '12px 14px',
                border: '1px solid rgba(255, 255, 255, 0.14)',
                borderRadius: 8,
                background: 'rgba(0, 0, 0, 0.28)',
                color: 'rgba(255, 255, 255, 0.88)',
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                fontSize: 13,
                lineHeight: 1.65,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {sessionLogs.map((line, index) => (
                <div key={`${index}-${line}`}>{line}</div>
              ))}
            </div>
          </div>
        ) : null}
      </Form>
      )}
    </Modal>
  )
}
