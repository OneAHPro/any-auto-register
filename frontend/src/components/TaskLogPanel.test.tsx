// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from './TaskLogPanel'

vi.mock('@/lib/utils', () => ({
  API_BASE: '/api',
  apiFetch: vi.fn(),
  getToken: vi.fn(() => ''),
}))

describe('TaskLogPanel terminal feedback', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockResolvedValue({
      logs: [],
      status: 'done',
      success: 1,
      registered: 2,
      total: 3,
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('shows a partial result instead of claiming the login completed', async () => {
    render(<TaskLogPanel taskId="login-task" mode="login" />)

    expect(await screen.findByText('登录成功：1')).toBeTruthy()
    expect(screen.getByText('已处理：2')).toBeTruthy()
    expect(screen.getByText('登录总数：3')).toBeTruthy()
    expect(screen.getByText('登录部分完成（成功 1 / 3）')).toBeTruthy()
    expect(screen.queryByText('登录完成')).toBeNull()
    expect(screen.queryByText(/注册/)).toBeNull()
  })

  it('shows login failure when a done task has zero successes', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: ['登录失败: 邮箱验证码校验失败'],
      status: 'done',
      success: 0,
      registered: 1,
      total: 1,
    })

    render(<TaskLogPanel taskId="failed-login-task" mode="login" />)

    expect(await screen.findByText('登录失败（成功 0 / 1）')).toBeTruthy()
    expect(screen.queryByText('登录完成')).toBeNull()
  })

  it('shows failure when a done task reports no processed accounts', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: ['致命错误: 登录任务未启动'],
      status: 'done',
      success: 0,
      registered: 0,
      total: 0,
    })

    render(<TaskLogPanel taskId="empty-login-task" mode="login" />)

    expect(await screen.findByText('登录失败（成功 0 / 0）')).toBeTruthy()
    expect(screen.queryByText('登录完成')).toBeNull()
  })

  it('shows completion only when every login succeeds', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: [],
      status: 'done',
      success: 2,
      registered: 2,
      total: 2,
    })

    render(<TaskLogPanel taskId="successful-login-task" mode="login" />)

    expect(await screen.findByText('登录完成')).toBeTruthy()
  })

  it('uses relogin wording for relogin tasks', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: ['重登已成功，但 Codex2API 同步失败: token invalid'],
      status: 'done',
      success: 0,
      registered: 1,
      total: 1,
      meta: { mode: 'relogin' },
    })

    render(<TaskLogPanel taskId="relogin-task" />)

    expect(await screen.findByText('重登失败（成功 0 / 1）')).toBeTruthy()
    expect(screen.getByText('重登成功：0')).toBeTruthy()
    expect(screen.queryByText(/注册完成/)).toBeNull()
  })

  it('uses probe wording for scheduled remote authentication monitors', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: ['RT 探针检查完成'],
      status: 'done',
      source: 'schedule',
      success: 72,
      registered: 100,
      total: 101,
      meta: {
        automation: true,
        mode: 'remote_auth_monitor',
      },
    })

    render(<TaskLogPanel taskId="scheduled-probe-task" />)

    expect(await screen.findByText('探针正常：72')).toBeTruthy()
    expect(screen.getByText('已检查：100')).toBeTruthy()
    expect(screen.getByText('检查总数：101')).toBeTruthy()
    expect(screen.getByText('探针检查部分完成（成功 72 / 101）')).toBeTruthy()
    expect(screen.queryByText(/注册/)).toBeNull()
  })

  it('uses processing wording for manual relogin tasks', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: ['手动重登完成'],
      status: 'done',
      source: 'manual_relogin',
      success: 2,
      registered: 2,
      total: 2,
      meta: { mode: 'relogin' },
    })

    render(<TaskLogPanel taskId="manual-relogin-task" />)

    expect(await screen.findByText('重登成功：2')).toBeTruthy()
    expect(screen.getByText('已处理：2')).toBeTruthy()
    expect(screen.getByText('处理总数：2')).toBeTruthy()
    expect(screen.getByText('重登完成')).toBeTruthy()
  })

  it('keeps registration wording for actual registration tasks', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: ['注册完成'],
      status: 'done',
      source: 'manual',
      success: 3,
      registered: 3,
      total: 3,
      meta: {},
    })

    render(<TaskLogPanel taskId="registration-task" />)

    expect(await screen.findByText('注册成功：3')).toBeTruthy()
    expect(screen.getByText('已注册：3')).toBeTruthy()
    expect(screen.getByText('总共注册：3')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toBe('注册完成')
  })

  it('announces a partial relogin result as terminal status', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      logs: ['一个账号同步失败'],
      status: 'done',
      success: 1,
      registered: 2,
      total: 2,
      meta: { mode: 'relogin' },
    })

    render(<TaskLogPanel taskId="partial-relogin-task" />)

    const terminalStatus = await screen.findByRole('status')
    expect(terminalStatus.textContent).toBe('重登部分完成（成功 1 / 2）')
    expect(terminalStatus.getAttribute('aria-live')).toBe('polite')
    expect(terminalStatus.getAttribute('aria-atomic')).toBe('true')
  })

  it('retries persisted failed login bindings and follows the new task', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path, options) => {
      if (path === '/tasks/failed-login-task/retryable') {
        return { count: 5, items: [{ id: 7, email: 'failed@example.com' }] }
      }
      if (path === '/tasks/failed-login-task/retry-failed' && options?.method === 'POST') {
        return { task_id: 'retry-login-task', retry_count: 5, concurrency: 4 }
      }
      if (path === '/tasks/retry-login-task/retryable') {
        return { count: 0, items: [] }
      }
      if (path === '/tasks/retry-login-task') {
        return {
          logs: ['重试成功'],
          status: 'done',
          success: 1,
          registered: 1,
          total: 1,
          meta: { mode: 'login' },
        }
      }
      return {
        logs: ['登录失败'],
        status: 'done',
        success: 0,
        registered: 1,
        total: 1,
        meta: { mode: 'login' },
      }
    })

    render(<TaskLogPanel taskId="failed-login-task" />)

    const retryButton = await screen.findByRole('button', { name: '重试失败账号（5）' })
    fireEvent.click(retryButton)

    const concurrencyInput = await screen.findByRole('spinbutton', { name: '失败账号重试并发数' })
    fireEvent.change(concurrencyInput, { target: { value: '4' } })
    fireEvent.click(screen.getByRole('button', { name: '开始重试' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        '/tasks/failed-login-task/retry-failed',
        {
          method: 'POST',
          body: JSON.stringify({ concurrency: 4 }),
        },
      )
    })
    expect(await screen.findByText('重试成功')).toBeTruthy()
    expect(screen.getByText('登录完成')).toBeTruthy()
  })
})
