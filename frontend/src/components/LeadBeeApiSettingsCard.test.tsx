// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch } from '@/lib/utils'
import LeadBeeApiSettingsCard from './LeadBeeApiSettingsCard'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

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

describe('LeadBeeApiSettingsCard', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) {
        return {
          leadbee_api_enabled: 'yes',
          leadbee_api_key: 'RETURNED_KEY_MUST_NOT_RENDER',
          leadbee_api_secret: 'RETURNED_SECRET_MUST_NOT_RENDER',
          leadbee_api_product_id: 'prod-capacity',
        }
      }
      if (path === '/config/leadbee/test' && options?.method === 'POST') {
        return {
          ok: true,
          configured_product_available: true,
          balance_available: '35.70',
          balance_reserved: '0.00',
          unit_price: '1.30',
          estimated_order_capacity: 27,
          currency: 'CNY',
          credential: 'HIDDEN_CREDENTIAL',
          signature: 'HIDDEN_SIGNATURE',
          phone: 'HIDDEN_PHONE',
          sms_code: 'HIDDEN_SMS',
        }
      }
      if (path === '/config' && options?.method === 'PUT') return { ok: true }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  afterEach(() => cleanup())

  it('loads write-only credentials and shows sanitized API capacity automatically', async () => {
    render(<LeadBeeApiSettingsCard />)

    expect(await screen.findByText('LeadBee API 接码')).toBeTruthy()
    const enabled = await screen.findByRole('switch', { name: '启用 LeadBee Open API' })
    await waitFor(() => expect(enabled.getAttribute('aria-checked')).toBe('true'))
    expect((screen.getByLabelText('LeadBee API Key') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('LeadBee API Secret') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('LeadBee 产品 ID') as HTMLInputElement).value).toBe('prod-capacity')

    expect(await screen.findByText('API 可用余额 ¥35.70')).toBeTruthy()
    expect(screen.getByText('已占用 ¥0.00')).toBeTruthy()
    expect(screen.getByText('单价 ¥1.30/次')).toBeTruthy()
    expect(screen.getByText('预计可接 27 次')).toBeTruthy()
    expect(document.body.textContent).not.toContain('RETURNED_KEY_MUST_NOT_RENDER')
    expect(document.body.textContent).not.toContain('RETURNED_SECRET_MUST_NOT_RENDER')
    expect(document.body.textContent).not.toContain('HIDDEN_CREDENTIAL')
    expect(document.body.textContent).not.toContain('HIDDEN_SIGNATURE')
    expect(document.body.textContent).not.toContain('HIDDEN_PHONE')
    expect(document.body.textContent).not.toContain('HIDDEN_SMS')
  })

  it('saves only the four LeadBee fields and blanks submitted credentials', async () => {
    const user = userEvent.setup()
    render(<LeadBeeApiSettingsCard />)
    const apiKey = await screen.findByLabelText('LeadBee API Key') as HTMLInputElement
    const apiSecret = screen.getByLabelText('LeadBee API Secret') as HTMLInputElement
    await user.type(apiKey, 'NEW_KEY')
    await user.type(apiSecret, 'NEW_SECRET')
    await user.click(screen.getByRole('button', { name: '保存 API 配置' }))

    await waitFor(() => {
      expect(vi.mocked(apiFetch).mock.calls.some(
        ([path, options]) => path === '/config' && options?.method === 'PUT',
      )).toBe(true)
    })
    const saveCall = vi.mocked(apiFetch).mock.calls.find(
      ([path, options]) => path === '/config' && options?.method === 'PUT',
    )
    expect(JSON.parse(String(saveCall?.[1]?.body || '{}'))).toEqual({
      data: {
        leadbee_api_enabled: true,
        leadbee_api_key: 'NEW_KEY',
        leadbee_api_secret: 'NEW_SECRET',
        leadbee_api_product_id: 'prod-capacity',
      },
    })
    await waitFor(() => {
      expect(apiKey.value).toBe('')
      expect(apiSecret.value).toBe('')
    })
  })

  it('tests current unsaved values without persisting them', async () => {
    const user = userEvent.setup()
    render(<LeadBeeApiSettingsCard />)
    await screen.findByText('API 可用余额 ¥35.70')
    const product = screen.getByLabelText('LeadBee 产品 ID')
    await user.clear(product)
    await user.type(product, 'prod-unsaved')
    await user.click(screen.getByRole('button', { name: '刷新余额' }))

    await waitFor(() => {
      const testCalls = vi.mocked(apiFetch).mock.calls.filter(
        ([path, options]) => path === '/config/leadbee/test' && options?.method === 'POST',
      )
      expect(testCalls.length).toBeGreaterThan(1)
    })
    const testCalls = vi.mocked(apiFetch).mock.calls.filter(
      ([path, options]) => path === '/config/leadbee/test' && options?.method === 'POST',
    )
    expect(JSON.parse(String(testCalls.at(-1)?.[1]?.body || '{}'))).toEqual({
      data: {
        leadbee_api_enabled: true,
        leadbee_api_key: '',
        leadbee_api_secret: '',
        leadbee_api_product_id: 'prod-unsaved',
      },
    })
  })

  it('invalidates a pending balance response when any tested field changes', async () => {
    const pendingTest = deferred<Record<string, unknown>>()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/config' && !options) {
        return {
          leadbee_api_enabled: true,
          leadbee_api_product_id: 'prod-before',
        }
      }
      if (path === '/config/leadbee/test' && options?.method === 'POST') return pendingTest.promise
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    render(<LeadBeeApiSettingsCard />)

    const refresh = await screen.findByRole('button', { name: '刷新余额' })
    await waitFor(() => expect(refresh.classList.contains('ant-btn-loading')).toBe(true))
    await user.type(screen.getByLabelText('LeadBee 产品 ID'), '-changed')

    await act(async () => {
      pendingTest.resolve({
        ok: true,
        configured_product_available: true,
        balance_available: '999.99',
        balance_reserved: '0.00',
        unit_price: '1.00',
        estimated_order_capacity: 999,
        currency: 'CNY',
      })
      await pendingTest.promise
    })

    await waitFor(() => expect(refresh.classList.contains('ant-btn-loading')).toBe(false))
    expect(screen.queryByText('API 可用余额 ¥999.99')).toBeNull()
  })
})
