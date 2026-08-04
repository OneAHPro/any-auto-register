// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { apiFetch } from '@/lib/utils'
import SmsPool from './SmsPool'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
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
})

describe('SmsPool', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/sms-pool/stats') {
        return { total: 4, unused: 1, reserved: 1, active: 1, used: 1 }
      }
      if (path.startsWith('/sms-pool?')) {
        return {
          total: 4,
          page: 1,
          page_size: 50,
          items: [
            {
              id: 1,
              code: 'bei-sms-FULL-SECRET-0001',
              code_hint: 'bei-****-0001',
              base_url: 'https://sms.example.com/box',
              status: 'unused',
              created_at: '2026-08-01T00:00:00Z',
            },
            {
              id: 2,
              code: 'bei-sms-FULL-SECRET-0002',
              code_hint: 'bei-****-0002',
              base_url: 'https://sms.example.com/box',
              status: 'used',
              used_by_email: 'used@example.com',
              used_at: '2026-08-01T00:10:00Z',
              created_at: '2026-08-01T00:00:00Z',
            },
            {
              id: 3,
              code: 'bei-sms-FULL-SECRET-0003',
              code_hint: 'bei-****-0003',
              base_url: 'https://sms.example.com/box',
              status: 'active',
              reserved_task_id: 'task-active',
              updated_at: '2026-08-01T00:05:00Z',
              created_at: '2026-08-01T00:00:00Z',
            },
            {
              id: 4,
              code: 'bei-sms-FULL-SECRET-0004',
              code_hint: 'bei-****-0004',
              base_url: 'https://sms.example.com/box',
              status: 'reserved',
              reserved_task_id: 'task-reserved',
              reserved_at: '2026-08-01T00:04:00Z',
              created_at: '2026-08-01T00:00:00Z',
            },
          ],
        }
      }
      if (path === '/sms-pool/import' && options?.method === 'POST') {
        return { imported: 2, duplicates: 0, invalid: [] }
      }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  afterEach(() => {
    cleanup()
    Reflect.deleteProperty(document, 'execCommand')
  })

  it('shows full cards, receive URLs, counts and usage states', async () => {
    render(<SmsPool />)

    expect(await screen.findByText('SMS接码池')).toBeTruthy()
    expect(await screen.findByText('bei-sms-FULL-SECRET-0001')).toBeTruthy()
    expect(screen.getByText('bei-sms-FULL-SECRET-0002')).toBeTruthy()
    expect(screen.queryByText('bei-****-0001')).toBeNull()
    expect(screen.queryByText('bei-****-0002')).toBeNull()
    expect(screen.getAllByText('https://sms.example.com/box').length).toBeGreaterThan(0)
    expect(screen.getByText('未使用')).toBeTruthy()
    expect(screen.getByText('已使用')).toBeTruthy()
    expect(screen.getAllByText('使用中').length).toBeGreaterThan(0)
    expect(screen.getByText('待回收')).toBeTruthy()
    expect(screen.getByText('可用 1')).toBeTruthy()
    expect(screen.getByText('待回收 1')).toBeTruthy()
    expect(
      screen.getByText(new Date('2026-08-01T00:05:00Z').toLocaleString('zh-CN')),
    ).toBeTruthy()
  })

  it('copies the full card code', async () => {
    const user = userEvent.setup()
    const fullCode = 'bei-sms-FULL-SECRET-0001'
    let copiedText = ''
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn(() => {
        copiedText = document.getSelection()?.toString() || ''
        return true
      }),
    })
    render(<SmsPool />)

    const code = await screen.findByText(fullCode)
    const row = code.closest('tr')
    expect(row).not.toBeNull()
    await user.click(within(row as HTMLTableRowElement).getByRole('button', { name: '复制' }))

    expect(copiedText).toBe(fullCode)
  })

  it('imports cards with a default receive URL then reloads the pool', async () => {
    const user = userEvent.setup()
    render(<SmsPool />)
    await screen.findByText('bei-sms-FULL-SECRET-0001')

    await user.clear(screen.getByLabelText('默认接码地址'))
    await user.type(screen.getByLabelText('默认接码地址'), 'https://sms.example.com/new-box')
    await user.type(screen.getByLabelText('接码卡密'), 'card-one\ncard-two')
    await user.click(screen.getByRole('button', { name: /导入卡密/ }))

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith('/sms-pool/import', expect.objectContaining({
        method: 'POST',
      }))
    })
    const importCall = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/sms-pool/import')
    expect(JSON.parse(String(importCall?.[1]?.body || '{}'))).toEqual({
      content: 'card-one\ncard-two',
      default_base_url: 'https://sms.example.com/new-box',
    })
    expect(vi.mocked(apiFetch).mock.calls.filter(([path]) => path === '/sms-pool/stats').length).toBeGreaterThan(1)
  })

  it('silently refreshes usage state while the page remains open', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval')
    render(<SmsPool />)
    await screen.findByText('bei-sms-FULL-SECRET-0001')
    const refreshCallback = intervalSpy.mock.calls.find(([, delay]) => delay === 5_000)?.[0]

    expect(refreshCallback).toBeTypeOf('function')
    await (refreshCallback as () => void)()

    await waitFor(() => {
      expect(
        vi.mocked(apiFetch).mock.calls.filter(([path]) => path === '/sms-pool/stats').length,
      ).toBeGreaterThan(1)
    })
    intervalSpy.mockRestore()
  })

  it('requests active cards independently through the pending-recovery filter', async () => {
    const user = userEvent.setup()
    render(<SmsPool />)
    await screen.findByText('bei-sms-FULL-SECRET-0001')

    await user.click(screen.getByRole('combobox', { name: '状态筛选' }))
    await user.click(await screen.findByText('待回收', { selector: '.ant-select-item-option-content' }))

    await waitFor(() => {
      expect(
        vi.mocked(apiFetch).mock.calls.some(
          ([path]) => String(path).includes('status=active'),
        ),
      ).toBe(true)
    })
  })
})
