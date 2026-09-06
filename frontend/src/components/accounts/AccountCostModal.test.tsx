// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { apiFetch } from '@/lib/utils'
import { AccountCostModal } from './AccountCostModal'

vi.mock('@/lib/utils', () => ({ apiFetch: vi.fn() }))

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })) })
  const getComputedStyle = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => getComputedStyle(element))
})
afterEach(cleanup)
beforeEach(() => { vi.mocked(apiFetch).mockReset() })
const account = { id: 17, email: 'cost@example.com', extra: { purchase_cost_cny: '12.50' } }

function setup(cost: string | null = '12.50') {
  const onSaved = vi.fn()
  const onCancel = vi.fn()
  render(<AccountCostModal account={{ ...account, extra: { purchase_cost_cny: cost } }} onSaved={onSaved} onCancel={onCancel} />)
  return { user: userEvent.setup(), onSaved, onCancel }
}

describe('AccountCostModal', () => {
  it('saves a zero purchase cost as a real value', async () => {
    vi.mocked(apiFetch).mockResolvedValue({})
    const { user, onSaved } = setup()
    const input = screen.getByRole('spinbutton', { name: '购入成本（元）' })
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('12.50'))
    await user.clear(input)
    await user.type(input, '0')
    await user.click(screen.getByRole('button', { name: /^保\s*存$/ }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/accounts/17', { method: 'PATCH', body: JSON.stringify({ purchase_cost_cny: '0' }) }))
    expect(onSaved).toHaveBeenCalledWith(17, '0')
  })

  it('clears a stored purchase cost with an explicit null', async () => {
    vi.mocked(apiFetch).mockResolvedValue({})
    const { user, onSaved } = setup()
    await user.click(screen.getByRole('button', { name: '清空成本' }))
    await user.click(screen.getByRole('button', { name: /^保\s*存$/ }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/accounts/17', { method: 'PATCH', body: JSON.stringify({ purchase_cost_cny: null }) }))
    expect(onSaved).toHaveBeenCalledWith(17, null)
  })

  it('retains the editor and entered price after a failed save and lets the user retry', async () => {
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error('保存服务暂不可用')).mockResolvedValueOnce({})
    const { user, onSaved } = setup(null)
    const input = screen.getByRole('spinbutton', { name: '购入成本（元）' })
    await user.type(input, '23.45')
    await user.click(screen.getByRole('button', { name: /^保\s*存$/ }))
    expect(await screen.findByText('保存服务暂不可用')).toBeTruthy()
    expect((input as HTMLInputElement).value).toBe('23.45')
    expect(onSaved).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /^保\s*存$/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(17, '23.45'))
  })

  it.each(['-1', '1000000000', '0.001'])('rejects an out-of-range cost %s before saving', async (cost) => {
    const { user } = setup(null)
    await user.type(screen.getByRole('spinbutton', { name: '购入成本（元）' }), cost)
    await user.click(screen.getByRole('button', { name: /^保\s*存$/ }))
    expect(await screen.findByText('请输入 0 至 999999999.99 元，最多两位小数')).toBeTruthy()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('submits only once when the form is submitted twice while validation is pending', async () => {
    let resolveSave!: (value: unknown) => void
    vi.mocked(apiFetch).mockImplementation(() => new Promise((resolve) => { resolveSave = resolve }))
    const { onSaved } = setup()
    const form = screen.getByRole('spinbutton', { name: '购入成本（元）' }).closest('form')!
    fireEvent.submit(form)
    fireEvent.submit(form)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1))
    resolveSave({})
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  })

  it('cancels without saving', async () => {
    const { user, onCancel } = setup()
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^取\s*消$/ }))
    expect(onCancel).toHaveBeenCalled()
    expect(apiFetch).not.toHaveBeenCalled()
  })
})
