// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { App, type FormInstance } from 'antd'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { apiFetch } from '@/lib/utils'
import Supply from './Supply'

vi.mock('@/lib/utils', () => ({ apiFetch: vi.fn() }))
vi.mock('@/components/settings/MailImportPanel', () => ({ default: ({ form }: { form: FormInstance }) => <button onClick={() => form.setFieldsValue({ mail_provider: 'mail_import', mail_import_source: 'applemail', applemail_pool_dir: 'mail', applemail_pool_file: 'purchased.json' })}>导入 AppleMail 资料</button> }))
vi.mock('@/components/ChatGPTExistingAccountLoginModal', () => ({ ChatGPTExistingAccountLoginModal: ({ open }: { open: boolean }) => open ? <div role="dialog">登录任务</div> : null }))
vi.mock('@/components/CodexAccountImportModal', () => ({ CodexAccountImportModal: ({ open, onCompleted }: { open: boolean; onCompleted: () => void }) => open ? <div role="dialog"><button onClick={onCompleted}>完成 JSON 导入</button></div> : null }))

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() })) })
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
afterEach(cleanup)
beforeEach(() => { vi.mocked(apiFetch).mockReset().mockImplementation(async (_, options) => options?.method === 'PUT' ? { ok: true } : { mail_provider: 'microsoft', codex2api_api_key: '********', smtp_password: '********' }) })
function mount(path = '/supply') {
  render(<App><MemoryRouter initialEntries={[path]}><Routes><Route path="/supply" element={<Supply />} /><Route path="/accounts/chatgpt" element={<div>账号池页面</div>} /></Routes></MemoryRouter></App>)
}
describe('Unified account supply', () => {
  it('persists the actual imported mailbox provider without overwriting unrelated secrets', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('button', { name: '导入 AppleMail 资料' }))
    await user.click(screen.getByRole('button', { name: /保存并继续登录/ }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/config', {
      method: 'PUT', body: JSON.stringify({ data: { mail_provider: 'applemail', applemail_pool_dir: 'mail', applemail_pool_file: 'purchased.json' } }),
    }))
    expect(await screen.findByRole('button', { name: /设置登录任务/ })).toBeTruthy()
  })
  it('stays with imported data when saving mailbox configuration fails', async () => {
    vi.mocked(apiFetch).mockImplementation(async (_, options) => { if (options?.method === 'PUT') throw new Error('配置保存失败'); return {} })
    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('button', { name: '导入 AppleMail 资料' }))
    await user.click(screen.getByRole('button', { name: /保存并继续登录/ }))
    expect(await screen.findByText('配置保存失败')).toBeTruthy()
    expect(screen.getByRole('button', { name: '导入 AppleMail 资料' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /设置登录任务/ })).toBeNull()
  })
  it('launches JSON import and returns to the account pool on completion', async () => {
    const user = userEvent.setup()
    mount('/supply?method=json')
    await user.click(screen.getByRole('button', { name: /选择文件或粘贴凭据/ }))
    await user.click(await screen.findByRole('button', { name: '完成 JSON 导入' }))
    expect(await screen.findByText('账号池页面')).toBeTruthy()
  })
})
