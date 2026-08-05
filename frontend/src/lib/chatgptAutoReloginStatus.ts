export interface ChatGPTAutoReloginStatus {
  enabled: boolean
  state: string
  reason?: string | null
  eligible_accounts?: number
  active_task_id?: string | null
  interval_minutes?: number
  concurrency?: number
  next_run_at?: string | null
}

export function formatAutoReloginCountdown(
  status: ChatGPTAutoReloginStatus | null,
  nowMs: number,
): string {
  if (!status) return '--:--'
  if (!status.enabled || status.state === 'disabled') return '自动认证已关闭'
  if (status.state === 'running') return '当前轮运行中'
  if (status.state === 'stopping') return '当前轮正在停止'
  if (status.state === 'paused_no_accounts' || status.reason === 'no_eligible_accounts') {
    return '等待可用账号'
  }
  if (!status.next_run_at) return '--:--'

  const nextRunMs = Date.parse(status.next_run_at)
  if (!Number.isFinite(nextRunMs)) return '--:--'
  const remaining = Math.max(0, Math.ceil((nextRunMs - nowMs) / 1000))
  const hours = Math.floor(remaining / 3600)
  const minutes = Math.floor((remaining % 3600) / 60)
  const seconds = remaining % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return hours > 0
    ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`
}
