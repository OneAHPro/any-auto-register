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
      if (path === '/operations/sale-prices') return { items: [] }
      if (path === '/codex2api/targets/2/health' && options?.method === 'POST') {
        return { target_id: 2, health_status: 'healthy' }
      }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  afterEach(cleanup)

  it('keeps an unset sale price empty and saves a per-instance price without changing connection credentials', async () => {
    const original = vi.mocked(apiFetch).getMockImplementation()!
    let savedPrice: string | undefined
    vi.mocked(apiFetch).mockImplementation(async (path, options) => {
      if (path === '/operations/sale-prices') return { items: savedPrice ? [{ target_id: 2, price_cny_per_usd: savedPrice, effective_at: '2026-09-07T00:00:00Z' }] : [] }
      if (path === '/operations/targets/2/sale-price' && options?.method === 'PUT') {
        savedPrice = JSON.parse(String(options.body)).price_cny_per_usd
        return { target_id: 2, price_cny_per_usd: savedPrice, effective_at: '2026-09-07T00:00:00Z' }
      }
      return original(path, options)
    })
    const user = userEvent.setup()
    render(<Codex2APITargets />)
    const row = (await screen.findByText('node-b')).closest('tr')!
    expect(within(row).getByText('售价未设置')).toBeTruthy()
    await user.click(within(row).getByRole('button', { name: '设置售价' }))
    const input = await screen.findByRole('textbox', { name: '销售单价（元/美元）' }) as HTMLInputElement
    expect(input.value).toBe('')
    await user.type(input, '0.24')
    await user.click(screen.getByRole('button', { name: '保存售价' }))
    expect(await screen.findByText('¥0.2400/$')).toBeTruthy()
    const call = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/operations/targets/2/sale-price')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ price_cny_per_usd: '0.2400' })
    expect(vi.mocked(apiFetch).mock.calls.some(([path, options]) => path === '/codex2api/targets/2' && options?.method)).toBe(false)
  })

  it.each(['', '-0.2', '0.12345'])('rejects invalid sale price %s without writing to the instance', async (input) => {
    const user = userEvent.setup()
    render(<Codex2APITargets />)
    const row = (await screen.findByText('node-b')).closest('tr')!
    await user.click(within(row).getByRole('button', { name: '设置售价' }))
    const field = await screen.findByRole('textbox', { name: '销售单价（元/美元）' })
    if (input) await user.type(field, input)
    await user.click(screen.getByRole('button', { name: '保存售价' }))
    expect(await screen.findByText('请输入非负售价，最多保留四位小数')).toBeTruthy()
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path.includes('/sale-price') && path !== '/operations/sale-prices')).toBe(false)
  })

  it('keeps target management available when price data fails to load', async () => {
    const original = vi.mocked(apiFetch).getMockImplementation()!
    vi.mocked(apiFetch).mockImplementation(async (path, options) => {
      if (path === '/operations/sale-prices') throw new Error('price service unavailable')
      return original(path, options)
    })
    render(<Codex2APITargets />)
    expect(await screen.findByText('node-b')).toBeTruthy()
    expect(await screen.findByText('售价读取失败：price service unavailable')).toBeTruthy()
    expect(screen.getByRole('button', { name: /添加实例/ })).toBeTruthy()
  })

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
