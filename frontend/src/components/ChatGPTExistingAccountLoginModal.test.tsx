// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

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

function renderModal() {
  return render(
    <ChatGPTExistingAccountLoginModal
      open
      onClose={() => {}}
      onDone={() => {}}
    />,
  )
}

function taskPayload() {
  const taskCall = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/tasks/register')
  return JSON.parse(String(taskCall?.[1]?.body || '{}'))
}

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
        return { total: 8, unused: 5, reserved: 1, active: 0, used: 2 }
      }
      if (path === '/tasks/register') return { task_id: 'login-task-1' }
      throw new Error(`unexpected path: ${path}`)
    })
  })

  afterEach(() => cleanup())

  it('defaults to the card pool when API is not configured', async () => {
    const user = userEvent.setup()
    renderModal()

    expect(await screen.findByText('可用邮箱 5 个')).toBeTruthy()
    expect((screen.getByRole('radio', { name: '仅卡密池' }) as HTMLInputElement).checked).toBe(true)
    expect(screen.getByText('卡密池可用 5 张')).toBeTruthy()
    expect(screen.queryByLabelText('LeadBee 接码卡密')).toBeNull()
    expect(screen.getByRole('switch', { name: '登录后新增或轮换 MFA' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText(/共享接码地址仍可能被供货商访问/)).toBeTruthy()

    await user.click(screen.getByRole('button', { name: '开始登录并接码' }))
    expect(await screen.findByText('任务 login-task-1')).toBeTruthy()

    const payload = taskPayload()
    expect(payload.extra.chatgpt_existing_account_sms_mode).toBe('pool')
    expect(payload.extra.chatgpt_existing_account_rotate_mfa).toBe(true)
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_use_sms_pool')
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_codes')
  })

  it('defaults to API priority, shows capacity, and allows 50 concurrent logins', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config') {
        return {
          mail_provider: 'microsoft',
          default_executor: 'protocol',
          default_captcha_solver: 'yescaptcha',
          leadbee_api_enabled: 'yes',
          leadbee_api_key: 'RETURNED_KEY_MUST_NOT_RENDER',
          leadbee_api_secret: 'RETURNED_SECRET_MUST_NOT_RENDER',
          leadbee_api_product_id: 'prod-capacity',
          leadbee_api_client_order_id: 'RETURNED_REF_MUST_NOT_RENDER',
        }
      }
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        return {
          ok: true,
          configured_product_available: true,
          balance_available: '35.70',
          balance_reserved: '0.00',
          unit_price: '1.30',
          estimated_order_capacity: 27,
          currency: 'CNY',
          signature: 'HIDDEN_SIGNATURE',
          phone: 'HIDDEN_PHONE',
          sms_code: 'HIDDEN_SMS',
        }
      }
      if (path.startsWith('/mail-imports/snapshot')) {
        return { count: path.includes('type=applemail') ? 0 : 50 }
      }
      if (path === '/sms-pool/stats') {
        return { total: 20, unused: 18, reserved: 2, active: 0, used: 0 }
      }
      if (path === '/tasks/register') return { task_id: 'api-priority-task' }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    renderModal()

    expect(await screen.findByText('可用邮箱 50 个')).toBeTruthy()
    expect((screen.getByRole('radio', { name: 'API优先' }) as HTMLInputElement).checked).toBe(true)
    expect(screen.getByText('API 可用余额 ¥35.70 · 单价 ¥1.30/次 · 预计可接 27 次')).toBeTruthy()
    expect(screen.getByText('卡密池可用 18 张')).toBeTruthy()
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path === '/sms-pool/stats')).toBe(true)
    expect(vi.mocked(apiFetch).mock.calls.some(
      ([path, options]) => path === '/config/leadbee/test' && options?.method === 'POST',
    )).toBe(true)

    const concurrency = screen.getByRole('spinbutton', { name: '并发数' }) as HTMLInputElement
    expect(concurrency.getAttribute('aria-valuemax')).toBe('50')
    expect(screen.getByRole('button', { name: '开始登录并接码' }).parentElement?.style.position)
      .toBe('sticky')
    await user.clear(concurrency)
    await user.type(concurrency, '50')
    await user.click(screen.getByRole('button', { name: '开始登录并接码' }))
    expect(await screen.findByText('任务 api-priority-task')).toBeTruthy()

    const payload = taskPayload()
    expect(payload.concurrency).toBe(50)
    expect(payload.extra.chatgpt_existing_account_sms_mode).toBe('api_fallback_pool')
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_api')
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_use_sms_pool')
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_codes')
    expect(JSON.stringify(payload)).not.toContain('RETURNED_KEY_MUST_NOT_RENDER')
    expect(JSON.stringify(payload)).not.toContain('RETURNED_SECRET_MUST_NOT_RENDER')
    expect(JSON.stringify(payload)).not.toContain('RETURNED_REF_MUST_NOT_RENDER')
    expect(document.body.textContent).not.toContain('HIDDEN_SIGNATURE')
    expect(document.body.textContent).not.toContain('HIDDEN_PHONE')
    expect(document.body.textContent).not.toContain('HIDDEN_SMS')
  })

  it('keeps API priority selected when live balance cannot be loaded', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config') {
        return {
          mail_provider: 'microsoft',
          leadbee_api_enabled: true,
          leadbee_api_product_id: 'prod-ready',
        }
      }
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        throw new Error('PRIVATE_PROVIDER_FAILURE')
      }
      if (path.startsWith('/mail-imports/snapshot')) {
        return { count: path.includes('type=applemail') ? 0 : 3 }
      }
      if (path === '/sms-pool/stats') return { total: 2, unused: 2 }
      throw new Error(`unexpected path: ${path}`)
    })
    renderModal()

    expect(await screen.findByText('余额暂未获取')).toBeTruthy()
    expect((screen.getByRole('radio', { name: 'API优先' }) as HTMLInputElement).checked).toBe(true)
    expect(screen.getByText('卡密池可用 2 张')).toBeTruthy()
    expect(document.body.textContent).not.toContain('PRIVATE_PROVIDER_FAILURE')
  })

  it('allows choosing only the card pool even when global API is enabled', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config') {
        return {
          mail_provider: 'microsoft',
          leadbee_api_enabled: true,
          leadbee_api_product_id: 'prod-ready',
        }
      }
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        return {
          ok: true,
          configured_product_available: true,
          balance_available: '10.00',
          unit_price: '1.00',
          estimated_order_capacity: 10,
          currency: 'CNY',
        }
      }
      if (path.startsWith('/mail-imports/snapshot')) {
        return { count: path.includes('type=applemail') ? 0 : 2 }
      }
      if (path === '/sms-pool/stats') return { total: 2, unused: 2 }
      if (path === '/tasks/register') return { task_id: 'pool-only-task' }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    renderModal()
    await screen.findByText('API 可用余额 ¥10.00 · 单价 ¥1.00/次 · 预计可接 10 次')

    await user.click(screen.getByRole('radio', { name: '仅卡密池' }))
    await user.click(screen.getByRole('button', { name: '开始登录并接码' }))
    expect(await screen.findByText('任务 pool-only-task')).toBeTruthy()

    const payload = taskPayload()
    expect(payload.extra.chatgpt_existing_account_sms_mode).toBe('pool')
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_api')
  })

  it('allows choosing no phone verification for already verified accounts', async () => {
    const user = userEvent.setup()
    renderModal()
    await screen.findByText('可用邮箱 5 个')

    await user.click(screen.getByRole('radio', { name: '无需接码' }))
    await user.click(screen.getByRole('button', { name: '开始登录' }))
    expect(await screen.findByText('任务 login-task-1')).toBeTruthy()

    const payload = taskPayload()
    expect(payload.extra.chatgpt_existing_account_sms_mode).toBe('none')
    expect(payload.extra.chatgpt_existing_account_login_stage).toBe('refresh_token')
  })

  it('defaults to no phone verification when neither API nor cards are available', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/config') return { mail_provider: 'microsoft' }
      if (path.startsWith('/mail-imports/snapshot')) {
        return { count: path.includes('type=applemail') ? 0 : 1 }
      }
      if (path === '/sms-pool/stats') return { total: 0, unused: 0 }
      throw new Error(`unexpected path: ${path}`)
    })
    renderModal()

    await screen.findByText('可用邮箱 1 个')
    expect((screen.getByRole('radio', { name: '无需接码' }) as HTMLInputElement).checked).toBe(true)
    expect(screen.getByText('卡密池可用 0 张')).toBeTruthy()
  })

  it('keeps the Microsoft and AppleMail provider plan in one batch', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/config') return { mail_provider: 'microsoft' }
      if (path.startsWith('/mail-imports/snapshot?type=microsoft')) return { count: 2 }
      if (path.startsWith('/mail-imports/snapshot?type=applemail')) return { count: 1 }
      if (path === '/sms-pool/stats') return { total: 0, unused: 0 }
      if (path === '/tasks/register') return { task_id: 'combined-task' }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    renderModal()
    await screen.findByText('可用邮箱 3 个')
    await user.click(screen.getByRole('button', { name: '开始登录' }))
    await screen.findByText('任务 combined-task')

    expect(taskPayload().extra.chatgpt_existing_account_mail_provider_plan).toEqual([
      'microsoft',
      'microsoft',
      'applemail',
    ])
  })
})
