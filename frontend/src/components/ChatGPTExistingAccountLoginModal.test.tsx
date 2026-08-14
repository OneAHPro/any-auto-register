// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { apiFetch } from '@/lib/utils'
import { ChatGPTExistingAccountLoginModal } from './ChatGPTExistingAccountLoginModal'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
}))

vi.mock('./TaskLogPanel', () => ({
  TaskLogPanel: ({ taskId, mode }: { taskId: string; mode?: string }) => (
    <div>
      <span>任务 {taskId}</span>
      <span>任务模式 {mode || 'default'}</span>
    </div>
  ),
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

describe('ChatGPTExistingAccountLoginModal', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/config') {
        return {
          mail_provider: 'microsoft',
          default_executor: 'protocol',
          default_captcha_solver: 'yescaptcha',
        }
      }
      if (path.startsWith('/mail-imports/snapshot')) {
        return { count: path.includes('type=applemail') ? 0 : 5 }
      }
      if (path === '/sms-pool/stats') {
        return { total: 8, unused: 5, reserved: 1, used: 2 }
      }
      if (path === '/tasks/register') {
        return { task_id: 'login-task-1' }
      }
      throw new Error(`unexpected path: ${path}`)
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('loads the imported mailbox count and gets AT plus RT in the first login pass', async () => {
    const user = userEvent.setup()
    render(
      <ChatGPTExistingAccountLoginModal
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    )

    expect(await screen.findByText('可用邮箱 5 个')).toBeTruthy()
    expect(apiFetch).toHaveBeenCalledWith(
      '/mail-imports/snapshot?type=microsoft&preview_limit=1',
    )
    expect(screen.getByText('系统会直接完成已有账号 OAuth 登录并保存 AT + RT；已绑定手机号的账号无需卡密。')).toBeTruthy()
    expect(screen.queryByLabelText('LeadBee 接码卡密')).toBeNull()
    await user.click(screen.getByRole('button', { name: '开始登录' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/tasks/register', expect.objectContaining({
        method: 'POST',
      }))
    })
    const taskCall = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/tasks/register')
    const payload = JSON.parse(String(taskCall?.[1]?.body || '{}'))
    expect(payload.count).toBe(5)
    expect(payload.extra.chatgpt_existing_account_login_stage).toBe('refresh_token')
    expect(payload.extra.chatgpt_existing_account_bind_phone_and_get_rt).toBe(false)
    expect(await screen.findByText('任务 login-task-1')).toBeTruthy()
    expect(screen.getByText('任务模式 login')).toBeTruthy()
  })

  it('combines Microsoft and AppleMail imports into one login batch', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/config') {
        return {
          mail_provider: 'microsoft',
          applemail_pool_dir: '/runtime/mail',
          applemail_pool_file: 'active-applemail.json',
          default_executor: 'protocol',
          default_captcha_solver: 'yescaptcha',
        }
      }
      if (path.startsWith('/mail-imports/snapshot?type=microsoft')) {
        return { count: 10 }
      }
      if (path.startsWith('/mail-imports/snapshot?type=applemail')) {
        return { count: 1 }
      }
      if (path === '/sms-pool/stats') {
        return { total: 0, unused: 0, reserved: 0, used: 0 }
      }
      if (path === '/tasks/register') {
        return { task_id: 'combined-login-task' }
      }
      throw new Error(`unexpected path: ${path}`)
    })

    const user = userEvent.setup()
    render(
      <ChatGPTExistingAccountLoginModal
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    )

    expect(await screen.findByText('可用邮箱 11 个')).toBeTruthy()
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) =>
      String(path).startsWith('/mail-imports/snapshot?type=microsoft'))).toBe(true)
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) =>
      String(path).startsWith('/mail-imports/snapshot?type=applemail'))).toBe(true)

    await user.click(screen.getByRole('button', { name: '开始登录' }))
    await screen.findByText('任务 combined-login-task')

    const taskCall = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/tasks/register')
    const payload = JSON.parse(String(taskCall?.[1]?.body || '{}'))
    expect(payload.count).toBe(11)
    expect(payload.extra.chatgpt_existing_account_mail_provider_plan).toEqual([
      ...Array(10).fill('microsoft'),
      'applemail',
    ])
  })

  it('reveals one-code-per-line input and starts login plus phone verification', async () => {
    const user = userEvent.setup()
    render(
      <ChatGPTExistingAccountLoginModal
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    )

    expect(await screen.findByText('可用邮箱 5 个')).toBeTruthy()
    await user.click(screen.getByRole('switch', {
      name: '未绑定手机号时自动接码',
    }))

    expect(screen.getByText('已填写 0 / 需要 5')).toBeTruthy()
    await user.type(
      screen.getByLabelText('LeadBee 接码卡密'),
      'card-1\ncard-2\ncard-3\ncard-4\ncard-5',
    )
    expect(screen.getByText('已填写 5 / 需要 5')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '开始登录并接码' }))

    await waitFor(() => {
      expect(screen.getByText('任务 login-task-1')).toBeTruthy()
    })
    const taskCall = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/tasks/register')
    const payload = JSON.parse(String(taskCall?.[1]?.body || '{}'))
    expect(payload.extra).toMatchObject({
      chatgpt_existing_account_login_stage: 'access_token',
      chatgpt_existing_account_allow_phone_verification: false,
      chatgpt_existing_account_bind_phone_and_get_rt: true,
      chatgpt_existing_account_leadbee_codes: [
        'card-1',
        'card-2',
        'card-3',
        'card-4',
        'card-5',
      ],
    })
  })

  it('blocks submission when the LeadBee card count differs from the login count', async () => {
    const user = userEvent.setup()
    render(
      <ChatGPTExistingAccountLoginModal
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    )

    await screen.findByText('可用邮箱 5 个')
    await user.click(screen.getByRole('switch', {
      name: '未绑定手机号时自动接码',
    }))
    await user.type(screen.getByLabelText('LeadBee 接码卡密'), 'card-1\ncard-2')
    await user.click(screen.getByRole('button', { name: '开始登录并接码' }))

    expect(await screen.findByText('卡密数量需与登录数量一致（需要 5 个，当前 2 个）')).toBeTruthy()
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path === '/tasks/register')).toBe(false)
  })

  it('uses the SMS pool without rendering or submitting card secrets', async () => {
    const user = userEvent.setup()
    render(
      <ChatGPTExistingAccountLoginModal
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    )

    await screen.findByText('可用邮箱 5 个')
    await user.click(screen.getByRole('switch', {
      name: '未绑定手机号时自动接码',
    }))
    await user.click(screen.getByRole('switch', { name: '使用 SMS 接码池' }))

    expect(screen.getByText('可用卡密 5 个')).toBeTruthy()
    expect(screen.queryByLabelText('LeadBee 接码卡密')).toBeNull()
    await user.click(screen.getByRole('button', { name: '开始登录并接码' }))

    await waitFor(() => {
      expect(screen.getByText('任务 login-task-1')).toBeTruthy()
    })
    const taskCall = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/tasks/register')
    const payload = JSON.parse(String(taskCall?.[1]?.body || '{}'))
    expect(payload.extra.chatgpt_existing_account_use_sms_pool).toBe(true)
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_codes')
  })

  it('uses the server-side LeadBee API without loading or gating on legacy card inventory', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/config') {
        return {
          mail_provider: 'microsoft',
          default_executor: 'protocol',
          default_captcha_solver: 'yescaptcha',
          leadbee_api_enabled: 'yes',
          leadbee_api_key: 'fixture-config-key',
          leadbee_api_secret: 'fixture-config-secret',
          leadbee_api_product_id: 'fixture-product',
          leadbee_api_client_order_id: 'fixture-client-reference',
        }
      }
      if (path.startsWith('/mail-imports/snapshot?type=microsoft')) {
        return { count: 2 }
      }
      if (path.startsWith('/mail-imports/snapshot?type=applemail')) {
        return { count: 0 }
      }
      if (path === '/sms-pool/stats') {
        throw new Error('legacy inventory request must be skipped')
      }
      if (path === '/tasks/register') {
        return { task_id: 'leadbee-api-login-task' }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(
      <ChatGPTExistingAccountLoginModal
        open
        onClose={() => {}}
        onDone={() => {}}
      />,
    )

    expect(await screen.findByText('可用邮箱 2 个')).toBeTruthy()
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path === '/sms-pool/stats')).toBe(false)
    expect(screen.getByText(/LeadBee API 已启用/)).toBeTruthy()
    await user.click(screen.getByRole('switch', {
      name: '未绑定手机号时自动接码',
    }))

    expect(screen.queryByRole('switch', { name: '使用 SMS 接码池' })).toBeNull()
    expect(screen.queryByLabelText('LeadBee 接码卡密')).toBeNull()
    expect(document.body.textContent).not.toContain('fixture-config-key')
    expect(document.body.textContent).not.toContain('fixture-config-secret')

    await user.click(screen.getByRole('button', { name: '开始登录并接码' }))
    expect(await screen.findByText('任务 leadbee-api-login-task')).toBeTruthy()

    const taskCall = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/tasks/register')
    const payload = JSON.parse(String(taskCall?.[1]?.body || '{}'))
    expect(payload.extra.chatgpt_existing_account_leadbee_api).toBe(true)
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_use_sms_pool')
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_codes')
    expect(JSON.stringify(payload)).not.toContain('fixture-config-key')
    expect(JSON.stringify(payload)).not.toContain('fixture-config-secret')
    expect(JSON.stringify(payload)).not.toContain('fixture-client-reference')
  })
})
