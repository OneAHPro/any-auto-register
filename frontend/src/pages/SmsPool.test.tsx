// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
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
        return { total: 3, unused: 1, reserved: 1, used: 1 }
      }
      if (path.startsWith('/sms-pool?')) {
        return {
          total: 3,
          page: 1,
          page_size: 50,
          items: [
            {
              id: 1,
              code_hint: 'bei-****-0001',
              base_url: 'https://sms.example.com/box',
              status: 'unused',
              created_at: '2026-08-01T00:00:00Z',
            },
            {
              id: 2,
              code_hint: 'bei-****-0002',
              base_url: 'https://sms.example.com/box',
              status: 'used',
              used_by_email: 'used@example.com',
              used_at: '2026-08-01T00:10:00Z',
              created_at: '2026-08-01T00:00:00Z',
            },
            {
              id: 3,
              code_hint: 'bei-****-0003',
              base_url: 'https://sms.example.com/box',
              status: 'active',
              reserved_task_id: 'task-active',
              reserved_at: '2026-08-01T00:05:00Z',
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

  afterEach(() => cleanup())

  it('shows masked cards, receive URLs, counts and usage states', async () => {
    render(<SmsPool />)

    expect(await screen.findByText('SMS接码池')).toBeTruthy()
    expect(await screen.findByText('bei-****-0001')).toBeTruthy()
    expect(screen.getByText('bei-****-0002')).toBeTruthy()
    expect(screen.getAllByText('https://sms.example.com/box').length).toBeGreaterThan(0)
    expect(screen.getByText('未使用')).toBeTruthy()
    expect(screen.getByText('已使用')).toBeTruthy()
    expect(screen.getByText('使用中')).toBeTruthy()
    expect(screen.getByText('可用 1')).toBeTruthy()
  })

  it('imports cards with a default receive URL then reloads the pool', async () => {
    const user = userEvent.setup()
    render(<SmsPool />)
    await screen.findByText('bei-****-0001')

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
    await screen.findByText('bei-****-0001')
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
})
