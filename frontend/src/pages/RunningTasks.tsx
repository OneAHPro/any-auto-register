import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  message,
  Popconfirm,
  Progress,
  Row,
  Space,
  Tag,
  Typography,
} from 'antd'
import {
  DeleteOutlined,
  FileTextOutlined,
  LoadingOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'

const { Text, Title } = Typography
const TASK_SUMMARY_REQUEST_TIMEOUT_MS = 15_000

interface TaskSnapshot {
  id: string
  platform: string
  source: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'stopped'
  total: number
  success: number
  registered: number
  skipped: number
  error_count: number
  created_at: number | string | null
  updated_at: number | string | null
  meta?: {
    automation?: boolean
    invalid_rt_count?: number
    relogin_failed_count?: number
    deleted_account_count?: number
    alert_sent?: boolean
    alert_reason?: string
  }
}

const PLATFORM_LABELS: Record<string, string> = {
  chatgpt: 'ChatGPT',
  cursor: 'Cursor',
  grok: 'Grok',
  kiro: 'Kiro',
  tavily: 'Tavily',
  openblocklabs: 'OpenBlock Labs',
}

const SOURCE_LABELS: Record<string, string> = {
  manual: '手动',
  api: 'API',
  schedule: '调度',
}

const STATUS_CONFIG: Record<string, { color: string; label: string; icon?: React.ReactNode }> = {
  pending: { color: 'default', label: '等待中', icon: <LoadingOutlined /> },
  running: { color: 'processing', label: '运行中', icon: <LoadingOutlined /> },
  done: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
  stopped: { color: 'warning', label: '已停止' },
}

function toUnixSeconds(value: unknown): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed) return null
    const maybeNum = Number(trimmed)
    if (Number.isFinite(maybeNum)) {
      return maybeNum > 1_000_000_000_000 ? maybeNum / 1000 : maybeNum
    }
    const parsed = Date.parse(trimmed)
    if (Number.isFinite(parsed)) return parsed / 1000
    return null
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  // 兼容毫秒时间戳
  return value > 1_000_000_000_000 ? value / 1000 : value
}

function formatDuration(startTs: unknown, endTs?: unknown): string {
  const start = toUnixSeconds(startTs)
  const end = toUnixSeconds(endTs) ?? (Date.now() / 1000)
  if (start === null || !Number.isFinite(end)) return '-'
  const seconds = Math.max(0, Math.floor(end - start))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

export default function RunningTasks() {
  const [tasks, setTasks] = useState<TaskSnapshot[]>([])
  const [loading, setLoading] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now() / 1000)
  const activeRequestRef = useRef<{ controller: AbortController } | null>(null)
  const mountedRef = useRef(false)

  const load = useCallback(async () => {
    if (activeRequestRef.current) return
    const request = { controller: new AbortController() }
    activeRequestRef.current = request
    const ownsRequest = () => activeRequestRef.current === request
    let timeoutId: ReturnType<typeof setTimeout> | undefined
    if (mountedRef.current) setLoading(true)
    try {
      const timeout = new Promise<never>((_resolve, reject) => {
        timeoutId = setTimeout(() => {
          request.controller.abort()
          reject(new Error('task summary request timed out'))
        }, TASK_SUMMARY_REQUEST_TIMEOUT_MS)
      })
      const data = (await Promise.race([
        apiFetch('/tasks/summary', { signal: request.controller.signal }),
        timeout,
      ])) as TaskSnapshot[]
      // sort: running first, then pending, then finished (newest first)
      const sorted = [...(data || [])].sort((a, b) => {
        const oa = a.status === 'running' ? 0 : a.status === 'pending' ? 1 : 2
        const ob = b.status === 'running' ? 0 : b.status === 'pending' ? 1 : 2
        if (oa !== ob) return oa - ob
        const ta = toUnixSeconds(a.created_at) ?? 0
        const tb = toUnixSeconds(b.created_at) ?? 0
        return tb - ta
      })
      if (mountedRef.current && ownsRequest()) {
        setTasks(sorted)
        setLoadFailed(false)
      }
    } catch {
      if (mountedRef.current && ownsRequest()) setLoadFailed(true)
    } finally {
      if (timeoutId !== undefined) clearTimeout(timeoutId)
      if (ownsRequest()) {
        activeRequestRef.current = null
        if (mountedRef.current) setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void load()
    const poll = setInterval(() => {
      void load()
      setNow(Date.now() / 1000)
    }, 2500)
    // tick every second for live duration
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => {
      mountedRef.current = false
      const activeRequest = activeRequestRef.current
      activeRequestRef.current = null
      activeRequest?.controller.abort()
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [load])

  const isActive = (t: TaskSnapshot) => t.status === 'running' || t.status === 'pending'
  const activeTasks = tasks.filter(isActive)
  const finishedTasks = tasks.filter((t) => !isActive(t))

  const handleDelete = async (taskId: string) => {
    try {
      await apiFetch(`/tasks/${taskId}`, { method: 'DELETE' })
      if (logTaskId === taskId) setLogTaskId(null)
      setTasks((prev) => prev.filter((t) => t.id !== taskId))
      message.success('任务已删除')
    } catch (error) {
      const detail = error instanceof Error ? error.message : '删除失败'
      message.error(detail)
    }
  }

  const renderTask = (task: TaskSnapshot) => {
    const cfg = STATUS_CONFIG[task.status] || { color: 'default', label: task.status }
    const failed = Math.max(0, Math.floor(Number(task.error_count) || 0))
    const totalRaw = Number(task.total)
    const doneRaw = Number(task.registered)
    const success = Number.isFinite(Number(task.success)) ? Number(task.success) : 0
    const skipped = Number.isFinite(Number(task.skipped)) ? Number(task.skipped) : 0
    const total = Number.isFinite(totalRaw) && totalRaw > 0 ? Math.floor(totalRaw) : 0
    const done = Number.isFinite(doneRaw) && doneRaw > 0 ? Math.floor(doneRaw) : 0
    const pct = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0
    const isAutomaticAuthentication = Boolean(task.meta?.automation)
    const invalidRtCount = Math.max(0, Number(task.meta?.invalid_rt_count) || 0)
    const reloginFailedCount = Math.max(0, Number(task.meta?.relogin_failed_count) || 0)
    const deletedAccountCount = Math.max(0, Number(task.meta?.deleted_account_count) || 0)

    const duration = isActive(task)
      ? formatDuration(task.created_at, now)
      : formatDuration(task.created_at, task.updated_at)

    return (
      <Card
        key={task.id}
        size="small"
        style={{ marginBottom: 12 }}
        bodyStyle={{ padding: '12px 16px' }}
      >
        <Row gutter={[12, 8]} align="middle" wrap>
          {/* Task ID + platform */}
          <Col flex="220px">
            <Space direction="vertical" size={2}>
              <Text code style={{ fontSize: 11 }}>
                {task.id}
              </Text>
              <Space size={4}>
                <Tag color="blue" style={{ margin: 0 }}>
                  {PLATFORM_LABELS[task.platform] || task.platform}
                </Tag>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {isAutomaticAuthentication
                    ? '自动认证'
                    : SOURCE_LABELS[task.source] || task.source || '-'}
                </Text>
              </Space>
            </Space>
          </Col>

          {/* Status */}
          <Col flex="90px">
            <Badge status={cfg.color as any} text={cfg.label} />
          </Col>

          {/* Duration */}
          <Col flex="70px">
            <Text type="secondary" style={{ fontSize: 12 }}>
              ⏱ {duration}
            </Text>
          </Col>

          {/* Progress bar */}
          <Col flex="1" style={{ minWidth: 160 }}>
            <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Progress
                  percent={pct}
                  size="small"
                status={
                  task.status === 'failed'
                    ? 'exception'
                    : task.status === 'done'
                      ? 'success'
                      : task.status === 'stopped'
                        ? 'exception'
                        : 'active'
                }
                format={() => `${done}/${total}`}
              />
              <Space size={8}>
                <Text style={{ fontSize: 11, color: '#10b981' }}>
                  ✓ 成功 {success}
                </Text>
                {failed > 0 && (
                  <Text style={{ fontSize: 11, color: '#dc2626' }}>
                    ✗ 失败 {failed}
                  </Text>
                )}
                {skipped > 0 && (
                  <Text style={{ fontSize: 11, color: '#d97706' }}>
                    → 跳过 {skipped}
                  </Text>
                )}
                {isAutomaticAuthentication && (
                  <>
                    <Tag color={invalidRtCount > 0 ? 'warning' : 'default'} style={{ margin: 0 }}>
                      鉴权失效 {invalidRtCount}
                    </Tag>
                    <Tag color={reloginFailedCount > 0 ? 'error' : 'default'} style={{ margin: 0 }}>
                      重登失败 {reloginFailedCount}
                    </Tag>
                    <Tag color={deletedAccountCount > 0 ? 'warning' : 'default'} style={{ margin: 0 }}>
                      已删除账号 {deletedAccountCount}
                    </Tag>
                    {task.meta?.alert_sent ? (
                      <Tag color="processing" style={{ margin: 0 }}>邮件已提醒</Tag>
                    ) : task.meta?.alert_reason === 'below_threshold' ? (
                      <Tag style={{ margin: 0 }}>未触发邮件</Tag>
                    ) : null}
                  </>
                )}
              </Space>
            </Space>
          </Col>

          {/* Log button */}
          <Col>
            <Space>
              <Button
                size="small"
                icon={<FileTextOutlined />}
                onClick={() => setLogTaskId(task.id)}
              >
                查看日志
              </Button>
              {!isActive(task) && (
                <Popconfirm
                  title="确认删除该任务记录？"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={() => handleDelete(task.id)}
                >
                  <Button size="small" danger icon={<DeleteOutlined />}>
                    删除
                  </Button>
                </Popconfirm>
              )}
            </Space>
          </Col>
        </Row>
      </Card>
    )
  }

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 16,
        }}
      >
        <Title level={4} style={{ margin: 0 }}>
          任务运行
        </Title>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
          刷新
        </Button>
      </div>

      {loadFailed && (
        <Alert
          type="warning"
          showIcon
          message="任务列表加载失败，正在自动重试"
          style={{ marginBottom: 16 }}
        />
      )}

      {/* Active tasks */}
      {activeTasks.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <Text
            strong
            style={{ display: 'block', marginBottom: 8, fontSize: 13, color: '#6366f1' }}
          >
            进行中 ({activeTasks.length})
          </Text>
          {activeTasks.map(renderTask)}
        </div>
      )}

      {/* Finished tasks */}
      {finishedTasks.length > 0 && (
        <div>
          <Text
            strong
            style={{ display: 'block', marginBottom: 8, fontSize: 13, color: '#6b7280' }}
          >
            已完成 ({finishedTasks.length})
          </Text>
          {finishedTasks.map(renderTask)}
        </div>
      )}

      {tasks.length === 0 && !loading && !loadFailed && (
        <Empty description="暂无任务记录" style={{ marginTop: 60 }} />
      )}

      {/* Log drawer */}
      <Drawer
        title={
          <Space>
            <FileTextOutlined />
            <span>任务日志</span>
            {logTaskId && (
              <Text code style={{ fontSize: 11 }}>
                {logTaskId}
              </Text>
            )}
          </Space>
        }
        open={!!logTaskId}
        onClose={() => setLogTaskId(null)}
        width={720}
        destroyOnClose
        bodyStyle={{ padding: 16 }}
      >
        {logTaskId && <TaskLogPanel taskId={logTaskId} />}
      </Drawer>
    </div>
  )
}
