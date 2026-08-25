// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { Form } from 'antd'
import userEvent from '@testing-library/user-event'

import { apiFetch } from '@/lib/utils'
import ChatGPTAutoReloginSection from './ChatGPTAutoReloginSection'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
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

function renderSection(initialValues: Record<string, unknown> = {}) {
  return render(
    <Form initialValues={initialValues} layout="vertical">
      <ChatGPTAutoReloginSection />
    </Form>,
  )
}

describe('ChatGPTAutoReloginSection', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockResolvedValue({
      state: 'idle',
      eligible_accounts: 2,
      last_task_id: 'relogin-task-1',
      last_started_at: '2026-08-01T00:00:00Z',
      next_run_at: '2026-08-01T00:30:00Z',
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('shows disabled defaults and supplies bounded values', async () => {
    renderSection()

    await screen.findByText('空闲')

    expect(screen.getByRole('switch', { name: '启用 ChatGPT 自动重登' }).getAttribute('aria-checked')).toBe('false')
    const interval = screen.getByRole('spinbutton', { name: 'Codex2API 鉴权巡检间隔（分钟）' }) as HTMLInputElement
    const concurrency = screen.getByRole('spinbutton', { name: '异常账号重登并发数' }) as HTMLInputElement
    const threshold = screen.getByRole('spinbutton', { name: '重登失败告警阈值（账号数）' }) as HTMLInputElement
    const quotaThreshold = screen.getByRole('spinbutton', { name: 'Codex2API 当前剩余可用额度告警阈值（美元）' }) as HTMLInputElement
    expect(interval.value).toBe('2')
    expect(interval.getAttribute('aria-valuemin')).toBe('2')
    expect(interval.getAttribute('aria-valuemax')).toBe('1440')
    expect(concurrency.value).toBe('3')
    expect(concurrency.getAttribute('aria-valuemin')).toBe('1')
    expect(concurrency.getAttribute('aria-valuemax')).toBe('3')
    expect(threshold.value).toBe('20')
    expect(threshold.getAttribute('aria-valuemin')).toBe('1')
    expect(threshold.getAttribute('aria-valuemax')).toBe('10000')
    expect(quotaThreshold.value).toBe('0.00')
    expect(quotaThreshold.getAttribute('aria-valuemin')).toBe('0')
    expect(quotaThreshold.getAttribute('aria-valuemax')).toBe('10000000')
    expect(screen.getByText(/重登失败账号数达到或超过此值时通过已启用的通知渠道发送提醒/)).toBeTruthy()
    expect(screen.getByText(/当前剩余可用额度低于此值都会通过已启用的通知渠道发送提醒/)).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'SMTP 服务器地址' })).toBeTruthy()
    expect(screen.getByLabelText('SMTP 访问凭证')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: '告警接收邮箱' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '发送测试邮件' })).toBeTruthy()
    expect(screen.getByText('告警通知')).toBeTruthy()
    expect(screen.getByRole('switch', { name: '启用 Bark 强提醒' }).getAttribute('aria-checked')).toBe('false')
    expect(screen.getByLabelText('Bark 推送地址')).toBeTruthy()
    expect(screen.getByRole('button', { name: '发送测试 Bark 通知' })).toBeTruthy()
    expect(screen.getByText(/critical \+ call=1/)).toBeTruthy()
    expect(screen.getByText(/主动触发 Codex2API 的 wham-only 轻量鉴权探针/)).toBeTruthy()
    expect(screen.getByText(/正常与限流账号不会刷新本地 RT/)).toBeTruthy()
  })

  it('sends a test email with the current unsaved SMTP form values', async () => {
    const user = userEvent.setup()
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/automations/chatgpt-relogin') {
        return { state: 'idle', eligible_accounts: 2 }
      }
      if (path === '/config/smtp/test') {
        return { ok: true, message: '测试邮件已发送' }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    renderSection({
      smtp_host: 'smtp.example.com',
      smtp_port: 465,
      smtp_username: 'sender@example.com',
      smtp_password: '',
      smtp_sender_email: 'sender@example.com',
      smtp_recipient_email: '1666606639@qq.com',
      smtp_use_ssl: true,
      smtp_force_auth_login: false,
    })

    await user.click(await screen.findByRole('button', { name: '发送测试邮件' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        '/config/smtp/test',
        expect.objectContaining({ method: 'POST' }),
      )
    })
    const call = vi.mocked(apiFetch).mock.calls.find(
      ([path]) => path === '/config/smtp/test',
    )
    const payload = JSON.parse(String(call?.[1]?.body || '{}'))
    expect(payload.data).toMatchObject({
      smtp_host: 'smtp.example.com',
      smtp_port: 465,
      smtp_username: 'sender@example.com',
      smtp_password: '',
      smtp_sender_email: 'sender@example.com',
      smtp_recipient_email: '1666606639@qq.com',
      smtp_use_ssl: true,
      smtp_force_auth_login: false,
    })
    expect(await screen.findByText('测试邮件已发送')).toBeTruthy()
  })

  it('shows the SMTP test failure detail and allows another attempt', async () => {
    const user = userEvent.setup()
    let attempts = 0
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/automations/chatgpt-relogin') {
        return { state: 'idle', eligible_accounts: 2 }
      }
      if (path === '/config/smtp/test') {
        attempts += 1
        if (attempts === 1) throw new Error('SMTP 认证失败')
        return { ok: true, message: '测试邮件已发送' }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    renderSection({ smtp_host: 'smtp.example.com', smtp_port: 465 })

    const button = await screen.findByRole('button', { name: '发送测试邮件' })
    await user.click(button)
    expect((await screen.findByRole('alert')).textContent).toContain('SMTP 认证失败')

    await user.click(button)
    expect(await screen.findByText('测试邮件已发送')).toBeTruthy()
    expect(attempts).toBe(2)
  })

  it('sends a Bark test with the current unsaved form values', async () => {
    const user = userEvent.setup()
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/automations/chatgpt-relogin') {
        return { state: 'idle', eligible_accounts: 2 }
      }
      if (path === '/config/bark/test') {
        return { ok: true, message: '测试 Bark 强提醒已发送' }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    renderSection({
      bark_enabled: true,
      bark_endpoint: 'https://api.day.app/UNSAVED_DEVICE_KEY',
    })

    await user.click(await screen.findByRole('button', { name: '发送测试 Bark 通知' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        '/config/bark/test',
        expect.objectContaining({ method: 'POST' }),
      )
    })
    const call = vi.mocked(apiFetch).mock.calls.find(
      ([path]) => path === '/config/bark/test',
    )
    const payload = JSON.parse(String(call?.[1]?.body || '{}'))
    expect(payload.data).toEqual({
      bark_enabled: true,
      bark_endpoint: 'https://api.day.app/UNSAVED_DEVICE_KEY',
    })
    expect(await screen.findByText('测试 Bark 强提醒已发送')).toBeTruthy()
  })

  it('shows a Bark test failure and allows another attempt', async () => {
    const user = userEvent.setup()
    let attempts = 0
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/automations/chatgpt-relogin') {
        return { state: 'idle', eligible_accounts: 2 }
      }
      if (path === '/config/bark/test') {
        attempts += 1
        if (attempts === 1) throw new Error('Bark 地址无效')
        return { ok: true, message: '测试 Bark 强提醒已发送' }
      }
      throw new Error(`unexpected request: ${path}`)
    })
    renderSection({ bark_enabled: true, bark_endpoint: '' })

    const button = await screen.findByRole('button', { name: '发送测试 Bark 通知' })
    await user.click(button)
    expect((await screen.findByRole('alert')).textContent).toContain('Bark 地址无效')

    await user.click(button)
    expect(await screen.findByText('测试 Bark 强提醒已发送')).toBeTruthy()
    expect(attempts).toBe(2)
  })

  it('does not start another Bark test while one is in flight', async () => {
    const user = userEvent.setup()
    const barkRequest = deferred<Record<string, unknown>>()
    vi.mocked(apiFetch).mockImplementation((path: string) => {
      if (path === '/automations/chatgpt-relogin') {
        return Promise.resolve({ state: 'idle', eligible_accounts: 2 })
      }
      if (path === '/config/bark/test') return barkRequest.promise
      return Promise.reject(new Error(`unexpected request: ${path}`))
    })
    renderSection({ bark_enabled: true, bark_endpoint: '' })

    const button = await screen.findByRole('button', { name: '发送测试 Bark 通知' })
    await user.click(button)
    await user.click(button)

    expect(
      vi.mocked(apiFetch).mock.calls.filter(([path]) => path === '/config/bark/test'),
    ).toHaveLength(1)
    await act(async () => {
      barkRequest.resolve({ ok: true, message: '测试 Bark 强提醒已发送' })
      await barkRequest.promise
    })
    expect(await screen.findByText('测试 Bark 强提醒已发送')).toBeTruthy()
  })

  it('shows the explicit no-account paused status', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      state: 'paused_no_accounts',
      eligible_accounts: 0,
      last_task_id: null,
      last_started_at: null,
      next_run_at: null,
    })

    renderSection()

    expect(await screen.findByText('已暂停：没有可登录账号')).toBeTruthy()
    expect(screen.getByText('0')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain('已暂停：没有可登录账号')
  })

  it('does not start another status request while the previous poll is unresolved', async () => {
    const first = deferred<Record<string, unknown>>()
    const second = deferred<Record<string, unknown>>()
    vi.mocked(apiFetch)
      .mockReset()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    const setIntervalSpy = vi.spyOn(window, 'setInterval')

    renderSection()

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1))
    const callback = setIntervalSpy.mock.calls.find(([, delay]) => delay === 5_000)?.[0]
    expect(callback).toBeTypeOf('function')

    ;(callback as () => void)()
    await Promise.resolve()
    expect(apiFetch).toHaveBeenCalledTimes(1)

    await act(async () => {
      first.resolve({ state: 'idle', eligible_accounts: 2 })
      await first.promise
    })
    ;(callback as () => void)()
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2))

    await act(async () => {
      second.resolve({ state: 'running', eligible_accounts: 2 })
      await second.promise
    })
    expect(await screen.findByText('运行中')).toBeTruthy()
    setIntervalSpy.mockRestore()
  })

  it('announces a status refresh failure as an alert', async () => {
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error('status offline'))

    renderSection()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('状态暂时不可用，将自动重试。')
  })

  it('fetches immediately, refreshes on the timer, and clears the timer on unmount', async () => {
    const setIntervalSpy = vi.spyOn(window, 'setInterval')
    const clearIntervalSpy = vi.spyOn(window, 'clearInterval')
    const { unmount } = renderSection()

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/automations/chatgpt-relogin')
    })

    const callback = setIntervalSpy.mock.calls.find(([, delay]) => delay === 5_000)?.[0]
    expect(callback).toBeTypeOf('function')
    await (callback as () => void)()
    await waitFor(() => {
      expect(vi.mocked(apiFetch).mock.calls.filter(([path]) => path === '/automations/chatgpt-relogin')).toHaveLength(2)
    })

    unmount()
    expect(clearIntervalSpy).toHaveBeenCalled()
    setIntervalSpy.mockRestore()
    clearIntervalSpy.mockRestore()
  })
})
