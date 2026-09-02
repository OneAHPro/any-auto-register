import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Form,
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
  theme,
} from 'antd'
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloudServerOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'

import { apiFetch } from '@/lib/utils'

const { Paragraph, Text, Title } = Typography

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
  const { token } = theme.useToken()
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

  const load = useCallback(async () => {
    setLoading(true)
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
  }, [])

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

  const columns = [
    {
      title: '目标',
      key: 'target',
      width: 230,
      render: (_value: unknown, record: Codex2APITarget) => (
        <Space direction="vertical" size={2}>
          <Space size={8}>
            <span className={`target-status-dot target-status-dot--${record.health_status}`} />
            <Text strong>{record.name}</Text>
            <Tag bordered={false}>{TYPE_LABELS[record.target_type] || record.target_type}</Tag>
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.server_label || `目标 #${record.id}`}
          </Text>
        </Space>
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
      title: '节点地址',
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
      fixed: 'right' as const,
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
    <div className="control-plane-page page-enter">
      <section className="control-plane-heading">
        <div>
          <Text className="control-plane-eyebrow">CODEX2API FLEET</Text>
          <Title level={2} style={{ margin: '4px 0 6px' }}>目标节点</Title>
          <Paragraph type="secondary" style={{ margin: 0, maxWidth: 720 }}>
            这里登记每一套未修改的 Codex2API。账号身份、额度和归属由本系统统一管理。
          </Paragraph>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load}>刷新</Button>
          <Button icon={<PlusOutlined />} onClick={openCreatePool} disabled={targets.length === 0}>新建号池</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>添加目标</Button>
        </Space>
      </section>

      <Row gutter={[12, 12]} className="control-plane-metrics">
        <Col xs={24} sm={8}>
          <Card size="small" className="control-metric-card">
            <CloudServerOutlined />
            <div><strong>{targets.length}</strong><span>已登记节点</span></div>
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small" className="control-metric-card control-metric-card--healthy">
            <CheckCircleOutlined />
            <div><strong>{healthyCount}</strong><span>迁移就绪</span></div>
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small" className="control-metric-card">
            <ApiOutlined />
            <div><strong>{pools.length}</strong><span>逻辑号池</span></div>
          </Card>
        </Col>
      </Row>

      {targets.some(item => item.health_status === 'degraded') ? (
        <Alert
          showIcon
          type="warning"
          message="异常节点已自动退出迁移候选"
          description="连续两次探测恢复健康后，节点才会重新参与调度。"
        />
      ) : null}

      <Card
        className="control-plane-table-card"
        styles={{ body: { padding: 0 } }}
        title={<Space><span className="control-plane-rail" />节点拓扑</Space>}
      >
        <Table<Codex2APITarget>
          rowKey="id"
          columns={columns}
          dataSource={targets}
          loading={loading}
          pagination={false}
          scroll={{ x: 1220 }}
          locale={{ emptyText: '还没有目标。先添加当前正在使用的 Codex2API。' }}
        />
      </Card>

      <Card title="号池与目标绑定" className="control-plane-table-card">
        {pools.length ? (
          <div className="pool-chip-list">
            {pools.map(pool => (
              <div className="pool-chip" key={pool.id} style={{ borderColor: token.colorBorder }}>
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
      </Card>

      <Modal
        title={editing ? `编辑目标 · ${editing.name}` : '添加 Codex2API 目标'}
        open={editorOpen}
        onCancel={() => setEditorOpen(false)}
        onOk={saveTarget}
        confirmLoading={saving}
        okText="保存目标"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={targetForm} layout="vertical" requiredMark="optional">
          <Row gutter={12}>
            <Col span={14}>
              <Form.Item name="name" label="目标名称" rules={[{ required: true, message: '请输入目标名称' }]}>
                <Input placeholder="例如：美国二号机" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item name="target_type" label="节点类型" rules={[{ required: true }]}>
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
            <Col span={18}>
              <Form.Item name="default_pool_id" label="默认号池">
                <Input placeholder="PUBLIC_POOL" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      <Modal
        title="新建企业号池"
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
            <Col span={12}>
              <Form.Item name="id" label="号池 ID" rules={[{ required: true, pattern: /^[A-Z][A-Z0-9_]{1,63}$/, message: '使用大写字母、数字和下划线' }]}>
                <Input placeholder="ENTERPRISE_A_POOL" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="name" label="显示名称" rules={[{ required: true }]}>
                <Input placeholder="企业 A 号池" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}>
              <Form.Item name="pool_type" label="号池类型" rules={[{ required: true }]}>
                <Select options={Object.entries(TYPE_LABELS).map(([value, label]) => ({ value, label }))} />
              </Form.Item>
            </Col>
            <Col span={16}>
              <Form.Item name="target_id" label="承载目标" rules={[{ required: true }]}>
                <Select options={targets.map(item => ({ value: item.id, label: `${item.name} · #${item.id}` }))} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}><Form.Item name="customer_id" label="客户 ID"><Input placeholder="customer-a" /></Form.Item></Col>
            <Col span={12}><Form.Item name="customer_name" label="客户名称"><Input placeholder="企业 A" /></Form.Item></Col>
          </Row>
          <Form.Item name="remote_api_key_ids" label="Codex2API API Key ID" extra="多个 ID 用逗号分隔；留空表示统计该目标全部 Key。">
            <Input placeholder="11, 12" />
          </Form.Item>
          <Row gutter={12}>
            <Col span={6}><Form.Item name="min_accounts" label="最少账号"><InputNumber min={0} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={6}><Form.Item name="max_accounts" label="最多账号"><InputNumber min={0} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={6}><Form.Item name="safe_concurrency_per_account" label="单号安全并发"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={6}><Form.Item name="min_lease_hours" label="最小租约(时)"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
          </Row>
          <Form.Item name="bandwidth_mbps" label="目标带宽 Mbps">
            <InputNumber min={0} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
