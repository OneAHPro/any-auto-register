// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch } from '@/lib/utils'
import Codex2APITargets from './Codex2APITargets'

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

const target = {
  id: 2,
  name: 'node-b',
  target_type: 'enterprise',
  server_label: '美国二号机',
  base_url: 'https://node-b.example.com',
  admin_key: '********',
  default_pool_id: 'ENTERPRISE_A_POOL',
  enabled: true,
  health_status: 'healthy',
  capabilities: { migratable: true, restore: true },
  account_count: 12,
  last_health_at: '2026-09-03T00:00:00Z',
  last_sync_at: '2026-09-03T00:01:00Z',
  last_error: '',
}

describe('Codex2API target management', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset().mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/codex2api/targets' && !options?.method) return { targets: [target] }
      if (path === '/codex2api/pools') return { pools: [] }
      if (path === '/codex2api/targets/2/health' && options?.method === 'POST') {
        return { target_id: 2, health_status: 'healthy' }
      }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  afterEach(cleanup)

  it('shows target health, capabilities, account count, and masked key', async () => {
    render(<Codex2APITargets />)

    expect(await screen.findByText('node-b')).toBeTruthy()
    expect(screen.getByText('健康')).toBeTruthy()
    expect(screen.getByText('12 个账号')).toBeTruthy()
    expect(screen.getByText('可迁移')).toBeTruthy()
    expect(screen.getByText('********')).toBeTruthy()
    expect(screen.queryByText(/admin-secret/)).toBeNull()
  })

  it('never pre-fills the saved admin key while editing', async () => {
    const user = userEvent.setup()
    render(<Codex2APITargets />)

    const row = (await screen.findByText('node-b')).closest('tr')
    expect(row).toBeTruthy()
    await user.click(within(row as HTMLElement).getByRole('button', { name: /编辑/ }))

    const dialog = await screen.findByRole('dialog')
    const secretInput = within(dialog).getByLabelText('Admin Key') as HTMLInputElement
    expect(secretInput.value).toBe('')
    expect(secretInput.type).toBe('password')
  })

  it('runs an explicit health probe and reloads the list', async () => {
    const user = userEvent.setup()
    render(<Codex2APITargets />)

    const row = (await screen.findByText('node-b')).closest('tr')
    await user.click(within(row as HTMLElement).getByRole('button', { name: /检查健康/ }))

    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        '/codex2api/targets/2/health',
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })
})
