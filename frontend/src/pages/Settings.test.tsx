// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { apiFetch } from '@/lib/utils'
import Settings from './Settings'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

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
})

describe('Settings ChatGPT automatic relogin config', () => {
  let configResponse: Record<string, unknown>

  beforeEach(() => {
    configResponse = {
      mail_provider: 'luckmail',
      chatgpt_auto_relogin_enabled: '0',
      chatgpt_auto_relogin_interval_minutes: '45',
      chatgpt_auto_relogin_concurrency: '3',
      chatgpt_auto_relogin_alert_threshold: '5',
      chatgpt_auto_relogin_quota_alert_threshold_usd: '1200.50',
      smtp_host: 'smtp.example.com',
      smtp_port: '587',
      smtp_username: 'notify@example.com',
      smtp_password: '',
      smtp_sender_email: 'notify@example.com',
      smtp_recipient_email: 'owner@example.com',
      smtp_use_ssl: '1',
      smtp_force_auth_login: '0',
      bark_enabled: '1',
      bark_endpoint: 'server-secret-must-not-return',
    }
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/config') {
        return configResponse
      }
      if (path === '/automations/chatgpt-relogin') {
        return { state: 'disabled', eligible_accounts: 0 }
      }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  afterEach(() => cleanup())

  it('rehydrates string config and saves all normalized automation values', async () => {
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(screen.getByText('Codex2API'))
    const interval = await screen.findByRole('spinbutton', { name: 'Codex2API 鉴权巡检间隔（分钟）' }) as HTMLInputElement
    const concurrency = screen.getByRole('spinbutton', { name: '异常账号重登并发数' }) as HTMLInputElement
    const threshold = screen.getByRole('spinbutton', { name: '重登失败告警阈值（账号数）' }) as HTMLInputElement
    const quotaThreshold = screen.getByRole('spinbutton', { name: 'Codex2API 剩余额度告警阈值（美元）' }) as HTMLInputElement
    const barkEndpoint = screen.getByLabelText('Bark 推送地址') as HTMLInputElement
    expect(interval.value).toBe('45')
    expect(concurrency.value).toBe('3')
    expect(threshold.value).toBe('5')
    expect(quotaThreshold.value).toBe('1200.50')
    expect(barkEndpoint.value).toBe('')
    expect(screen.getByRole('switch', { name: '启用 Bark 强提醒' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('switch', { name: '启用 ChatGPT 自动重登' }).getAttribute('aria-checked')).toBe('false')

    await user.click(screen.getByRole('button', { name: /保存配置/ }))

    await waitFor(() => {
      expect(vi.mocked(apiFetch).mock.calls.some(([path, options]) => path === '/config' && options?.method === 'PUT')).toBe(true)
    })
    const call = vi.mocked(apiFetch).mock.calls.find(([path, options]) => path === '/config' && options?.method === 'PUT')
    const payload = JSON.parse(String(call?.[1]?.body || '{}'))
    expect(payload.data).toMatchObject({
      chatgpt_auto_relogin_enabled: false,
      chatgpt_auto_relogin_interval_minutes: 45,
      chatgpt_auto_relogin_concurrency: 3,
      chatgpt_auto_relogin_alert_threshold: 5,
      chatgpt_auto_relogin_quota_alert_threshold_usd: 1200.5,
      smtp_host: 'smtp.example.com',
      smtp_recipient_email: 'owner@example.com',
      bark_enabled: true,
      bark_endpoint: '',
    })
  })

  it('hydrates and saves the Codex2API account-removal switch independently', async () => {
    configResponse = {
      ...configResponse,
      codex2api_enabled: '1',
      codex2api_delete_on_account_remove_enabled: '1',
    }
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(screen.getByText('Codex2API'))
    const removalSwitch = await screen.findByRole('switch', {
      name: '删除本地 ChatGPT 账号时，同步删除 Codex2API 认证',
    })
    expect(screen.getByText('删除联动')).toBeTruthy()
    expect(
      screen.getByText('自动清理、单个删除和批量删除均生效；远端删除失败时保留本地账号。'),
    ).toBeTruthy()
    expect(removalSwitch.getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('switch', { name: '启用自动上传' }).getAttribute('aria-checked')).toBe('true')

    await user.click(removalSwitch)
    await user.click(screen.getByRole('button', { name: /保存配置/ }))

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(
        ([path, options]) => path === '/config' && options?.method === 'PUT',
      )
      const payload = JSON.parse(String(call?.[1]?.body || '{}'))
      expect(payload.data).toMatchObject({
        codex2api_enabled: true,
        codex2api_delete_on_account_remove_enabled: false,
      })
    })
    expect(removalSwitch.getAttribute('aria-checked')).toBe('false')
    expect(screen.getByRole('switch', { name: '启用自动上传' }).getAttribute('aria-checked')).toBe('true')
  })

  it('defaults the Codex2API account-removal switch to off when the key is missing', async () => {
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(screen.getByText('Codex2API'))
    const removalSwitch = await screen.findByRole('switch', {
      name: '删除本地 ChatGPT 账号时，同步删除 Codex2API 认证',
    })
    expect(removalSwitch.getAttribute('aria-checked')).toBe('false')
  })

  it.each([
    ['missing interval and null concurrency', { mail_provider: 'luckmail', chatgpt_auto_relogin_concurrency: null }],
    ['blank interval and missing concurrency', { mail_provider: 'luckmail', chatgpt_auto_relogin_interval_minutes: '   ' }],
  ])('uses scheduler defaults for %s', async (_, response) => {
    configResponse = response
    const user = userEvent.setup()
    render(<Settings />)

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/config')
    })
    await user.click(screen.getByText('Codex2API'))

    await waitFor(() => {
      expect((screen.getByRole('spinbutton', { name: 'Codex2API 鉴权巡检间隔（分钟）' }) as HTMLInputElement).value).toBe('2')
      expect((screen.getByRole('spinbutton', { name: '异常账号重登并发数' }) as HTMLInputElement).value).toBe('10')
      expect((screen.getByRole('spinbutton', { name: '重登失败告警阈值（账号数）' }) as HTMLInputElement).value).toBe('20')
    })
  })

  it('blocks saving until the initial config request succeeds', async () => {
    const configRequest = deferred<Record<string, unknown>>()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configRequest.promise
      if (path === '/automations/chatgpt-relogin') return { state: 'disabled', eligible_accounts: 0 }
      if (path === '/config' && options?.method === 'PUT') return { ok: true }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()

    render(<Settings />)

    const loadingStatus = screen.getByRole('status')
    expect(loadingStatus.textContent).toContain('正在加载配置')
    const saveButton = screen.getByRole('button', { name: /保存配置/ }) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)
    await user.click(saveButton)
    expect(vi.mocked(apiFetch).mock.calls.some(([path, options]) => path === '/config' && options?.method === 'PUT')).toBe(false)

    await act(async () => {
      configRequest.resolve(configResponse)
      await configRequest.promise
    })
    await waitFor(() => expect(saveButton.disabled).toBe(false))
  })

  it('shows a load error, keeps saving blocked, and retries the config request', async () => {
    let configAttempts = 0
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) {
        configAttempts += 1
        if (configAttempts === 1) throw new Error('数据库暂时不可用')
        return configResponse
      }
      if (path === '/automations/chatgpt-relogin') return { state: 'disabled', eligible_accounts: 0 }
      if (path === '/config' && options?.method === 'PUT') return { ok: true }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()

    render(<Settings />)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('数据库暂时不可用')
    const saveButton = screen.getByRole('button', { name: /保存配置/ }) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)
    await user.click(saveButton)
    expect(vi.mocked(apiFetch).mock.calls.some(([path, options]) => path === '/config' && options?.method === 'PUT')).toBe(false)

    await user.click(screen.getByRole('button', { name: '重试加载' }))
    await waitFor(() => expect(configAttempts).toBe(2))
    await waitFor(() => expect(saveButton.disabled).toBe(false))
    expect(screen.queryByText('数据库暂时不可用')).toBeNull()
  })

  it('shows the backend detail and clears the saved state when a later save fails', async () => {
    let putAttempts = 0
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/automations/chatgpt-relogin') return { state: 'disabled', eligible_accounts: 0 }
      if (path === '/config' && options?.method === 'PUT') {
        putAttempts += 1
        if (putAttempts === 1) return { ok: true }
        throw new Error('后端校验：并发数无效')
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    const saveButton = await screen.findByRole('button', { name: /保存配置/ })
    await waitFor(() => expect((saveButton as HTMLButtonElement).disabled).toBe(false))
    await user.click(saveButton)
    expect(await screen.findByRole('button', { name: /已保存/ })).toBeTruthy()

    await user.click(screen.getByRole('button', { name: /已保存/ }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('后端校验：并发数无效')
    expect(screen.queryByRole('button', { name: /已保存/ })).toBeNull()
    expect(screen.getByRole('button', { name: /保存配置/ })).toBeTruthy()
  })

  it('shows a generic accessible error when saving rejects with an unknown value', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/automations/chatgpt-relogin') return { state: 'disabled', eligible_accounts: 0 }
      if (path === '/config' && options?.method === 'PUT') throw Symbol('offline')
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    const saveButton = await screen.findByRole('button', { name: /保存配置/ })
    await waitFor(() => expect((saveButton as HTMLButtonElement).disabled).toBe(false))
    await user.click(saveButton)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('保存配置失败，请稍后重试')
  })
})
