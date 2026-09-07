import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Col,
  Form,
  Grid,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'

import { apiFetch } from '@/lib/utils'
import { ConsolePageHeader } from '@/components/console/ConsolePageHeader'
import './management-workspace.css'

const { Text } = Typography

interface Codex2APITarget {
  id: number
  name: string
  target_type: string
  server_label: string
  base_url: string
  admin_key: string
  default_pool_id: string
  enabled: boolean
  health_status: string
  health_success_count?: number
  health_failure_count?: number
  capabilities: Record<string, unknown>
  last_health_at?: string | null
  last_sync_at?: string | null
  last_error?: string
  account_count: number
}

interface SalePrice {
  target_id: number
  price_cny_per_usd: string
  effective_at: string
}

function normalizeSalePrice(value: string): string {
  const text = value.trim()
  if (!/^\d+(?:\.\d{1,4})?$/.test(text)) throw new Error('请输入非负售价，最多保留四位小数')
  const [whole, fraction = ''] = text.split('.')
  return `${whole.replace(/^0+(?=\d)/, '')}.${fraction.padEnd(4, '0')}`
}

interface AccountPool {
  id: string
  name: string
  pool_type: string
  customer_id?: string
  target_id?: number | null
  min_accounts?: number
  max_accounts?: number
  safe_concurrency_per_account?: number
}

interface TargetFormValues {
  name: string
  target_type: string
  server_label?: string
  base_url: string
  admin_key?: string
  default_pool_id?: string
  enabled: boolean
}

interface PoolFormValues {
  id: string
  name: string
  pool_type: string
  customer_id?: string
  customer_name?: string
  target_id: number
  remote_api_key_ids?: string
  bandwidth_mbps?: number
  min_accounts?: number
  max_accounts?: number
  safe_concurrency_per_account?: number
  min_lease_hours?: number
}

const TYPE_LABELS: Record<string, string> = {
  public: '公共',
  enterprise: '企业',
  float: '浮动',
  standby: '备用',
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error || '请求失败')
}

function formatTime(value?: string | null): string {
  if (!value) return '尚未执行'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function healthMeta(status: string) {
  switch (status) {
    case 'healthy':
      return { color: 'success', label: '健康' }
    case 'recovering':
      return { color: 'processing', label: '恢复中' }
    case 'degraded':
      return { color: 'error', label: '异常' }
    default:
      return { color: 'default', label: '未检查' }
  }
}

function parseApiKeyIds(value?: string): number[] {
  return Array.from(new Set(
    String(value || '')
      .split(/[\s,，]+/)
      .map(item => Number(item.trim()))
      .filter(item => Number.isSafeInteger(item) && item > 0),
  )).sort((left, right) => left - right)
}

export default function Codex2APITargets() {
  const screens = Grid.useBreakpoint()
  const [targetForm] = Form.useForm<TargetFormValues>()
  const [poolForm] = Form.useForm<PoolFormValues>()
  const [targets, setTargets] = useState<Codex2APITarget[]>([])
  const [pools, setPools] = useState<AccountPool[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [probingId, setProbingId] = useState<number | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [poolOpen, setPoolOpen] = useState(false)
  const [editing, setEditing] = useState<Codex2APITarget | null>(null)
  const [salePrices, setSalePrices] = useState<SalePrice[]>([])
  const [priceLoading, setPriceLoading] = useState(true)
  const [priceLoadError, setPriceLoadError] = useState('')
  const [saleEditing, setSaleEditing] = useState<Codex2APITarget | null>(null)
  const [salePriceInput, setSalePriceInput] = useState('')
  const [priceSaving, setPriceSaving] = useState(false)
  const [priceSaveError, setPriceSaveError] = useState('')
  const priceRequestEpoch = useRef(0)

  const loadSalePrices = useCallback(async () => {
    const epoch = ++priceRequestEpoch.current
    setPriceLoading(true)
    try {
      const response = await apiFetch('/operations/sale-prices')
      if (epoch !== priceRequestEpoch.current) return
      setSalePrices(Array.isArray(response?.items) ? response.items : [])
      setPriceLoadError('')
    } catch (error: unknown) {
      if (epoch === priceRequestEpoch.current) setPriceLoadError(`售价读取失败：${errorText(error)}`)
    } finally {
      if (epoch === priceRequestEpoch.current) setPriceLoading(false)
    }
  }, [])

  const openSalePrice = (target: Codex2APITarget) => {
    setSaleEditing(target)
    setSalePriceInput(salePrices.find(item => item.target_id === target.id)?.price_cny_per_usd ?? '')
    setPriceSaveError('')
  }

  const saveSalePrice = async () => {
    if (!saleEditing) return
    let price: string
    try { price = normalizeSalePrice(salePriceInput) }
    catch (error) { setPriceSaveError(errorText(error)); return }
    setPriceSaving(true)
    setPriceSaveError('')
    try {
      await apiFetch(`/operations/targets/${saleEditing.id}/sale-price`, {
        method: 'PUT', body: JSON.stringify({ price_cny_per_usd: price }),
      })
      setSaleEditing(null)
      message.success('销售单价已保存')
      await loadSalePrices()
    } catch (error: unknown) { setPriceSaveError(`售价保存失败：${errorText(error)}`) }
    finally { setPriceSaving(false) }
  }

  const load = useCallback(async () => {
    setLoading(true)
    void loadSalePrices()
    try {
      const [targetData, poolData] = await Promise.all([
        apiFetch('/codex2api/targets'),
        apiFetch('/codex2api/pools'),
      ])
      setTargets(Array.isArray(targetData?.targets) ? targetData.targets : [])
      setPools(Array.isArray(poolData?.pools) ? poolData.pools : [])
    } catch (error: unknown) {
      message.error(`加载控制面失败：${errorText(error)}`)
    } finally {
      setLoading(false)
    }
  }, [loadSalePrices])

  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const healthyCount = useMemo(
    () => targets.filter(item => item.enabled && item.health_status === 'healthy').length,
    [targets],
  )

  const openCreate = () => {
    setEditing(null)
    targetForm.resetFields()
    targetForm.setFieldsValue({
      target_type: 'enterprise',
      default_pool_id: 'PUBLIC_POOL',
      enabled: true,
    } as TargetFormValues)
    setEditorOpen(true)
  }

  const openEdit = (target: Codex2APITarget) => {
    setEditing(target)
    targetForm.resetFields()
    targetForm.setFieldsValue({
      name: target.name,
      target_type: target.target_type,
      server_label: target.server_label,
      base_url: target.base_url,
      admin_key: '',
      default_pool_id: target.default_pool_id,
      enabled: target.enabled,
    })
    setEditorOpen(true)
  }

  const saveTarget = async () => {
    const values = await targetForm.validateFields()
    const payload: Record<string, unknown> = { ...values }
    if (editing && !String(values.admin_key || '').trim()) delete payload.admin_key
    setSaving(true)
    try {
      await apiFetch(
        editing ? `/codex2api/targets/${editing.id}` : '/codex2api/targets',
        {
          method: editing ? 'PATCH' : 'POST',
          body: JSON.stringify(payload),
        },
      )
      message.success(editing ? '目标已更新' : '目标已添加')
      setEditorOpen(false)
      targetForm.resetFields()
      await load()
    } catch (error: unknown) {
      message.error(`保存目标失败：${errorText(error)}`)
    } finally {
      setSaving(false)
    }
  }

  const probeTarget = async (target: Codex2APITarget) => {
    setProbingId(target.id)
    try {
      await apiFetch(`/codex2api/targets/${target.id}/health`, { method: 'POST' })
      message.success(`${target.name} 健康检查已完成`)
      await load()
    } catch (error: unknown) {
      message.error(`健康检查失败：${errorText(error)}`)
    } finally {
      setProbingId(null)
    }
  }

  const openCreatePool = () => {
    poolForm.resetFields()
    poolForm.setFieldsValue({
      pool_type: 'enterprise',
      target_id: targets[0]?.id,
      min_accounts: 0,
      max_accounts: 0,
      safe_concurrency_per_account: 3,
      min_lease_hours: 6,
      bandwidth_mbps: 0,
    } as PoolFormValues)
    setPoolOpen(true)
  }

  const savePool = async () => {
    const values = await poolForm.validateFields()
    setSaving(true)
    try {
      await apiFetch('/codex2api/pools', {
        method: 'POST',
        body: JSON.stringify({
          ...values,
          id: values.id.trim().toUpperCase(),
          remote_api_key_ids: parseApiKeyIds(values.remote_api_key_ids),
        }),
      })
      message.success('号池已创建')
      setPoolOpen(false)
      poolForm.resetFields()
      await load()
    } catch (error: unknown) {
      message.error(`创建号池失败：${errorText(error)}`)
    } finally {
      setSaving(false)
    }
  }

  const renderSalePrice = (record: Codex2APITarget) => {
    const price = salePrices.find(item => item.target_id === record.id)
    const amount = Number(price?.price_cny_per_usd)
    const label = priceLoading ? '读取售价…' : !price ? '售价未设置'
      : Number.isFinite(amount) && amount >= 0 ? `¥${amount.toFixed(4)}/$` : '售价数据待确认'
    return <Space size={[8, 4]} wrap style={{ marginTop: 8 }}>
      <Text>{label}</Text>
      <Button size="small" onClick={() => openSalePrice(record)} disabled={priceLoading}>设置售价</Button>
    </Space>
  }

  const columns = [
    {
      title: '实例 / 销售单价',
      key: 'target',
      width: 230,
      render: (_value: unknown, record: Codex2APITarget) => (
        <div className="management-instance-identity">
          <div className="management-instance-name">
            <span className={`target-status-dot target-status-dot--${record.health_status}`} />
            <Text strong>{record.name}</Text>
          </div>
          <Space size={[4, 4]} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {record.server_label || `实例 #${record.id}`}
            </Text>
            <Tag bordered={false}>{TYPE_LABELS[record.target_type] || record.target_type}</Tag>
          </Space>
          {renderSalePrice(record)}
        </div>
      ),
    },
    {
      title: '健康度',
      key: 'health',
      width: 130,
      render: (_value: unknown, record: Codex2APITarget) => {
        const meta = healthMeta(record.health_status)
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '连接地址',
      dataIndex: 'base_url',
      key: 'base_url',
      ellipsis: true,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: '能力',
      key: 'capabilities',
      width: 210,
      render: (_value: unknown, record: Codex2APITarget) => (
        <Space size={[4, 4]} wrap>
          {record.capabilities?.migratable === true ? <Tag color="blue">可迁移</Tag> : <Tag>仅观测</Tag>}
          {record.capabilities?.restore === true ? <Tag color="cyan">可恢复</Tag> : null}
          <Tag>{record.account_count} 个账号</Tag>
        </Space>
      ),
    },
    {
      title: '密钥',
      dataIndex: 'admin_key',
      key: 'admin_key',
      width: 110,
      render: (value: string) => <Text code>{value || '未设置'}</Text>,
    },
    {
      title: '最近探测',
      dataIndex: 'last_health_at',
      key: 'last_health_at',
      width: 180,
      render: formatTime,
    },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      fixed: screens.md ? 'right' as const : undefined,
      render: (_value: unknown, record: Codex2APITarget) => (
        <Space>
          <Button
            size="small"
            icon={<SafetyCertificateOutlined />}
            loading={probingId === record.id}
            onClick={() => probeTarget(record)}
          >
            检查健康
          </Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>
            编辑
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div className="console-page management-workspace">
      <ConsolePageHeader
        title="实例管理"
        description="查看各 Codex2API 实例的连接状态，管理号池归属与接入信息。"
        actions={<Space wrap>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load}>刷新</Button>
          <Button icon={<PlusOutlined />} onClick={openCreatePool} disabled={targets.length === 0}>新建号池</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>添加实例</Button>
        </Space>}
      />
      <dl className="management-summary">
        <div><dt>已接入实例</dt><dd>{targets.length}</dd></div>
        <div><dt>迁移就绪</dt><dd>{healthyCount}</dd></div>
        <div><dt>逻辑号池</dt><dd>{pools.length}</dd></div>
      </dl>

      {priceLoadError && <Alert type="warning" showIcon message={priceLoadError} action={<Button onClick={() => void loadSalePrices()}>重试读取售价</Button>} />}

      {targets.some(item => item.health_status === 'degraded') ? (
        <Alert
          showIcon
          type="warning"
          message="异常节点已自动退出迁移候选"
          description="连续两次探测恢复健康后，节点才会重新参与调度。"
        />
      ) : null}

      <section className="console-panel management-table-section" aria-labelledby="instances-heading">
        <div className="console-section-heading"><h2 id="instances-heading">实例列表</h2><span>{screens.md ? '连接、健康与账号承载情况' : '左右滑动查看连接信息和操作'}</span></div>
        <Table<Codex2APITarget>
          rowKey="id"
          columns={columns}
          dataSource={targets}
          loading={loading}
          pagination={false}
          scroll={{ x: 1220 }}
          locale={{ emptyText: '尚未接入实例。添加正在使用的 Codex2API 后，即可查看连接与账号状态。' }}
        />
      </section>

      <Modal
        title={saleEditing ? `设置 ${saleEditing.name} 的销售单价` : '设置销售单价'}
        open={Boolean(saleEditing)}
        onCancel={() => { if (!priceSaving) setSaleEditing(null) }}
        onOk={() => void saveSalePrice()}
        confirmLoading={priceSaving}
        okText="保存售价"
        cancelText="取消"
        maskClosable={false}
        destroyOnHidden
      >
        <label htmlFor="instance-sale-price">销售单价（元/美元）</label>
        <Input id="instance-sale-price" aria-label="销售单价（元/美元）" inputMode="decimal" value={salePriceInput} onChange={event => setSalePriceInput(event.target.value)} placeholder="例如 0.2400" disabled={priceSaving} style={{ margin: '8px 0' }} />
        <Text type="secondary" style={{ fontSize: 12 }}>每 1 美元计费对应的人民币售价，仅对当前实例生效。请输入实际售价，0 表示零售价。</Text>
        {priceSaveError && <Alert type="error" showIcon message={priceSaveError} style={{ marginTop: 12 }} />}
      </Modal>

      <section className="console-panel management-pool-section" aria-labelledby="pools-heading">
        <div className="console-section-heading"><h2 id="pools-heading">号池归属</h2><span>每套实例的保底规模与容量上限</span></div>
        {pools.length ? (
          <div className="management-pool-list">
            {pools.map(pool => (
              <div className="management-pool-row" key={pool.id}>
                <div>
                  <Text strong>{pool.name}</Text>
                  <Text type="secondary">{pool.id}</Text>
                </div>
                <Space wrap>
                  <Tag>{TYPE_LABELS[pool.pool_type] || pool.pool_type}</Tag>
                  <Tag color={pool.target_id ? 'blue' : 'default'}>
                    {pool.target_id ? `目标 #${pool.target_id}` : '未绑定目标'}
                  </Tag>
                  <Text type="secondary">
                    {pool.min_accounts || 0}–{pool.max_accounts || '∞'} 个账号
                  </Text>
                </Space>
              </div>
            ))}
          </div>
        ) : (
          <Text type="secondary">尚未建立企业号池。公共池、浮动池和备用池会在服务初始化后自动创建。</Text>
        )}
      </section>

      <Modal
        className="management-dialog"
        title={editing ? `编辑实例 · ${editing.name}` : '添加 Codex2API 实例'}
        open={editorOpen}
        onCancel={() => setEditorOpen(false)}
        onOk={saveTarget}
        confirmLoading={saving}
        okText="保存实例"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={targetForm} layout="vertical" requiredMark="optional">
          <Row gutter={12}>
            <Col xs={24} sm={14}>
              <Form.Item name="name" label="实例名称" rules={[{ required: true, message: '请输入目标名称' }]}>
                <Input placeholder="例如：美国二号机" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={10}>
              <Form.Item name="target_type" label="实例类型" rules={[{ required: true }]}>
                <Select options={Object.entries(TYPE_LABELS).map(([value, label]) => ({ value, label }))} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="server_label" label="服务器标识">
            <Input placeholder="机房、地域或主机名" />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="Base URL"
            rules={[{ required: true, type: 'url', message: '请输入完整 HTTP(S) 地址' }]}
          >
            <Input placeholder="https://codex2api.example.com" autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="admin_key"
            label="Admin Key"
            extra={editing ? '留空表示保持现有密钥；已保存的密钥不会回填到浏览器。' : '密钥会加密保存，保存后只显示掩码。'}
            rules={editing ? [] : [{ required: true, message: '请输入 Admin Key' }]}
          >
            <Input.Password aria-label="Admin Key" autoComplete="new-password" placeholder={editing ? '留空不修改' : '输入 Admin Key'} />
          </Form.Item>
          <Row gutter={12}>
            <Col xs={24} sm={18}>
              <Form.Item name="default_pool_id" label="默认号池">
                <Input placeholder="PUBLIC_POOL" />
              </Form.Item>
            </Col>
            <Col xs={12} sm={6}>
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      <Modal
        className="management-dialog"
        title="新建号池"
        open={poolOpen}
        onCancel={() => setPoolOpen(false)}
        onOk={savePool}
        confirmLoading={saving}
        okText="创建号池"
        cancelText="取消"
        width={680}
        destroyOnHidden
      >
        <Form form={poolForm} layout="vertical" requiredMark="optional">
          <Row gutter={12}>
            <Col xs={24} sm={12}>
              <Form.Item name="id" label="号池 ID" rules={[{ required: true, pattern: /^[A-Z][A-Z0-9_]{1,63}$/, message: '使用大写字母、数字和下划线' }]}>
                <Input placeholder="ENTERPRISE_A_POOL" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="name" label="显示名称" rules={[{ required: true }]}>
                <Input placeholder="企业 A 号池" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col xs={24} sm={8}>
              <Form.Item name="pool_type" label="号池类型" rules={[{ required: true }]}>
                <Select options={Object.entries(TYPE_LABELS).map(([value, label]) => ({ value, label }))} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={16}>
              <Form.Item name="target_id" label="承载实例" rules={[{ required: true }]}>
                <Select options={targets.map(item => ({ value: item.id, label: `${item.name} · #${item.id}` }))} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col xs={24} sm={12}><Form.Item name="customer_id" label="客户 ID"><Input placeholder="customer-a" /></Form.Item></Col>
            <Col xs={24} sm={12}><Form.Item name="customer_name" label="客户名称"><Input placeholder="企业 A" /></Form.Item></Col>
          </Row>
          <Form.Item name="remote_api_key_ids" label="Codex2API API Key ID" extra="多个 ID 用逗号分隔；留空表示统计该目标全部 Key。">
            <Input placeholder="11, 12" />
          </Form.Item>
          <Row gutter={12}>
            <Col xs={12} sm={6}><Form.Item name="min_accounts" label="保底账号数"><InputNumber min={0} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} sm={6}><Form.Item name="max_accounts" label="账号数上限"><InputNumber min={0} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} sm={6}><Form.Item name="safe_concurrency_per_account" label="单号安全并发"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} sm={6}><Form.Item name="min_lease_hours" label="最小租约(时)"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
          </Row>
          <Form.Item name="bandwidth_mbps" label="目标带宽 Mbps">
            <InputNumber min={0} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
