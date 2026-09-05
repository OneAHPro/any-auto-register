import { Component, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Input, Modal, Progress, Radio, Select, Space, Tag, Typography, Upload, message } from 'antd'
import { FolderOpenOutlined } from '@ant-design/icons'
import type { UploadFile } from 'antd/es/upload/interface'
import { apiFetch } from '@/lib/utils'

export type CodexImportFormat = 'txt' | 'json' | 'json_at' | 'at_txt' | 'auto'
export interface CodexImportTarget { id: string | number; name: string; enabled: boolean }
export interface CodexImportPool { id: string; name: string; targets: CodexImportTarget[] }
interface ImportFile { name: string; content: string }
interface JobItem { index: number; file: string; status: string; email?: string; message?: string }
interface Job { id: string; status: 'queued'|'running'|'completed'|'failed'|'interrupted'; total?: number; processed?: number; success?: number; updated?: number; duplicate?: number; failed?: number; items?: JobItem[]; error?: string }

const choices: Array<{ format: CodexImportFormat; title: string; description: string }> = [
  { format: 'txt', title: 'RT TXT', description: '每行一个 Refresh Token' },
  { format: 'json', title: 'JSON', description: '兼容 CLIProxyAPI / Sub2Api' },
  { format: 'json_at', title: 'JSON AT-only', description: '仅读取 access token，忽略 RT/ST' },
  { format: 'at_txt', title: 'AT TXT', description: '每行一个 Access Token' },
  { format: 'auto', title: '文件夹递归', description: '选择文件夹，递归读取 txt / json' },
  { format: 'auto', title: '粘贴 Session JSON', description: '粘贴后自动识别格式' },
]

function readFile(file: File): Promise<ImportFile> {
  return file.text().then(content => ({ name: file.name, content }))
}

export interface CodexAccountImportModalProps {
  open: boolean
  onClose: () => void
  onLegacyImport?: () => void
  onCompleted?: () => void
}

function CodexAccountImportModalContent({ open, onClose, onLegacyImport, onCompleted }: CodexAccountImportModalProps) {
  const [pools, setPools] = useState<CodexImportPool[]>([])
  const [poolId, setPoolId] = useState('PUBLIC_POOL')
  const [targetId, setTargetId] = useState<string | number>()
  const [format, setFormat] = useState<CodexImportFormat>('txt')
  const [files, setFiles] = useState<ImportFile[]>([])
  const [paste, setPaste] = useState('')
  const [optionsError, setOptionsError] = useState('')
  const [submitError, setSubmitError] = useState('')
  const [job, setJob] = useState<Job | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const jobId = job?.id
  const jobStatus = job?.status
  const inputRef = useRef<HTMLInputElement>(null)
  const selectedPool = pools.find(pool => pool.id === poolId)
  const targets = useMemo(() => (selectedPool?.targets || []).filter(target => target.enabled), [selectedPool])

  useEffect(() => {
    if (!open) return
    setOptionsError('')
    apiFetch('/codex-import/options').then((data) => {
      const nextPools = Array.isArray(data?.pools) ? data.pools : []
      setPools(nextPools)
      const defaultId = String(data?.default_pool_id || 'PUBLIC_POOL')
      setPoolId(nextPools.some((pool: CodexImportPool) => pool.id === defaultId) ? defaultId : (nextPools[0]?.id || defaultId))
    }).catch((error) => setOptionsError(error instanceof Error ? error.message : '加载导入选项失败'))
  }, [open])

  useEffect(() => {
    setTargetId(targets.length === 1 ? targets[0].id : undefined)
  }, [poolId, pools, targets])

  useEffect(() => {
    if (!jobId || !open || ['completed', 'failed', 'interrupted'].includes(jobStatus || '')) return
    let cancelled = false
    const poll = async () => {
      try {
        const next = await apiFetch(`/codex-import/jobs/${encodeURIComponent(jobId)}`) as Job
        if (!cancelled) setJob(next)
      } catch (error) {
        if (!cancelled) setSubmitError(error instanceof Error ? error.message : '读取导入进度失败')
      }
    }
    const timer = window.setInterval(() => { void poll() }, 2000)
    void poll()
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [jobId, jobStatus, open])

  const collectFolder = async (selected: FileList | null) => {
    if (!selected) return
    const candidates = Array.from(selected).filter(file => /\.(txt|json)$/i.test(file.name))
    setFiles(await Promise.all(candidates.map(readFile)))
  }

  const submit = async () => {
    setSubmitError('')
    const payloadFiles = format === 'auto' && paste.trim() ? [{ name: 'session.json', content: paste }] : files
    if (format === 'auto' && !payloadFiles.length) { setSubmitError('请选择文件或粘贴 Session JSON'); return }
    if (format !== 'auto' && !files.length) { setSubmitError('请先选择文件'); return }
    setSubmitting(true)
    try {
      const response = await apiFetch('/codex-import', { method: 'POST', body: JSON.stringify({ pool_id: poolId, ...(targetId !== undefined ? { target_id: targetId } : {}), format, files: payloadFiles }) }) as { job_id: string; status: Job['status'] }
      setFiles([]); setPaste(''); setJob({ id: response.job_id, status: response.status || 'queued' }); onCompleted?.()
    } catch (error) { setSubmitError(error instanceof Error ? error.message : '提交导入任务失败') }
    finally { setSubmitting(false) }
  }

  const percent = job?.total ? Math.min(100, Math.round(((job.processed || 0) / job.total) * 100)) : job?.status === 'completed' ? 100 : 0
  const isDone = !!job && ['completed', 'failed', 'interrupted'].includes(job.status)
  return <Modal title="导入 Codex 账号" open={open} onCancel={onClose} footer={null} width={720} destroyOnClose={false}>
    {optionsError && <Alert type="error" showIcon message={optionsError} action={<Button size="small" onClick={() => setOptionsError('')}>重试</Button>} style={{ marginBottom: 12 }} />}
    <Typography.Text type="secondary">选择导入方式</Typography.Text>
    <Radio.Group value={format} onChange={event => { setFormat(event.target.value); setSubmitError('') }} style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8, margin: '8px 0 16px' }}>
      {choices.map((choice, index) => <Radio.Button key={`${choice.format}-${index}`} value={choice.format} style={{ height: 66, padding: '9px 12px' }}><strong>{choice.title}</strong><br /><Typography.Text type="secondary" style={{ fontSize: 12 }}>{choice.description}</Typography.Text></Radio.Button>)}
    </Radio.Group>
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>旧版邮箱/密码格式：email----password（点击下方链接继续）</Typography.Text>
      <Space wrap>
        <Typography.Text>目标池</Typography.Text>
        <Select aria-label="目标池" value={poolId} onChange={setPoolId} style={{ width: 220 }} options={(pools.length ? pools : [{ id: 'PUBLIC_POOL', name: 'PUBLIC_POOL', targets: [] }]).map(pool => ({ value: pool.id, label: pool.name }))} />
        {targets.length > 1 && <Select aria-label="目标" placeholder="选择目标" value={targetId} onChange={setTargetId} style={{ width: 220 }} options={targets.map(target => ({ value: target.id, label: target.name }))} />}
      </Space>
      {format === 'auto' && <Input.TextArea aria-label="Session JSON" rows={5} placeholder="粘贴 Session JSON，或选择文件夹" value={paste} onChange={event => setPaste(event.target.value)} />}
      {format !== 'auto' && <Upload accept={format === 'json' || format === 'json_at' ? '.json' : '.txt'} multiple beforeUpload={() => false} showUploadList={{ showRemoveIcon: true }} onChange={async ({ fileList }: { fileList: UploadFile[] }) => { const list = await Promise.all(fileList.map(item => item.originFileObj ? readFile(item.originFileObj) : Promise.resolve(null))); setFiles(list.filter(Boolean) as ImportFile[]) }}><Button>选择文件</Button></Upload>}
      {format === 'auto' && <><input ref={inputRef} type="file" multiple accept=".txt,.json" {...({ webkitdirectory: '', directory: '' } as any)} style={{ display: 'none' }} onChange={event => { void collectFolder(event.target.files) }} /><Button icon={<FolderOpenOutlined />} onClick={() => inputRef.current?.click()}>选择文件夹（递归）</Button>{files.length > 0 && <Typography.Text type="secondary">已读取 {files.length} 个文件</Typography.Text>}</>}
      <Space><Button type="primary" loading={submitting} onClick={() => { void submit() }}>提交导入</Button>{onLegacyImport && <Button type="link" onClick={onLegacyImport}>使用旧版邮箱/密码导入</Button>}</Space>
      {submitError && <Alert type="error" showIcon message={submitError} />}
      {job && <div aria-live="polite"><Space><Tag color={job.status === 'completed' ? 'success' : job.status === 'failed' ? 'error' : 'processing'}>{job.status}</Tag><Typography.Text>任务 {job.id}</Typography.Text></Space><Progress percent={percent} status={job.status === 'failed' ? 'exception' : undefined} /><Typography.Text type="secondary">总数 {job.total ?? '-'} · 已处理 {job.processed ?? 0} · 成功 {job.success ?? 0} · 重复 {job.duplicate ?? 0} · 失败 {job.failed ?? 0}</Typography.Text>{job.error && <Alert type="error" message={job.error} style={{ marginTop: 8 }} />}{isDone && <Button style={{ marginTop: 8 }} onClick={() => { onCompleted?.(); message.info('账号列表已刷新'); }}>刷新账号列表</Button>}</div>}
    </Space>
  </Modal>
}

class ImportErrorBoundary extends Component<{ children: React.ReactNode }, { hasError: boolean }> {
  state = { hasError: false }
  static getDerivedStateFromError() { return { hasError: true } }
  render() { return this.state.hasError ? <Alert type="error" showIcon message="导入界面加载失败，请关闭后重试" /> : this.props.children }
}

export function CodexAccountImportModal(props: CodexAccountImportModalProps) {
  return <ImportErrorBoundary><CodexAccountImportModalContent {...props} /></ImportErrorBoundary>
}
