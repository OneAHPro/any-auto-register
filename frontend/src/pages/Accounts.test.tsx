// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { apiFetch } from '@/lib/utils'
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

  it('does not show ChatGPT staged-login actions on another platform', async () => {
    routeState.platform = 'kiro'
    render(<Accounts />)

    await screen.findByText('eligible@example.com')
    expect(screen.queryByRole('button', { name: /登录$/ })).toBeNull()
    expect(screen.queryByRole('button', { name: '接码' })).toBeNull()
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
})
