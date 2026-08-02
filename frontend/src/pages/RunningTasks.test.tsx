// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { apiFetch } from '@/lib/utils'
import RunningTasks from './RunningTasks'

vi.mock('@/lib/utils', () => ({
  apiFetch: vi.fn(),
}))

vi.mock('@/components/TaskLogPanel', () => ({
  TaskLogPanel: () => null,
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

describe('RunningTasks automatic authentication history', () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockResolvedValue([
      {
        id: 'task-auto-history',
        platform: 'chatgpt',
        source: 'schedule',
        status: 'done',
        total: 64,
        progress: '64/64',
        success: 53,
        registered: 64,
        skipped: 0,
        errors: [],
        created_at: 1_786_000_000,
        updated_at: 1_786_000_060,
        control: { stop_requested: false },
        meta: {
          automation: true,
          invalid_rt_count: 6,
          relogin_failed_count: 5,
          alert_sent: true,
          alert_reason: 'sent',
        },
      },
    ])
  })

  afterEach(() => cleanup())

  it('labels scheduled cycles and displays their alert counters', async () => {
    render(<RunningTasks />)

    expect(await screen.findByText('自动认证')).toBeTruthy()
    expect(screen.getByText('RT失效 6')).toBeTruthy()
    expect(screen.getByText('重登失败 5')).toBeTruthy()
    expect(screen.getByText('邮件已提醒')).toBeTruthy()
  })
})
