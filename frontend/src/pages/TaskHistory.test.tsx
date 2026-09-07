// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiFetch } from '@/lib/utils'
import TaskHistory from './TaskHistory'

vi.mock('@/lib/utils', async () => {
  const actual = await vi.importActual<typeof import('@/lib/utils')>('@/lib/utils')
  return { ...actual, apiFetch: vi.fn() }
})

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

describe('TaskHistory status labels', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset().mockResolvedValue({
      total: 5,
      items: [
        { id: 1, created_at: '', platform: 'chatgpt', email: 'ok@example.com', status: 'success', error: '' },
        { id: 2, created_at: '', platform: 'chatgpt', email: 'failed@example.com', status: 'failed', error: 'failed' },
        { id: 3, created_at: '', platform: 'chatgpt', email: 'skipped@example.com', status: 'skipped', error: '' },
        { id: 4, created_at: '', platform: 'chatgpt', email: 'removed@example.com', status: 'removed', error: '' },
        { id: 5, created_at: '', platform: 'chatgpt', email: 'legacy@example.com', status: 'legacy_state', error: '' },
      ],
    })
  })

  afterEach(cleanup)

  it('renders removed distinctly and keeps unknown legacy statuses neutral', async () => {
    render(<TaskHistory />)

    expect(await screen.findByText('成功')).toBeTruthy()
    expect(screen.getAllByText('失败')).toHaveLength(1)
    expect(screen.getByText('已跳过')).toBeTruthy()
    const removed = screen.getByText('已删除')
    expect(removed.closest('.ant-tag')?.className).toContain('ant-tag-warning')
    const legacy = screen.getByText('legacy_state')
    expect(legacy.closest('.ant-tag')?.className).not.toContain('ant-tag-error')
  })
})

describe('TaskHistory remote pagination', () => {
  afterEach(cleanup)
  it('loads the selected page from the server and keeps the complete record count', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path) => ({
      total: 82,
      items: [{ id: path.includes('page=2') ? 51 : 1, created_at: '', platform: 'chatgpt', email: path.includes('page=2') ? 'page-two@example.com' : 'first@example.com', status: 'success', error: '' }],
    }))
    render(<TaskHistory />)
    expect(await screen.findByText('first@example.com')).toBeTruthy()
    fireEvent.click(screen.getByTitle('2'))
    expect(await screen.findByText('page-two@example.com')).toBeTruthy()
    await waitFor(() => expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path.includes('page=2') && path.includes('platform=chatgpt'))).toBe(true))
    expect(screen.getByText('共 82 条记录')).toBeTruthy()
  })
  it('shows a recoverable load failure instead of a successful empty state', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('连接超时'))
    render(<TaskHistory />)
    expect(await screen.findByText('记录加载失败，请刷新重试')).toBeTruthy()
  })
})

describe('TaskHistory details and stale page protection', () => {
  afterEach(cleanup)
  it('opens the complete error text in the record drawer', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ total: 1, items: [{ id: 1, created_at: '', platform: 'chatgpt', email: 'detail@example.com', status: 'failed', error: '完整原因：访问令牌过期，需要重新登录。' }] })
    render(<TaskHistory />)
    fireEvent.click(await screen.findByRole('button', { name: '查看 detail@example.com 记录' }))
    expect(await screen.findByText('账号记录详情')).toBeTruthy()
    expect(screen.getAllByText('完整原因：访问令牌过期，需要重新登录。')).toHaveLength(2)
  })
  it('does not present an old page as the selected page after a failed request', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path) => {
      if (path.includes('page=2')) throw new Error('连接中断')
      return { total: 82, items: [{ id: 1, created_at: '', platform: 'chatgpt', email: 'page-one@example.com', status: 'success', error: '' }] }
    })
    render(<TaskHistory />)
    expect(await screen.findByText('page-one@example.com')).toBeTruthy()
    fireEvent.click(screen.getByTitle('2'))
    expect(await screen.findByText('记录加载失败，请刷新重试')).toBeTruthy()
    expect(screen.queryByText('page-one@example.com')).toBeNull()
  })
})

it('deletes only selected history records after confirmation', async () => {
  let deleted = false
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (path === '/tasks/logs/batch-delete' && options?.method === 'POST') { deleted = true; return { deleted: 1, not_found: [], total_requested: 1 } }
    return { total: deleted ? 0 : 1, items: deleted ? [] : [{ id: 27, created_at: '', platform: 'chatgpt', email: 'selected@example.com', status: 'failed', error: '' }] }
  })
  render(<TaskHistory />)
  expect(await screen.findByText('selected@example.com')).toBeTruthy()
  fireEvent.click(screen.getAllByRole('checkbox')[1])
  fireEvent.click(await screen.findByRole('button', { name: /删除 1 条/ }))
  expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path === '/tasks/logs/batch-delete')).toBe(false)
  fireEvent.click(await screen.findByRole('button', { name: '删 除' }))
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/tasks/logs/batch-delete', { method: 'POST', body: JSON.stringify({ ids: [27] }) }))
  await waitFor(() => expect(screen.queryByText('selected@example.com')).toBeNull())
  cleanup()
})
