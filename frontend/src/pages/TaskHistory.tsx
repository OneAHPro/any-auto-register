import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Empty, Popconfirm, Select, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { ConsolePageHeader } from '@/components/console/ConsolePageHeader'
import './operations-workspace.css'

const { Text } = Typography
interface TaskLogItem { id: number; created_at: string; platform: string; email: string; status: string; error: string }
interface TaskLogListResponse { total: number; items: TaskLogItem[] }
interface TaskLogBatchDeleteResponse { deleted: number; not_found: number[]; total_requested: number }
const TASK_STATUS_PRESENTATION: Record<string, { label: string; color?: string }> = {
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '已跳过' },
  removed: { label: '已删除', color: 'warning' },
}
const PAGE_SIZE = 20
const formatTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '未记录'

export default function TaskHistory() {
  const [logs, setLogs] = useState<TaskLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [resultFilter, setResultFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [detail, setDetail] = useState<TaskLogItem | null>(null)
  const requestRef = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE), platform: 'chatgpt' })
      const data = await apiFetch(`/tasks/logs?${params}`, { signal: controller.signal }) as TaskLogListResponse
      if (controller.signal.aborted) return
      const items = data.items || []
      const nextTotal = data.total || 0
      const lastPage = Math.max(1, Math.ceil(nextTotal / PAGE_SIZE))
      if (page > lastPage) { setPage(lastPage); return }
      setLogs(items)
      setTotal(nextTotal)
      setSelectedRowKeys(prev => prev.filter(key => items.some(item => item.id === key)))
      setLoadFailed(false)
    } catch {
      if (!controller.signal.aborted) setLoadFailed(true)
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }, [page])

  useEffect(() => {
    void load()
    return () => requestRef.current?.abort()
  }, [load])

  const handleBatchDelete = async () => {
    if (!selectedRowKeys.length) return
    setDeleting(true)
    try {
      const result = await apiFetch('/tasks/logs/batch-delete', { method: 'POST', body: JSON.stringify({ ids: selectedRowKeys }) }) as TaskLogBatchDeleteResponse
      message.success(`已删除 ${result.deleted} 条任务历史`)
      if (result.not_found.length > 0) message.warning(`${result.not_found.length} 条记录不存在或已被删除`)
      if (detail && selectedRowKeys.includes(detail.id)) setDetail(null)
      setSelectedRowKeys([])
      await load()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '删除失败，请重试')
    } finally { setDeleting(false) }
  }

  const columns: TableColumnsType<TaskLogItem> = [
    { title: '账号', dataIndex: 'email', width: 240, render: (email: string) => <span className="operations-email">{email || '未记录邮箱'}</span> },
    { title: '结果', dataIndex: 'status', width: 104, render: (status: string) => { const presentation = TASK_STATUS_PRESENTATION[status] || { label: status || '未知' }; return <Tag color={presentation.color}>{presentation.label}</Tag> } },
    { title: '结果说明', dataIndex: 'error', width: 300, render: (error: string) => <span className="operations-history-error">{error || '—'}</span> },
    { title: '记录时间', dataIndex: 'created_at', width: 180, render: formatTime },
    { title: '操作', key: 'action', width: 88, render: (_, record) => <Button type="link" onClick={() => setDetail(record)} aria-label={`查看 ${record.email || '账号'} 记录`}>查看</Button> },
  ]
  const visibleLogs = logs.filter(log => !resultFilter || log.status === resultFilter)

  return (
    <div className="console-page operations-page operations-history-page">
      <ConsolePageHeader title="账号记录" description="查看 ChatGPT 账号执行结果与失败原因。" actions={<><Button href="/running-tasks">查看任务日志</Button><Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新记录</Button></>} />
      {loadFailed && <Alert type="warning" showIcon message="记录加载失败，请刷新重试" />}
      <section className="console-panel operations-history-panel" aria-label="账号执行记录">
        <div className="console-toolbar operations-history-toolbar">
          <div className="operations-inline-group"><Text>共 {total} 条记录</Text><Text type="secondary">当前页 {logs.length} 条</Text></div>
          <div className="operations-inline-group"><span className="operations-helper">当前页结果</span><Select aria-label="筛选当前页结果" value={resultFilter} onChange={value => { setResultFilter(value); setSelectedRowKeys([]) }} options={[{ value: '', label: '全部结果' }, { value: 'success', label: '成功' }, { value: 'failed', label: '失败' }, { value: 'skipped', label: '已跳过' }, { value: 'removed', label: '已删除' }]} />
            {!!selectedRowKeys.length && <Popconfirm title={`确认删除选中的 ${selectedRowKeys.length} 条任务历史？`} onConfirm={handleBatchDelete} okText="删除" cancelText="取消" okButtonProps={{ danger: true, loading: deleting }}><Button danger icon={<DeleteOutlined />} loading={deleting}>删除 {selectedRowKeys.length} 条</Button></Popconfirm>}
          </div>
        </div>
        <Table rowKey="id" columns={columns} dataSource={visibleLogs} loading={loading} scroll={{ x: 944 }} rowSelection={{ selectedRowKeys, onChange: keys => setSelectedRowKeys(keys as number[]) }} pagination={{ current: page, total, pageSize: PAGE_SIZE, showSizeChanger: false, onChange: nextPage => { setPage(nextPage); setLogs([]); setLoadFailed(false); setSelectedRowKeys([]) } }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={loadFailed ? '等待重新读取记录' : resultFilter ? '当前页没有符合结果的记录' : '完成账号任务后，执行结果会显示在这里'} /> }} />
      </section>
      <Drawer title="账号记录详情" open={!!detail} onClose={() => setDetail(null)} width="min(560px, 100vw)">
        {detail && <dl className="operations-record-detail"><div><dt>账号</dt><dd className="operations-email">{detail.email || '未记录邮箱'}</dd></div><div><dt>结果</dt><dd>{TASK_STATUS_PRESENTATION[detail.status]?.label || detail.status || '未知'}</dd></div><div><dt>记录时间</dt><dd>{formatTime(detail.created_at)}</dd></div><div><dt>结果说明</dt><dd><pre>{detail.error || '无错误信息'}</pre></dd></div></dl>}
      </Drawer>
    </div>
  )
}
