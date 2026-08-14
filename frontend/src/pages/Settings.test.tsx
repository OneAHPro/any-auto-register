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
      if (path === '/mail-imports/providers') {
        return {
          items: [{
            type: 'microsoft',
            label: 'Microsoft',
            description: '',
            content_placeholder: '',
            helper_text: '',
            supports_filename: false,
            filename_label: '',
            filename_placeholder: '',
            preview_empty_text: '',
          }],
        }
      }
      if (path.startsWith('/mail-imports/snapshot?')) {
        return {
          type: 'microsoft',
          label: 'Microsoft',
          count: 0,
          items: [],
          truncated: false,
          filename: '',
          path: '',
          pool_dir: '',
        }
      }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  afterEach(() => cleanup())

  it('rehydrates string config and saves all normalized automation values', async () => {
    const user = userEvent.setup()
    render(<Settings page="codex2api" />)
    const interval = await screen.findByRole('spinbutton', { name: 'Codex2API 鉴权巡检间隔（分钟）' }) as HTMLInputElement
    const concurrency = screen.getByRole('spinbutton', { name: '异常账号重登并发数' }) as HTMLInputElement
    const threshold = screen.getByRole('spinbutton', { name: '重登失败告警阈值（账号数）' }) as HTMLInputElement
    const quotaThreshold = screen.getByRole('spinbutton', { name: 'Codex2API 剩余额度告警阈值（美元）' }) as HTMLInputElement
    const barkEndpoint = screen.getByLabelText('Bark 推送地址') as HTMLInputElement
    await waitFor(() => expect(interval.value).toBe('45'))
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
    render(<Settings page="codex2api" />)
    const removalSwitch = await screen.findByRole('switch', {
      name: '删除本地 ChatGPT 账号时，同步删除 Codex2API 认证',
    })
    await waitFor(() => expect(removalSwitch.getAttribute('aria-checked')).toBe('true'))
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
    render(<Settings page="codex2api" />)
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
    render(<Settings page="codex2api" />)

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/config')
    })
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

  it('renders Codex2API as a standalone page without the global registration tab', async () => {
    render(<Settings page="codex2api" />)

    expect(await screen.findByRole('heading', { name: 'Codex2API' })).toBeTruthy()
    expect(screen.getByText('删除联动')).toBeTruthy()
    expect(screen.getByLabelText('Codex2API 鉴权巡检间隔（分钟）')).toBeTruthy()
    expect(screen.getByText('告警通知')).toBeTruthy()
    expect(screen.queryByText('注册设置')).toBeNull()
  })

  it('renders mailbox import as a standalone page with its save button', async () => {
    render(<Settings page="mail-import" />)

    expect(await screen.findByRole('heading', { name: '邮箱导入' })).toBeTruthy()
    expect(await screen.findByRole('button', { name: '确认导入' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /保存配置/ })).toBeTruthy()
    expect(screen.queryByText('注册设置')).toBeNull()
  })

  it('keeps global settings tabs focused on global configuration', async () => {
    const user = userEvent.setup()
    render(<Settings />)

    expect(await screen.findByText('注册设置')).toBeTruthy()
    expect(screen.queryByText('Codex2API')).toBeNull()
    await user.click(screen.getByText('邮箱服务'))
    expect(screen.queryByRole('button', { name: '确认导入' })).toBeNull()
  })

  it('renders write-only LeadBee Open API fields and discards credentials returned by config', async () => {
    configResponse = {
      ...configResponse,
      leadbee_api_enabled: 'yes',
      leadbee_api_key: 'BUGGY_KEY_VALUE',
      leadbee_api_secret: 'BUGGY_SECRET_VALUE',
      leadbee_api_product_id: 'prod-saved',
    }
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))

    expect(await screen.findByText('LeadBee Open API')).toBeTruthy()
    const enabled = screen.getByRole('switch', { name: '启用 LeadBee Open API' })
    const apiKey = screen.getByLabelText('LeadBee API Key') as HTMLInputElement
    const apiSecret = screen.getByLabelText('LeadBee API Secret') as HTMLInputElement
    const productId = screen.getByLabelText('LeadBee 产品 ID') as HTMLInputElement
    await waitFor(() => expect(enabled.getAttribute('aria-checked')).toBe('true'))
    expect(enabled.id).toBe('leadbee_api_enabled')
    expect(apiKey.id).toBe('leadbee_api_key')
    expect(apiSecret.id).toBe('leadbee_api_secret')
    expect(productId.id).toBe('leadbee_api_product_id')
    expect(apiKey.type).toBe('password')
    expect(apiSecret.type).toBe('password')
    expect(apiKey.placeholder).toBe('留空则保留已保存值')
    expect(apiSecret.placeholder).toBe('留空则保留已保存值')
    expect(apiKey.value).toBe('')
    expect(apiSecret.value).toBe('')
    expect(productId.value).toBe('prod-saved')
    expect(document.body.innerHTML).not.toContain('BUGGY_KEY_VALUE')
    expect(document.body.innerHTML).not.toContain('BUGGY_SECRET_VALUE')
    expect(screen.getByRole('button', { name: '测试 LeadBee API' })).toBeTruthy()
  })

  it.each([true, '1', 'true', 'yes', 'on'])(
    'normalizes the LeadBee enabled value %s to on',
    async (enabledValue) => {
      configResponse = {
        ...configResponse,
        leadbee_api_enabled: enabledValue,
      }
      const user = userEvent.setup()
      render(<Settings />)

      await user.click(await screen.findByText('ChatGPT'))

      const enabled = await screen.findByRole('switch', { name: '启用 LeadBee Open API' })
      await waitFor(() => expect(enabled.getAttribute('aria-checked')).toBe('true'))
    },
  )

  it('tests LeadBee with only the current unsaved form values', async () => {
    configResponse = {
      ...configResponse,
      leadbee_api_enabled: false,
      leadbee_api_product_id: 'prod-saved',
    }
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        return {
          ok: true,
          product_ids: ['prod-unsaved'],
          configured_product_available: true,
          balance_available: '12.50',
          currency: 'USD',
        }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    const enabled = await screen.findByRole('switch', { name: '启用 LeadBee Open API' })
    await user.click(enabled)
    await user.type(screen.getByLabelText('LeadBee API Key'), 'KEY_INPUT')
    await user.type(screen.getByLabelText('LeadBee API Secret'), 'SECRET_INPUT')
    const productId = screen.getByLabelText('LeadBee 产品 ID')
    await user.clear(productId)
    await user.type(productId, 'prod-unsaved')
    await user.click(screen.getByRole('button', { name: '测试 LeadBee API' }))

    await waitFor(() => {
      expect(
        vi.mocked(apiFetch).mock.calls.some(
          ([path, options]) => path === '/config/leadbee/test' && options?.method === 'POST',
        ),
      ).toBe(true)
    })
    const call = vi.mocked(apiFetch).mock.calls.find(
      ([path, options]) => path === '/config/leadbee/test' && options?.method === 'POST',
    )
    expect(call?.[1]).toEqual({
      method: 'POST',
      body: JSON.stringify({
        data: {
          leadbee_api_enabled: true,
          leadbee_api_key: 'KEY_INPUT',
          leadbee_api_secret: 'SECRET_INPUT',
          leadbee_api_product_id: 'prod-unsaved',
        },
      }),
    })
    expect(
      vi.mocked(apiFetch).mock.calls.some(
        ([path, options]) => path === '/config' && options?.method === 'PUT',
      ),
    ).toBe(false)
  })

  it('keeps blank LeadBee credentials in the normal save payload', async () => {
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    expect((await screen.findByLabelText('LeadBee API Key') as HTMLInputElement).value).toBe('')
    await user.click(screen.getByText('注册设置'))
    await user.click(screen.getByRole('button', { name: /保存配置/ }))

    await waitFor(() => {
      expect(
        vi.mocked(apiFetch).mock.calls.some(
          ([path, options]) => path === '/config' && options?.method === 'PUT',
        ),
      ).toBe(true)
    })
    const call = vi.mocked(apiFetch).mock.calls.find(
      ([path, options]) => path === '/config' && options?.method === 'PUT',
    )
    const payload = JSON.parse(String(call?.[1]?.body || '{}'))
    expect(payload.data).toMatchObject({
      leadbee_api_key: '',
      leadbee_api_secret: '',
    })
    expect(Object.prototype.hasOwnProperty.call(payload.data, 'leadbee_api_key')).toBe(true)
    expect(Object.prototype.hasOwnProperty.call(payload.data, 'leadbee_api_secret')).toBe(true)
  })

  it('blanks newly submitted LeadBee credentials after a successful normal save', async () => {
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    const apiKey = await screen.findByLabelText('LeadBee API Key') as HTMLInputElement
    const apiSecret = screen.getByLabelText('LeadBee API Secret') as HTMLInputElement
    await user.type(apiKey, 'NEW_KEY')
    await user.type(apiSecret, 'NEW_SECRET')
    await user.click(screen.getByText('注册设置'))
    await user.click(screen.getByRole('button', { name: /保存配置/ }))

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(
        ([path, options]) => path === '/config' && options?.method === 'PUT',
      )
      const payload = JSON.parse(String(call?.[1]?.body || '{}'))
      expect(payload.data).toMatchObject({
        leadbee_api_key: 'NEW_KEY',
        leadbee_api_secret: 'NEW_SECRET',
      })
    })
    await user.click(screen.getByText('ChatGPT'))
    await waitFor(() => {
      expect((screen.getByLabelText('LeadBee API Key') as HTMLInputElement).value).toBe('')
      expect((screen.getByLabelText('LeadBee API Secret') as HTMLInputElement).value).toBe('')
    })
  })

  it('clears a successful LeadBee test result when any tested field changes', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        return {
          ok: true,
          product_ids: ['prod-checked'],
          configured_product_available: true,
          balance_available: '1.00',
          currency: 'USD',
        }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    const testButton = await screen.findByRole('button', { name: '测试 LeadBee API' })
    const edits = [
      () => user.click(screen.getByRole('switch', { name: '启用 LeadBee Open API' })),
      () => user.type(screen.getByLabelText('LeadBee API Key'), 'K'),
      () => user.type(screen.getByLabelText('LeadBee API Secret'), 'S'),
      () => user.type(screen.getByLabelText('LeadBee 产品 ID'), 'P'),
    ]

    for (const edit of edits) {
      await user.click(testButton)
      expect(await screen.findByRole('status', { name: 'LeadBee API 连接成功' })).toBeTruthy()

      await edit()

      await waitFor(() => {
        expect(screen.queryByRole('status', { name: 'LeadBee API 连接成功' })).toBeNull()
      })
    }
  })

  it('ignores a pending LeadBee response after the tested fields change', async () => {
    const testRequest = deferred<Record<string, unknown>>()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/config/leadbee/test' && options?.method === 'POST') return testRequest.promise
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    await user.click(await screen.findByRole('button', { name: '测试 LeadBee API' }))
    await waitFor(() => {
      expect(
        vi.mocked(apiFetch).mock.calls.some(
          ([path, options]) => path === '/config/leadbee/test' && options?.method === 'POST',
        ),
      ).toBe(true)
    })
    await user.type(screen.getByLabelText('LeadBee 产品 ID'), 'prod-changed')

    await act(async () => {
      testRequest.resolve({
        ok: true,
        product_ids: ['prod-stale'],
        configured_product_available: true,
        balance_available: '1.00',
        currency: 'USD',
      })
      await testRequest.promise
    })

    await waitFor(() => {
      expect(screen.queryByRole('status', { name: 'LeadBee API 连接成功' })).toBeNull()
    })
    expect(document.body.textContent).not.toContain('prod-stale')
  })

  it('clears a successful LeadBee result after delayed config hydration changes the fields', async () => {
    const configRequest = deferred<Record<string, unknown>>()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configRequest.promise
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        return {
          ok: true,
          product_ids: ['prod-before-hydration'],
          configured_product_available: true,
          balance_available: '1.00',
          currency: 'USD',
        }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    await user.click(await screen.findByRole('button', { name: '测试 LeadBee API' }))
    expect(await screen.findByRole('status', { name: 'LeadBee API 连接成功' })).toBeTruthy()

    await act(async () => {
      configRequest.resolve({
        ...configResponse,
        leadbee_api_enabled: true,
        leadbee_api_product_id: 'prod-hydrated',
      })
      await configRequest.promise
    })

    await waitFor(() => {
      expect((screen.getByLabelText('LeadBee 产品 ID') as HTMLInputElement).value).toBe('prod-hydrated')
      expect(screen.queryByRole('status', { name: 'LeadBee API 连接成功' })).toBeNull()
    })
  })

  it('discards a pending LeadBee result and stops loading after delayed config hydration', async () => {
    const configRequest = deferred<Record<string, unknown>>()
    const testRequest = deferred<Record<string, unknown>>()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configRequest.promise
      if (path === '/config/leadbee/test' && options?.method === 'POST') return testRequest.promise
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    const testButton = await screen.findByRole('button', { name: '测试 LeadBee API' })
    await user.click(testButton)
    await waitFor(() => expect(testButton.classList.contains('ant-btn-loading')).toBe(true))

    await act(async () => {
      configRequest.resolve({
        ...configResponse,
        leadbee_api_enabled: true,
        leadbee_api_product_id: 'prod-hydrated',
      })
      await configRequest.promise
    })
    await waitFor(() => {
      expect((screen.getByLabelText('LeadBee 产品 ID') as HTMLInputElement).value).toBe('prod-hydrated')
    })

    await act(async () => {
      testRequest.resolve({
        ok: true,
        product_ids: ['prod-stale-hydration'],
        configured_product_available: true,
        balance_available: '1.00',
        currency: 'USD',
      })
      await testRequest.promise
    })

    await waitFor(() => {
      expect(screen.queryByRole('status', { name: 'LeadBee API 连接成功' })).toBeNull()
      expect(testButton.classList.contains('ant-btn-loading')).toBe(false)
    })
    expect(document.body.textContent).not.toContain('prod-stale-hydration')
  })

  it('stops a pending LeadBee test when a successful save clears its credentials', async () => {
    const testRequest = deferred<Record<string, unknown>>()
    const boundsSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      width: 800,
      height: 600,
      top: 0,
      right: 800,
      bottom: 600,
      left: 0,
      toJSON: () => ({}),
    })
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/config' && options?.method === 'PUT') return { ok: true }
      if (path === '/config/leadbee/test' && options?.method === 'POST') return testRequest.promise
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()

    try {
      render(<Settings />)
      await user.click(await screen.findByText('ChatGPT'))
      const apiKey = await screen.findByLabelText('LeadBee API Key') as HTMLInputElement
      const apiSecret = screen.getByLabelText('LeadBee API Secret') as HTMLInputElement
      await user.type(apiKey, 'SAVE_KEY')
      await user.type(apiSecret, 'SAVE_SECRET')
      const testButton = screen.getByRole('button', { name: '测试 LeadBee API' })
      await user.click(testButton)
      await waitFor(() => expect(testButton.classList.contains('ant-btn-loading')).toBe(true))

      const saveButton = await screen.findByRole('button', { name: /保存配置/ }) as HTMLButtonElement
      await waitFor(() => expect(saveButton.disabled).toBe(false))
      await user.click(saveButton)
      await waitFor(() => {
        expect(
          vi.mocked(apiFetch).mock.calls.some(
            ([path, options]) => path === '/config' && options?.method === 'PUT',
          ),
        ).toBe(true)
        expect(apiKey.value).toBe('')
        expect(apiSecret.value).toBe('')
      })

      await act(async () => {
        testRequest.resolve({
          ok: true,
          product_ids: ['prod-stale-save'],
          configured_product_available: true,
          balance_available: '1.00',
          currency: 'USD',
        })
        await testRequest.promise
      })

      await waitFor(() => {
        expect(screen.queryByRole('status', { name: 'LeadBee API 连接成功' })).toBeNull()
        expect(testButton.classList.contains('ant-btn-loading')).toBe(false)
      })
      expect(document.body.textContent).not.toContain('prod-stale-save')
    } finally {
      boundsSpy.mockRestore()
    }
  })

  it('renders only whitelisted LeadBee test metadata from a successful response', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        return {
          ok: true,
          product_ids: ['prod-1'],
          configured_product_available: true,
          balance_available: '12.50',
          currency: 'USD',
          credential: 'HIDDEN_CREDENTIAL',
          signature: 'HIDDEN_SIGNATURE',
          phone: 'HIDDEN_PHONE',
          sms_code: 'HIDDEN_SMS',
        }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    await user.click(await screen.findByRole('button', { name: '测试 LeadBee API' }))

    const result = await screen.findByRole('status', { name: 'LeadBee API 连接成功' })
    expect(result.textContent).toContain('产品 ID：prod-1')
    expect(result.textContent).toContain('已配置产品：可用')
    expect(result.textContent).toContain('余额：12.50 USD')
    expect(document.body.textContent).not.toContain('HIDDEN_CREDENTIAL')
    expect(document.body.textContent).not.toContain('HIDDEN_SIGNATURE')
    expect(document.body.textContent).not.toContain('HIDDEN_PHONE')
    expect(document.body.textContent).not.toContain('HIDDEN_SMS')
  })

  it('shows a fixed generic LeadBee failure without provider error details', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) return configResponse
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        throw new Error('PROVIDER_PRIVATE_ERROR')
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<Settings />)

    await user.click(await screen.findByText('ChatGPT'))
    await user.click(await screen.findByRole('button', { name: '测试 LeadBee API' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('LeadBee 连接测试失败，请检查凭证和产品 ID')
    expect(document.body.textContent).not.toContain('PROVIDER_PRIVATE_ERROR')

    await user.type(screen.getByLabelText('LeadBee 产品 ID'), 'P')
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })
})
