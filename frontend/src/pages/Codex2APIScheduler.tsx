import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Modal,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
  theme,
} from 'antd'
import {
  ArrowRightOutlined,
  CheckOutlined,
  ClockCircleOutlined,
  ReloadOutlined,
  SafetyOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'

import { apiFetch } from '@/lib/utils'

const { Paragraph, Text, Title } = Typography

interface PlanAction {
  identity_id: string
  local_account_id: number
  email?: string
  action: string
  source_target_id: number
  destination_target_id: number
  reason: string
}

interface CostEstimate {
  revenue_cny?: string | number
  account_cost_cny?: string | number
  bandwidth_cost_cny?: string | number
  operations_cost_cny?: string | number
  margin_cny?: string | number
}

interface PlanInput {
  forecast_7d_usd?: string | number
  safe_7d_quota?: string | number
  utilization?: string | number
  quota_fresh?: boolean
  target_healthy?: boolean
}

interface SchedulerPlan {
  pool_id: string
  current_count: number
  desired_count: number
  scale_up_count: number
  scale_down_count: number
  executable: boolean
  requires_confirmation: boolean
  blockers: string[]
  actions: PlanAction[]
  input?: PlanInput
  cost_estimated?: boolean
  cost_note?: string
  current_costs?: CostEstimate | null
  desired_costs?: CostEstimate | null
}

interface SchedulerRun {
  id: string
  mode: string
  status: string
  trigger: string
  plan: SchedulerPlan
  executed: Array<Record<string, unknown>>
  errors: Record<string, unknown>
  created_at: string
  completed_at?: string | null
}

interface AccountPool {
  id: string
  name: string
  target_id?: number | null
}

const BLOCKER_LABELS: Record<string, string> = {
  quota_stale: '额度数据已过期',
  target_unhealthy: '目标节点未健康',
  identity_ambiguous: '账号身份需人工确认',
  lease_active: '账号仍在最小租约期',
}

const REASON_LABELS: Record<string, string> = {
  forecast_capacity_required: '预测用量需要扩容',
  sustained_low_utilization: '连续低利用率可缩容',
  manual_assignment: '人工分配',
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error || '请求失败')
}

function formatTime(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function money(value?: string | number): string {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `¥${parsed.toFixed(2)}` : '-'
}

function runStatus(status: string) {
  switch (status) {
    case 'awaiting_confirmation':
      return { color: 'gold', label: '待人工确认' }
    case 'confirmed':
      return { color: 'processing', label: '已确认' }
    case 'queued':
      return { color: 'processing', label: '执行中' }
    case 'completed':
    case 'committed':
      return { color: 'success', label: '已完成' }
    case 'expired':
      return { color: 'default', label: '已过期' }
    case 'failed':
      return { color: 'error', label: '失败' }
    default:
      return { color: 'default', label: status || '未知' }
  }
}

function normalizeRun(value: unknown): SchedulerRun | null {
  if (!value || typeof value !== 'object') return null
  const run = value as Partial<SchedulerRun>
  if (!run.id || !run.plan || typeof run.plan !== 'object') return null
  return {
    id: String(run.id),
    mode: String(run.mode || 'dry_run'),
    status: String(run.status || 'awaiting_confirmation'),
    trigger: String(run.trigger || 'manual'),
    plan: {
      pool_id: String(run.plan.pool_id || ''),
      current_count: Number(run.plan.current_count || 0),
      desired_count: Number(run.plan.desired_count || 0),
      scale_up_count: Number(run.plan.scale_up_count || 0),
      scale_down_count: Number(run.plan.scale_down_count || 0),
      executable: run.plan.executable !== false,
      requires_confirmation: run.plan.requires_confirmation !== false,
      blockers: Array.isArray(run.plan.blockers) ? run.plan.blockers.map(String) : [],
      actions: Array.isArray(run.plan.actions) ? run.plan.actions : [],
      input: run.plan.input,
      cost_estimated: run.plan.cost_estimated === true,
      cost_note: run.plan.cost_note,
      current_costs: run.plan.current_costs,
      desired_costs: run.plan.desired_costs,
    },
    executed: Array.isArray(run.executed) ? run.executed : [],
    errors: run.errors && typeof run.errors === 'object' ? run.errors : {},
    created_at: String(run.created_at || ''),
    completed_at: run.completed_at,
  }
}

export default function Codex2APIScheduler() {
  const { token } = theme.useToken()
  const [run, setRun] = useState<SchedulerRun | null>(null)
  const [runs, setRuns] = useState<SchedulerRun[]>([])
  const [pools, setPools] = useState<AccountPool[]>([])
  const [selectedPool, setSelectedPool] = useState('')
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [applying, setApplying] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [planData, runData, poolData] = await Promise.all([
        apiFetch('/scheduler/plan'),
        apiFetch('/scheduler/runs'),
        apiFetch('/codex2api/pools'),
      ])
      setRun(normalizeRun(planData?.run))
      setRuns(
        (Array.isArray(runData?.runs) ? runData.runs : [])
          .map(normalizeRun)
          .filter((item: SchedulerRun | null): item is SchedulerRun => item !== null),
      )
      setPools(Array.isArray(poolData?.pools) ? poolData.pools : [])
    } catch (error: unknown) {
      message.error(`加载调度计划失败：${errorText(error)}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const generatePlan = async () => {
    setGenerating(true)
    try {
      const result = await apiFetch('/scheduler/plan', {
        method: 'POST',
        body: JSON.stringify({ pool_id: selectedPool }),
      })
      const generated = (Array.isArray(result?.runs) ? result.runs : [])
        .map(normalizeRun)
        .filter((item: SchedulerRun | null): item is SchedulerRun => item !== null)
      if (generated.length) setRun(generated[generated.length - 1])
      message.success('已生成新的预览计划，尚未调整任何账号')
      await load()
    } catch (error: unknown) {
      message.error(`生成计划失败：${errorText(error)}`)
    } finally {
      setGenerating(false)
    }
  }

  const applyPlan = async () => {
    if (!run) return
    setApplying(true)
    try {
      const refreshed = normalizeRun((await apiFetch('/scheduler/plan'))?.run)
      if (!refreshed || refreshed.id !== run.id || refreshed.status !== 'awaiting_confirmation') {
        setRun(refreshed)
        setConfirmOpen(false)
        message.warning('计划已变化或过期，请查看最新计划后重新确认')
        return
      }
      await apiFetch('/scheduler/apply', {
        method: 'POST',
        body: JSON.stringify({ run_id: run.id, confirm: true }),
      })
      setConfirmOpen(false)
      message.success('计划已入队，迁移进度会持久化保存')
      await load()
    } catch (error: unknown) {
      message.error(`执行计划失败：${errorText(error)}`)
    } finally {
      setApplying(false)
    }
  }

  const actionColumns = [
    {
      title: '账号',
      key: 'account',
      render: (_value: unknown, action: PlanAction) => (
        <Space direction="vertical" size={1}>
          <Text>{action.email || `账号 #${action.local_account_id}`}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>{action.identity_id}</Text>
        </Space>
      ),
    },
    {
      title: '动作',
      dataIndex: 'action',
      key: 'action',
      width: 100,
      render: (value: string) => (
        <Tag color={value === 'scale_up' ? 'blue' : 'gold'}>
          {value === 'scale_up' ? '扩容' : '缩容'}
        </Tag>
      ),
    },
    {
      title: '流向',
      key: 'route',
      width: 190,
      render: (_value: unknown, action: PlanAction) => (
        <Space size={6}>
          <Tag>目标 #{action.source_target_id}</Tag>
          <ArrowRightOutlined style={{ color: token.colorTextTertiary }} />
          <Tag color="blue">目标 #{action.destination_target_id}</Tag>
        </Space>
      ),
    },
    {
      title: '原因',
      dataIndex: 'reason',
      key: 'reason',
      render: (value: string) => REASON_LABELS[value] || value || '-',
    },
  ]

  const historyColumns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: formatTime,
    },
    {
      title: '号池',
      key: 'pool',
      render: (_value: unknown, item: SchedulerRun) => item.plan.pool_id || '-',
    },
    {
      title: '规模',
      key: 'capacity',
      width: 120,
      render: (_value: unknown, item: SchedulerRun) => `${item.plan.current_count} → ${item.plan.desired_count}`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 140,
      render: (value: string) => {
        const meta = runStatus(value)
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
  ]

  const canApply = Boolean(
    run
    && run.status === 'awaiting_confirmation'
    && run.plan.executable
    && run.plan.actions.length > 0,
  )
  const actionDirection = run?.plan.scale_up_count
    ? `扩容 ${run.plan.scale_up_count}`
    : run?.plan.scale_down_count
      ? `缩容 ${run.plan.scale_down_count}`
      : '保持现状'
  const utilization = useMemo(() => {
    const value = Number(run?.plan.input?.utilization)
    return Number.isFinite(value) ? Math.round(value * 100) : null
  }, [run])

  return (
    <div className="control-plane-page page-enter">
      <section className="control-plane-heading">
        <div>
          <Text className="control-plane-eyebrow">CAPACITY DESK</Text>
          <Title level={2} style={{ margin: '4px 0 6px' }}>号池调度</Title>
          <Paragraph type="secondary" style={{ margin: 0, maxWidth: 760 }}>
            系统只提出扩缩容建议。每次执行前都会重新读取最新计划，并要求人工再次确认。
          </Paragraph>
        </div>
        <Space wrap>
          <Select
            allowClear
            value={selectedPool || undefined}
            placeholder="全部号池"
            style={{ minWidth: 210 }}
            onChange={value => setSelectedPool(value || '')}
            options={pools.map(pool => ({ value: pool.id, label: `${pool.name} · ${pool.id}` }))}
          />
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load}>刷新</Button>
          <Button type="primary" icon={<ThunderboltOutlined />} loading={generating} onClick={generatePlan}>
            生成预览计划
          </Button>
        </Space>
      </section>

      <Alert
        type="info"
        showIcon
        icon={<SafetyOutlined />}
        message="扩容和缩容均由人工确认"
        description="生成计划不会修改 Codex2API。只有点击“执行计划”并在确认窗口再次确认后，迁移才会入队。"
        style={{ marginBottom: 16 }}
      />

      {run ? (
        <>
          <Card className="scheduler-plan-card" styles={{ body: { padding: 0 } }}>
            <div className="scheduler-plan-strip">
              <div>
                <Text type="secondary">当前计划</Text>
                <Title level={3}>{run.plan.pool_id || '未指定号池'}</Title>
              </div>
              <div className="scheduler-capacity-path" aria-label="账号规模变化">
                <span>当前 {run.plan.current_count}</span>
                <ArrowRightOutlined />
                <strong>建议 {run.plan.desired_count}</strong>
                <Tag color={actionDirection === '保持现状' ? 'default' : 'blue'}>{actionDirection}</Tag>
              </div>
              <div className="scheduler-plan-actions">
                <Tag color={runStatus(run.status).color}>{runStatus(run.status).label}</Tag>
                <Button
                  type="primary"
                  icon={<CheckOutlined />}
                  disabled={!canApply}
                  onClick={() => setConfirmOpen(true)}
                >
                  执行计划
                </Button>
              </div>
            </div>

            <Row gutter={0} className="scheduler-signal-grid">
              <Col xs={24} md={8}>
                <div className="scheduler-signal">
                  <Text type="secondary">预测七日用量</Text>
                  <strong>${Number(run.plan.input?.forecast_7d_usd || 0).toFixed(2)}</strong>
                  <span>安全单号额度 ${Number(run.plan.input?.safe_7d_quota || 0).toFixed(2)}</span>
                </div>
              </Col>
              <Col xs={24} md={8}>
                <div className="scheduler-signal">
                  <Text type="secondary">容量利用率</Text>
                  <strong>{utilization === null ? '待采集' : `${utilization}%`}</strong>
                  <span>{run.plan.input?.quota_fresh === false ? '额度数据过期' : '额度数据可用'}</span>
                </div>
              </Col>
              <Col xs={24} md={8}>
                <div className="scheduler-signal">
                  <Text type="secondary">成本与毛利</Text>
                  {run.plan.cost_estimated && run.plan.desired_costs ? (
                    <>
                      <strong>{money(run.plan.desired_costs.margin_cny)}</strong>
                      <span>预计毛利 · 账号租金 {money(run.plan.desired_costs.account_cost_cny)}</span>
                    </>
                  ) : (
                    <>
                      <strong>成本未估算</strong>
                      <span>{run.plan.cost_note || '客户用量或带宽数据尚不完整'}</span>
                    </>
                  )}
                </div>
              </Col>
            </Row>
          </Card>

          {run.plan.blockers.length ? (
            <Alert
              type="warning"
              showIcon
              message="当前计划仅供观察"
              description={run.plan.blockers.map(item => BLOCKER_LABELS[item] || item).join('；')}
              style={{ marginTop: 16 }}
            />
          ) : null}

          <Card title={`计划动作 · ${run.plan.actions.length}`} className="control-plane-table-card" styles={{ body: { padding: 0 } }}>
            <Table<PlanAction>
              rowKey={item => `${item.identity_id}:${item.action}`}
              columns={actionColumns}
              dataSource={run.plan.actions}
              pagination={false}
              scroll={{ x: 760 }}
              locale={{ emptyText: '本轮无需调整账号' }}
            />
          </Card>
        </>
      ) : (
        <Card><Empty description="尚未生成调度计划" /></Card>
      )}

      <Card title={<Space><ClockCircleOutlined />运行记录</Space>} className="control-plane-table-card" styles={{ body: { padding: 0 } }}>
        <Table<SchedulerRun>
          rowKey="id"
          columns={historyColumns}
          dataSource={runs}
          loading={loading}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          scroll={{ x: 620 }}
        />
      </Card>

      <Modal
        title="请确认后执行"
        open={confirmOpen}
        onCancel={() => setConfirmOpen(false)}
        onOk={applyPlan}
        okText="确认执行"
        cancelText="返回检查"
        confirmLoading={applying}
        okButtonProps={{ danger: true }}
      >
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <Alert
            type="warning"
            showIcon
            message={`${run?.plan.pool_id || '该号池'}：${actionDirection}`}
            description={`共 ${run?.plan.actions.length || 0} 个账号动作，执行后会对源目标排空并迁移凭证。`}
          />
          <Text type="secondary">
            确认时系统会再次检查计划版本、额度新鲜度和目标健康状态；检查不通过时不会写入远端。
          </Text>
        </Space>
      </Modal>
    </div>
  )
}
