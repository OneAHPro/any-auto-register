// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { message } from 'antd'

import { apiFetch } from '@/lib/utils'
import { formatAutoReloginCountdown } from '@/lib/chatgptAutoReloginStatus'
import Accounts from './Accounts'

const routeState = vi.hoisted(() => ({ platform: 'chatgpt' }))

vi.mock('react-router-dom', () => ({
  useParams: () => ({ platform: routeState.platform }),
}))

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
}))

vi.mock('@/components/ChatGPTExistingAccountLoginModal', () => ({
  ChatGPTExistingAccountLoginModal: ({ open, onDone }: { open: boolean; onDone: () => void }) =>
    open ? (
      <div role="dialog" aria-label="existing-login-modal">
        <button onClick={onDone}>完成登录</button>
      </div>
    ) : null,
}))

vi.mock('@/components/ChatGPTPhoneVerificationModal', () => ({
  ChatGPTPhoneVerificationModal: ({
    open,
    account,
    onSuccess,
    onClose,
  }: {
    open: boolean
    account: { email?: string } | null
    onSuccess: () => void
    onClose: () => void
  }) =>
    open ? (
      <div role="dialog" aria-label="phone-verification-modal">
        <span>{account?.email}</span>
        <button onClick={onSuccess}>完成接码</button>
        <button onClick={onClose}>关闭接码结果</button>
      </div>
    ) : null,
}))

vi.mock('@/components/TaskLogPanel', () => ({
  TaskLogPanel: ({ taskId, mode, onDone }: { taskId: string; mode?: string; onDone?: () => void }) => (
    <div data-testid="task-log-panel">
      <span>{`${mode}:${taskId}`}</span>
      <button onClick={onDone}>完成重登任务</button>
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

const eligibleAccount = {
  id: 17,
  platform: 'chatgpt',
  email: 'eligible@example.com',
  password: 'secret',
  token: 'access-token',
  status: 'registered',
  created_at: '2026-07-30T00:00:00Z',
  extra_json: JSON.stringify({
    access_token: 'access-token',
    refresh_token: '',
    mailbox_login_context: {
      provider: 'microsoft',
      email: 'eligible@example.com',
      extra: { client_id: 'mail-client', refresh_token: 'mail-refresh' },
    },
  }),
}

const legacyAccount = {
  ...eligibleAccount,
  id: 19,
  email: 'legacy@example.com',
  extra_json: JSON.stringify({ access_token: 'access-token', refresh_token: '' }),
}

const completedAccount = {
  ...eligibleAccount,
  id: 18,
  email: 'complete@example.com',
  extra_json: JSON.stringify({ access_token: 'access-token', refresh_token: 'refresh-token' }),
}

function accountRequestCount() {
  return vi.mocked(apiFetch).mock.calls.filter(([path]) => String(path).startsWith('/accounts?')).length
}

function createMessageResult(): ReturnType<typeof message.success> {
  const result = (() => undefined) as ReturnType<typeof message.success>
  result.then = Promise.resolve(true).then.bind(Promise.resolve(true))
  return result
}

async function selectAccount(user: ReturnType<typeof userEvent.setup>, email: string) {
  const row = (await screen.findByText(email)).closest('tr')
  expect(row).toBeTruthy()
  await user.click(within(row as HTMLElement).getByRole('checkbox'))
}

async function confirmBatchDelete(user: ReturnType<typeof userEvent.setup>, count: number) {
  await user.click(screen.getByRole('button', { name: new RegExp(`删除 ${count} 个$`) }))
  const popover = screen.getByText(`确认删除选中的 ${count} 个账号？`).closest('.ant-popover')
  expect(popover).toBeTruthy()
  await user.click(within(popover as HTMLElement).getByRole('button', { name: /删\s*除/ }))
}

describe('Accounts ChatGPT staged login integration', () => {
  beforeEach(() => {
    routeState.platform = 'chatgpt'
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.startsWith('/accounts?')) {
        return { items: [eligibleAccount, completedAccount], total: 2 }
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      throw new Error(`unexpected path: ${path}`)
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('formats the next automatic authentication deadline as a live countdown', () => {
    const now = Date.parse('2026-08-03T00:00:00Z')

    expect(
      formatAutoReloginCountdown(
        {
          enabled: true,
          state: 'idle',
          next_run_at: '2026-08-03T00:10:00Z',
        },
        now,
      ),
    ).toBe('10:00')
  })

  it('opens staged login and phone verification from the ChatGPT account page and refreshes after completion', async () => {
    const user = userEvent.setup()
    render(<Accounts />)

    const eligibleRow = (await screen.findByText('eligible@example.com')).closest('tr')
    const completedRow = screen.getByText('complete@example.com').closest('tr')
    expect(eligibleRow).toBeTruthy()
    expect(completedRow).toBeTruthy()

    const eligibleActions = within(eligibleRow as HTMLElement).getAllByRole('button')
    const phoneActionIndex = eligibleActions.findIndex((button) => button.textContent === '接码')
    expect(phoneActionIndex).toBeGreaterThanOrEqual(0)
    expect(phoneActionIndex).toBeLessThan(eligibleActions.findIndex((button) => button.textContent === '详情'))
    expect(within(completedRow as HTMLElement).queryByRole('button', { name: '接码' })).toBeNull()

    await user.click(screen.getByRole('button', { name: /登录$/ }))
    expect(await screen.findByRole('dialog', { name: 'existing-login-modal' })).toBeTruthy()
    const beforeLoginRefresh = accountRequestCount()
    await user.click(screen.getByRole('button', { name: '完成登录' }))
    await waitFor(() => expect(accountRequestCount()).toBeGreaterThan(beforeLoginRefresh))
    expect(screen.getByRole('dialog', { name: 'existing-login-modal' })).toBeTruthy()

    await user.click(within(eligibleRow as HTMLElement).getByRole('button', { name: '接码' }))
    expect((await screen.findByRole('dialog', { name: 'phone-verification-modal' })).textContent).toContain(
      'eligible@example.com',
    )
    const beforePhoneRefresh = accountRequestCount()
    await user.click(screen.getByRole('button', { name: '完成接码' }))
    await waitFor(() => expect(accountRequestCount()).toBeGreaterThan(beforePhoneRefresh))
    expect(screen.getByRole('dialog', { name: 'phone-verification-modal' })).toBeTruthy()

    await user.click(screen.getByRole('button', { name: '关闭接码结果' }))
    expect(screen.queryByRole('dialog', { name: 'phone-verification-modal' })).toBeNull()
  })

  it('shows the ChatGPT login action when the account list is empty', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.startsWith('/accounts?')) return { items: [], total: 0 }
      if (path.startsWith('/actions/')) return { actions: [] }
      throw new Error(`unexpected path: ${path}`)
    })

    render(<Accounts />)

    expect(await screen.findByText('0 个账号')).toBeTruthy()
    expect(screen.getByRole('button', { name: /登录$/ })).toBeTruthy()
  })

  it('documents dash-delimited email and password imports', async () => {
    const user = userEvent.setup()
    render(<Accounts />)

    await screen.findByText('eligible@example.com')
    await user.click(screen.getByRole('button', { name: /导入$/ }))

    expect(await screen.findByText(/email----password/)).toBeTruthy()
  })

  it('shows the current automatic authentication cycle after the account count', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.startsWith('/accounts?')) {
        return { items: [eligibleAccount, completedAccount], total: 64 }
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/automations/chatgpt-relogin') {
        return {
          enabled: true,
          state: 'running',
          reason: 'task_running',
          eligible_accounts: 64,
          active_task_id: 'task-auto-running',
          next_run_at: null,
          interval_minutes: 10,
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })

    render(<Accounts />)

    const accountCount = await screen.findByText('64 个账号')
    const countdown = await screen.findByText('下次执行：当前轮运行中')
    expect(
      accountCount.compareDocumentPosition(countdown) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('runs the automatic authentication immediately once and reflects the running status', async () => {
    let resolveRunNow!: (value: Record<string, unknown>) => void
    const pendingRunNow = new Promise<Record<string, unknown>>((resolve) => {
      resolveRunNow = resolve
    })
    const success = vi.spyOn(message, 'success').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 64 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/automations/chatgpt-relogin') {
        return {
          enabled: true,
          state: 'idle',
          reason: 'scheduled',
          eligible_accounts: 64,
          next_run_at: '2099-08-03T00:10:00Z',
          interval_minutes: 10,
        }
      }
      if (path === '/automations/chatgpt-relogin/run-now' && options?.method === 'POST') {
        return pendingRunNow
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    const runNow = await screen.findByRole('button', { name: /立即执行/ })
    expect((runNow as HTMLButtonElement).disabled).toBe(false)
    await user.click(runNow)
    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/automations/chatgpt-relogin/run-now', {
        method: 'POST',
      })
    })
    expect((runNow as HTMLButtonElement).disabled).toBe(true)
    await user.click(runNow)
    expect(
      vi.mocked(apiFetch).mock.calls.filter(
        ([path]) => path === '/automations/chatgpt-relogin/run-now',
      ),
    ).toHaveLength(1)

    resolveRunNow({
      accepted: true,
      task_id: 'task-auto-now',
      status: {
        enabled: true,
        state: 'running',
        reason: 'task_running',
        eligible_accounts: 64,
        active_task_id: 'task-auto-now',
        next_run_at: null,
      },
    })
    await waitFor(() => {
      expect(screen.getByText('下次执行：当前轮运行中')).toBeTruthy()
      expect(success).toHaveBeenCalledWith('自动化流程已立即启动')
    })
    expect(
      (screen.getByRole('button', { name: /立即执行/ }) as HTMLButtonElement).disabled,
    ).toBe(true)
    success.mockRestore()
  })

  it.each([
    [{ enabled: false, state: 'disabled', eligible_accounts: 64 }, 'disabled'],
    [{ enabled: true, state: 'running', eligible_accounts: 64 }, 'running'],
    [{ enabled: true, state: 'idle', eligible_accounts: 0 }, 'no accounts'],
  ])('disables immediate execution while automation is %s', async (status) => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/automations/chatgpt-relogin') return status
      throw new Error(`unexpected path: ${path}`)
    })

    render(<Accounts />)

    expect(
      (await screen.findByRole('button', { name: /立即执行/ }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('reports a bounded immediate-execution error and restores the button', async () => {
    const error = vi.spyOn(message, 'error').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/automations/chatgpt-relogin') {
        return { enabled: true, state: 'idle', eligible_accounts: 1 }
      }
      if (path === '/automations/chatgpt-relogin/run-now' && options?.method === 'POST') {
        throw new Error('当前已有 ChatGPT 自动化任务正在运行')
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    const runNow = await screen.findByRole('button', { name: /立即执行/ })
    await user.click(runNow)

    await waitFor(() => {
      expect(error).toHaveBeenCalledWith(
        '立即执行失败：当前已有 ChatGPT 自动化任务正在运行',
      )
      expect((runNow as HTMLButtonElement).disabled).toBe(false)
    })
    error.mockRestore()
  })

  it('does not show ChatGPT staged-login actions on another platform', async () => {
    routeState.platform = 'kiro'
    render(<Accounts />)

    await screen.findByText('eligible@example.com')
    expect(screen.queryByRole('button', { name: /登录$/ })).toBeNull()
    expect(screen.queryByRole('button', { name: '接码' })).toBeNull()
    expect(screen.queryByRole('button', { name: /立即执行/ })).toBeNull()
  })

  it('does not offer phone verification for a legacy AT-only account without mailbox credentials', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.startsWith('/accounts?')) return { items: [legacyAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      throw new Error(`unexpected path: ${path}`)
    })

    render(<Accounts />)

    const row = (await screen.findByText('legacy@example.com')).closest('tr')
    expect(within(row as HTMLElement).queryByRole('button', { name: '接码' })).toBeNull()
  })

  it('keeps the email column fixed while the ChatGPT table scrolls horizontally', async () => {
    render(<Accounts />)

    await screen.findByText('eligible@example.com')
    expect(screen.getByRole('columnheader', { name: '邮箱' }).className).toContain(
      'ant-table-cell-fix-left',
    )
  })

  it('constrains long refresh-token previews inside their table cell', async () => {
    render(<Accounts />)

    const completedRow = (await screen.findByText('complete@example.com')).closest('tr')
    const tokenPreview = within(completedRow as HTMLElement).getByText('refresh-token')
    expect((tokenPreview as HTMLElement).style.display).toBe('inline-block')
  })

  it('starts a full relogin task for selected ChatGPT accounts', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) {
        return { items: [eligibleAccount, completedAccount], total: 2 }
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/tasks/chatgpt-relogin' && options?.method === 'POST') {
        return { task_id: 'relogin-task-1' }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    const eligibleRow = (await screen.findByText('eligible@example.com')).closest('tr')
    await user.click(within(eligibleRow as HTMLElement).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /重登所选.*1/ }))
    expect(
      (await screen.findByRole('spinbutton', { name: '重登并发数' }) as HTMLInputElement).value,
    ).toBe('1')
    await user.click(await screen.findByRole('button', { name: '确认' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/tasks/chatgpt-relogin', {
        method: 'POST',
        body: JSON.stringify({ account_ids: [17], concurrency: 1 }),
      })
    })
    expect(await screen.findByText('relogin:relogin-task-1')).toBeTruthy()
    expect(screen.queryByText('已选 1 个')).toBeNull()
  })

  it('starts selected ChatGPT relogins with the chosen concurrency', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) {
        return { items: [eligibleAccount, completedAccount], total: 2 }
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/tasks/chatgpt-relogin' && options?.method === 'POST') {
        return { task_id: 'relogin-task-concurrent' }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    const eligibleRow = (await screen.findByText('eligible@example.com')).closest('tr')
    const completedRow = screen.getByText('complete@example.com').closest('tr')
    await user.click(within(eligibleRow as HTMLElement).getByRole('checkbox'))
    await user.click(within(completedRow as HTMLElement).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /重登所选.*2/ }))
    const concurrencyInput = await screen.findByRole('spinbutton', { name: '重登并发数' })
    await user.clear(concurrencyInput)
    await user.type(concurrencyInput, '2')
    await user.click(await screen.findByRole('button', { name: '确认' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/tasks/chatgpt-relogin', {
        method: 'POST',
        body: JSON.stringify({ account_ids: [17, 18], concurrency: 2 }),
      })
    })
    expect(await screen.findByText('relogin:relogin-task-concurrent')).toBeTruthy()
  })

  it('starts forced MFA rotation for all eligible ChatGPT accounts', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) {
        return { items: [eligibleAccount, completedAccount], total: 2 }
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/tasks/chatgpt-relogin' && options?.method === 'POST') {
        return { task_id: 'mfa-reset-all-task', count: 2, concurrency: 5 }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await screen.findByText('eligible@example.com')
    await user.click(screen.getByRole('button', { name: /重设全部 MFA/ }))
    await user.click(await screen.findByRole('button', { name: '确认重设' }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/tasks/chatgpt-relogin', {
        method: 'POST',
        body: JSON.stringify({
          all_eligible: true,
          rotate_mfa: true,
          concurrency: 5,
        }),
      })
    })
    expect(await screen.findByText('relogin:mfa-reset-all-task')).toBeTruthy()
    expect(screen.getByText('重设全部 ChatGPT MFA')).toBeTruthy()
  })

  it('keeps the relogin action visible before an account is selected', async () => {
    render(<Accounts />)

    await screen.findByText('eligible@example.com')
    const reloginButton = screen.getByRole('button', { name: /重登所选.*0/ })
    expect((reloginButton as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows the relogin start failure and does not open a task panel', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/tasks/chatgpt-relogin' && options?.method === 'POST') {
        throw new Error('账号缺少邮箱登录凭据')
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    const row = (await screen.findByText('eligible@example.com')).closest('tr')
    await user.click(within(row as HTMLElement).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /重登所选.*1/ }))
    await user.click(await screen.findByRole('button', { name: '确认' }))

    expect(await screen.findByText('启动重登失败: 账号缺少邮箱登录凭据')).toBeTruthy()
    expect(screen.queryByTestId('task-log-panel')).toBeNull()
  })

  it('clears selected accounts and relogin errors when the route platform changes', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/tasks/chatgpt-relogin' && options?.method === 'POST') {
        throw new Error('账号缺少邮箱登录凭据')
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    const { rerender } = render(<Accounts />)

    const row = (await screen.findByText('eligible@example.com')).closest('tr')
    await user.click(within(row as HTMLElement).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /重登所选.*1/ }))
    await user.click(await screen.findByRole('button', { name: '确认' }))
    expect(await screen.findByText('启动重登失败: 账号缺少邮箱登录凭据')).toBeTruthy()
    expect(screen.getByText('已选 1 个')).toBeTruthy()

    routeState.platform = 'kiro'
    rerender(<Accounts />)

    await waitFor(() => {
      expect(screen.queryByText('已选 1 个')).toBeNull()
      expect(screen.queryByText('启动重登失败: 账号缺少邮箱登录凭据')).toBeNull()
    })
  })

  it('does not reopen a previous relogin task after switching away and back', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/tasks/chatgpt-relogin' && options?.method === 'POST') {
        return { task_id: 'route-relogin-task' }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    const { rerender } = render(<Accounts />)

    const row = (await screen.findByText('eligible@example.com')).closest('tr')
    await user.click(within(row as HTMLElement).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /重登所选.*1/ }))
    await user.click(await screen.findByRole('button', { name: '确认' }))
    expect(await screen.findByText('relogin:route-relogin-task')).toBeTruthy()

    routeState.platform = 'kiro'
    rerender(<Accounts />)
    await waitFor(() => expect(screen.queryByTestId('task-log-panel')).toBeNull())

    routeState.platform = 'chatgpt'
    rerender(<Accounts />)
    await waitFor(() => expect(screen.queryByTestId('task-log-panel')).toBeNull())
  })

  it('ignores a relogin response that arrives after the platform changed', async () => {
    let resolveRelogin!: (value: { task_id: string }) => void
    const pendingRelogin = new Promise<{ task_id: string }>((resolve) => {
      resolveRelogin = resolve
    })
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/tasks/chatgpt-relogin' && options?.method === 'POST') {
        return pendingRelogin
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    const { rerender } = render(<Accounts />)

    const row = (await screen.findByText('eligible@example.com')).closest('tr')
    await user.click(within(row as HTMLElement).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /重登所选.*1/ }))
    await user.click(await screen.findByRole('button', { name: '确认' }))
    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        '/tasks/chatgpt-relogin',
        expect.objectContaining({ method: 'POST' }),
      )
    })

    routeState.platform = 'kiro'
    rerender(<Accounts />)
    resolveRelogin({ task_id: 'late-relogin-task' })
    await Promise.resolve()

    routeState.platform = 'chatgpt'
    rerender(<Accounts />)
    await waitFor(() => expect(screen.queryByTestId('task-log-panel')).toBeNull())
    expect(screen.queryByText('relogin:late-relogin-task')).toBeNull()
  })

  it('keeps failed batch deletions selected and reports a remote ambiguity', async () => {
    const warning = vi.spyOn(message, 'warning').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount, completedAccount], total: 2 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        return {
          total_requested: 2,
          total_unique: 2,
          deleted: 1,
          failed: 1,
          not_found: [],
          remote_deleted: 1,
          remote_already_absent: 0,
          remote_skipped: 0,
          items: [
            { account_id: 17, ok: true, status: 'deleted', codex2api: { status: 'deleted' } },
            {
              account_id: 18,
              ok: false,
              status: 'failed',
              error_code: 'remote_ambiguous',
              message: 'Codex2API 存在多个匹配认证，已保留本地账号',
              codex2api: { status: 'ambiguous' },
            },
          ],
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(screen.getByText('已选 1 个')).toBeTruthy())
    const successfulRow = screen.getByText('eligible@example.com').closest('tr')
    const failedRow = screen.getByText('complete@example.com').closest('tr')
    expect((within(successfulRow as HTMLElement).getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
    expect((within(failedRow as HTMLElement).getByRole('checkbox') as HTMLInputElement).checked).toBe(true)
    expect(warning).toHaveBeenCalledWith(
      expect.stringContaining('批量删除部分完成：删除 1 个，失败 1 个'),
    )
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('Codex2API 存在多个匹配认证'))
    warning.mockRestore()
  })

  it('clears all selected accounts after a fully successful batch deletion', async () => {
    const success = vi.spyOn(message, 'success').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount, completedAccount], total: 2 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        return {
          total_requested: 2,
          total_unique: 2,
          deleted: 2,
          failed: 0,
          not_found: [],
          remote_deleted: 2,
          remote_already_absent: 0,
          remote_skipped: 0,
          items: [
            { account_id: 17, ok: true, status: 'deleted', codex2api: { status: 'deleted' } },
            { account_id: 18, ok: true, status: 'deleted', codex2api: { status: 'deleted' } },
          ],
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(screen.queryByText(/已选 \d+ 个/)).toBeNull())
    expect((within(screen.getByText('eligible@example.com').closest('tr') as HTMLElement).getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
    expect((within(screen.getByText('complete@example.com').closest('tr') as HTMLElement).getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
    expect(success).toHaveBeenCalledWith('批量删除完成：删除 2 个')
    success.mockRestore()
  })

  it('clears all selections for a legacy response whose counts prove every request completed', async () => {
    const success = vi.spyOn(message, 'success').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount, completedAccount], total: 2 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        return { deleted: 2, not_found: [], total_requested: 2 }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(screen.queryByText(/已选 \d+ 个/)).toBeNull())
    expect(success).toHaveBeenCalledWith('批量删除完成：删除 2 个')
    success.mockRestore()
  })

  it('clears only the explicit top-level not-found account when other outcomes are unknown', async () => {
    const error = vi.spyOn(message, 'error').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount, completedAccount], total: 2 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        return {
          deleted: 0,
          failed: 1,
          not_found: [17],
          total_requested: 2,
          items: [],
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(screen.getByText('已选 1 个')).toBeTruthy())
    const notFoundRow = screen.getByText('eligible@example.com').closest('tr')
    const unknownRow = screen.getByText('complete@example.com').closest('tr')
    expect((within(notFoundRow as HTMLElement).getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
    expect((within(unknownRow as HTMLElement).getByRole('checkbox') as HTMLInputElement).checked).toBe(true)
    expect(error).toHaveBeenCalledWith('批量删除失败：失败 1 个')
    error.mockRestore()
  })

  it('does not clear selections when a failed batch response has no trustworthy item IDs', async () => {
    const warning = vi.spyOn(message, 'warning').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount, completedAccount], total: 2 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        return {
          total_requested: 2,
          total_unique: 2,
          deleted: 1,
          failed: 1,
          not_found: [],
          items: [],
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(screen.getByText('已选 2 个')).toBeTruthy())
    expect(warning).toHaveBeenCalledWith('批量删除部分完成：删除 1 个，失败 1 个')
    warning.mockRestore()
  })

  it('does not clear unknown selections when legacy completion counts are incomplete', async () => {
    const success = vi.spyOn(message, 'success').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount, completedAccount], total: 2 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        return { deleted: 1, not_found: [], total_requested: 2 }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(screen.getByText('已选 2 个')).toBeTruthy())
    expect(success).toHaveBeenCalledWith('批量删除完成：删除 1 个')
    success.mockRestore()
  })

  it('keeps every selected account after a fully failed batch deletion', async () => {
    const error = vi.spyOn(message, 'error').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount, completedAccount], total: 2 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        return {
          total_requested: 2,
          total_unique: 2,
          deleted: 0,
          failed: 2,
          not_found: [],
          remote_deleted: 0,
          remote_already_absent: 0,
          remote_skipped: 0,
          items: [
            { account_id: 17, ok: false, status: 'failed', message: '远端服务暂时不可用' },
            { account_id: 18, ok: false, status: 'failed', message: '远端服务暂时不可用' },
          ],
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(screen.getByText('已选 2 个')).toBeTruthy())
    expect(error).toHaveBeenCalledWith(expect.stringContaining('批量删除失败：失败 2 个'))
    error.mockRestore()
  })

  it('reloads after a failed batch request without clearing the selection', async () => {
    const error = vi.spyOn(message, 'error').mockReturnValue(createMessageResult())
    let accountLoads = 0
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) {
        accountLoads += 1
        return { items: [eligibleAccount, completedAccount], total: 2 }
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/batch-delete' && options?.method === 'POST') {
        throw new Error('连接中断，删除结果未知')
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    await selectAccount(user, 'complete@example.com')
    await confirmBatchDelete(user, 2)

    await waitFor(() => expect(accountLoads).toBe(2))
    expect(screen.getByText('已选 2 个')).toBeTruthy()
    expect(error).toHaveBeenCalledWith('批量删除失败：连接中断，删除结果未知')
    error.mockRestore()
  })

  it('reports when a single deletion also removes the Codex2API credential', async () => {
    const success = vi.spyOn(message, 'success').mockReturnValue(createMessageResult())
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) return { items: [eligibleAccount], total: 1 }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/17' && options?.method === 'DELETE') {
        return {
          ok: true,
          account_id: 17,
          local_deleted: true,
          codex2api: { enabled: true, status: 'deleted', remote_id: 91 },
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    const row = (await screen.findByText('eligible@example.com')).closest('tr')
    await user.click(within(row as HTMLElement).getByRole('button', { name: '删除' }))
    const popover = screen.getByText('确认删除该账号吗？').closest('.ant-popover')
    await user.click(within(popover as HTMLElement).getByRole('button', { name: /删\s*除/ }))

    await waitFor(() => {
      expect(success).toHaveBeenCalledWith('本地账号与 Codex2API 认证已删除')
    })
    success.mockRestore()
  })

  it('keeps a successful delete message when the following account refresh fails', async () => {
    const success = vi.spyOn(message, 'success').mockReturnValue(createMessageResult())
    const error = vi.spyOn(message, 'error').mockReturnValue(createMessageResult())
    let accountLoads = 0
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) {
        accountLoads += 1
        if (accountLoads === 1) return { items: [eligibleAccount], total: 1 }
        throw new Error('数据库暂时不可用')
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/17' && options?.method === 'DELETE') {
        return {
          ok: true,
          account_id: 17,
          local_deleted: true,
          codex2api: { enabled: true, status: 'deleted', remote_id: 91 },
        }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    const row = (await screen.findByText('eligible@example.com')).closest('tr')
    await user.click(within(row as HTMLElement).getByRole('button', { name: '删除' }))
    const popover = screen.getByText('确认删除该账号吗？').closest('.ant-popover')
    await user.click(within(popover as HTMLElement).getByRole('button', { name: /删\s*除/ }))

    await waitFor(() => expect(accountLoads).toBe(2))
    expect(success).toHaveBeenCalledWith('本地账号与 Codex2API 认证已删除')
    expect(error).toHaveBeenCalledWith('刷新账号列表失败：数据库暂时不可用')
    expect(error).not.toHaveBeenCalledWith(expect.stringMatching(/^删除失败：/))
    success.mockRestore()
    error.mockRestore()
  })

  it('shows a single-delete API detail and keeps the failed account selected', async () => {
    const error = vi.spyOn(message, 'error').mockReturnValue(createMessageResult())
    let accountLoads = 0
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/accounts?')) {
        accountLoads += 1
        return { items: [eligibleAccount], total: 1 }
      }
      if (path.startsWith('/actions/')) return { actions: [] }
      if (path === '/accounts/17' && options?.method === 'DELETE') {
        throw { detail: 'Codex2API 匹配结果不唯一，已保留本地账号' }
      }
      throw new Error(`unexpected path: ${path}`)
    })
    const user = userEvent.setup()
    render(<Accounts />)

    await selectAccount(user, 'eligible@example.com')
    const row = screen.getByText('eligible@example.com').closest('tr')
    await user.click(within(row as HTMLElement).getByRole('button', { name: '删除' }))
    const popover = screen.getByText('确认删除该账号吗？').closest('.ant-popover')
    await user.click(within(popover as HTMLElement).getByRole('button', { name: /删\s*除/ }))

    await waitFor(() => {
      expect(error).toHaveBeenCalledWith('删除失败：Codex2API 匹配结果不唯一，已保留本地账号')
    })
    expect(screen.getByText('已选 1 个')).toBeTruthy()
    expect(screen.getByText('eligible@example.com')).toBeTruthy()
    expect(accountLoads).toBe(2)
    error.mockRestore()
  })
})
