// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
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
    Object.defineProperty(window, 'innerWidth', { value: 1280, writable: true, configurable: true })
    localStorage.clear()
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

  it('opens the purchased-account workspace without exposing legacy platforms', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('menuitem', { name: '账号池' }))

    await waitFor(() => expect(window.location.pathname).toBe('/accounts/chatgpt'))
    expect(screen.queryByRole('menuitem', { name: '平台管理' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: '注册账号' })).toBeNull()
  })

  it('opens target management and pool scheduling from primary navigation', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('menuitem', { name: '实例管理' }))
    await waitFor(() => expect(window.location.pathname).toBe('/codex2api/targets'))

    await user.click(screen.getByRole('menuitem', { name: '号池调度' }))
    await waitFor(() => expect(window.location.pathname).toBe('/codex2api/scheduler'))
  })

  it('opens the unified supply workspace from navigation', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('menuitem', { name: '补充账号' }))

    await waitFor(() => expect(window.location.pathname).toBe('/supply'))
  })

  it('keeps the primary navigation pinned while long pages scroll', async () => {
    render(<App />)

    const sider = await screen.findByRole('complementary')

    expect(sider.style.position).toBe('sticky')
    expect(sider.style.top).toBe('0px')
    expect(sider.style.height).toBe('100vh')
    expect(within(sider).getAllByRole('menu')[0].style.overflowY).toBe('auto')
  })

  it('allows the content pane to shrink beside the mobile sidebar', async () => {
    render(<App />)

    const content = await screen.findByRole('main')

    expect(content.style.minWidth).toBe('0px')
    expect(content.classList.contains('app-content')).toBe(true)
  })

  it('redirects old new-account registration links to the existing-account supply flow', async () => {
    window.history.replaceState({}, '', '/register')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe('/supply'))
    expect(window.location.search).toContain('method=login')
  })

  it('redirects a legacy platform account route to ChatGPT', async () => {
    window.history.replaceState({}, '', '/accounts/grok')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe('/accounts/chatgpt'))
  })

  it('opens and closes the mobile navigation after choosing an entry', async () => {
    Object.defineProperty(window, 'innerWidth', { value: 390, writable: true })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: '打开导航' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('menuitem', { name: '实例管理' }))
    await waitFor(() => expect(window.location.pathname).toBe('/codex2api/targets'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})
