import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Empty, Progress, Skeleton, Table, Tag, theme } from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { ConsolePageHeader } from '@/components/console/ConsolePageHeader'
import './operations-workspace.css'

type Amount = string | null
interface OverviewTarget {
  id: number
  name: string
  enabled: boolean
  health_status: string
  total_accounts: number
  normal_accounts: number
  limited_accounts: number
  abnormal_accounts: number
  remaining_percent_avg: number | null
  estimated_remaining_usd: Amount
  today_billed_usd: Amount
  total_billed_usd: Amount
  today_requests: number | null
  price_cny_per_usd: Amount
  today_revenue_cny: Amount
  total_revenue_cny: Amount
  captured_at: string | null
  last_health_at?: string | null
  billing_status: 'available' | 'stale' | 'unavailable'
  error: string | null
}
interface PurchaseBatch {
  id: number | string
  created_at: string | null
  source: string
  expected_count: number
  cost_cny: Amount
  linked_accounts: number
}
interface BillingTrend {
  complete: boolean
  points: { date: string; billed_usd: Amount; revenue_cny: Amount; requests: number | null }[]
}
interface OperationsOverview {
  as_of: string
  timezone: string
  date: string
  coverage: {
    targets_total: number
    targets_available: number
    billing_complete: boolean
    today_complete: boolean
    today_costs_complete?: boolean
    prices_complete: boolean
    costs_complete: boolean
    unknown_cost_accounts: number
    undated_cost_cny: Amount
    errors: string[]
  }
  finance: {
    today_cost_cny: Amount
    total_cost_cny: Amount
    today_billed_usd: Amount
    total_billed_usd: Amount
    today_revenue_cny: Amount
    total_revenue_cny: Amount
    today_profit_cny: Amount
    total_profit_cny: Amount
    break_even_percent: number | null
    remaining_cost_cny: Amount
  }
  supply: { today_purchased_accounts: number; total_purchased_accounts: number; today_added_accounts: number }
  account_status: { total: number; normal: number; scheduling: number; rate_limited: number; abnormal: number; auth_invalid: number; errors: number }
  targets: OverviewTarget[]
  recent_batches: PurchaseBatch[]
  attention: { target_id: number; name: string; reason: string; severity: 'warning' | 'error' }[]
  trend?: BillingTrend
}
interface TaskSummary { id: string; platform: string; status: string; error_count: number }
const REFRESH_INTERVAL_MS = 15_000
const REQUEST_TIMEOUT_MS = 45_000
const healthLabels: Record<string, { label: string; color: string }> = {
  healthy: { label: '健康', color: 'success' }, recovering: { label: '恢复中', color: 'processing' },
  degraded: { label: '异常', color: 'error' }, unknown: { label: '未检查', color: 'default' },
}
const sourceLabels: Record<string, string> = {
  json_import: 'JSON 导入', credential_import: '凭据导入', credentials_import: '凭据导入',
  password_login: '账号登录', task_login: '账号登录', existing_login: '已有账号登录', mail_import: '邮箱导入', manual: '手动录入', legacy: '历史购号记录',
}
function number(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
function count(value: unknown): string {
  const parsed = number(value)
  return parsed === null ? '—' : Math.max(0, Math.trunc(parsed)).toLocaleString('en-US')
}
function money(value: unknown, currency: 'CNY' | 'USD' = 'CNY'): string {
  const parsed = number(value)
  if (parsed === null) return '—'
  const prefix = `${parsed < 0 ? '-' : ''}${currency === 'CNY' ? '¥' : '$'}`
  const amount = Math.abs(parsed)
  if (amount > 0 && amount < 0.01) return `${parsed < 0 ? '-' : ''}<${currency === 'CNY' ? '¥' : '$'}0.01`
  return prefix + amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function salePrice(value: Amount): string {
  const parsed = number(value)
  return parsed === null ? '未设置' : `¥${parsed.toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 })}`
}
function time(value: string | null | undefined, short = false): string {
  if (!value) return '未记录'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '未记录'
  return parsed.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', ...(short ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' } : {}) })
}
function MoneyMetric({ label, value, currency = 'CNY', note, signed = false }: { label: string; value: Amount | undefined; currency?: 'CNY' | 'USD'; note?: string; signed?: boolean }) {
  const { token } = theme.useToken()
  const amount = number(value)
  const color = signed && amount !== null && amount !== 0 ? amount > 0 ? token.colorSuccess : token.colorError : undefined
  return <div className="business-metric"><span className="business-metric-label">{label}</span><strong className="business-metric-value" style={{ color }} title={value === null || value === undefined ? undefined : `${currency} ${value}`}>{money(value, currency)}</strong>{note && <span className="business-metric-note">{note}</span>}</div>
}
function MoneyPair({ today, total, currency }: { today: Amount; total: Amount; currency: 'CNY' | 'USD' }) {
  return <div className="business-cell-stack"><span className="business-number-pair" title={today === null ? undefined : `${currency} ${today}`}><span className="business-cell-label">今日</span><span>{money(today, currency)}</span></span><span className="business-number-pair" title={total === null ? undefined : `${currency} ${total}`}><span className="business-cell-label">累计</span><span>{money(total, currency)}</span></span></div>
}

function DailyBillingTrend({ trend }: { trend?: BillingTrend }) {
  const [selectedDate, setSelectedDate] = useState('')
  const points = [...(trend?.points || [])].sort((a, b) => a.date.localeCompare(b.date)).slice(-7)
  const values = points.map(point => number(point.billed_usd)).filter((value): value is number => value !== null)
  const maximum = Math.max(0, ...values)
  const minimum = Math.min(0, ...values)
  const range = maximum - minimum || 1
  const baseline = -minimum / range * 100
  const selected = points.find(point => point.date === selectedDate) || points.at(-1)
  return <section className="console-panel business-trend-panel" aria-label="近 7 日产出趋势">
    <div className="business-trend-heading">
      <div><h2 className="console-section-heading">近 7 日产出趋势</h2>{trend?.complete === false && <Tag color="warning">部分记录</Tag>}<span className="business-cell-label">已计费美元</span></div>
      {selected && <div role="status" aria-label="趋势日期详情" className="business-trend-detail"><span>{selected.date.slice(5).replace('-', '/')}</span><strong title={selected.billed_usd === null ? undefined : `USD ${selected.billed_usd}`}>{number(selected.billed_usd) === null ? '账单未获取' : money(selected.billed_usd, 'USD')}</strong><span>预计 {money(selected.revenue_cny)}</span><span>{count(selected.requests)} 次请求</span></div>}
    </div>
    {points.length ? <div className="business-trend-chart">
      <div className="business-trend-axis" aria-hidden="true"><span>{values.length ? money(maximum, 'USD') : '—'}</span><span>{values.length ? money(minimum, 'USD') : '—'}</span></div>
      <div className="business-trend-plot">
        <div className="business-trend-grid" aria-hidden="true"><span /><span /><span /></div>
        <ol className="business-trend-days" aria-label="每日已计费美元" style={{ gridTemplateColumns: `repeat(${points.length}, minmax(0, 1fr))` }}>
          {points.map(point => {
            const value = number(point.billed_usd)
            const label = value === null ? `${point.date}，已计费美元未获取` : `${point.date}，已计费 ${money(point.billed_usd, 'USD')}`
            return <li key={point.date}><button type="button" className={`business-trend-day${selected?.date === point.date ? ' is-selected' : ''}`} aria-label={label} aria-pressed={selected?.date === point.date} onFocus={() => setSelectedDate(point.date)} onMouseEnter={() => setSelectedDate(point.date)} onClick={() => setSelectedDate(point.date)}>
              <span className="business-trend-bar-area" aria-hidden="true">{value === null ? <span className="business-trend-gap">—</span> : <span className={`business-trend-bar${value === 0 ? ' is-zero' : ''}`} style={{ height: value === 0 ? 2 : `${Math.abs(value) / range * 100}%`, bottom: `${value >= 0 ? baseline : (value - minimum) / range * 100}%` }} />}</span>
              <span className="business-trend-date" aria-hidden="true">{point.date.slice(5).replace('-', '/')}</span>
            </button></li>
          })}
        </ol>
      </div>
    </div> : <div className="business-trend-empty">尚未获取每日账单，已有数据读取后将在这里显示。</div>}
  </section>
}

export default function Dashboard() {
  const [overview, setOverview] = useState<OperationsOverview | null>(null)
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [overviewFailed, setOverviewFailed] = useState(false)
  const [tasksFailed, setTasksFailed] = useState(false)
  const requestRef = useRef<AbortController | null>(null)

  const load = useCallback(async (refresh = false) => {
    if (requestRef.current) return
    const controller = new AbortController()
    requestRef.current = controller
    setLoading(true)
    let timeoutId: ReturnType<typeof setTimeout> | undefined
    let overviewSettled = false
    let tasksSettled = false
    const ownsRequest = () => requestRef.current === controller && !controller.signal.aborted
    const overviewRequest = apiFetch(`/operations/overview?refresh=${refresh ? 1 : 0}`, { signal: controller.signal })
      .then((data: OperationsOverview) => {
        if (!data?.finance || !data.coverage || !Array.isArray(data.targets)) throw new Error('invalid overview response')
        if (ownsRequest()) { setOverview(data); setOverviewFailed(false) }
      })
      .catch(() => { if (ownsRequest()) setOverviewFailed(true) })
      .finally(() => { overviewSettled = true })
    const tasksRequest = apiFetch('/tasks/summary', { signal: controller.signal })
      .then((data: TaskSummary[]) => {
        if (!Array.isArray(data)) throw new Error('invalid task summary response')
        if (ownsRequest()) { setTasks(data.filter(task => task.platform === 'chatgpt')); setTasksFailed(false) }
      })
      .catch(() => { if (ownsRequest()) setTasksFailed(true) })
      .finally(() => { tasksSettled = true })
    try {
      await Promise.race([
        Promise.allSettled([overviewRequest, tasksRequest]),
        new Promise<void>(resolve => {
          timeoutId = setTimeout(() => {
            if (ownsRequest()) {
              if (!overviewSettled) setOverviewFailed(true)
              if (!tasksSettled) setTasksFailed(true)
              controller.abort()
            }
            resolve()
          }, REQUEST_TIMEOUT_MS)
        }),
      ])
    } finally {
      if (timeoutId !== undefined) clearTimeout(timeoutId)
      if (requestRef.current === controller) { requestRef.current = null; setLoading(false) }
    }
  }, [])

  useEffect(() => {
    void load()
    const interval = setInterval(() => void load(), REFRESH_INTERVAL_MS)
    return () => {
      clearInterval(interval)
      const controller = requestRef.current
      requestRef.current = null
      controller?.abort()
    }
  }, [load])

  const coverage = overview?.coverage
  const finance = overview?.finance
  const accountStatus = overview?.account_status
  const supply = overview?.supply
  const costsIncomplete = coverage?.costs_complete === false
  const costDatesIncomplete = coverage?.today_costs_complete === false || (number(coverage?.undated_cost_cny) ?? 0) > 0
  const todayCostNote = costDatesIncomplete && (number(coverage?.undated_cost_cny) ?? 0) > 0
    ? `另有 ${money(coverage?.undated_cost_cny)} 成本日期待核实`
    : costsIncomplete ? '部分账号尚未登记成本' : '按购号成本登记日期统计'
  const todayProfitNote = costsIncomplete ? '待录齐成本' : costDatesIncomplete ? '待核实购买日期' : coverage?.prices_complete === false ? '待补齐实例售价' : '预计销售额减今日购号支出'
  const todayIncomeNote = coverage?.prices_complete === false ? '部分实例未设置售价' : coverage?.today_complete === false ? '部分统计' : '按实例当前售价估算'
  const incomeNote = coverage?.prices_complete === false ? '部分实例未设置售价' : coverage?.billing_complete === false ? '部分统计' : '按实例当前售价估算'
  const profitNote = costsIncomplete ? '待录齐成本' : coverage?.prices_complete === false ? '待补齐实例售价' : '仅扣除购号成本'
  const activeTasks = tasks?.filter(task => ['running', 'pending'].includes(task.status)).length
  const attentionTasks = tasks?.filter(task => !['running', 'pending'].includes(task.status) && (task.status === 'failed' || task.error_count > 0)).length
  const recovery = number(finance?.break_even_percent)

  const targetColumns: TableColumnsType<OverviewTarget> = [
    { title: '实例', key: 'name', width: 160, render: (_, target) => {
      const health = target.enabled ? healthLabels[target.health_status] || healthLabels.unknown : { label: '已停用', color: 'default' }
      return <div className="business-cell-stack"><span className="business-instance-name">{target.name}</span><Tag color={health.color}>{health.label}</Tag></div>
    } },
    { title: '账号可用情况', key: 'accounts', width: 124, render: (_, target) => <div className="business-cell-stack"><span className="business-number-pair"><span className="business-cell-label">正常 / 总数</span><span>{count(target.normal_accounts)} / {count(target.total_accounts)}</span></span><span className="business-cell-label">受限 {count(target.limited_accounts)} · 异常 {count(target.abnormal_accounts)}</span></div> },
    { title: '剩余额度', key: 'remaining', width: 132, render: (_, target) => <div className="business-cell-stack"><span title={target.estimated_remaining_usd === null ? undefined : `USD ${target.estimated_remaining_usd}`}>{money(target.estimated_remaining_usd, 'USD')}</span><span className="business-cell-label">平均余量 {number(target.remaining_percent_avg) === null ? '—' : `${target.remaining_percent_avg!.toFixed(1)}%`}</span></div> },
    { title: '已计费美元', key: 'billed', width: 144, render: (_, target) => <MoneyPair today={target.today_billed_usd} total={target.total_billed_usd} currency="USD" /> },
    { title: '销售单价', key: 'price', width: 110, render: (_, target) => <div className="business-cell-stack"><span>{salePrice(target.price_cny_per_usd)}</span><span className="business-cell-label">每 $1 额度</span></div> },
    { title: '预计销售额', key: 'revenue', width: 144, render: (_, target) => <MoneyPair today={target.today_revenue_cny} total={target.total_revenue_cny} currency="CNY" /> },
    { title: '今日请求', dataIndex: 'today_requests', width: 96, render: count },
    { title: '数据与检查时间', key: 'updated', width: 182, render: (_, target) => <div className="business-cell-stack"><span className="business-cell-label">数据 {time(target.captured_at, true)}</span>{target.last_health_at !== undefined && <span className="business-cell-label">健康 {time(target.last_health_at, true)}</span>}{target.billing_status !== 'available' && <Tag color={target.billing_status === 'stale' ? 'warning' : 'default'}>{target.billing_status === 'stale' ? '缓存过期' : '计费未获取'}</Tag>}{target.error && <span className="business-cell-label">{target.error}</span>}</div> },
  ]
  const batchColumns: TableColumnsType<PurchaseBatch> = [
    { title: '购号批次', key: 'batch', width: 172, render: (_, batch) => <div className="business-cell-stack"><span>{sourceLabels[batch.source] || batch.source || '未注明来源'}</span><span className="business-cell-label">{time(batch.created_at, true)}</span></div> },
    { title: '购号数量', dataIndex: 'expected_count', width: 96, render: count },
    { title: '已关联账号', dataIndex: 'linked_accounts', width: 108, render: count },
    { title: '整批成本', dataIndex: 'cost_cny', width: 108, render: (value: Amount) => money(value) },
  ]

  return (
    <div className="console-page operations-page business-overview">
      <ConsolePageHeader title="运营总览" description="比较实例产出、购号成本与回本进度。" actions={<><Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load(true)}>刷新数据</Button><Button type="primary" href="/supply" icon={<PlusOutlined />}>补充账号</Button></>} />
      {overviewFailed && <Alert type="warning" showIcon message={overview ? '经营数据读取失败，已保留上次结果' : '经营数据读取失败，请刷新重试'} />}
      {loading && !overview ? <section className="console-panel"><Skeleton active paragraph={{ rows: 4 }} /></section> : <div className="business-financial-layout">
        <div>
          <section className="business-money-group" aria-label="今日经营">
            <div className="business-money-heading"><h2>今日经营</h2><span>{overview?.date || '日期未获取'} · 上海时区</span><span>购入 {count(supply?.today_purchased_accounts)} 个 · 补入 {count(supply?.today_added_accounts)} 个</span></div>
            <div className="business-money-metrics">
              <MoneyMetric label="今日已登记成本" value={finance?.today_cost_cny} note={todayCostNote} />
              <MoneyMetric label="今日已计费美元" value={finance?.today_billed_usd} currency="USD" note={coverage?.today_complete === false ? '部分统计' : '实例计费累计'} />
              <MoneyMetric label="今日预计销售额" value={finance?.today_revenue_cny} note={todayIncomeNote} />
              <MoneyMetric label="今日收支差" value={costDatesIncomplete ? null : finance?.today_profit_cny} note={todayProfitNote} signed />
            </div>
          </section>
          <section className="business-money-group" aria-label="累计经营">
            <div className="business-money-heading"><h2>累计经营</h2><span>累计购入 {count(supply?.total_purchased_accounts)} 个账号</span></div>
            <div className="business-money-metrics">
              <MoneyMetric label="累计购号成本" value={finance?.total_cost_cny} note={costsIncomplete ? '已录入成本合计' : '全部已录入购号成本'} />
              <MoneyMetric label="全部累计美元" value={finance?.total_billed_usd} currency="USD" note={coverage?.billing_complete === false ? '部分统计' : '全部时间已计费'} />
              <MoneyMetric label="累计预计销售额" value={finance?.total_revenue_cny} note={incomeNote} />
              <MoneyMetric label="估算购号盈亏" value={finance?.total_profit_cny} note={profitNote} signed />
            </div>
          </section>
        </div>
        <section className="business-recovery" aria-label="购号回本">
          <div><h2>购号回本进度</h2><span className="business-metric-value">{recovery === null ? '—' : `${recovery.toLocaleString('en-US', { maximumFractionDigits: 1 })}%`}</span>{recovery !== null && <Progress percent={Math.max(0, Math.min(100, recovery))} showInfo={false} size="small" status={recovery >= 100 ? 'success' : 'normal'} />}</div>
          <div className="business-recovery-amount"><span>尚差回本</span><strong>{money(finance?.remaining_cost_cny)}</strong></div>
          <div className="business-recovery-amount"><span>累计购号支出</span><strong>{money(finance?.total_cost_cny)}</strong></div>
          <p className="business-metric-note">{costsIncomplete ? '待录齐成本' : '按预计销售额覆盖购号成本计算'}</p>
        </section>
      </div>}

      <section className="business-account-strip" aria-label="账号状态分布">
        {([['账号总数', accountStatus?.total], ['状态正常', accountStatus?.normal], ['额度受限', accountStatus?.rate_limited], ['账号异常', accountStatus?.abnormal], ['调度中', accountStatus?.scheduling], ['鉴权失效', accountStatus?.auth_invalid]] as const).map(([label, value]) => <span className="business-account-stat" key={label}>{label}<strong>{count(value)}</strong></span>)}
        <a href="/accounts/chatgpt">查看账号池</a>
      </section>

      <DailyBillingTrend trend={overview?.trend} />

      <section className="console-panel" aria-labelledby="overview-target-heading">
        <div className="operations-section-top"><div><h2 id="overview-target-heading" className="console-section-heading">实例经营对比</h2><p className="operations-helper">{count(coverage?.targets_available)} / {count(coverage?.targets_total)} 个实例计费可用 · 金额按各实例售价估算</p></div><div className="operations-action-links"><Button href="/codex2api/scheduler">号池调度</Button><Button href="/codex2api/targets">管理实例与售价</Button></div></div>
        <Table className="business-instance-table" rowKey="id" columns={targetColumns} dataSource={overview?.targets || []} loading={loading && !overview} pagination={false} scroll={{ x: 1192 }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={overview ? '尚未添加实例' : '尚未获取实例经营数据'} /> }} />
        <div className="business-table-note"><span>剩余额度为估算值；销售单价单位为人民币 / 美元额度。</span><span>{overview?.as_of ? `统计更新 ${time(overview.as_of)}` : '尚未获取统计时间'}</span></div>
      </section>

      <div className="business-bottom-grid">
        <section className="console-panel" aria-labelledby="overview-batch-heading">
          <div className="operations-section-top"><h2 id="overview-batch-heading" className="console-section-heading">最近购号批次</h2><span className="operations-helper">整批总价与已关联账号</span></div>
          <Table className="business-batches-table" rowKey="id" columns={batchColumns} dataSource={overview?.recent_batches || []} pagination={false} scroll={{ x: 484 }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={overview ? '暂无已记录的购号批次' : '尚未获取购号批次'} /> }} />
          <div className="business-cost-notes"><span>今日购入 {count(supply?.today_purchased_accounts)} 个 · 今日补入 {count(supply?.today_added_accounts)} 个</span>{costsIncomplete && <span>{count(coverage?.unknown_cost_accounts)} 个账号未录购号成本，盈亏与回本待录齐成本后计算。</span>}{(number(coverage?.undated_cost_cny) ?? 0) > 0 && <span>{money(coverage?.undated_cost_cny)} 成本未注明购买日期，仅计入累计成本。</span>}</div>
        </section>
        <section className="console-panel" aria-labelledby="overview-attention-heading">
          <div className="operations-section-top"><h2 id="overview-attention-heading" className="console-section-heading">需要处理</h2><span className="operations-helper">实例异常与低余量</span></div>
          {overview?.attention?.length ? <ul className="business-attention-list">{overview.attention.map((item, index) => <li key={`${item.target_id}-${index}`}><div className="business-attention-title"><span>{item.name}</span><Tag color={item.severity === 'error' ? 'error' : 'warning'}>{item.severity === 'error' ? '异常' : '留意'}</Tag></div><p>{item.reason}</p></li>)}</ul> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={overview ? '当前没有待处理的实例事项' : '尚未获取待处理事项'} />}
          {!!coverage?.errors?.length && <div className="business-cost-notes">{coverage.errors.map((error, index) => <span key={index}>{error}</span>)}</div>}
        </section>
      </div>

      <section className="business-task-strip" aria-label="任务状态"><h2>任务</h2><span>执行中 {count(activeTasks)}</span><span>需要处理 {count(attentionTasks)}</span><span className="operations-helper">保留记录 {count(tasks?.length)}</span>{tasksFailed && <span className="operations-helper">任务状态读取失败{tasks ? '，显示上次结果' : ''}</span>}<a href="/running-tasks">查看任务日志</a><a href="/history">账号记录</a></section>
      <p className="business-data-note">每 15 秒读取缓存；“刷新数据”重新采集。预计销售额按实例当前售价计算，仅扣除购号成本。{coverage?.billing_complete === false || coverage?.today_complete === false ? '当前计费来源不完整，请结合部分统计标记与更新时间查看。' : ''}</p>
    </div>
  )
}
