// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
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
