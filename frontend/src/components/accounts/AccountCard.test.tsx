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
    expect(within(card).getByText('user-42')).toBeTruthy()
    expect(within(card).getByText('7天使用')).toBeTruthy()
    expect(within(card).getByText('12%')).toBeTruthy()
    expect(within(card).getByText('$98.34')).toBeTruthy()
    expect(within(card).getByText('$721.66')).toBeTruthy()
    expect(within(card).getByText('有效期至')).toBeTruthy()
    expect(within(card).getByRole('button', { name: '详情' })).toBeTruthy()
    expect(within(card).getByRole('button', { name: '删除' })).toBeTruthy()
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
    expect(within(card).getByText('probe-account-1')).toBeTruthy()
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
})
