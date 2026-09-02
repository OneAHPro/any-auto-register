// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch } from '@/lib/utils'
import Codex2APIScheduler from './Codex2APIScheduler'

vi.mock('@/lib/utils', () => ({ apiFetch: vi.fn() }))

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

const run = {
  id: 'run-1',
  mode: 'dry_run',
  status: 'awaiting_confirmation',
  trigger: 'automatic',
  created_at: '2026-09-03T00:00:00Z',
  completed_at: null,
  plan: {
    pool_id: 'ENTERPRISE_A_POOL',
    current_count: 1,
    desired_count: 2,
    scale_up_count: 1,
    scale_down_count: 0,
    executable: true,
    requires_confirmation: true,
    blockers: [],
    cost_estimated: false,
    actions: [{
      identity_id: 'identity-1',
      local_account_id: 17,
      email: 'account@example.com',
      action: 'scale_up',
      source_target_id: 1,
      destination_target_id: 2,
      reason: 'forecast_capacity_required',
    }],
  },
  executed: [],
  errors: {},
}

describe('Codex2API scheduler console', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset().mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/scheduler/plan' && !options?.method) return { run }
      if (path === '/scheduler/runs') return { runs: [run] }
      if (path === '/codex2api/pools') {
        return { pools: [{ id: 'ENTERPRISE_A_POOL', name: '企业 A 号池', target_id: 2 }] }
      }
      if (path === '/scheduler/apply' && options?.method === 'POST') {
        return { run_id: 'run-1', status: 'queued' }
      }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  afterEach(cleanup)

  it('shows capacity, blockers, account actions, and an honest cost state', async () => {
    render(<Codex2APIScheduler />)

    expect((await screen.findAllByText('ENTERPRISE_A_POOL')).length).toBeGreaterThan(0)
    expect(screen.getByText('当前 1')).toBeTruthy()
    expect(screen.getByText('建议 2')).toBeTruthy()
    expect(screen.getByText('account@example.com')).toBeTruthy()
    expect(screen.getByText('成本未估算')).toBeTruthy()
  })

  it('requires a second explicit confirmation before applying a fresh plan', async () => {
    const user = userEvent.setup()
    render(<Codex2APIScheduler />)

    await user.click(await screen.findByRole('button', { name: /执行计划/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('请确认后执行')).toBeTruthy()
    expect(
      vi.mocked(apiFetch).mock.calls.some(([, options]) => options?.method === 'POST' && options.body === JSON.stringify({ run_id: 'run-1', confirm: true })),
    ).toBe(false)

    await user.click(within(dialog).getByRole('button', { name: '确认执行' }))

    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith('/scheduler/apply', {
        method: 'POST',
        body: JSON.stringify({ run_id: 'run-1', confirm: true }),
      })
    })
  })
})
