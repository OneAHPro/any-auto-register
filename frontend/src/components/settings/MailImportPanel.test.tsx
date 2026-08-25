// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { App, Form } from 'antd'

import { apiFetch } from '@/lib/utils'
import MailImportPanel from './MailImportPanel'

vi.mock('@/lib/utils', () => ({ apiFetch: vi.fn() }))

const providers = [
  {
    type: 'microsoft',
    label: 'Microsoft',
    description: '',
    content_placeholder: '',
    helper_text: '',
    supports_filename: false,
    filename_label: '',
    filename_placeholder: '',
    preview_empty_text: '',
  },
  {
    type: 'applemail',
    label: 'AppleMail',
    description: '',
    content_placeholder: '',
    helper_text: '',
    supports_filename: true,
    filename_label: '文件名',
    filename_placeholder: '',
    preview_empty_text: '',
  },
]

function snapshot(
  type: 'microsoft' | 'applemail',
  items: Array<{ email: string; account_type?: string; pool_state?: string; last_error?: string }> = [],
) {
  const availableCount = items.filter(item => !item.pool_state || item.pool_state === 'available').length
  return {
    type,
    label: type,
    count: availableCount,
    available_count: availableCount,
    visible_count: items.length,
    items: items.map((item, index) => ({
      index: index + 1,
      mailbox: 'INBOX',
      enabled: true,
      has_oauth: true,
      ...item,
    })),
    truncated: false,
    filename: '',
    path: '',
    pool_dir: '',
  }
}

function renderPanel(initialValues: Record<string, unknown> = {}) {
  function Harness() {
    const [form] = Form.useForm()
    return (
      <App>
        <Form
          form={form}
          initialValues={{
            mail_provider: 'mail_import',
            mail_import_source: 'microsoft',
            ...initialValues,
          }}
        >
          <MailImportPanel form={form} />
        </Form>
      </App>
    )
  }
  return render(<Harness />)
}

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

describe('MailImportPanel automatic detection', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(apiFetch).mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === '/mail-imports/providers') return { items: providers }
      if (path.startsWith('/mail-imports/snapshot?')) {
        return snapshot(path.includes('type=applemail') ? 'applemail' : 'microsoft')
      }
      if (path === '/mail-imports/detect') {
        const body = JSON.parse(String(options?.body || '{}'))
        const microsoft = String(body.content).includes('@outlook.com') ? 1 : 0
        const applemail = String(body.content).includes('@gmail.com') ? 1 : 0
        return {
          counts: { microsoft, applemail, unresolved: 0 },
          can_import: true,
          has_duplicates: false,
          duplicate_emails: [],
          rows: [],
        }
      }
      if (path === '/mail-imports') {
        return {
          type: 'microsoft',
          summary: { total: 1, success: 1, failed: 0 },
          snapshot: snapshot('microsoft'),
          errors: [],
          meta: {},
        }
      }
      throw new Error(`unexpected request: ${path}`)
    })
  })

  it('documents both supported dash delimiters', async () => {
    renderPanel()

    expect(await screen.findByText(/完整的 --- 或 ---- 分隔符/)).toBeTruthy()
  })

  it('detects mixed content without showing separate pool windows', async () => {
    const user = userEvent.setup()
    renderPanel()

    const textarea = await screen.findByRole('textbox', { name: '邮箱导入内容' })
    await user.type(
      textarea,
      'one@outlook.com----https://mail.test/one\ntwo@gmail.com----password----QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3',
    )

    expect(await screen.findByText('微软邮箱 1')).toBeTruthy()
    expect(screen.getByText('AppleMail 1')).toBeTruthy()
    expect(screen.getByText('待确认 0')).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: '邮箱池视图' })).toBeNull()
    expect(screen.getByText('统一兼容导入')).toBeTruthy()
  })

  it('submits auto type by default', async () => {
    const user = userEvent.setup()
    renderPanel()

    const textarea = await screen.findByRole('textbox', { name: '邮箱导入内容' })
    await user.type(textarea, 'one@outlook.com----https://mail.test/one')
    await screen.findByText('微软邮箱 1')
    await user.click(screen.getByRole('button', { name: '确认导入' }))

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/mail-imports')
      expect(call).toBeTruthy()
      expect(JSON.parse(String(call?.[1]?.body || '{}')).type).toBe('auto')
    })
  })

  it('requires explicit manual fallback for an unresolved row', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/mail-imports/providers') return { items: providers }
      if (path.startsWith('/mail-imports/snapshot?')) return snapshot('microsoft')
      if (path === '/mail-imports/detect') {
        return {
          counts: { microsoft: 0, applemail: 0, unresolved: 1 },
          can_import: false,
          has_duplicates: false,
          duplicate_emails: [],
          rows: [{ line_number: 1, email: 'one@example.com', resolved: false, message: '请使用手动类型兜底' }],
        }
      }
      if (path === '/mail-imports') return {
        type: 'microsoft',
        summary: { total: 1, success: 1, failed: 0 },
        snapshot: snapshot('microsoft'),
        errors: [],
        meta: {},
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    renderPanel()

    const textarea = await screen.findByRole('textbox', { name: '邮箱导入内容' })
    await user.type(
      textarea,
      'one@example.com----password----client-id-value-1234567890----refresh-token-value-12345678901234567890',
    )

    expect(await screen.findByText('有 1 条内容无法可靠识别')).toBeTruthy()
    expect((screen.getByRole('button', { name: '确认导入' }) as HTMLButtonElement).disabled).toBe(true)
    await user.click(screen.getByRole('button', { name: '按当前邮箱池类型导入' }))
    expect((screen.getByRole('button', { name: '确认导入' }) as HTMLButtonElement).disabled).toBe(false)
    await user.click(screen.getByRole('button', { name: '确认导入' }))

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/mail-imports')
      expect(JSON.parse(String(call?.[1]?.body || '{}')).type).toBe('microsoft')
    })
  })

  it('keeps manual import available when automatic detection is unavailable', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/mail-imports/providers') return { items: providers }
      if (path.startsWith('/mail-imports/snapshot?')) return snapshot('microsoft')
      if (path === '/mail-imports/detect') throw new Error('检测服务暂不可用')
      if (path === '/mail-imports') return {
        type: 'microsoft',
        summary: { total: 1, success: 1, failed: 0 },
        snapshot: snapshot('microsoft'),
        errors: [],
        meta: {},
      }
      throw new Error(`unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    renderPanel()

    await user.type(
      await screen.findByRole('textbox', { name: '邮箱导入内容' }),
      'one@outlook.com----https://mail.test/one',
    )

    expect(await screen.findByText('自动识别请求失败')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '按当前邮箱池类型导入' }))
    await user.click(screen.getByRole('button', { name: '确认导入' }))

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(([path]) => path === '/mail-imports')
      expect(JSON.parse(String(call?.[1]?.body || '{}')).type).toBe('microsoft')
    })
  })

  it('shows all imported provider pools in one compatibility preview', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/mail-imports/providers') return { items: providers }
      if (path.startsWith('/mail-imports/snapshot?type=microsoft')) {
        return snapshot('microsoft', [{ email: 'one@outlook.com' }])
      }
      if (path.startsWith('/mail-imports/snapshot?type=applemail')) {
        return snapshot('applemail', [{ email: 'two@gmail.com', account_type: 'chatgpt_google_password' }])
      }
      throw new Error(`unexpected request: ${path}`)
    })

    renderPanel()

    expect(await screen.findByText('统一兼容导入')).toBeTruthy()
    expect(await screen.findByText('one@outlook.com')).toBeTruthy()
    expect(await screen.findByText('two@gmail.com')).toBeTruthy()
    expect(screen.getByText('已导入: 2 个邮箱')).toBeTruthy()
  })

  it('loads the configured AppleMail file even when its form fields are not rendered', async () => {
    renderPanel({
      applemail_pool_dir: '/shared/data/mail',
      applemail_pool_file: 'active-pool.json',
    })

    await waitFor(() => {
      const request = vi.mocked(apiFetch).mock.calls.find(([path]) => (
        String(path).startsWith('/mail-imports/snapshot?type=applemail')
      ))
      expect(request).toBeTruthy()
      const url = new URL(String(request?.[0]), 'https://fixture.local')
      expect(url.searchParams.get('pool_dir')).toBe('/shared/data/mail')
      expect(url.searchParams.get('pool_file')).toBe('active-pool.json')
    })
  })

  it('keeps processing and failed mailboxes visible with distinct states', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path === '/mail-imports/providers') return { items: providers }
      if (path.startsWith('/mail-imports/snapshot?type=microsoft')) {
        return snapshot('microsoft', [
          { email: 'working@outlook.com', pool_state: 'leased' },
          { email: 'failed@outlook.com', pool_state: 'failed', last_error: 'invalid MFA' },
        ])
      }
      if (path.startsWith('/mail-imports/snapshot?type=applemail')) return snapshot('applemail')
      throw new Error(`unexpected request: ${path}`)
    })

    renderPanel()

    expect(await screen.findByText('working@outlook.com')).toBeTruthy()
    expect(await screen.findByText('failed@outlook.com')).toBeTruthy()
    expect(screen.getByText('处理中')).toBeTruthy()
    expect(screen.getByText('登录失败')).toBeTruthy()
    expect(screen.getByText('已导入: 2 个邮箱')).toBeTruthy()
  })
})
