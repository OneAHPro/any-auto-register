// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch } from '@/lib/utils'
import RunningTasks from './RunningTasks'

vi.mock('@/lib/utils', async () => {
  const actual = await vi.importActual<typeof import('@/lib/utils')>('@/lib/utils')
  return {
    ...actual,
    apiFetch: vi.fn(),
    getToken: vi.fn(() => ''),
  }
})

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

function automaticSummary(overrides: Record<string, unknown> = {}) {
  return {
    id: 'task-auto-history',
    platform: 'chatgpt',
    source: 'schedule',
    status: 'done',
    total: 64,
    success: 53,
    registered: 64,
    skipped: 0,
    error_count: 2,
    created_at: 1_786_000_000,
    updated_at: 1_786_000_060,
    meta: {
      automation: true,
      invalid_rt_count: 6,
      relogin_failed_count: 5,
      deleted_account_count: 3,
      alert_sent: true,
      alert_reason: 'sent',
    },
    ...overrides,
  }
}

function configureApi(summary = automaticSummary()) {
  vi.mocked(apiFetch).mockImplementation(async (path) => {
    if (path === '/tasks/summary') return [summary]
    if (path === `/tasks/${summary.id}`) {
      return {
        ...summary,
        progress: '64/64',
        errors: ['full detail only'],
        logs: ['delayed full log'],
        control: { stop_requested: false },
      }
    }
    if (path === `/tasks/${summary.id}/retryable`) return { count: 0 }
    throw new Error(`unexpected API path: ${path}`)
  })
}

describe('RunningTasks lightweight summaries', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    configureApi()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('loads cards from the summary endpoint and separates failures from deleted accounts', async () => {
    render(<RunningTasks />)

    expect(await screen.findByText('自动认证')).toBeTruthy()
    expect(apiFetch).toHaveBeenCalledWith(
      '/tasks/summary',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path === '/tasks')).toBe(false)
    expect(screen.getByText('✓ 成功 53')).toBeTruthy()
    expect(screen.getByText('✗ 失败 2')).toBeTruthy()
    expect(screen.getByText('已删除账号 3')).toBeTruthy()
    expect(screen.getByText('鉴权失效 6')).toBeTruthy()
    expect(screen.getByText('重登失败 5')).toBeTruthy()
    expect(screen.getByText('邮件已提醒')).toBeTruthy()
  })

  it('shows an explicit zero deleted-account counter for automatic tasks', async () => {
    configureApi(automaticSummary({
      meta: {
        automation: true,
        invalid_rt_count: 0,
        relogin_failed_count: 0,
        deleted_account_count: 0,
      },
    }))

    render(<RunningTasks />)

    expect(await screen.findByText('已删除账号 0')).toBeTruthy()
  })

  it('does not start a second summary request while the previous request is pending', async () => {
    vi.useFakeTimers()
    let resolveFirst!: (value: unknown) => void
    const firstRequest = new Promise((resolve) => {
      resolveFirst = resolve
    })
    vi.mocked(apiFetch)
      .mockReset()
      .mockReturnValueOnce(firstRequest)
      .mockResolvedValue([automaticSummary()])

    render(<RunningTasks />)
    expect(apiFetch).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(7_500)
    })
    expect(apiFetch).toHaveBeenCalledTimes(1)

    await act(async () => {
      resolveFirst([automaticSummary()])
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500)
    })
    expect(apiFetch).toHaveBeenCalledTimes(2)
  })

  it('releases the polling lock after a failed summary request', async () => {
    vi.useFakeTimers()
    vi.mocked(apiFetch)
      .mockReset()
      .mockRejectedValueOnce(new Error('temporary network failure'))
      .mockResolvedValue([automaticSummary()])

    render(<RunningTasks />)
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(apiFetch).toHaveBeenCalledTimes(1)
    expect(screen.getByText('任务列表加载失败，正在自动重试')).toBeTruthy()
    expect(screen.queryByText('暂无任务记录')).toBeNull()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500)
    })

    expect(apiFetch).toHaveBeenCalledTimes(2)
    expect(screen.getByText('自动认证')).toBeTruthy()
    expect(screen.queryByText('任务列表加载失败，正在自动重试')).toBeNull()
  })

  it('times out a permanently pending request and retries on the next poll', async () => {
    vi.useFakeTimers()
    let resolveRetry!: (value: unknown) => void
    const retryRequest = new Promise((resolve) => {
      resolveRetry = resolve
    })
    vi.mocked(apiFetch)
      .mockReset()
      .mockReturnValueOnce(new Promise(() => {}))
      .mockReturnValueOnce(retryRequest)

    render(<RunningTasks />)
    expect(apiFetch).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })
    expect(screen.getByText('任务列表加载失败，正在自动重试')).toBeTruthy()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500)
    })

    expect(apiFetch).toHaveBeenCalledTimes(2)
    await act(async () => {
      resolveRetry([automaticSummary()])
      await Promise.resolve()
    })
    expect(screen.getByText('自动认证')).toBeTruthy()
    expect(screen.queryByText('任务列表加载失败，正在自动重试')).toBeNull()
  })

  it('aborts the current request on unmount without updating state afterward', async () => {
    let resolveRequest!: (value: unknown) => void
    const pendingRequest = new Promise((resolve) => {
      resolveRequest = resolve
    })
    vi.mocked(apiFetch).mockReset().mockReturnValueOnce(pendingRequest)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    const { unmount } = render(<RunningTasks />)
    const requestOptions = vi.mocked(apiFetch).mock.calls[0][1]
    const signal = requestOptions?.signal
    expect(signal).toBeInstanceOf(AbortSignal)
    expect(signal?.aborted).toBe(false)

    unmount()
    expect(signal?.aborted).toBe(true)

    await act(async () => {
      resolveRequest([automaticSummary()])
      await Promise.resolve()
    })
    const unmountedWarnings = consoleError.mock.calls
      .flat()
      .map(String)
      .filter((message) => /unmounted|state update/i.test(message))
    expect(unmountedWarnings).toEqual([])
    consoleError.mockRestore()
  })

  it('starts a replacement request immediately after StrictMode cleanup', async () => {
    let firstSignal: AbortSignal | undefined
    vi.mocked(apiFetch)
      .mockReset()
      .mockImplementationOnce((_path, options) => {
        firstSignal = options?.signal ?? undefined
        if (!firstSignal) return new Promise(() => {})
        return new Promise((_resolve, reject) => {
          firstSignal?.addEventListener(
            'abort',
            () => reject(new DOMException('aborted', 'AbortError')),
            { once: true },
          )
        })
      })
      .mockResolvedValueOnce([automaticSummary()])

    render(
      <StrictMode>
        <RunningTasks />
      </StrictMode>,
    )

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2))
    expect(firstSignal?.aborted).toBe(true)
    expect(await screen.findByText('自动认证')).toBeTruthy()
    expect(screen.queryByText('任务列表加载失败，正在自动重试')).toBeNull()
  })

  it('loads the full task snapshot only after opening the log drawer', async () => {
    configureApi()
    render(<RunningTasks />)
    expect(await screen.findByText('自动认证')).toBeTruthy()

    expect(
      vi.mocked(apiFetch).mock.calls.some(([path]) => path === '/tasks/task-auto-history'),
    ).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: /查看日志/ }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/tasks/task-auto-history')
    })
    expect(await screen.findByText('delayed full log')).toBeTruthy()
  })

  it('keeps delete interaction working with summary-only cards', async () => {
    const user = userEvent.setup()
    const task = automaticSummary({ id: 'task-delete-me' })
    configureApi(task)
    vi.mocked(apiFetch).mockImplementation(async (path, options) => {
      if (path === '/tasks/summary') return [task]
      if (path === '/tasks/task-delete-me' && options?.method === 'DELETE') {
        return { ok: true }
      }
      throw new Error(`unexpected API path: ${path}`)
    })
    render(<RunningTasks />)
    expect(await screen.findByText('自动认证')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: /删除/ }))
    await user.click(await screen.findByRole('button', { name: '删 除' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/tasks/task-delete-me', { method: 'DELETE' })
    })
    expect(screen.queryByText('task-delete-me')).toBeNull()
  })
})
