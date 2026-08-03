import { useEffect, useRef, useState } from 'react'
import { Button, InputNumber, message, Popconfirm, Space, Tag } from 'antd'
import { CopyOutlined, FastForwardOutlined, RedoOutlined, StopOutlined } from '@ant-design/icons'

import { API_BASE, apiFetch, getToken } from '@/lib/utils'

interface TaskLogPanelProps {
  taskId: string
  onDone?: () => void
  mode?: 'register' | 'login' | 'relogin'
}

type TaskTerminalStatus = 'idle' | 'done' | 'partial' | 'failed' | 'stopped'
type TaskDisplayMode = NonNullable<TaskLogPanelProps['mode']> | 'remote_auth_monitor'

const CHATGPT_RETRY_MAX_CONCURRENCY = 10

interface RegisterSummary {
  success: number
  registered: number
  total: number
}

function parseCounter(value: unknown): number {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n < 0) return 0
  return Math.floor(n)
}

function normalizeSummary(next: RegisterSummary): RegisterSummary {
  const success = parseCounter(next.success)
  const registered = Math.max(parseCounter(next.registered), success)
  const total = Math.max(parseCounter(next.total), registered)
  return { success, registered, total }
}

function mergeSummary(previous: RegisterSummary, incoming: Partial<RegisterSummary>): RegisterSummary {
  return normalizeSummary({
    success: incoming.success ?? previous.success,
    registered: incoming.registered ?? previous.registered,
    total: incoming.total ?? previous.total,
  })
}

function resolveTerminalStatus(
  reportedStatus: string | undefined,
  summary: RegisterSummary,
): TaskTerminalStatus {
  if (reportedStatus === 'failed') return 'failed'
  if (reportedStatus === 'stopped') return 'stopped'
  if (reportedStatus !== 'done') return 'idle'
  if (summary.success === 0) return 'failed'
  if (summary.total > 0 && summary.success < summary.total) return 'partial'
  return 'done'
}

export function TaskLogPanel({ taskId, onDone, mode = 'register' }: TaskLogPanelProps) {
  const [activeTaskId, setActiveTaskId] = useState(taskId)
  const [resolvedMode, setResolvedMode] = useState<TaskDisplayMode>(mode)
  const [lines, setLines] = useState<string[]>([])
  const [summary, setSummary] = useState<RegisterSummary>({ success: 0, registered: 0, total: 0 })
  const [error, setError] = useState('')
  const [terminalStatus, setTerminalStatus] = useState<TaskTerminalStatus>('idle')
  const [skipLoading, setSkipLoading] = useState(false)
  const [stopLoading, setStopLoading] = useState(false)
  const [retryLoading, setRetryLoading] = useState(false)
  const [retryableCount, setRetryableCount] = useState(0)
  const [retryConcurrency, setRetryConcurrency] = useState(1)
  const [stopRequested, setStopRequested] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const onDoneRef = useRef(onDone)
  const nextSinceRef = useRef(0)

  const isFinished = terminalStatus !== 'idle' || stopRequested
  const wording = resolvedMode === 'remote_auth_monitor'
    ? {
        action: '探针检查',
        success: '探针正常',
        processed: '已检查',
        total: '检查总数',
        completed: '探针检查完成',
      }
    : resolvedMode === 'relogin'
      ? {
          action: '重登',
          success: '重登成功',
          processed: '已处理',
          total: '处理总数',
          completed: '重登完成',
        }
      : resolvedMode === 'login'
        ? {
            action: '登录',
            success: '登录成功',
            processed: '已处理',
            total: '登录总数',
            completed: '登录完成',
          }
        : {
            action: '注册',
            success: '注册成功',
            processed: '已注册',
            total: '总共注册',
            completed: '注册完成',
          }

  const handleCopyAll = async () => {
    try {
      await navigator.clipboard.writeText(lines.join('\n'))
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const handleSkipCurrent = async () => {
    if (isFinished) return
    setSkipLoading(true)
    try {
      const response = await apiFetch(`/tasks/${activeTaskId}/skip-current`, { method: 'POST' }) as {
        control?: { targeted_skip_attempts?: number }
      }
      const targeted = Number(response.control?.targeted_skip_attempts || 0)
      message.success(
        targeted > 1
          ? `已发送跳过 ${targeted} 个进行中账号请求`
          : '已发送跳过当前账号请求',
      )
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setSkipLoading(false)
    }
  }

  const handleStopTask = async () => {
    if (isFinished) return
    setStopLoading(true)
    try {
      await apiFetch(`/tasks/${activeTaskId}/stop`, { method: 'POST' })
      setStopRequested(true)
      message.success('已发送停止任务请求，正在停止进行中的线程')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setStopLoading(false)
    }
  }

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    setActiveTaskId(taskId)
    setResolvedMode(mode)
  }, [mode, taskId])

  useEffect(() => {
    let cancelled = false
    if (terminalStatus === 'idle') {
      setRetryableCount(0)
      return
    }
    const loadRetryable = async () => {
      try {
        const result = await apiFetch(`/tasks/${activeTaskId}/retryable`) as {
          count?: number
        }
        if (!cancelled) setRetryableCount(parseCounter(result.count))
      } catch {
        if (!cancelled) setRetryableCount(0)
      }
    }
    void loadRetryable()
    return () => {
      cancelled = true
    }
  }, [activeTaskId, terminalStatus])

  const handleRetryFailed = async () => {
    if (retryableCount <= 0 || retryLoading) return
    const concurrency = Math.min(
      Math.max(Math.trunc(Number(retryConcurrency) || 1), 1),
      CHATGPT_RETRY_MAX_CONCURRENCY,
      retryableCount,
    )
    setRetryLoading(true)
    try {
      const result = await apiFetch(`/tasks/${activeTaskId}/retry-failed`, {
        method: 'POST',
        body: JSON.stringify({ concurrency }),
      }) as { task_id?: string, retry_count?: number, concurrency?: number }
      const nextTaskId = String(result.task_id || '').trim()
      if (!nextTaskId) throw new Error('重试任务未返回任务 ID')
      const actualConcurrency = parseCounter(result.concurrency) || concurrency
      message.success(`已按原邮箱启动 ${parseCounter(result.retry_count)} 个失败账号重试（并发 ${actualConcurrency}）；接码池任务会重新领取卡密`)
      setRetryableCount(0)
      setRetryConcurrency(1)
      setActiveTaskId(nextTaskId)
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '启动重试失败'
      message.error(detail)
    } finally {
      setRetryLoading(false)
    }
  }

  useEffect(() => {
    if (!activeTaskId) return
    const controller = new AbortController()
    let cancelled = false
    let latestSummary: RegisterSummary = { success: 0, registered: 0, total: 0 }
    const baseRetryMs = 1000
    const maxRetryMs = 8000
    nextSinceRef.current = 0
    setLines([])
    setSummary({ success: 0, registered: 0, total: 0 })
    setError('')
    setTerminalStatus('idle')
    setStopRequested(false)

    const sleep = async (ms: number) =>
      new Promise((resolve) => setTimeout(resolve, ms))

    const initSnapshot = async (): Promise<boolean> => {
      try {
        const snapshot = await apiFetch(`/tasks/${activeTaskId}`) as {
          logs?: string[]
          status?: TaskTerminalStatus | string
          success?: number
          registered?: number
          total?: number
          source?: string
          meta?: { automation?: boolean; mode?: string }
          control?: { stop_requested?: boolean }
        }
        if (cancelled) return true

        const snapshotLines = Array.isArray(snapshot.logs) ? snapshot.logs : []
        setLines(snapshotLines)
        latestSummary = mergeSummary(latestSummary, {
          success: snapshot.success,
          registered: snapshot.registered,
          total: snapshot.total,
        })
        setSummary(latestSummary)
        if (
          snapshot.source === 'schedule'
          && snapshot.meta?.automation === true
          && snapshot.meta?.mode === 'remote_auth_monitor'
        ) {
          setResolvedMode('remote_auth_monitor')
        } else if (snapshot.meta?.mode === 'login' || snapshot.meta?.mode === 'relogin') {
          setResolvedMode(snapshot.meta.mode)
        }
        nextSinceRef.current = snapshotLines.length
        setStopRequested(Boolean(snapshot.control?.stop_requested))

        if (snapshot.status === 'done' || snapshot.status === 'failed' || snapshot.status === 'stopped') {
          setTerminalStatus(resolveTerminalStatus(snapshot.status, latestSummary))
          onDoneRef.current?.()
          return true
        }
      } catch (error_: unknown) {
        if (!cancelled) {
          const detail = error_ instanceof Error ? error_.message : '获取任务快照失败'
          setError(detail)
        }
      }
      return false
    }

    const connectStreamOnce = async (): Promise<boolean> => {
      try {
        const token = getToken()
        const headers: Record<string, string> = {}
        if (token) headers.Authorization = `Bearer ${token}`

        const since = nextSinceRef.current
        const response = await fetch(`${API_BASE}/tasks/${activeTaskId}/logs/stream?since=${since}`, {
          headers,
          signal: controller.signal,
        })

        if (!response.ok) {
          setError(`日志流连接失败 (${response.status})`)
          return true
        }

        if (!response.body) {
          setError('日志流未返回可读数据')
          return false
        }

        setError('')
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (!cancelled) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const parts = buffer.split('\n\n')
          buffer = parts.pop() || ''

          for (const part of parts) {
            const match = part.match(/^data:\s*(.+)$/m)
            if (!match) continue
            try {
              const payload = JSON.parse(match[1]) as {
                line?: string
                done?: boolean
                status?: TaskTerminalStatus
                success?: number
                registered?: number
                total?: number
              }
              latestSummary = mergeSummary(latestSummary, {
                success: payload.success,
                registered: payload.registered,
                total: payload.total,
              })
              setSummary(latestSummary)
              if (payload.line) {
                nextSinceRef.current += 1
                setLines((previous) => [...previous, payload.line!])
              }
              if (payload.done) {
                setTerminalStatus(
                  resolveTerminalStatus(payload.status || 'done', latestSummary),
                )
                onDoneRef.current?.()
                return true
              }
            } catch {
              // ignore malformed SSE payload
            }
          }
        }

        return false
      } catch (error_: unknown) {
        if (!cancelled && !(error_ instanceof DOMException && error_.name === 'AbortError')) {
          return false
        }
        return true
      }
    }

    const connectStream = async () => {
      const shouldStopImmediately = await initSnapshot()
      if (shouldStopImmediately || cancelled) return

      let retryCount = 0
      while (!cancelled) {
        const shouldStop = await connectStreamOnce()
        if (shouldStop || cancelled) return

        retryCount += 1
        const retryMs = Math.min(baseRetryMs * (2 ** (retryCount - 1)), maxRetryMs)
        setError(`日志流连接中断，${retryMs / 1000}s 后重试（第 ${retryCount} 次）`)
        await sleep(retryMs)
      }
    }

    void connectStream()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [activeTaskId])

  useEffect(() => {
    if (!panelRef.current) return
    panelRef.current.scrollTop = panelRef.current.scrollHeight
  }, [lines])

  const footerText =
    terminalStatus === 'done'
      ? { text: wording.completed, color: '#10b981' }
      : terminalStatus === 'partial'
        ? {
            text: `${wording.action}部分完成（成功 ${summary.success} / ${summary.total}）`,
            color: '#d97706',
          }
      : terminalStatus === 'stopped'
        ? { text: '任务已停止', color: '#d97706' }
        : terminalStatus === 'failed'
          ? {
              text: `${wording.action}失败（成功 ${summary.success} / ${summary.total}）`,
              color: '#dc2626',
            }
          : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Space wrap style={{ marginBottom: 8 }}>
        <Tag color="green">{wording.success}：{summary.success}</Tag>
        <Tag color="blue">{wording.processed}：{summary.registered}</Tag>
        <Tag color="default">{wording.total}：{summary.total}</Tag>
      </Space>

      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <Space>
          {terminalStatus !== 'idle' && retryableCount > 0 ? (
            <Popconfirm
              title={`确认重试 ${retryableCount} 个失败账号？`}
              description={(
                <Space size={8}>
                  <span>并发数</span>
                  <InputNumber
                    aria-label="失败账号重试并发数"
                    min={1}
                    max={Math.min(CHATGPT_RETRY_MAX_CONCURRENCY, retryableCount)}
                    precision={0}
                    size="small"
                    value={retryConcurrency}
                    onChange={(value) => {
                      const nextValue = Math.trunc(Number(value) || 1)
                      setRetryConcurrency(Math.min(
                        Math.max(nextValue, 1),
                        CHATGPT_RETRY_MAX_CONCURRENCY,
                        retryableCount,
                      ))
                    }}
                    style={{ width: 72 }}
                  />
                  <span>（最多 {Math.min(CHATGPT_RETRY_MAX_CONCURRENCY, retryableCount)}）</span>
                </Space>
              )}
              onConfirm={handleRetryFailed}
              onOpenChange={(open) => {
                if (open) setRetryConcurrency(1)
              }}
              okText="开始重试"
              cancelText="取消"
              disabled={retryLoading}
            >
              <Button
                size="small"
                type="primary"
                aria-label={`重试失败账号（${retryableCount}）`}
                icon={<RedoOutlined />}
                loading={retryLoading}
              >
                重试失败账号（{retryableCount}）
              </Button>
            </Popconfirm>
          ) : null}
          <Button
            size="small"
            icon={<FastForwardOutlined />}
            onClick={handleSkipCurrent}
            loading={skipLoading}
            disabled={isFinished}
          >
            跳过当前账号
          </Button>
          <Button
            size="small"
            danger
            icon={<StopOutlined />}
            onClick={handleStopTask}
            loading={stopLoading}
            disabled={isFinished}
          >
            停止任务
          </Button>
        </Space>
        <Button size="small" icon={<CopyOutlined />} onClick={handleCopyAll} disabled={lines.length === 0}>
          复制日志
        </Button>
      </div>

      <div
        ref={panelRef}
        className="log-panel"
        style={{
          flex: 1,
          overflowY: 'auto',
          overflowX: 'hidden',
          background: '#ffffff',
          border: '1px solid #e5e7eb',
          borderRadius: 8,
          padding: 12,
          fontFamily: 'monospace',
          fontSize: 12,
          minHeight: 320,
          maxHeight: '65vh',
          userSelect: 'text',
          WebkitUserSelect: 'text',
          cursor: 'text',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {lines.length === 0 && !error && <div style={{ color: '#9ca3af' }}>等待日志...</div>}
        {error && <div style={{ color: '#dc2626' }}>{error}</div>}
        {lines.map((line, index) => (
          <div
            key={index}
            style={{
              lineHeight: 1.5,
              color:
                line.includes('✓') || line.includes('成功')
                  ? '#059669'
                  : line.includes('✗') || line.includes('失败') || line.includes('错误')
                    ? '#dc2626'
                    : line.includes('停止') || line.includes('跳过')
                      ? '#d97706'
                      : '#1f2937',
            }}
          >
            {line}
          </div>
        ))}
      </div>

      {footerText ? (
        <div
          role="status"
          aria-live="polite"
          aria-atomic="true"
          style={{ fontSize: 12, color: footerText.color, marginTop: 8 }}
        >
          {footerText.text}
        </div>
      ) : null}
    </div>
  )
}

export default TaskLogPanel
