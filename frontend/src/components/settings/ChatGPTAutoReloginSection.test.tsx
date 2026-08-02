// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { Form } from 'antd'

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
    const interval = screen.getByRole('spinbutton', { name: '自动重登间隔（分钟）' }) as HTMLInputElement
    const concurrency = screen.getByRole('spinbutton', { name: '自动重登并发数' }) as HTMLInputElement
    expect(interval.value).toBe('30')
    expect(interval.getAttribute('aria-valuemin')).toBe('20')
    expect(interval.getAttribute('aria-valuemax')).toBe('1440')
    expect(concurrency.value).toBe('10')
    expect(concurrency.getAttribute('aria-valuemin')).toBe('1')
    expect(concurrency.getAttribute('aria-valuemax')).toBe('10')
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
