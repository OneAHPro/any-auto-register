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
  beforeEach(() => { vi.mocked(apiFetch).mockImplementation(async (path: string) => path === '/codex-import/options' ? { pools: [{ id: 'PUBLIC_POOL', name: 'Public', targets: [{ id: 1, name: 'Primary', enabled: true }] }], default_pool_id: 'PUBLIC_POOL' } : path === '/codex-import' ? ({ job_id: 'job-1', status: 'queued' }) : ({ id: 'job-1', status: 'completed', total: 1, processed: 1, success: 1 })) })
  afterEach(() => cleanup())
  it('loads pools, submits selected txt files, and reports job progress', async () => {
    const user = userEvent.setup(); render(<CodexAccountImportModal open onClose={() => {}} />)
    expect(await screen.findByText('RT TXT')).toBeTruthy()
    const input = document.querySelector('input[type="file"]:not([webkitdirectory])') as HTMLInputElement
    const file = new File(['rt-token'], 'tokens.txt', { type: 'text/plain' })
    await user.upload(input, file)
    await user.click(screen.getByRole('button', { name: '提交导入' }))
    await waitFor(() => expect(vi.mocked(apiFetch)).toHaveBeenCalledWith('/codex-import', expect.objectContaining({ method: 'POST' })))
    expect(await screen.findByText('任务 job-1')).toBeTruthy()
  })
})
