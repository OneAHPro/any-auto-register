import { useEffect, useMemo, useRef, useState } from 'react'
import { App, Alert, Button, Card, Form, Input, InputNumber, Popconfirm, Select, Space, Switch, Table, Tag, Typography } from 'antd'
import type { FormInstance } from 'antd'

import { apiFetch } from '@/lib/utils'

type MailImportProviderType = 'applemail' | 'microsoft'
type MailImportSelectionType = MailImportProviderType | 'outlook' | 'hotmail' | 'mailapi'
type MailImportFormProviderType = MailImportProviderType | 'mail_import'

interface MailImportPanelProps {
  form: FormInstance
}

interface MailImportProviderDescriptor {
  type: MailImportProviderType
  label: string
  description: string
  content_placeholder: string
  helper_text: string
  supports_filename: boolean
  filename_label: string
  filename_placeholder: string
  preview_empty_text: string
}

interface MailImportDisplayProvider extends Omit<MailImportProviderDescriptor, 'type'> {
  type: MailImportSelectionType
  apiType: MailImportProviderType
}

interface MailImportSnapshotItem {
  index: number
  email: string
  mailbox: string
  enabled?: boolean | null
  has_oauth?: boolean | null
  account_type?: 'microsoft_oauth' | 'mailapi_url' | 'applemail_oauth' | 'icloud_web' | 'chatgpt_password' | 'chatgpt_google_password' | 'chatgpt_password_totp' | 'chatgpt_password_remote_totp' | 'chatgpt_password_url_otp' | 'chatgpt_password_reset_url_mail' | null
  pool_state?: string
  last_error?: string
  last_task_id?: string
}

interface UnifiedMailImportSnapshotItem extends MailImportSnapshotItem {
  providerType: MailImportProviderType
  key: string
}

interface MailImportSnapshot {
  type: MailImportProviderType
  label: string
  count: number
  available_count?: number | null
  visible_count?: number | null
  items: MailImportSnapshotItem[]
  truncated: boolean
  filename: string
  path: string
  pool_dir: string
}

interface MailImportSummary {
  total: number
  success: number
  failed: number
}

interface MailImportResult {
  type: MailImportProviderType | 'auto'
  summary: MailImportSummary
  snapshot: MailImportSnapshot
  errors: string[]
  meta: Record<string, unknown>
}

interface MailImportDetectionRow {
  line_number: number
  email: string
  provider?: MailImportProviderType | null
  account_type?: MailImportSnapshotItem['account_type']
  resolved: boolean
  message: string
}

interface MailImportDetection {
  counts: {
    microsoft: number
    applemail: number
    unresolved: number
  }
  can_import: boolean
  has_duplicates: boolean
  duplicate_emails: string[]
  rows: MailImportDetectionRow[]
}

const SUPPORTED_IMPORT_TYPES: MailImportProviderType[] = ['applemail', 'microsoft']
const SUPPORTED_SELECTION_TYPES: MailImportSelectionType[] = ['applemail', 'microsoft', 'outlook', 'hotmail', 'mailapi']

function isSupportedImportType(value: string): value is MailImportProviderType {
  return SUPPORTED_IMPORT_TYPES.includes(value as MailImportProviderType)
}

function isSupportedSelectionType(value: string): value is MailImportSelectionType {
  return SUPPORTED_SELECTION_TYPES.includes(value as MailImportSelectionType)
}

function toImportApiType(value: MailImportSelectionType): MailImportProviderType {
  return value === 'applemail' ? 'applemail' : 'microsoft'
}

function resolveMicrosoftImportType(domain: string) {
  return domain.includes('hotmail') ? 'hotmail' : 'outlook'
}

function resolvePreferredImportType(
  currentMailProvider: string,
  mailImportSource: string,
  luckmailEmailType: string,
  luckmailDomain: string,
  applemailPoolFile: string,
): MailImportSelectionType {
  if (currentMailProvider === 'mail_import') {
    return mailImportSource === 'applemail' ? 'applemail' : resolveMicrosoftImportType(String(luckmailDomain || '').trim().toLowerCase())
  }
  if (currentMailProvider === 'applemail') return 'applemail'
  if (currentMailProvider === 'microsoft' || currentMailProvider === 'outlook') {
    return resolveMicrosoftImportType(String(luckmailDomain || '').trim().toLowerCase())
  }

  const normalizedLuckmailType = String(luckmailEmailType || '').trim().toLowerCase()
  const normalizedLuckmailDomain = String(luckmailDomain || '').trim().toLowerCase()
  const isMicrosoftMailbox =
    normalizedLuckmailType.startsWith('ms_')
    || normalizedLuckmailDomain.includes('outlook')
    || normalizedLuckmailDomain.includes('hotmail')

  if (isMicrosoftMailbox) {
    return resolveMicrosoftImportType(normalizedLuckmailDomain)
  }

  if (String(applemailPoolFile || '').trim()) {
    return 'applemail'
  }

  return 'outlook'
}

function buildDisplayProviders(providers: MailImportProviderDescriptor[]) {
  const items: MailImportDisplayProvider[] = []

  for (const provider of providers) {
    if (provider.type === 'applemail') {
      items.push({
        ...provider,
        type: 'applemail',
        apiType: 'applemail',
        label: 'iCloud MFA / AppleMail / 小苹果',
      })
      continue
    }

    items.push(
      {
        ...provider,
        type: 'outlook',
        apiType: 'microsoft',
        label: 'Outlook',
        description: '导入 Outlook 本地号池，支持 mixed 导入（OAuth / MailAPI URL）；运行时按账号类型自动选择 Graph/IMAP 或 MailAPI URL 轮询取码。',
        helper_text: '支持完整的 --- 或 ---- 分隔符并自动识别：邮箱----密码----client_id----refresh_token 或 邮箱----mailapi_url；当前视图仅展示 @outlook 的 OAuth 账号。',
        content_placeholder: 'example@outlook.com----password----client_id----refresh_token',
        preview_empty_text: '当前还没有可预览的 Outlook 已导入账号。',
      },
      {
        ...provider,
        type: 'hotmail',
        apiType: 'microsoft',
        label: 'Hotmail',
        description: '导入 Hotmail 本地号池，支持 mixed 导入（OAuth / MailAPI URL）；运行时按账号类型自动选择 Graph/IMAP 或 MailAPI URL 轮询取码。',
        helper_text: '支持完整的 --- 或 ---- 分隔符并自动识别：邮箱----密码----client_id----refresh_token 或 邮箱----mailapi_url；当前视图仅展示 @hotmail 的 OAuth 账号。',
        content_placeholder: 'example@hotmail.com----password----client_id----refresh_token',
        preview_empty_text: '当前还没有可预览的 Hotmail 已导入账号。',
      },
      {
        ...provider,
        type: 'mailapi',
        apiType: 'microsoft',
        label: 'MailAPI URL',
        description: '导入 MailAPI URL 账号池（邮箱----mailapi_url），运行时通过 URL 轮询网页内容提取验证码。',
        helper_text: '支持 mixed 导入及完整的 --- 或 ---- 分隔符。当前视图仅展示 account_type=mailapi_url 的账号。',
        content_placeholder: 'example@hotmail.com----https://mailapi.icu/key?type=html&orderNo=xxxxxxxx',
        preview_empty_text: '当前还没有可预览的 MailAPI URL 已导入账号。',
      },
    )
  }

  return items
}

function buildImportSuccessMessage(result: MailImportResult) {
  if (result.type === 'applemail') {
    const fileLabel = result.snapshot.filename ? `，已绑定 ${result.snapshot.filename}` : ''
    return `导入成功，共 ${result.summary.success} 个邮箱${fileLabel}`
  }
  return `导入完成：成功 ${result.summary.success} / 失败 ${result.summary.failed}`
}

function buildResultMessage(result: MailImportResult) {
  if (result.type === 'applemail') {
    return `导入完成：成功 ${result.summary.success} / 失败 ${result.summary.failed}`
  }
  return `导入完成：成功 ${result.summary.success} / 失败 ${result.summary.failed}`
}

export default function MailImportPanel({ form }: MailImportPanelProps) {
  const { message } = App.useApp()
  const watchOptions = { form, preserve: true }
  const currentMailProvider = String(Form.useWatch('mail_provider', watchOptions) || '') as MailImportFormProviderType
  const currentMailImportSource = String(Form.useWatch('mail_import_source', watchOptions) || 'microsoft')
  const watchedPoolDir = String(Form.useWatch('applemail_pool_dir', watchOptions) || 'mail')
  const watchedPoolFile = String(Form.useWatch('applemail_pool_file', watchOptions) || '')
  const watchedLuckmailEmailType = String(Form.useWatch('luckmail_email_type', watchOptions) || '')
  const watchedLuckmailDomain = String(Form.useWatch('luckmail_domain', watchOptions) || '')

  const [providers, setProviders] = useState<MailImportDisplayProvider[]>([])
  const [selectedType, setSelectedType] = useState<MailImportSelectionType>('outlook')
  const [content, setContent] = useState('')
  const [filename, setFilename] = useState('')
  const [importing, setImporting] = useState(false)
  const [deletingEmail, setDeletingEmail] = useState('')
  const [batchDeleting, setBatchDeleting] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])
  const [loadingSnapshot, setLoadingSnapshot] = useState(false)
  const [snapshots, setSnapshots] = useState<Partial<Record<MailImportProviderType, MailImportSnapshot>>>({})
  const [result, setResult] = useState<MailImportResult | null>(null)
  const [aliasSplitEnabled, setAliasSplitEnabled] = useState(false)
  const [aliasSplitCount, setAliasSplitCount] = useState(5)
  const [aliasIncludeOriginal, setAliasIncludeOriginal] = useState(false)
  const [detection, setDetection] = useState<MailImportDetection | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [detectionError, setDetectionError] = useState('')
  const [manualFallback, setManualFallback] = useState(false)
  const detectionRequestId = useRef(0)

  const providerMap = useMemo(
    () => new Map(providers.map((provider) => [provider.type, provider])),
    [providers],
  )
  const selectedProvider = providerMap.get(selectedType) ?? null
  const selectedApiType = selectedProvider?.apiType ?? toImportApiType(selectedType)
  const hasImportContent = Boolean(content.trim())
  const supportsAliasSplit = manualFallback
    ? selectedApiType === 'microsoft'
    : hasImportContent
      ? Boolean(detection?.counts.microsoft)
      : selectedApiType === 'microsoft'
  const supportsFilename = manualFallback
    ? selectedApiType === 'applemail'
    : hasImportContent
      ? Boolean(detection?.counts.applemail)
      : Boolean(selectedProvider?.supports_filename)
  const preferredImportType = useMemo(
    () => resolvePreferredImportType(
      currentMailProvider,
      currentMailImportSource,
      watchedLuckmailEmailType,
      watchedLuckmailDomain,
      watchedPoolFile,
    ),
    [currentMailImportSource, currentMailProvider, watchedLuckmailDomain, watchedLuckmailEmailType, watchedPoolFile],
  )
  const tableData = useMemo<UnifiedMailImportSnapshotItem[]>(() => {
    let displayIndex = 0
    return (['microsoft', 'applemail'] as MailImportProviderType[]).flatMap((providerType) => (
      (snapshots[providerType]?.items || []).map((item) => ({
        ...item,
        index: ++displayIndex,
        providerType,
        key: `${providerType}::${item.email}::${item.mailbox || ''}`,
      }))
    ))
  }, [snapshots])
  const importedCount = SUPPORTED_IMPORT_TYPES.reduce(
    (total, providerType) => total + Math.max(
      0,
      Number(
        snapshots[providerType]?.visible_count
        ?? snapshots[providerType]?.count
        ?? snapshots[providerType]?.items?.length
        ?? 0,
      ),
    ),
    0,
  )
  const hasTruncatedSnapshot = SUPPORTED_IMPORT_TYPES.some(
    providerType => Boolean(snapshots[providerType]?.truncated),
  )

  const loadProviders = async () => {
    try {
      const data = await apiFetch('/mail-imports/providers') as { items?: MailImportProviderDescriptor[] }
      const items = Array.isArray(data.items) ? data.items.filter((item) => isSupportedImportType(item.type)) : []
      const displayProviders = buildDisplayProviders(items)
      setProviders(displayProviders)

      if (isSupportedSelectionType(preferredImportType) && displayProviders.some((item) => item.type === preferredImportType)) {
        setSelectedType(preferredImportType)
      } else if (displayProviders.length > 0) {
        setSelectedType(displayProviders[0].type)
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : '加载邮箱导入配置失败'
      message.error(detail)
    }
  }

  const loadSnapshots = async () => {
    setLoadingSnapshot(true)
    try {
      const entries = await Promise.all(SUPPORTED_IMPORT_TYPES.map(async (providerType) => {
        const params = new URLSearchParams({
          type: providerType,
          preview_limit: '500',
        })
        if (providerType === 'applemail') {
          if (watchedPoolDir.trim()) {
            params.set('pool_dir', watchedPoolDir.trim())
          }
          if (watchedPoolFile.trim()) {
            params.set('pool_file', watchedPoolFile.trim())
          }
        }
        try {
          const nextSnapshot = await apiFetch(`/mail-imports/snapshot?${params.toString()}`) as MailImportSnapshot
          return [providerType, nextSnapshot] as const
        } catch {
          return [providerType, null] as const
        }
      }))
      const nextSnapshots: Partial<Record<MailImportProviderType, MailImportSnapshot>> = {}
      for (const [providerType, providerSnapshot] of entries) {
        if (providerSnapshot) nextSnapshots[providerType] = providerSnapshot
      }
      setSnapshots(nextSnapshots)
    } catch {
      setSnapshots({})
    } finally {
      setLoadingSnapshot(false)
    }
  }

  useEffect(() => {
    void loadProviders()
  }, [])

  useEffect(() => {
    if (providerMap.has(preferredImportType)) {
      setSelectedType(preferredImportType)
    }
  }, [preferredImportType, providerMap])

  useEffect(() => {
    if (!providers.length) return
    void loadSnapshots()
    const timer = window.setInterval(() => {
      void loadSnapshots()
    }, 3000)
    return () => window.clearInterval(timer)
  }, [providers.length, watchedPoolDir, watchedPoolFile])

  useEffect(() => {
    setSelectedRowKeys([])
  }, [snapshots])

  useEffect(() => {
    const payload = content.trim()
    const requestId = ++detectionRequestId.current
    setManualFallback(false)
    setDetectionError('')
    if (!payload) {
      setDetection(null)
      setDetecting(false)
      return
    }

    setDetecting(true)
    const timer = window.setTimeout(() => {
      void apiFetch('/mail-imports/detect', {
        method: 'POST',
        body: JSON.stringify({ content: payload }),
      }).then((response) => {
        if (detectionRequestId.current !== requestId) return
        setDetection(response as MailImportDetection)
      }).catch((error) => {
        if (detectionRequestId.current !== requestId) return
        setDetection(null)
        setDetectionError(error instanceof Error ? error.message : '自动识别失败')
      }).finally(() => {
        if (detectionRequestId.current === requestId) setDetecting(false)
      })
    }, 300)

    return () => window.clearTimeout(timer)
  }, [content])

  const handleImport = async () => {
    const payload = content.trim()
    if (!payload) {
      message.error('请输入导入内容')
      return
    }
    if (!manualFallback && (!detection || !detection.can_import)) {
      message.error('请等待自动识别完成，或对未识别内容启用手动兜底')
      return
    }

    setImporting(true)
    try {
      const apiType = toImportApiType(selectedType)
      const body: Record<string, unknown> = {
        type: manualFallback ? apiType : 'auto',
        content: payload,
        enabled: true,
        bind_to_config: true,
        preferred_provider: selectedApiType,
        filename: filename.trim(),
        pool_dir: String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail',
        alias_split_enabled: aliasSplitEnabled,
        alias_split_count: aliasSplitCount,
        alias_include_original: aliasIncludeOriginal,
      }

      const response = await apiFetch('/mail-imports', {
        method: 'POST',
        body: JSON.stringify(body),
      }) as MailImportResult

      setResult(response)
      if (response.snapshot?.type) {
        setSnapshots((current) => ({
          ...current,
          [response.snapshot.type]: response.snapshot,
        }))
      }
      setContent('')
      setFilename('')
      setDetection(null)
      setManualFallback(false)

      if (response.type === 'applemail') {
        form.setFieldsValue({
          mail_provider: 'mail_import',
          mail_import_source: 'applemail',
          applemail_pool_dir: response.snapshot.pool_dir,
          applemail_pool_file: response.snapshot.filename,
        })
      } else if (response.type === 'microsoft') {
        form.setFieldsValue({
          mail_provider: 'mail_import',
          mail_import_source: 'microsoft',
        })
      } else if (response.type === 'auto') {
        const boundProvider = String(response.meta.bound_provider || '')
        const applemailPoolFile = String(response.meta.applemail_pool_file || '')
        const applemailPoolDir = String(response.meta.applemail_pool_dir || '')
        form.setFieldsValue({
          ...(boundProvider ? {
            mail_provider: 'mail_import',
            mail_import_source: boundProvider,
          } : {}),
          ...(applemailPoolFile ? {
            applemail_pool_dir: applemailPoolDir || 'mail',
            applemail_pool_file: applemailPoolFile,
          } : {}),
        })
      }

      message.success(buildImportSuccessMessage(response))
      window.setTimeout(() => void loadSnapshots(), 0)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '邮箱导入失败'
      message.error(detail)
    } finally {
      setImporting(false)
    }
  }

  const handleTypeChange = (value: MailImportSelectionType) => {
    setSelectedType(value)
    form.setFieldsValue({
      mail_provider: 'mail_import',
      mail_import_source: value === 'applemail' ? 'applemail' : 'microsoft',
    })
  }

  const handleDelete = async (item: UnifiedMailImportSnapshotItem) => {
    const apiType = item.providerType
    const email = String(item.email || '').trim()
    if (!email) return

    setDeletingEmail(email)
    try {
      const body: Record<string, unknown> = {
        type: apiType,
        email,
      }

      if (apiType === 'applemail') {
        body.mailbox = item.mailbox || ''
        body.pool_dir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
        body.pool_file = String(form.getFieldValue('applemail_pool_file') || '').trim()
      }

      const response = await apiFetch('/mail-imports/delete', {
        method: 'POST',
        body: JSON.stringify(body),
      }) as MailImportResult

      setResult(response)
      setSnapshots((current) => ({
        ...current,
        [apiType]: response.snapshot,
      }))
      setSelectedRowKeys([])
      message.success(`已删除 ${email}`)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '删除失败'
      message.error(detail)
    } finally {
      setDeletingEmail('')
    }
  }

  const handleBatchDelete = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选要删除的邮箱')
      return
    }

    const selectedItems = tableData.filter((item) => selectedRowKeys.includes(item.key))
    if (!selectedItems.length) {
      message.warning('未找到要删除的邮箱')
      return
    }

    setBatchDeleting(true)
    try {
      let success = 0
      let failed = 0
      let lastResponse: MailImportResult | null = null
      for (const apiType of SUPPORTED_IMPORT_TYPES) {
        const providerItems = selectedItems.filter(item => item.providerType === apiType)
        if (!providerItems.length) continue
        const body: Record<string, unknown> = {
          type: apiType,
          items: providerItems.map((item) => ({
            email: item.email,
            mailbox: item.mailbox || '',
          })),
        }
        if (apiType === 'applemail') {
          body.pool_dir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
          body.pool_file = String(form.getFieldValue('applemail_pool_file') || '').trim()
        }
        const response = await apiFetch('/mail-imports/batch-delete', {
          method: 'POST',
          body: JSON.stringify(body),
        }) as MailImportResult
        lastResponse = response
        success += response.summary.success
        failed += response.summary.failed
        setSnapshots((current) => ({
          ...current,
          [apiType]: response.snapshot,
        }))
      }

      if (lastResponse) {
        setResult({
          ...lastResponse,
          type: 'auto',
          summary: { total: success + failed, success, failed },
        })
      }
      setSelectedRowKeys([])
      message.success(`批量删除完成：成功 ${success} / 失败 ${failed}`)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '批量删除失败'
      const shouldFallbackToSingleDelete = /405|404|Method Not Allowed|Not Found/i.test(detail)

      if (!shouldFallbackToSingleDelete) {
        message.error(detail)
        return
      }

      let success = 0
      let failed = 0
      const errors: string[] = []

      for (const item of selectedItems) {
        try {
          const apiType = item.providerType
          const body: Record<string, unknown> = {
            type: apiType,
            email: item.email,
          }

          if (apiType === 'applemail') {
            body.mailbox = item.mailbox || ''
            body.pool_dir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
            body.pool_file = String(form.getFieldValue('applemail_pool_file') || '').trim()
          }

          const response = await apiFetch('/mail-imports/delete', {
            method: 'POST',
            body: JSON.stringify(body),
          }) as MailImportResult

          setResult(response)
          setSnapshots((current) => ({
            ...current,
            [apiType]: response.snapshot,
          }))
          success += 1
        } catch (singleError) {
          failed += 1
          errors.push(singleError instanceof Error ? singleError.message : `删除失败: ${item.email}`)
        }
      }

      setSelectedRowKeys([])
      if (errors.length) {
        message.warning(`批量删除已回退单条删除：成功 ${success} / 失败 ${failed}`)
        setResult((prev) => prev ? {
          ...prev,
          errors,
          summary: { total: success + failed, success, failed },
        } : prev)
      } else {
        message.success(`批量删除已回退单条删除：成功 ${success} / 失败 ${failed}`)
      }
    } finally {
      setBatchDeleting(false)
      void loadSnapshots()
    }
  }

  const columns = useMemo(() => {
    const accountTypeLabels: Record<string, { label: string, color?: string }> = {
      microsoft_oauth: { label: 'Microsoft OAuth', color: 'blue' },
      mailapi_url: { label: 'MailAPI URL', color: 'purple' },
      applemail_oauth: { label: 'AppleMail OAuth', color: 'blue' },
      icloud_web: { label: 'iCloud Web', color: 'cyan' },
      chatgpt_password: { label: 'ChatGPT 密码', color: 'orange' },
      chatgpt_google_password: { label: 'Google 联邦', color: 'geekblue' },
      chatgpt_password_totp: { label: '密码 + TOTP', color: 'green' },
      chatgpt_password_remote_totp: { label: '远程 TOTP', color: 'green' },
      chatgpt_password_url_otp: { label: '密码 + 接码地址', color: 'orange' },
      chatgpt_password_reset_url_mail: { label: '需重置密码', color: 'volcano' },
    }
    const baseColumns = [
      {
        title: '#',
        dataIndex: 'index',
        key: 'index',
        width: 72,
      },
      {
        title: '邮箱',
        dataIndex: 'email',
        key: 'email',
      },
      {
        title: '来源',
        dataIndex: 'providerType',
        key: 'providerType',
        width: 170,
        render: (value: MailImportProviderType) => (
          <Tag color={value === 'microsoft' ? 'blue' : 'purple'}>
            {value === 'microsoft' ? '微软邮箱池' : 'Google / MFA / AppleMail'}
          </Tag>
        ),
      } as never,
      {
        title: '凭据类型',
        dataIndex: 'account_type',
        key: 'account_type',
        width: 180,
        render: (value: string | null | undefined, item: UnifiedMailImportSnapshotItem) => {
          const fallbackType = item.providerType === 'microsoft' ? 'microsoft_oauth' : 'applemail_oauth'
          const config = accountTypeLabels[String(value || fallbackType)] || { label: String(value || fallbackType) }
          return <Tag color={config.color}>{config.label}</Tag>
        },
      } as never,
      {
        title: '状态',
        key: 'status',
        width: 100,
        render: (_: unknown, item: UnifiedMailImportSnapshotItem) => {
          const state = String(item.pool_state || (item.enabled === false ? 'disabled' : 'available')).toLowerCase()
          if (state === 'claimed' || state === 'leased') {
            return <Tag color="processing">处理中</Tag>
          }
          if (state === 'failed' || state === 'quarantined') {
            return <Tag color="error" title={item.last_error || ''}>登录失败</Tag>
          }
          const enabled = item.enabled !== false || state === 'available'
          return <Tag color={enabled ? 'green' : 'default'}>{enabled ? '可用' : '停用'}</Tag>
        },
      } as never,
    ]

    baseColumns.push({
      title: '操作',
      key: 'action',
      width: 90,
      render: (_: unknown, item: UnifiedMailImportSnapshotItem) => (
        <Popconfirm
          title="确认删除这个邮箱吗？"
          description={item.email}
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true, loading: deletingEmail === item.email }}
          onConfirm={() => void handleDelete(item)}
        >
          <Button
            danger
            type="link"
            size="small"
            loading={deletingEmail === item.email}
            style={{ paddingInline: 0 }}
            disabled={['claimed', 'leased'].includes(String(item.pool_state || '').toLowerCase())}
          >
            删除
          </Button>
        </Popconfirm>
      ),
    } as never)

    return baseColumns
  }, [deletingEmail])

  return (
    <Card
      title={<Space><span>邮箱导入</span><Tag color="geekblue">统一兼容导入</Tag></Space>}
      style={{ marginBottom: 16 }}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <Typography.Text strong>
          粘贴后自动识别所有支持格式，字段支持完整的 --- 或 ---- 分隔符。
        </Typography.Text>
        <Typography.Text type="secondary">
          Microsoft OAuth、MailAPI URL、Google 联邦、密码 + TOTP、密码 + 接码地址和 AppleMail 统一从这里导入；下方同时显示两个邮箱池的全部账号。
        </Typography.Text>

        {supportsFilename ? (
          <Form.Item label={selectedProvider?.filename_label || '文件名'} style={{ marginBottom: 0 }}>
            <Input
              value={filename}
              onChange={(event) => setFilename(event.target.value)}
              placeholder={selectedProvider?.filename_placeholder || 'applemail_日期.json（可选）'}
            />
          </Form.Item>
        ) : null}

        {supportsAliasSplit ? (
          <div
            style={{
              border: '1px dashed rgba(127,127,127,0.35)',
              borderRadius: 8,
              padding: 12,
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
            }}
          >
            <Space align="center">
              <Typography.Text strong>邮箱裂变（别名）</Typography.Text>
              <Switch checked={aliasSplitEnabled} onChange={setAliasSplitEnabled} />
              <Typography.Text type="secondary">
                默认关闭；开启后每个原邮箱生成随机 6 位英文别名
              </Typography.Text>
            </Space>
            {aliasSplitEnabled ? (
              <Space align="center" wrap>
                <Typography.Text>每个原邮箱裂变数量</Typography.Text>
                <InputNumber
                  min={1}
                  max={5}
                  value={aliasSplitCount}
                  onChange={(value) => setAliasSplitCount(Math.max(1, Math.min(5, Number(value || 5))))}
                />
                <Typography.Text type="secondary">（1~5）</Typography.Text>
                <Typography.Text style={{ marginLeft: 16 }}>包含原邮箱</Typography.Text>
                <Switch checked={aliasIncludeOriginal} onChange={setAliasIncludeOriginal} />
              </Space>
            ) : null}
          </div>
        ) : null}

        <Input.TextArea
          aria-label="邮箱导入内容"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          rows={10}
          placeholder={'邮箱----接码地址\n邮箱----密码\n邮箱----密码----MFA密钥\n邮箱----密码----client_id----refresh_token'}
          style={{ fontFamily: 'monospace' }}
        />

        {hasImportContent ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <Typography.Text type="secondary">自动识别：</Typography.Text>
            <Tag color="blue">微软邮箱 {detection?.counts.microsoft || 0}</Tag>
            <Tag color="purple">AppleMail {detection?.counts.applemail || 0}</Tag>
            <Tag color={detection?.counts.unresolved ? 'orange' : 'green'}>
              待确认 {detection?.counts.unresolved || 0}
            </Tag>
            {detecting ? <Typography.Text type="secondary">检测中…</Typography.Text> : null}
          </div>
        ) : null}

        {detection?.counts.unresolved ? (
          <Alert
            type="warning"
            showIcon
            message={`有 ${detection.counts.unresolved} 条内容无法可靠识别`}
            description={detection.rows.find((row) => !row.resolved)?.message || '请检查格式，或按当前邮箱池类型导入。'}
            action={(
              <Button
                size="small"
                type={manualFallback ? 'primary' : 'default'}
                onClick={() => setManualFallback((enabled) => !enabled)}
              >
                {manualFallback ? '已启用手动兜底' : '按当前邮箱池类型导入'}
              </Button>
            )}
          />
        ) : null}

        {detectionError ? (
          <Alert
            type="error"
            showIcon
            message="自动识别请求失败"
            description={detectionError}
            action={(
              <Button
                size="small"
                type={manualFallback ? 'primary' : 'default'}
                onClick={() => setManualFallback((enabled) => !enabled)}
              >
                {manualFallback ? '已启用手动兜底' : '按当前邮箱池类型导入'}
              </Button>
            )}
          />
        ) : null}

        {manualFallback ? (
          <Space align="center" wrap>
            <Typography.Text strong>手动兜底类型</Typography.Text>
            <Select
              aria-label="手动导入类型"
              value={selectedType === 'applemail' ? 'applemail' : 'outlook'}
              onChange={handleTypeChange}
              style={{ width: 240 }}
              options={[
                { label: '微软邮箱', value: 'outlook' },
                { label: 'Google / MFA / AppleMail', value: 'applemail' },
              ]}
            />
          </Space>
        ) : null}

        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Button
            danger
            onClick={() => {
              setContent('')
              setFilename('')
              setResult(null)
              setDetection(null)
              setManualFallback(false)
            }}
          >
            清空
          </Button>
          <Space>
            <Button onClick={() => void loadSnapshots()} loading={loadingSnapshot}>
              刷新预览
            </Button>
            <Button
              type="primary"
              onClick={handleImport}
              loading={importing}
              disabled={hasImportContent && !manualFallback && (detecting || !detection?.can_import)}
            >
              确认导入
            </Button>
          </Space>
        </Space>

        {result ? (
          <Alert
            type={result.summary.failed ? 'warning' : 'success'}
            showIcon
            message={buildResultMessage(result)}
            description={result.errors.length ? (
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{result.errors.join('\n')}</pre>
            ) : undefined}
          />
        ) : null}

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Tag color="blue">已导入: {importedCount} 个邮箱</Tag>
          <Tag>微软邮箱 {snapshots.microsoft?.visible_count ?? snapshots.microsoft?.count ?? 0}</Tag>
          <Tag color="purple">Google / MFA / AppleMail {snapshots.applemail?.visible_count ?? snapshots.applemail?.count ?? 0}</Tag>
          {snapshots.applemail?.filename ? (
            <Typography.Text type="secondary">AppleMail 文件: {snapshots.applemail.filename}</Typography.Text>
          ) : null}
          {tableData.length ? (
            <Popconfirm
              title={`确认删除已勾选的 ${selectedRowKeys.length} 个邮箱吗？`}
              okText="批量删除"
              cancelText="取消"
              okButtonProps={{ danger: true, loading: batchDeleting }}
              onConfirm={() => void handleBatchDelete()}
              disabled={!selectedRowKeys.length}
            >
              <Button danger disabled={!selectedRowKeys.length} loading={batchDeleting}>
                批量删除
              </Button>
            </Popconfirm>
          ) : null}
        </div>
        {tableData.length ? (
          <Table
            rowKey="key"
            rowSelection={{
              selectedRowKeys,
              onChange: setSelectedRowKeys,
              getCheckboxProps: item => ({
                disabled: ['claimed', 'leased'].includes(String(item.pool_state || '').toLowerCase()),
              }),
            }}
            columns={columns}
            dataSource={tableData}
            size="small"
            pagination={false}
            scroll={{ y: 320 }}
          />
        ) : (
          <div
            style={{
              border: '1px solid rgba(127,127,127,0.25)',
              borderRadius: 8,
              padding: 12,
              background: 'rgba(127,127,127,0.06)',
              minHeight: 88,
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <Typography.Text type="secondary">
              当前两个邮箱池都没有可预览的已导入账号。
            </Typography.Text>
          </div>
        )}

        {hasTruncatedSnapshot ? (
          <Typography.Text type="secondary">单个邮箱池最多预览前 500 条记录，顶部总数仍为实际导入数量。</Typography.Text>
        ) : null}
      </Space>
    </Card>
  )
}
