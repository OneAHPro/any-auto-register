import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Form,
  Input,
  Row,
  Skeleton,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd'
import { ApiOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import type { FormInstance } from 'antd'

import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { apiFetch } from '@/lib/utils'

type LeadBeeConfigFields = {
  leadbee_api_enabled: boolean
  leadbee_api_key: string
  leadbee_api_secret: string
  leadbee_api_product_id: string
}

type LeadBeeCapacity = {
  configuredProductAvailable: boolean | null
  balanceAvailable: number | null
  balanceReserved: number | null
  unitPrice: number | null
  estimatedOrderCapacity: number | null
  currency: string
}

const EMPTY_CONFIG: LeadBeeConfigFields = {
  leadbee_api_enabled: false,
  leadbee_api_key: '',
  leadbee_api_secret: '',
  leadbee_api_product_id: '',
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function readSnapshot(form: FormInstance<LeadBeeConfigFields>): LeadBeeConfigFields {
  return {
    leadbee_api_enabled: parseBooleanConfigValue(form.getFieldValue('leadbee_api_enabled')),
    leadbee_api_key: String(form.getFieldValue('leadbee_api_key') ?? '').trim(),
    leadbee_api_secret: String(form.getFieldValue('leadbee_api_secret') ?? '').trim(),
    leadbee_api_product_id: String(form.getFieldValue('leadbee_api_product_id') ?? '').trim(),
  }
}

function snapshotsMatch(left: LeadBeeConfigFields, right: LeadBeeConfigFields): boolean {
  return left.leadbee_api_enabled === right.leadbee_api_enabled
    && left.leadbee_api_key === right.leadbee_api_key
    && left.leadbee_api_secret === right.leadbee_api_secret
    && left.leadbee_api_product_id === right.leadbee_api_product_id
}

function safeDecimal(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  const text = String(value).trim()
  if (!/^\d+(?:\.\d{1,8})?$/.test(text)) return null
  const parsed = Number(text)
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1_000_000_000
    ? parsed
    : null
}

function safeCapacity(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(String(value ?? '').trim())
  return Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= 1_000_000_000
    ? parsed
    : null
}

function sanitizeCapacity(value: unknown): LeadBeeCapacity | null {
  const data = asRecord(value)
  if (!data || data.ok !== true) return null
  const currencyText = String(data.currency ?? '').trim().toUpperCase()
  return {
    configuredProductAvailable:
      typeof data.configured_product_available === 'boolean'
        ? data.configured_product_available
        : null,
    balanceAvailable: safeDecimal(data.balance_available),
    balanceReserved: safeDecimal(data.balance_reserved),
    unitPrice: safeDecimal(data.unit_price),
    estimatedOrderCapacity: safeCapacity(data.estimated_order_capacity),
    currency: /^[A-Z]{3}$/.test(currencyText) ? currencyText : '',
  }
}

function moneySymbol(currency: string): string {
  if (currency === 'CNY') return '¥'
  if (currency === 'USD') return '$'
  if (currency === 'EUR') return '€'
  return currency ? `${currency} ` : ''
}

function formatMoney(value: number | null, currency: string): string {
  if (value === null) return '暂未获取'
  return `${moneySymbol(currency)}${value.toFixed(2)}`
}

export default function LeadBeeApiSettingsCard() {
  const [form] = Form.useForm<LeadBeeConfigFields>()
  const [loadingConfig, setLoadingConfig] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [capacity, setCapacity] = useState<LeadBeeCapacity | null>(null)
  const [testError, setTestError] = useState(false)
  const [loadError, setLoadError] = useState(false)
  const mountedRef = useRef(true)
  const loadGenerationRef = useRef(0)
  const testGenerationRef = useRef(0)
  const observedSnapshotRef = useRef<LeadBeeConfigFields | null>(null)
  const watchedEnabled = Form.useWatch('leadbee_api_enabled', form)
  const watchedApiKey = Form.useWatch('leadbee_api_key', form)
  const watchedApiSecret = Form.useWatch('leadbee_api_secret', form)
  const watchedProductId = Form.useWatch('leadbee_api_product_id', form)

  const invalidateTest = useCallback(() => {
    testGenerationRef.current += 1
    setTesting(false)
    setCapacity(null)
    setTestError(false)
  }, [])

  const testConnection = useCallback(async (providedSnapshot?: LeadBeeConfigFields) => {
    const snapshot = providedSnapshot ?? readSnapshot(form)
    const requestGeneration = ++testGenerationRef.current
    const ownsLatestRequest = () => (
      mountedRef.current && testGenerationRef.current === requestGeneration
    )
    const canCommitResponse = () => (
      ownsLatestRequest() && snapshotsMatch(snapshot, readSnapshot(form))
    )

    setTesting(true)
    setCapacity(null)
    setTestError(false)
    try {
      const response = await apiFetch('/config/leadbee/test', {
        method: 'POST',
        body: JSON.stringify({ data: snapshot }),
      })
      if (!canCommitResponse()) return
      const nextCapacity = sanitizeCapacity(response)
      if (!nextCapacity) throw new Error('invalid capacity response')
      setCapacity(nextCapacity)
    } catch {
      if (canCommitResponse()) setTestError(true)
    } finally {
      if (ownsLatestRequest()) setTesting(false)
    }
  }, [form])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      loadGenerationRef.current += 1
      testGenerationRef.current += 1
    }
  }, [])

  useEffect(() => {
    const snapshot = readSnapshot(form)
    const previousSnapshot = observedSnapshotRef.current
    observedSnapshotRef.current = snapshot
    if (!previousSnapshot || snapshotsMatch(previousSnapshot, snapshot)) return

    const invalidationGeneration = ++testGenerationRef.current
    const timer = window.setTimeout(() => {
      if (!mountedRef.current || testGenerationRef.current !== invalidationGeneration) return
      setTesting(false)
      setCapacity(null)
      setTestError(false)
    }, 0)
    return () => window.clearTimeout(timer)
  }, [form, watchedEnabled, watchedApiKey, watchedApiSecret, watchedProductId])

  useEffect(() => {
    const loadGeneration = ++loadGenerationRef.current
    void apiFetch('/config')
      .then((response) => {
        if (!mountedRef.current || loadGenerationRef.current !== loadGeneration) return
        const data = asRecord(response) ?? {}
        const snapshot: LeadBeeConfigFields = {
          leadbee_api_enabled: parseBooleanConfigValue(data.leadbee_api_enabled),
          leadbee_api_key: '',
          leadbee_api_secret: '',
          leadbee_api_product_id: String(data.leadbee_api_product_id ?? '').trim(),
        }
        observedSnapshotRef.current = snapshot
        form.setFieldsValue(snapshot)
        setLoadingConfig(false)
        if (snapshot.leadbee_api_enabled && snapshot.leadbee_api_product_id) {
          window.setTimeout(() => {
            if (mountedRef.current && loadGenerationRef.current === loadGeneration) {
              void testConnection(snapshot)
            }
          }, 0)
        }
      })
      .catch(() => {
        if (!mountedRef.current || loadGenerationRef.current !== loadGeneration) return
        observedSnapshotRef.current = EMPTY_CONFIG
        form.setFieldsValue(EMPTY_CONFIG)
        setLoadError(true)
        setLoadingConfig(false)
      })
  }, [form, testConnection])

  const save = async () => {
    const snapshot = readSnapshot(form)
    invalidateTest()
    setSaving(true)
    try {
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({ data: snapshot }),
      })
      if (!mountedRef.current) return
      const blankedSnapshot = {
        ...snapshot,
        leadbee_api_key: '',
        leadbee_api_secret: '',
      }
      observedSnapshotRef.current = blankedSnapshot
      form.setFieldsValue(blankedSnapshot)
      message.success('LeadBee API 配置已保存')
      if (blankedSnapshot.leadbee_api_enabled && blankedSnapshot.leadbee_api_product_id) {
        window.setTimeout(() => {
          if (mountedRef.current) void testConnection(blankedSnapshot)
        }, 0)
      }
    } catch {
      if (mountedRef.current) message.error('LeadBee API 配置保存失败')
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }

  const enabled = parseBooleanConfigValue(watchedEnabled)
  const balanceAvailable = capacity
    ? formatMoney(capacity.balanceAvailable, capacity.currency)
    : '暂未获取'
  const balanceReserved = capacity
    ? formatMoney(capacity.balanceReserved, capacity.currency)
    : '暂未获取'
  const unitPrice = capacity
    ? formatMoney(capacity.unitPrice, capacity.currency)
    : '暂未获取'

  return (
    <Card
      title={(
        <Space size={8}>
          <ApiOutlined />
          <span>LeadBee API 接码</span>
        </Space>
      )}
      extra={enabled ? <Tag color="success">已启用</Tag> : <Tag>未启用</Tag>}
    >
      {loadingConfig ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : (
        <Form form={form} layout="vertical" initialValues={EMPTY_CONFIG}>
          <Alert
            type="info"
            showIcon
            message="API 优先，余额不足时可自动切换卡密池"
            description="凭证仅提交到服务端。已保存凭证不会回显，留空保存会保留原值。"
            style={{ marginBottom: 16 }}
          />
          {loadError ? (
            <Alert
              type="error"
              showIcon
              message="LeadBee API 配置读取失败"
              description="请刷新页面后重试。"
              style={{ marginBottom: 16 }}
            />
          ) : null}
          <Form.Item
            name="leadbee_api_enabled"
            label="启用 LeadBee Open API"
            valuePropName="checked"
          >
            <Switch
              checkedChildren="开启"
              unCheckedChildren="关闭"
              onChange={invalidateTest}
            />
          </Form.Item>
          <Row gutter={16}>
            <Col xs={24} lg={12}>
              <Form.Item name="leadbee_api_key" label="LeadBee API Key">
                <Input.Password
                  placeholder="留空则保留已保存值"
                  autoComplete="new-password"
                  onChange={invalidateTest}
                />
              </Form.Item>
            </Col>
            <Col xs={24} lg={12}>
              <Form.Item name="leadbee_api_secret" label="LeadBee API Secret">
                <Input.Password
                  placeholder="留空则保留已保存值"
                  autoComplete="new-password"
                  onChange={invalidateTest}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="leadbee_api_product_id" label="LeadBee 产品 ID">
            <Input placeholder="例如 prod-1" onChange={invalidateTest} />
          </Form.Item>

          {capacity ? (
            <div role="status" aria-label="LeadBee API 连接成功" aria-live="polite">
              <Alert
                type={capacity.configuredProductAvailable === false ? 'warning' : 'success'}
                showIcon
                message={capacity.configuredProductAvailable === false
                  ? '当前产品暂不可用'
                  : 'API 接码可用'}
                description={(
                  <Space wrap size={[6, 8]} style={{ marginTop: 4 }}>
                    <Tag color="blue">API 可用余额 {balanceAvailable}</Tag>
                    <Tag>已占用 {balanceReserved}</Tag>
                    <Tag>单价 {unitPrice}/次</Tag>
                    <Tag color="success">
                      预计可接 {capacity.estimatedOrderCapacity ?? '暂未获取'} 次
                    </Tag>
                  </Space>
                )}
                style={{ marginBottom: 16 }}
              />
            </div>
          ) : (
            <Alert
              type={testError ? 'error' : 'warning'}
              showIcon
              message={testError ? 'LeadBee 连接测试失败' : '余额暂未获取'}
              description={testError
                ? '请检查 API 凭证和产品 ID。'
                : enabled
                  ? '点击刷新余额获取可用次数。'
                  : '启用 API 并保存配置后可查看余额。'}
              style={{ marginBottom: 16 }}
            />
          )}

          <Space wrap>
            <Button
              type="primary"
              aria-label="保存 API 配置"
              icon={<SaveOutlined />}
              loading={saving}
              disabled={loadError}
              onClick={() => { void save() }}
            >
              保存 API 配置
            </Button>
            <Button
              aria-label="刷新余额"
              icon={<ReloadOutlined />}
              loading={testing}
              disabled={loadingConfig || loadError}
              onClick={() => { void testConnection() }}
            >
              刷新余额
            </Button>
            <Typography.Text type="secondary">
              每分钟最多创建 60 个订单，同时处理 50 个 API 订单。
            </Typography.Text>
          </Space>
        </Form>
      )}
    </Card>
  )
}
