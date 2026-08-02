// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { apiFetch } from '@/lib/utils'
import { ChatGPTPhoneVerificationModal } from './ChatGPTPhoneVerificationModal'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
}))

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  const getComputedStyle = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => getComputedStyle(element))
})

const account = {
  id: 17,
  email: 'existing@example.com',
  token: 'access-token',
  extra: {
    access_token: 'access-token',
    refresh_token: '',
    oauth_resume_context: {
      version: 2,
      expires_at: 4102444800,
      code_verifier: 'prepared-verifier',
      oauth_state: 'prepared-state',
      flow_state: { page_type: 'add_phone' },
    },
  },
}

describe('ChatGPTPhoneVerificationModal', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
  })

  afterEach(() => {
    cleanup()
  })

  it('rejects a phone number without E.164 international prefix', async () => {
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    await user.type(screen.getByLabelText('手机号码'), '13800138000')
    await user.click(screen.getByRole('button', { name: '获取验证码' }))

    expect(await screen.findByText('请输入 E.164 国际格式手机号，例如 +447456344799')).toBeTruthy()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('blocks a legacy account before any phone or LeadBee request can start', async () => {
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={{
          ...account,
          extra: { access_token: 'access-token', refresh_token: '' },
        }}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    expect(await screen.findByText('当前账号缺少可续接的手机授权事务')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '获取验证码' })).toBeNull()
    expect(screen.queryByRole('button', { name: '开始自动接码' })).toBeNull()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('blocks a version 1 cookie snapshot before LeadBee can consume a code', async () => {
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={{
          ...account,
          extra: {
            access_token: 'access-token',
            refresh_token: '',
            oauth_resume_context: { version: 1, expires_at: 4102444800 },
          },
        }}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    expect(await screen.findByText('当前账号缺少可续接的手机授权事务')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '开始自动接码' })).toBeNull()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('accepts the redacted ready marker returned by the account API', async () => {
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={{
          ...account,
          extra: {
            access_token: 'access-token',
            refresh_token: '',
            oauth_resume_context: {
              version: 2,
              expires_at: 4102444800,
              ready: true,
              flow_state: { page_type: 'add_phone' },
            },
          },
        }}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    expect(await screen.findByRole('button', { name: '获取验证码' })).toBeTruthy()
    expect(screen.queryByText('当前账号缺少可续接的手机授权事务')).toBeNull()
  })

  it('sends, submits and completes phone verification', async () => {
    const onClose = vi.fn()
    const onSuccess = vi.fn()
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.endsWith('/phone-verification/start')) {
        return {
          session_id: 'phone-session-1',
          status: 'code_sent',
          message: '短信验证码已发送',
          resend_after: 0,
          expires_in: 600,
        }
      }
      if (path.endsWith('/phone-session-1/submit')) {
        return {
          session_id: 'phone-session-1',
          status: 'completed',
          message: '手机验证完成，Refresh Token 已保存',
          phone_verified: true,
          exchange_code_consumed: false,
          resend_after: 0,
          expires_in: 500,
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    )

    await user.type(screen.getByLabelText('手机号码'), '+447456344799')
    await user.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByText('短信验证码已发送')).toBeTruthy()
    expect(screen.getByRole('button', { name: '重新发送' })).toBeTruthy()

    await user.type(screen.getByLabelText('短信验证码'), '654321')
    await user.click(screen.getByRole('button', { name: '提交验证码' }))

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledOnce()
      expect(onClose).not.toHaveBeenCalled()
    })
    expect(screen.getByText('手机验证完成，Refresh Token 已保存')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /关\s*闭/ }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('starts LeadBee automatic receiving with an exchange code', async () => {
    const onClose = vi.fn()
    const onSuccess = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue({
      session_id: 'leadbee-session-1',
      provider: 'leadbee',
      automatic: true,
      phone: '+447456344799',
      status: 'completed',
      message: '手机验证完成，Refresh Token 已保存',
      phone_verified: true,
      exchange_code_consumed: true,
      resend_after: 0,
      expires_in: 500,
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    )

    await user.click(screen.getByRole('radio', { name: 'LeadBee 自动接码' }))
    await user.type(screen.getByLabelText('LeadBee 兑换码'), 'bei-sms-DEMO-CODE')
    await user.click(screen.getByRole('button', { name: '开始自动接码' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        '/chatgpt/17/phone-verification/start',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ leadbee_code: 'bei-sms-DEMO-CODE' }),
        }),
      )
      expect(onSuccess).toHaveBeenCalledOnce()
      expect(onClose).not.toHaveBeenCalled()
    })
    await user.click(screen.getByRole('button', { name: /关\s*闭/ }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('keeps polling while Refresh Token persistence is in progress', async () => {
    const onSuccess = vi.fn()
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.endsWith('/phone-verification/start')) {
        return {
          session_id: 'leadbee-persisting-session',
          provider: 'leadbee',
          automatic: true,
          phone: '+447456344799',
          status: 'persisting',
          message: '手机号验证已完成，正在安全保存 Refresh Token',
          phone_verified: true,
          exchange_code_consumed: true,
          resend_after: 0,
          expires_in: 500,
        }
      }
      if (path.endsWith('/leadbee-persisting-session')) {
        return {
          session_id: 'leadbee-persisting-session',
          provider: 'leadbee',
          automatic: true,
          phone: '+447456344799',
          status: 'completed',
          message: '手机验证完成，Refresh Token 已保存',
          phone_verified: true,
          exchange_code_consumed: true,
          resend_after: 0,
          expires_in: 499,
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={() => {}}
        onSuccess={onSuccess}
      />,
    )

    await user.click(screen.getByRole('radio', { name: 'LeadBee 自动接码' }))
    await user.type(screen.getByLabelText('LeadBee 兑换码'), 'bei-sms-PERSISTING')
    await user.click(screen.getByRole('button', { name: '开始自动接码' }))

    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce(), { timeout: 2500 })
    expect(apiFetch).toHaveBeenCalledWith(
      '/chatgpt/17/phone-verification/leadbee-persisting-session',
    )
  })

  it('keeps an unused LeadBee result visible and does not claim a new phone binding', async () => {
    const onClose = vi.fn()
    const onSuccess = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue({
      session_id: 'leadbee-session-unused',
      provider: 'leadbee',
      automatic: true,
      phone: '',
      status: 'completed',
      message: 'OpenAI 未要求新增手机号，LeadBee 兑换码未使用；Refresh Token 已保存',
      phone_verified: false,
      exchange_code_consumed: false,
      resend_after: 0,
      expires_in: 500,
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    )

    await user.click(screen.getByRole('radio', { name: 'LeadBee 自动接码' }))
    await user.type(screen.getByLabelText('LeadBee 兑换码'), 'bei-sms-UNUSED-CODE')
    await user.click(screen.getByRole('button', { name: '开始自动接码' }))

    expect(await screen.findByText(/LeadBee 兑换码未使用/)).toBeTruthy()
    expect(screen.getByText(/本次未新增或验证手机号/)).toBeTruthy()
    expect(onSuccess).toHaveBeenCalledOnce()
    expect(onClose).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /关\s*闭/ }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('keeps an invalid verification code in the form without submitting it', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      session_id: 'phone-session-1',
      status: 'code_sent',
      message: '短信验证码已发送',
      resend_after: 0,
      expires_in: 600,
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    await user.type(screen.getByLabelText('手机号码'), '+447456344799')
    await user.click(screen.getByRole('button', { name: '获取验证码' }))
    await user.type(await screen.findByLabelText('短信验证码'), '12')
    await user.click(screen.getByRole('button', { name: '提交验证码' }))

    expect(await screen.findByText('请输入 4 至 8 位数字验证码')).toBeTruthy()
    expect(apiFetch).toHaveBeenCalledTimes(1)
  })

  it('restores an active session without sending another SMS', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      session_id: 'active-phone-session',
      phone: '+12723858378',
      status: 'code_sent',
      message: '已恢复当前验证会话，未重复发送短信验证码',
      resend_after: 18,
      expires_in: 420,
      reused: true,
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    await user.type(screen.getByLabelText('手机号码'), '+17372289532')
    await user.click(screen.getByRole('button', { name: '获取验证码' }))

    expect(await screen.findByText('已恢复当前验证会话，未重复发送短信验证码')).toBeTruthy()
    expect((screen.getByLabelText('手机号码') as HTMLInputElement).value).toBe('+12723858378')
    expect(screen.getByLabelText('短信验证码').hasAttribute('disabled')).toBe(false)
  })

  it('keeps request errors visible inside the modal', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('该账号已有手机验证正在进行'))
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    await user.type(screen.getByLabelText('手机号码'), '+17372289532')
    await user.click(screen.getByRole('button', { name: '获取验证码' }))

    const dialog = screen.getByRole('dialog')
    expect(await within(dialog).findByText('该账号已有手机验证正在进行')).toBeTruthy()
    expect(screen.getByRole('button', { name: '获取验证码' }).hasAttribute('disabled')).toBe(false)
  })

  it('renders a failed automatic session message only once', async () => {
    const failure = '账号缺少邮箱接码凭据，请重新导入邮箱凭据后再接码'
    vi.mocked(apiFetch).mockResolvedValue({
      session_id: 'failed-leadbee-session',
      provider: 'leadbee',
      automatic: true,
      status: 'failed',
      message: failure,
      resend_after: 0,
      expires_in: 0,
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    await user.click(screen.getByRole('radio', { name: 'LeadBee 自动接码' }))
    await user.type(screen.getByLabelText('LeadBee 兑换码'), 'bei-sms-DEMO-CODE')
    await user.click(screen.getByRole('button', { name: '开始自动接码' }))

    const dialog = screen.getByRole('dialog')
    await waitFor(() => {
      expect(within(dialog).getAllByText(failure)).toHaveLength(1)
    })
  })

  it('shows the complete phone verification process log', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      session_id: 'failed-leadbee-session',
      provider: 'leadbee',
      automatic: true,
      status: 'failed',
      message: '登录会话已失效，本次未获取手机号、未发送短信',
      resend_after: 0,
      expires_in: 500,
      logs: [
        '[04:30:01] 开始 LeadBee 自动接码',
        '[04:30:02] 正在恢复 OpenAI 登录会话',
        '[04:30:03] 登录会话已失效，本次未获取手机号、未发送短信',
      ],
    })
    const user = userEvent.setup()
    render(
      <ChatGPTPhoneVerificationModal
        open
        account={account}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    )

    await user.click(screen.getByRole('radio', { name: 'LeadBee 自动接码' }))
    await user.type(screen.getByLabelText('LeadBee 兑换码'), 'bei-sms-DEMO-CODE')
    await user.click(screen.getByRole('button', { name: '开始自动接码' }))

    const dialog = screen.getByRole('dialog')
    expect(await within(dialog).findByText('接码日志')).toBeTruthy()
    expect(within(dialog).getByText('[04:30:01] 开始 LeadBee 自动接码')).toBeTruthy()
    expect(within(dialog).getByText('[04:30:02] 正在恢复 OpenAI 登录会话')).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: '复制日志' })).toBeTruthy()
  })
})
