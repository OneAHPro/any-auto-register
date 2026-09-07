// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { apiFetch } from '@/lib/utils'
import { CodexAccountImportModal } from './CodexAccountImportModal'
vi.mock('@/lib/utils', () => ({ apiFetch: vi.fn() }))
vi.mock('antd', async () => {
  const actual = await vi.importActual<any>('antd'); return actual
})
describe('CodexAccountImportModal', () => {
  beforeEach(() => { vi.mocked(apiFetch).mockReset().mockImplementation(async (path: string) => path === '/codex-import/options' ? { pools: [{ id: 'PUBLIC_POOL', name: 'Public', targets: [{ id: 1, name: 'Primary', enabled: true }] }], default_pool_id: 'PUBLIC_POOL' } : path === '/codex-import' ? ({ job_id: 'job-1', status: 'queued' }) : ({ id: 'job-1', status: 'completed', total: 1, processed: 1, success: 1 })) })
  afterEach(() => cleanup())
  it.each([['', undefined], ['0', '0.00'], ['12.5', '12.50']])('submits optional batch total %s with imported credentials', async (input, expected) => {
    const user = userEvent.setup()
    render(<CodexAccountImportModal open onClose={() => {}} />)
    await user.click(await screen.findByText('粘贴 Session JSON'))
    await user.type(screen.getByRole('textbox', { name: 'Session JSON' }), 'token-fixture')
    const cost = screen.getByRole('textbox', { name: '本批购号总成本（元）' })
    if (input) await user.type(cost, input)
    await user.click(await screen.findByRole('button', { name: '提交导入' }))
    await screen.findByText('任务 job-1')
    const call = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/codex-import')
    const payload = JSON.parse(String(call?.[1]?.body))
    expect(payload.files).toEqual([{ name: 'session.json', content: 'token-fixture' }])
    if (expected === undefined) {
      expect(payload).not.toHaveProperty('purchase_cost_cny')
      expect(payload).not.toHaveProperty('purchase_batch_key')
    } else {
      expect(payload.purchase_cost_cny).toBe(expected)
      expect(payload.purchase_batch_key).toMatch(/^[0-9a-f-]{36}$/i)
    }
  })

  it('retains batch identity after an uncertain submission and rotates it for the next accepted import', async () => {
    const original = vi.mocked(apiFetch).getMockImplementation()!
    let attempts = 0
    vi.mocked(apiFetch).mockImplementation(async (path, options) => {
      if (path === '/codex-import' && attempts++ === 0) throw new Error('network interruption')
      return original(path, options)
    })
    const user = userEvent.setup()
    render(<CodexAccountImportModal open onClose={() => {}} />)
    await user.click(await screen.findByText('粘贴 Session JSON'))
    await user.type(screen.getByRole('textbox', { name: 'Session JSON' }), 'first-token')
    await user.type(screen.getByRole('textbox', { name: '本批购号总成本（元）' }), '10')
    await user.click(await screen.findByRole('button', { name: '提交导入' }))
    await screen.findByText('network interruption')
    await user.click(await screen.findByRole('button', { name: '提交导入' }))
    await screen.findByText('任务 job-1')
    expect((screen.getByRole('textbox', { name: '本批购号总成本（元）' }) as HTMLInputElement).value).toBe('')
    await user.type(screen.getByRole('textbox', { name: 'Session JSON' }), 'next-token')
    await user.type(screen.getByRole('textbox', { name: '本批购号总成本（元）' }), '20')
    await user.click(await screen.findByRole('button', { name: '提交导入' }))
    await waitFor(() => expect(attempts).toBe(3))
    const payloads = vi.mocked(apiFetch).mock.calls.filter(([path]) => path === '/codex-import').map(([, options]) => JSON.parse(String(options?.body)))
    expect(payloads[1].purchase_batch_key).toBe(payloads[0].purchase_batch_key)
    expect(payloads[2].purchase_batch_key).not.toBe(payloads[1].purchase_batch_key)
  })

  it.each(['-1', '1.001'])('rejects an invalid purchase total %s without posting an import', async (input) => {
    const user = userEvent.setup()
    render(<CodexAccountImportModal open onClose={() => {}} />)
    await user.click(await screen.findByText('粘贴 Session JSON'))
    await user.type(screen.getByRole('textbox', { name: 'Session JSON' }), 'fixture-token')
    await user.type(screen.getByRole('textbox', { name: '本批购号总成本（元）' }), input)
    await user.click(await screen.findByRole('button', { name: '提交导入' }))
    expect(await screen.findByText('请输入非负金额，最多保留两位小数')).toBeTruthy()
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path === '/codex-import')).toBe(false)
  })

  it('loads pools, submits selected txt files, and reports job progress', async () => {
    const user = userEvent.setup(); render(<CodexAccountImportModal open onClose={() => {}} />)
    expect(await screen.findByText('RT TXT')).toBeTruthy()
    const input = document.querySelector('input[type="file"]:not([webkitdirectory])') as HTMLInputElement
    const file = new File(['rt-token'], 'tokens.txt', { type: 'text/plain' })
    await user.upload(input, file)
    await user.click(await screen.findByRole('button', { name: '提交导入' }))
    await waitFor(() => expect(vi.mocked(apiFetch)).toHaveBeenCalledWith('/codex-import', expect.objectContaining({ method: 'POST' })))
    expect(await screen.findByText('任务 job-1')).toBeTruthy()
  })
})
