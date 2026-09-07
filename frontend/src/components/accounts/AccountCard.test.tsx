// @vitest-environment jsdom

import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

import { AccountCard } from './AccountCard'

describe('AccountCard', () => {
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
  })

  afterEach(() => cleanup())

  const account = {
    id: 42,
    platform: 'chatgpt',
    email: 'operator@example.com',
    password: 'secret-password',
    user_id: 'user-42',
    status: 'registered',
    created_at: '2026-09-01T12:30:00Z',
    extra: {
      mailbox_login_context: { provider: 'microsoft' },
      chatgpt_local: {
        auth: { state: 'access_token_valid', http_status: 200 },
        subscription: {
          plan: 'pro',
          workspace_plan_type: 'team',
          subscription_active_until: '2026-09-30T21:47:00Z',
        },
        codex: { state: 'usable', http_status: 200 },
      },
    },
    chatgptLocal: {
      auth: { state: 'access_token_valid', http_status: 200 },
      subscription: {
        plan: 'pro',
        workspace_plan_type: 'team',
        subscription_active_until: '2026-09-30T21:47:00Z',
      },
      codex: { state: 'usable', http_status: 200 },
    },
    quota: {
      '7d': {
        usage_percent: 12,
        billed_usd: 98.34,
        remaining_usd: 721.66,
        reset_at: '2026-09-07T23:27:00Z',
        fresh: true,
      },
    },
    assignment: { target_name: 'Primary', pool_name: 'Team pool', state: 'active' },
  }

  it('shows account identity, plan, weekly quota, validity, and operations', () => {
    render(
      <AccountCard
        account={account}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        canPhoneVerification={false}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).getByText('operator@example.com')).toBeTruthy()
    expect(within(card).getByText('Pro')).toBeTruthy()
    expect(within(card).getByText('Microsoft')).toBeTruthy()
    expect(within(card).queryByText('user-42')).toBeNull()
    expect(within(card).queryByText('工作区')).toBeNull()
    expect(within(card).queryByText('当前目标')).toBeNull()
    expect(within(card).getByText('Team pool')).toBeTruthy()
    expect(within(card).getByText('7天使用')).toBeTruthy()
    expect(within(card).getByText('12%')).toBeTruthy()
    expect(within(card).queryByText('$98.34')).toBeNull()
    expect(within(card).getByText('价格')).toBeTruthy()
    expect(within(card).getByText('有效期至')).toBeTruthy()
    expect(within(card).getByRole('button', { name: '详情' })).toBeTruthy()
    expect(within(card).getByRole('button', { name: '删除' })).toBeTruthy()
  })

  it.each([
    [undefined, '未设置'],
    ['12.50', '¥12.50'],
    [0, '¥0.00'],
    ['999999999.99', '¥999999999.99'],
  ])('shows purchase cost %s independently from the actual price', (cost, expected) => {
    render(<AccountCard account={{ ...account, extra: { ...account.extra, purchase_cost_cny: cost, actual_price: 99 } }} platform="chatgpt" selected={false} onSelect={vi.fn()} onCopy={vi.fn()} onOpenDetails={vi.fn()} onDelete={vi.fn()} onEditCost={vi.fn()} />)
    const item = screen.getByRole('button', { name: '设置 operator@example.com 的账号成本' })
    expect(item.textContent).toContain('账号成本')
    expect(item.textContent).toContain(expected)
    expect(screen.getByText('¥99.00')).toBeTruthy()
  })

  it.each([
    ['20.00', 100, '¥0.2000/$'],
    [0, 100, '¥0.0000/$'],
    ['0.01', 1000, '<¥0.0001/$'],
    [undefined, 100, '未设置'],
    ['20.00', 0, '暂无计费'],
    ['20.00', -1, '暂无计费'],
    ['20.00', undefined, '暂无计费'],
    ['20.00', 'invalid', '暂无计费'],
  ])('shows live unit price for cost %s and billing %s', (cost, billed, expected) => {
    render(<AccountCard account={{ ...account, extra: { ...account.extra, purchase_cost_cny: cost }, chatgpt_display: { remote_status: 'active', quota_status: 'live', billing: { scope: 'all', billed_usd: billed }, quota: { window: '7d', billed_usd: 987 } } }} platform="chatgpt" selected={false} onSelect={vi.fn()} onCopy={vi.fn()} onOpenDetails={vi.fn()} onDelete={vi.fn()} onEditCost={vi.fn()} />)
    const quota = screen.getByRole('region', { name: '7天使用' })
    expect(within(quota).getByText('价格')).toBeTruthy()
    expect(within(quota).getByText(expected)).toBeTruthy()
    expect(within(quota).queryByText('状态')).toBeNull()
    expect(within(quota).queryByText('可用')).toBeNull()
  })

  it('does not substitute legacy window billing for missing all-time billing', () => {
    render(<AccountCard account={{ ...account, extra: { ...account.extra, purchase_cost_cny: '20.00' }, quota: { '7d': { continuous_billed_usd: 100, billed_usd: 40 } } }} platform="chatgpt" selected={false} onSelect={vi.fn()} onCopy={vi.fn()} onOpenDetails={vi.fn()} onDelete={vi.fn()} />)
    const quota = screen.getByRole('region', { name: '7天使用' })
    expect(within(quota).queryByText('$100.00')).toBeNull()
    expect(within(quota).queryByText('$40.00')).toBeNull()
    expect(within(quota).getByText('暂无计费')).toBeTruthy()
    expect(within(quota).queryByText('剩余估算')).toBeNull()
  })

  it('keeps all-time billing empty when a live account has only a quota-window cost', () => {
    render(<AccountCard account={{ ...account, extra: { ...account.extra, purchase_cost_cny: '20.00' }, chatgpt_display: { quota_status: 'live', quota: { window: '7d', billed_usd: 100, request_count: 123 } } }} platform="chatgpt" selected={false} onSelect={vi.fn()} onCopy={vi.fn()} onOpenDetails={vi.fn()} onDelete={vi.fn()} />)
    const quota = screen.getByRole('region', { name: '7天使用' })
    expect(within(quota).getByText('123')).toBeTruthy()
    expect(within(quota).queryByText('$100.00')).toBeNull()
    expect(within(quota).getByText('暂无计费')).toBeTruthy()
  })

  it('edits cost from a managed account without opening card details on a double click', () => {
    const onEditCost = vi.fn()
    const onOpenDetails = vi.fn()
    const managed = { ...account, remote_only: true }
    render(<AccountCard account={managed} platform="chatgpt" selected={false} onSelect={vi.fn()} onCopy={vi.fn()} onOpenDetails={onOpenDetails} onDelete={vi.fn()} onEditCost={onEditCost} />)
    const button = screen.getByRole('button', { name: '设置 operator@example.com 的账号成本' })
    fireEvent.click(button)
    fireEvent.doubleClick(button)
    expect(onEditCost).toHaveBeenCalledWith(managed)
    expect(onOpenDetails).not.toHaveBeenCalled()
  })

  it('does not send a temporary negative account id to the cost editor', () => {
    const onEditCost = vi.fn()
    render(<AccountCard account={{ ...account, id: -42, remote_only: true }} platform="chatgpt" selected={false} onSelect={vi.fn()} onCopy={vi.fn()} onOpenDetails={vi.fn()} onDelete={vi.fn()} onEditCost={onEditCost} />)
    const button = screen.getByRole('button', { name: '设置 operator@example.com 的账号成本' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(onEditCost).not.toHaveBeenCalled()
  })

  it('renders a remote Codex2API account with live billing and no local credential actions', () => {
    render(
      <AccountCard
        account={{
          id: -90210,
          platform: 'chatgpt',
          email: 'json-imported@example.com',
          password: '',
          token: '',
          status: 'registered',
          account_source: 'codex2api',
          remote_only: true,
          remote_id: 90210,
          target_name: 'default',
          chatgpt_display: {
            plan_type: 'pro',
            remote_status: 'active',
            quota_status: 'live',
            live_updated_at: '2026-09-05T06:59:00Z',
            billing: { billed_usd: 18.75, scope: 'all' },
            quota: {
              window: '7d',
              usage_percent: 42,
              billed_usd: 18.75,
              request_count: 1234,
              captured_at: '2026-09-05T06:59:00Z',
              reset_at: '2026-09-11T00:00:00Z',
            },
          },
          assignment: { target_name: 'default', pool_name: '公共池', state: 'active' },
        }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).queryByText('Codex2API 托管')).toBeNull()
    expect(within(card).getByText('Codex')).toBeTruthy()
    expect(within(card).getByText('$18.75')).toBeTruthy()
    expect(within(card).getByText('1,234')).toBeTruthy()
    expect(within(card).queryByRole('button', { name: '删除' })).toBeNull()
    expect(card.className).toContain('account-card--remote')
    expect((within(card).getByRole('checkbox') as HTMLInputElement).disabled).toBe(true)
  })

  it('loads credential controls only on request and keeps copy actions scoped to the account', () => {
    const onCopy = vi.fn()
    const onOpenDetails = vi.fn()
    render(<AccountCard account={{ ...account, extra: { ...account.extra, refresh_token: 'refresh-secret' } }} platform="chatgpt" selected={false} onSelect={vi.fn()} onCopy={onCopy} onOpenDetails={onOpenDetails} onDelete={vi.fn()} />)

    expect(screen.queryByRole('button', { name: '复制密码' })).toBeNull()
    expect(screen.queryByRole('button', { name: '复制Refresh Token' })).toBeNull()
    const toggle = screen.getByRole('button', { name: '登录凭据' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: '复制Refresh Token' }))
    expect(onCopy).toHaveBeenCalledWith('refresh-secret')
    expect(onOpenDetails).not.toHaveBeenCalled()
    fireEvent.click(toggle)
    expect(screen.queryByRole('button', { name: '复制密码' })).toBeNull()
  })

  it('identifies duplicate-email records by account id while retaining independent selection', () => {
    const onSelect = vi.fn()
    for (const id of [42, 43]) {
      render(<AccountCard account={{ ...account, id }} platform="chatgpt" selected={false} onSelect={onSelect} onCopy={vi.fn()} onOpenDetails={vi.fn()} onDelete={vi.fn()} />)
    }
    const records = screen.getAllByTestId('account-card')
    expect(within(records[0]).getByText('#42')).toBeTruthy()
    expect(within(records[1]).getByText('#43')).toBeTruthy()
    fireEvent.click(within(records[1]).getByRole('checkbox'))
    expect(onSelect).toHaveBeenCalledWith(43, true)
  })

  it('emits selection and copy actions without opening details', () => {
    const onSelect = vi.fn()
    const onCopy = vi.fn()
    const onOpenDetails = vi.fn()
    render(
      <AccountCard
        account={account}
        platform="chatgpt"
        selected={false}
        onSelect={onSelect}
        onCopy={onCopy}
        onOpenDetails={onOpenDetails}
        onDelete={vi.fn()}
        canPhoneVerification={false}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    fireEvent.click(within(card).getByRole('checkbox'))
    expect(onSelect).toHaveBeenCalledWith(42, true)

    fireEvent.click(within(card).getByRole('button', { name: '复制邮箱' }))
    expect(onCopy).toHaveBeenCalledWith('operator@example.com')
    expect(onOpenDetails).not.toHaveBeenCalled()
  })

  it('opens details from the focused card and preserves non-ChatGPT link copy', () => {
    const onOpenDetails = vi.fn()
    const onCopy = vi.fn()
    render(
      <AccountCard
        account={{ ...account, platform: 'kiro', cashier_url: 'https://checkout.example.test/trial' }}
        platform="kiro"
        selected={false}
        onSelect={vi.fn()}
        onCopy={onCopy}
        onOpenDetails={onOpenDetails}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    fireEvent.keyDown(card, { key: 'Enter' })
    expect(onOpenDetails).toHaveBeenCalledWith(expect.objectContaining({ platform: 'kiro' }))
    fireEvent.click(within(card).getByRole('button', { name: '复制试用链接' }))
    expect(onCopy).toHaveBeenCalledWith('https://checkout.example.test/trial')
  })

  it('keeps missing quota explicit instead of inventing usage values', () => {
    render(
      <AccountCard
        account={{ ...account, quota: {} }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        canPhoneVerification={false}
        moreAction={null}
      />,
    )

    expect(screen.getByText('尚无额度快照')).toBeTruthy()
  })

  it('derives plan and weekly usage from the stored local probe payload when no ledger row exists', () => {
    const localProbeAccount = {
      ...account,
      user_id: '',
      quota: {},
      chatgptLocal: {
        auth: { state: 'access_token_valid', http_status: 200, message: JSON.stringify({ id: 'auth-user-1' }) },
        subscription: { plan: 'unknown', workspace_plan_type: '' },
        codex: {
          state: 'usable',
          http_status: 200,
          message: JSON.stringify({
            plan_type: 'pro',
            account_id: 'probe-account-1',
            rate_limit: { primary_window: { used_percent: 24, reset_at: 1788775316 } },
          }),
        },
      },
    }

    render(
      <AccountCard
        account={localProbeAccount}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).getByText('Pro')).toBeTruthy()
    expect(within(card).getByText('24%')).toBeTruthy()
    expect(within(card).queryByText('probe-account-1')).toBeNull()
  })

  it('does not borrow a different quota window when the seven-day snapshot is incomplete', () => {
    render(
      <AccountCard
        account={{
          ...account,
          quota: { '7d': { remaining_usd: 12, reset_at: '2026-09-07T00:00:00Z' } },
          chatgptLocal: {
            ...account.chatgptLocal,
            codex: {
              ...account.chatgptLocal.codex,
              message: JSON.stringify({ rate_limit: { primary_window: { used_percent: 24 } } }),
            },
          },
        }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    expect(screen.getByText('额度快照缺少使用百分比')).toBeTruthy()
    expect(screen.queryByText('24%')).toBeNull()
  })

  it('uses the live account projection for an accurate compact quota card and removes the legacy status strip', () => {
    render(
      <AccountCard
        account={{
          id: 77,
          platform: 'chatgpt',
          email: 'live@example.com',
          password: 'password',
          status: 'registered',
          chatgpt_display: {
            plan_type: 'self_serve_business_prolite',
            plan_source: 'codex2api_live',
            subscription_active_until: '2026-10-04T01:56:53Z',
            billing: { billed_usd: 98.34, scope: 'all' },
            quota_status: 'live',
            quota: {
              window: '7d',
              usage_percent: 25,
              billed_usd: 98.34,
              reset_at: '2026-09-11T21:54:07+08:00',
              captured_at: '2026-09-05T02:17:39+08:00',
              request_count: 506,
              remote_status: 'active',
              source: 'codex2api_live',
            },
          },
        }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).getByText('Business Pro Lite')).toBeTruthy()
    expect(within(card).getByText('25%')).toBeTruthy()
    expect(within(card).getByText('506')).toBeTruthy()
    expect(within(card).getByText('$98.34')).toBeTruthy()
    expect(card.querySelector('.account-card__status-row')).toBeNull()
    expect(within(card).queryByText('剩余估算')).toBeNull()
  })

  it('renders the five-hour window for Plus accounts when live data includes both', () => {
    render(
      <AccountCard
        account={{
          ...account,
          chatgpt_display: {
            plan_type: 'plus',
            quota_status: 'live',
            quota: {
              window: '7d',
              usage_percent: 48,
              reset_at: '2026-09-12T00:00:00Z',
            },
            quota_windows: {
              '5h': {
                window: '5h',
                usage_percent: 16,
                reset_at: '2026-09-06T12:00:00Z',
              },
              '7d': {
                window: '7d',
                usage_percent: 48,
                reset_at: '2026-09-12T00:00:00Z',
              },
            },
          },
        }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).getByText('5小时使用')).toBeTruthy()
    expect(within(card).getByText('16%')).toBeTruthy()
    expect(within(card).queryByText('7天使用')).toBeNull()
    expect(within(card).queryByText('48%')).toBeNull()
  })

  it('shows quota-limited live status variants as limited instead of a generic state', () => {
    render(
      <AccountCard
        account={{
          ...account,
          chatgpt_display: {
            plan_type: 'pro',
            remote_status: 'rate_limited_5h',
            quota_status: 'live',
            quota: { window: '5h', usage_percent: 100 },
          },
        }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).getAllByText('限流中')).toHaveLength(1)
  })

  it('keeps a persisted invalid local account visibly invalid after a live refresh', () => {
    render(
      <AccountCard
        account={{
          ...account,
          status: 'invalid',
          chatgpt_display: {
            plan_type: 'pro',
            remote_status: 'active',
            quota_status: 'live',
            quota: { window: '7d', usage_percent: 10 },
          },
        }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).getAllByText('已失效')).toHaveLength(1)
    expect(within(card).queryByText('可用')).toBeNull()
  })

  it('marks a live account missing from the remote inventory instead of showing it as available', () => {
    render(
      <AccountCard
        account={{
          ...account,
          chatgpt_display: {
            plan_type: 'pro',
            quota_status: 'not_found',
            remote_status: null,
          },
        }}
        platform="chatgpt"
        selected={false}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onOpenDetails={vi.fn()}
        onDelete={vi.fn()}
        moreAction={null}
      />,
    )

    const card = screen.getByTestId('account-card')
    expect(within(card).getAllByText('远端未发现')).toHaveLength(1)
  })
})
