import { describe, expect, it } from 'vitest'

import {
  buildExistingAccountLoginTaskPayload,
  canStartChatGPTPhoneVerification,
  parseLeadBeeCodes,
  resolveMailboxSnapshotType,
} from './chatgptStagedLogin'

describe('chatgpt staged login helpers', () => {
  it('shows phone verification only when the AT-only account retained mailbox credentials', () => {
    expect(canStartChatGPTPhoneVerification({
      email: 'existing@example.com',
      token: 'access-token',
      extra: {
        access_token: 'access-token',
        refresh_token: '',
        mailbox_login_context: {
          provider: 'microsoft',
          email: 'existing@example.com',
          extra: { client_id: 'mail-client', refresh_token: 'mail-refresh' },
        },
      },
    })).toBe(true)
    expect(canStartChatGPTPhoneVerification({
      email: 'legacy@example.com',
      token: 'access-token',
      extra: { access_token: 'access-token', refresh_token: '' },
    })).toBe(false)
    expect(canStartChatGPTPhoneVerification({
      email: 'existing@example.com',
      token: 'access-token',
      extra: { access_token: 'access-token', refresh_token: 'refresh-token' },
    })).toBe(false)
    expect(canStartChatGPTPhoneVerification({
      email: 'existing@example.com',
      token: '',
      extra: {},
    })).toBe(false)
  })

  it('maps configured local mailbox providers to snapshot APIs', () => {
    expect(resolveMailboxSnapshotType({ mail_provider: 'microsoft' })).toBe('microsoft')
    expect(resolveMailboxSnapshotType({ mail_provider: 'outlook' })).toBe('microsoft')
    expect(resolveMailboxSnapshotType({
      mail_provider: 'mail_import',
      mail_import_source: 'applemail',
    })).toBe('applemail')
    expect(resolveMailboxSnapshotType({ mail_provider: 'luckmail' })).toBe(null)
  })

  it('builds a first-pass AT plus RT existing-account login payload', () => {
    const payload = buildExistingAccountLoginTaskPayload({
      count: 12,
      concurrency: 3,
      registerDelaySeconds: 0.5,
      executorType: 'protocol',
      captchaSolver: 'yescaptcha',
      bindPhoneAndGetRefreshToken: false,
      leadbeeCodes: [],
      config: {
        mail_provider: 'microsoft',
        mailbox_otp_timeout_seconds: 600,
        outlook_backend: 'graph',
      },
    })

    expect(payload.platform).toBe('chatgpt')
    expect(payload.count).toBe(12)
    expect(payload.concurrency).toBe(3)
    expect(payload.register_delay_seconds).toBe(0.5)
    expect(payload.extra).toMatchObject({
      mail_provider: 'microsoft',
      mailbox_otp_timeout_seconds: 600,
      outlook_backend: 'graph',
      chatgpt_registration_mode: 'refresh_token',
      chatgpt_has_refresh_token_solution: true,
      chatgpt_existing_account_login_only: true,
      chatgpt_existing_account_login_stage: 'refresh_token',
      chatgpt_existing_account_allow_phone_verification: false,
      chatgpt_existing_account_bind_phone_and_get_rt: false,
    })
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_codes')
  })

  it('normalizes one LeadBee card code per non-empty line', () => {
    expect(parseLeadBeeCodes('  card-one\n\ncard-two  \r\n card-three ')).toEqual([
      'card-one',
      'card-two',
      'card-three',
    ])
  })

  it('keeps the staged AT login and requests automatic phone verification afterwards', () => {
    const payload = buildExistingAccountLoginTaskPayload({
      count: 2,
      concurrency: 2,
      registerDelaySeconds: 0,
      executorType: 'protocol',
      captchaSolver: 'yescaptcha',
      bindPhoneAndGetRefreshToken: true,
      leadbeeCodes: ['card-one', 'card-two'],
      config: { mail_provider: 'microsoft' },
    })

    expect(payload.extra).toMatchObject({
      chatgpt_existing_account_login_stage: 'access_token',
      chatgpt_existing_account_allow_phone_verification: false,
      chatgpt_existing_account_bind_phone_and_get_rt: true,
      chatgpt_existing_account_leadbee_codes: ['card-one', 'card-two'],
    })
  })

  it('requests server-side SMS pool allocation without exposing card secrets', () => {
    const payload = buildExistingAccountLoginTaskPayload({
      count: 2,
      concurrency: 2,
      registerDelaySeconds: 0,
      executorType: 'protocol',
      captchaSolver: 'yescaptcha',
      bindPhoneAndGetRefreshToken: true,
      useSmsPool: true,
      leadbeeCodes: ['browser-secret-one', 'browser-secret-two'],
      config: { mail_provider: 'microsoft' },
    })

    expect(payload.extra).toMatchObject({
      chatgpt_existing_account_bind_phone_and_get_rt: true,
      chatgpt_existing_account_use_sms_pool: true,
    })
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_codes')
    expect(JSON.stringify(payload)).not.toContain('browser-secret')
  })

  it('requests server-side LeadBee API allocation without card, pool, or credential fields', () => {
    const payload = buildExistingAccountLoginTaskPayload({
      count: 2,
      concurrency: 2,
      registerDelaySeconds: 0,
      executorType: 'protocol',
      captchaSolver: 'yescaptcha',
      bindPhoneAndGetRefreshToken: true,
      leadbeeApi: true,
      useSmsPool: true,
      leadbeeCodes: ['fixture-card-one', 'fixture-card-two'],
      config: {
        mail_provider: 'microsoft',
        leadbee_api_enabled: 'yes',
        leadbee_api_key: 'fixture-config-key',
        leadbee_api_secret: 'fixture-config-secret',
        leadbee_api_product_id: 'fixture-product',
        leadbee_api_client_order_id: 'fixture-client-reference',
      },
    })

    expect(payload.extra).toMatchObject({
      chatgpt_existing_account_bind_phone_and_get_rt: true,
      chatgpt_existing_account_leadbee_api: true,
    })
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_use_sms_pool')
    expect(payload.extra).not.toHaveProperty('chatgpt_existing_account_leadbee_codes')
    expect(JSON.stringify(payload)).not.toContain('fixture-card')
    expect(JSON.stringify(payload)).not.toContain('fixture-config-key')
    expect(JSON.stringify(payload)).not.toContain('fixture-config-secret')
    expect(JSON.stringify(payload)).not.toContain('fixture-client-reference')
  })
})
