// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/utils'
import Dashboard from './Dashboard'

vi.mock('@/lib/utils', () => ({ apiFetch: vi.fn() }))
beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() })) })
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
afterEach(() => { cleanup(); vi.useRealTimers() })

// Local API fixture. Financial values are never used as production fallbacks.
function overviewFixture() {
  return {
    as_of: '2026-09-07T03:04:05Z', timezone: 'Asia/Shanghai', date: '2026-09-07',
    coverage: { targets_total: 3, targets_available: 3, billing_complete: true, today_complete: true, today_costs_complete: true, prices_complete: true, costs_complete: true, unknown_cost_accounts: 0, undated_cost_cny: '0.00', errors: [] as string[] },
    finance: { today_cost_cny: '70.50', total_cost_cny: '321.00', today_billed_usd: '87.650000', total_billed_usd: '1234.560000', today_revenue_cny: '61.36', total_revenue_cny: '864.19', today_profit_cny: '-9.14', total_profit_cny: '543.19', break_even_percent: 269.2 as number | null, remaining_cost_cny: '0.00' },
    supply: { today_purchased_accounts: 10, total_purchased_accounts: 80, today_added_accounts: 12 },
    account_status: { total: 140, normal: 103, scheduling: 8, rate_limited: 20, abnormal: 9, auth_invalid: 7, errors: 2 },
    targets: [{ id: 1, name: '主实例', enabled: true, health_status: 'healthy', total_accounts: 92, normal_accounts: 75, limited_accounts: 13, abnormal_accounts: 4, remaining_percent_avg: 48.5, estimated_remaining_usd: '123.450000', today_billed_usd: '23.650000', total_billed_usd: '345.560000', today_requests: 387, price_cny_per_usd: '0.70', today_revenue_cny: '16.55', total_revenue_cny: '241.89', captured_at: '2026-09-07T03:00:00Z', billing_status: 'available', error: null }],
    recent_batches: [{ id: 1, created_at: '2026-09-07T01:00:00Z', source: '凭据导入', expected_count: 10, cost_cny: '70.50', linked_accounts: 9 }],
    attention: [{ target_id: 2, name: '备用实例', reason: '剩余额度低于 10%', severity: 'warning' }],
  }
}

function configureApi(fixture: unknown = overviewFixture(), failedOverview = false) {
  vi.mocked(apiFetch).mockImplementation(async path => {
    if (path.startsWith('/operations/overview?')) { if (failedOverview) throw new Error('连接中断'); return fixture }
    if (path === '/tasks/summary') return [{ id: 'run', platform: 'chatgpt', status: 'running', error_count: 0 }, { id: 'failed', platform: 'chatgpt', status: 'failed', error_count: 4 }]
    throw new Error(`unexpected path ${path}`)
  })
}

describe('Dashboard business overview', () => {
  beforeEach(() => { vi.mocked(apiFetch).mockReset(); configureApi() })

  it('shows server-wide finance, account status, instance comparisons and purchase batches', async () => {
    render(<Dashboard />)
    expect(await screen.findByRole('heading', { name: '实例经营对比' })).toBeTruthy()
    const today = screen.getByRole('region', { name: '今日经营' })
    const cumulative = screen.getByRole('region', { name: '累计经营' })
    expect(within(today).getByText('¥70.50')).toBeTruthy()
    expect(within(today).getByText('$87.65')).toBeTruthy()
    expect(within(today).getByText('-¥9.14')).toBeTruthy()
    expect(within(today).getByText('今日收支差')).toBeTruthy()
    expect(within(cumulative).getByText('$1,234.56')).toBeTruthy()
    expect(within(cumulative).getByText('¥543.19')).toBeTruthy()
    expect(screen.getByText('140')).toBeTruthy()
    expect(screen.getByText('269.2%')).toBeTruthy()
    expect(screen.getByText('主实例')).toBeTruthy()
    expect(screen.getByText('剩余额度低于 10%')).toBeTruthy()
    expect(screen.getByText('凭据导入')).toBeTruthy()
    expect(screen.getByRole('link', { name: /补充账号/ }).getAttribute('href')).toBe('/supply')
    expect(screen.getByRole('link', { name: /查看账号池/ }).getAttribute('href')).toBe('/accounts/chatgpt')
    expect(screen.queryByText('平台分布')).toBeNull()
    expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path.startsWith('/accounts?') || path === '/codex2api/targets')).toBe(false)
  })

  it('does not turn missing income, costs or recovery data into zero', async () => {
    const fixture = overviewFixture()
    configureApi({ ...fixture, coverage: { ...fixture.coverage, billing_complete: false, today_complete: false, today_costs_complete: false, prices_complete: false, costs_complete: false, unknown_cost_accounts: 4, undated_cost_cny: '20.00' }, finance: { ...fixture.finance, today_revenue_cny: null, total_revenue_cny: null, today_profit_cny: null, total_profit_cny: null, break_even_percent: null, remaining_cost_cny: null } })
    render(<Dashboard />)
    const today = await screen.findByRole('region', { name: '今日经营' })
    expect(within(today).queryByText('¥0.00')).toBeNull()
    expect(screen.getAllByText('待录齐成本').length).toBeGreaterThan(0)
    expect(screen.getAllByText('部分统计').length).toBeGreaterThan(0)
    expect(screen.getByText(/4 个账号未录购号成本/)).toBeTruthy()
    expect(screen.getByText(/¥20.00 成本未注明购买日期/)).toBeTruthy()
  })

  it('retains the last overview when refreshing fails and identifies stale data', async () => {
    render(<Dashboard />)
    expect(await screen.findByText('主实例')).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('button', { name: /刷新数据/ }).className).not.toContain('ant-btn-loading'))
    configureApi(overviewFixture(), true)
    fireEvent.click(screen.getByRole('button', { name: /刷新数据/ }))
    expect(await screen.findByText('经营数据读取失败，已保留上次结果')).toBeTruthy()
    expect(screen.getByText('主实例')).toBeTruthy()
  })

  it('explains missing purchase dates separately while keeping cumulative profit available', async () => {
    const fixture = overviewFixture()
    configureApi({ ...fixture, coverage: { ...fixture.coverage, today_costs_complete: false, undated_cost_cny: '20.00' }, finance: { ...fixture.finance, today_profit_cny: null } })
    render(<Dashboard />)
    const today = await screen.findByRole('region', { name: '今日经营' })
    expect(within(today).getByText('今日已登记成本')).toBeTruthy()
    expect(within(today).getByText('另有 ¥20.00 成本日期待核实')).toBeTruthy()
    expect(within(today).getByText('待核实购买日期')).toBeTruthy()
    expect(within(today).queryByText('待补齐实例售价')).toBeNull()
    expect(within(screen.getByRole('region', { name: '累计经营' })).getByText('¥543.19')).toBeTruthy()
  })

  it('shows the daily billing series in chronological order and reads the supplied day values on focus', async () => {
    const fixture = overviewFixture()
    configureApi({ ...fixture, trend: { complete: true, points: [
      { date: '2026-09-07', billed_usd: '87.650000', revenue_cny: '21.04', requests: 121 },
      { date: '2026-09-05', billed_usd: '18.250000', revenue_cny: '4.38', requests: 42 },
      { date: '2026-09-06', billed_usd: '31.100000', revenue_cny: '7.46', requests: 58 },
    ] } })
    render(<Dashboard />)
    const trend = await screen.findByRole('region', { name: '近 7 日产出趋势' })
    const points = within(trend).getAllByRole('button')
    expect(points.map(point => point.getAttribute('aria-label'))).toEqual([
      '2026-09-05，已计费 $18.25', '2026-09-06，已计费 $31.10', '2026-09-07，已计费 $87.65',
    ])
    fireEvent.focus(points[0])
    const detail = screen.getByRole('status', { name: '趋势日期详情' })
    expect(detail.textContent).toContain('$18.25')
    expect(detail.textContent).toContain('¥4.38')
    expect(detail.textContent).toContain('42 次请求')
    expect(vi.mocked(apiFetch).mock.calls.every(([path]) => path.startsWith('/operations/overview?') || path === '/tasks/summary')).toBe(true)
  })

  it('distinguishes a missing billing day from a real zero without drawing a zero replacement', async () => {
    const fixture = overviewFixture()
    configureApi({ ...fixture, trend: { complete: false, points: [
      { date: '2026-09-05', billed_usd: '12.000000', revenue_cny: '2.88', requests: 12 },
      { date: '2026-09-06', billed_usd: null, revenue_cny: null, requests: null },
      { date: '2026-09-07', billed_usd: '0.000000', revenue_cny: '0.00', requests: 0 },
    ] } })
    render(<Dashboard />)
    const trend = await screen.findByRole('region', { name: '近 7 日产出趋势' })
    expect(within(trend).getByText('部分记录')).toBeTruthy()
    const missing = within(trend).getByRole('button', { name: '2026-09-06，已计费美元未获取' })
    expect(within(missing).getByText('—')).toBeTruthy()
    fireEvent.focus(missing)
    expect(screen.getByRole('status', { name: '趋势日期详情' }).textContent).toContain('账单未获取')
    expect(screen.getByRole('status', { name: '趋势日期详情' }).textContent).not.toContain('$0.00')
    expect(within(trend).getByRole('button', { name: '2026-09-07，已计费 $0.00' })).toBeTruthy()
  })

  it('does not label an entirely unavailable daily series as zero dollars', async () => {
    const fixture = overviewFixture()
    configureApi({ ...fixture, trend: { complete: false, points: [
      { date: '2026-09-07', billed_usd: null, revenue_cny: null, requests: null },
    ] } })
    render(<Dashboard />)
    const trend = await screen.findByRole('region', { name: '近 7 日产出趋势' })
    expect(within(trend).queryByText('$0.00')).toBeNull()
    expect(within(trend).getByText('账单未获取')).toBeTruthy()
  })

  it('keeps four decimal places for configured sale prices and preserves small positive billing', async () => {
    const fixture = overviewFixture()
    configureApi({ ...fixture, targets: [{ ...fixture.targets[0], price_cny_per_usd: '0.1234', today_billed_usd: '0.000123' }] })
    render(<Dashboard />)
    expect(await screen.findByText('¥0.1234')).toBeTruthy()
    expect(screen.getByText('<$0.01')).toBeTruthy()
    expect(screen.getByTitle('USD 0.000123')).toBeTruthy()
  })

  it('keeps missing initial data separate from real zero values while retaining task status', async () => {
    configureApi(undefined, true)
    render(<Dashboard />)
    expect(await screen.findByText('经营数据读取失败，请刷新重试')).toBeTruthy()
    expect(screen.queryByText('¥0.00')).toBeNull()
    expect(screen.getByText('执行中 1')).toBeTruthy()
  })

  it('polls cache every 15 seconds and requests a live refresh manually', async () => {
    vi.useFakeTimers()
    render(<Dashboard />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(vi.mocked(apiFetch).mock.calls.filter(([path]) => path === '/operations/overview?refresh=0')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: /刷新数据/ }))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(apiFetch).toHaveBeenCalledWith('/operations/overview?refresh=1', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('does not overlap periodic requests while a refresh is still pending', async () => {
    vi.useFakeTimers()
    vi.mocked(apiFetch).mockImplementation(path => path.startsWith('/operations/overview?') ? new Promise(() => {}) : Promise.resolve([]))
    render(<Dashboard />)
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(vi.mocked(apiFetch).mock.calls.filter(([path]) => path.startsWith('/operations/overview?'))).toHaveLength(1)
  })

  it('aborts its pending requests on unmount', async () => {
    vi.mocked(apiFetch).mockReturnValue(new Promise(() => {}))
    const { unmount } = render(<Dashboard />)
    const signal = vi.mocked(apiFetch).mock.calls[0][1]?.signal
    expect(signal?.aborted).toBe(false)
    unmount()
    expect(signal?.aborted).toBe(true)
  })
})
