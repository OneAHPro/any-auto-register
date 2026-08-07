// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(async (path: string) => path === '/platforms' ? [] : {}),
  getToken: vi.fn(() => ''),
  clearToken: vi.fn(),
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

describe('App primary navigation', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) !== '/api/auth/status') {
        throw new Error(`unexpected fetch: ${String(input)}`)
      }
      return {
        json: async () => ({ has_password: false }),
      } as Response
    }))
  })

  afterEach(() => {
    cleanup()
    window.history.replaceState({}, '', '/')
    vi.unstubAllGlobals()
  })

  it('opens Codex2API from the primary navigation', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByText('Codex2API'))

    await waitFor(() => expect(window.location.pathname).toBe('/codex2api'))
  })

  it('opens mail import from the primary navigation', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByText('邮箱导入'))

    await waitFor(() => expect(window.location.pathname).toBe('/mail-import'))
  })
})
