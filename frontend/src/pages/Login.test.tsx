// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { setToken } from '@/lib/utils'
import Login from './Login'
vi.mock('@/lib/utils', () => ({ setToken: vi.fn() }))
beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() })) })
})
beforeEach(() => { vi.mocked(setToken).mockReset(); vi.stubGlobal('fetch', vi.fn()) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Login console authentication', () => {
  it('keeps password and second-factor submission in sequence and accepts only six digits', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ requires_2fa: true, temp_token: 'challenge-token' }) } as Response)
    render(<Login />)
    expect(screen.getByRole('heading', { name: '登录中控台' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } })
    fireEvent.submit(screen.getByLabelText('密码').closest('form')!)
    expect(await screen.findByRole('heading', { name: '双因素验证' })).toBeTruthy()
    expect(fetch).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({ body: JSON.stringify({ password: 'secret' }) }))
    fireEvent.change(screen.getByLabelText('验证码'), { target: { value: 'abcdef' } })
    fireEvent.submit(screen.getByLabelText('验证码').closest('form')!)
    expect(await screen.findByText('验证码为 6 位数字')).toBeTruthy()
    expect(fetch).toHaveBeenCalledTimes(1)
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ access_token: 'session-token' }) } as Response)
    fireEvent.change(screen.getByLabelText('验证码'), { target: { value: '123456' } })
    fireEvent.submit(screen.getByLabelText('验证码').closest('form')!)
    await waitFor(() => expect(setToken).toHaveBeenCalledWith('session-token'))
    expect(fetch).toHaveBeenCalledWith('/api/auth/verify-totp', expect.objectContaining({ body: JSON.stringify({ temp_token: 'challenge-token', code: '123456' }) }))
  })
})

it('completes a password-only login and preserves server error messages', async () => {
  vi.mocked(fetch).mockResolvedValueOnce({ ok: false, json: async () => ({ detail: '密码不正确' }) } as Response)
  render(<Login />)
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'wrong' } })
  fireEvent.submit(screen.getByLabelText('密码').closest('form')!)
  expect(await screen.findByText('密码不正确')).toBeTruthy()
  expect(setToken).not.toHaveBeenCalled()
  vi.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ access_token: 'password-session' }) } as Response)
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'correct' } })
  fireEvent.submit(screen.getByLabelText('密码').closest('form')!)
  await waitFor(() => expect(setToken).toHaveBeenCalledWith('password-session'))
})
