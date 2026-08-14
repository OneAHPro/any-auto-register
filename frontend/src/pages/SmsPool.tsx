import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  InboxOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  UploadOutlined,
} from '@ant-design/icons'

import LeadBeeApiSettingsCard from '@/components/LeadBeeApiSettingsCard'
import { apiFetch } from '@/lib/utils'


const DEFAULT_SMS_BASE_URL = 'https://sms.leadbee.cn/smsbox'

type SmsPoolStatus = 'unused' | 'reserved' | 'active' | 'used'

interface SmsPoolItem {
  id: number
  code: string
  code_hint: string
  base_url: string
  status: SmsPoolStatus
  reserved_task_id?: string
  used_by_email?: string
  created_at?: string
  reserved_at?: string
  used_at?: string
  updated_at?: string
}

interface SmsPoolStats {
  total: number
  unused: number
  reserved: number
  active: number
  used: number
}

interface SmsPoolListResponse {
  total: number
  page: number
  page_size: number
  items: SmsPoolItem[]
}

function formatTime(value?: string) {
  return value ? new Date(value).toLocaleString('zh-CN') : '-'
}

function statusTag(status: SmsPoolStatus) {
  if (status === 'used') return <Tag color="default">已使用</Tag>
  if (status === 'active') return <Tag color="warning">待回收</Tag>
  if (status === 'reserved') return <Tag color="processing">使用中</Tag>
  return <Tag color="success">未使用</Tag>
}

export default function SmsPool() {
  const [items, setItems] = useState<SmsPoolItem[]>([])
  const [stats, setStats] = useState<SmsPoolStats>({
    total: 0,
    unused: 0,
    reserved: 0,
    active: 0,
    used: 0,
  })
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState<'all' | SmsPoolStatus>('all')
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [content, setContent] = useState('')
  const [defaultBaseUrl, setDefaultBaseUrl] = useState(DEFAULT_SMS_BASE_URL)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const params = new URLSearchParams({ page: String(page), page_size: '50' })
      if (status !== 'all') params.set('status', status)
      const [nextStats, response] = await Promise.all([
        apiFetch('/sms-pool/stats') as Promise<SmsPoolStats>,
        apiFetch(`/sms-pool?${params.toString()}`) as Promise<SmsPoolListResponse>,
      ])
      setStats(nextStats)
      setItems(response.items || [])
      setTotal(Number(response.total || 0))
    } catch (error) {
      if (!silent) {
        message.error(`读取 SMS 接码池失败: ${error instanceof Error ? error.message : '请求失败'}`)
      }
    } finally {
      if (!silent) setLoading(false)
    }
  }, [page, status])

  useEffect(() => {
    // This effect is the page's server-state synchronization entry point.
    load()
    const refreshTimer = window.setInterval(() => {
      void load(true)
    }, 5_000)
    return () => window.clearInterval(refreshTimer)
  }, [load])

  const handleImport = async () => {
    if (!content.trim()) {
      message.warning('请输入接码卡密')
      return
    }
    if (!defaultBaseUrl.trim()) {
      message.warning('请输入默认接码地址')
      return
    }
    setImporting(true)
    try {
      const result = await apiFetch('/sms-pool/import', {
        method: 'POST',
        body: JSON.stringify({
          content: content.trim(),
          default_base_url: defaultBaseUrl.trim(),
        }),
      }) as { imported: number; duplicates: number; invalid: { line: number; message: string }[] }
      message.success(`已导入 ${result.imported} 张卡密`)
      if (result.duplicates > 0) message.info(`已跳过 ${result.duplicates} 张重复卡密`)
      if (result.invalid.length > 0) message.warning(`${result.invalid.length} 行格式无效，未导入`)
      setContent('')
      setPage(1)
      await load()
    } catch (error) {
      message.error(`导入失败: ${error instanceof Error ? error.message : '请求失败'}`)
    } finally {
      setImporting(false)
    }
  }

  const columns: TableColumnsType<SmsPoolItem> = [
    {
      title: '接码卡密',
      dataIndex: 'code',
      key: 'code',
      width: 260,
      render: value => {
        const code = String(value || '')
        return (
          <Typography.Text code copyable={code ? { text: code, tooltips: ['复制', '已复制'] } : false}>
            {code || '-'}
        </Typography.Text>
        )
      },
    },
    {
      title: '接码地址',
      dataIndex: 'base_url',
      key: 'base_url',
      ellipsis: true,
      render: value => <Typography.Text type="secondary">{String(value || '-')}</Typography.Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: value => statusTag(value as SmsPoolStatus),
    },
    {
      title: '使用账号 / 任务',
      key: 'owner',
      width: 220,
      render: (_, item) => item.used_by_email || item.reserved_task_id || '-',
    },
    {
      title: '状态时间',
      key: 'status_time',
      width: 180,
      render: (_, item) => formatTime(
        item.used_at || item.reserved_at || item.updated_at || item.created_at,
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 16,
          flexWrap: 'wrap',
        }}
      >
        <div>
          <Typography.Title level={2} style={{ margin: 0, fontSize: 24 }}>
            SMS接码
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
            管理 LeadBee API 接码与备用卡密池，登录任务可按所选模式自动取号。
          </Typography.Paragraph>
        </div>
        <Space wrap>
          <Tag color="success" icon={<InboxOutlined />}>可用 {stats.unused}</Tag>
          <Tag color="processing">使用中 {stats.reserved}</Tag>
          <Tag color="warning">待回收 {stats.active}</Tag>
          <Tag icon={<SafetyCertificateOutlined />}>已使用 {stats.used}</Tag>
          <Tag>总计 {stats.total}</Tag>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => load()}>刷新</Button>
        </Space>
      </div>

      <LeadBeeApiSettingsCard />

      <Card title="导入接码卡密">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="一行一张卡密"
            description="仅填写卡密时使用下方默认地址；单独指定地址可写成：卡密----https://接码地址。"
          />
          <Input
            aria-label="默认接码地址"
            value={defaultBaseUrl}
            onChange={event => setDefaultBaseUrl(event.target.value)}
            placeholder={DEFAULT_SMS_BASE_URL}
          />
          <Input.TextArea
            aria-label="接码卡密"
            value={content}
            onChange={event => setContent(event.target.value)}
            placeholder={'bei-sms-xxxx-xxxx\nbei-sms-yyyy-yyyy----https://sms.example.com/smsbox'}
            autoSize={{ minRows: 4, maxRows: 10 }}
            spellCheck={false}
          />
          <Button
            type="primary"
            icon={<UploadOutlined />}
            loading={importing}
            onClick={handleImport}
          >
            导入卡密
          </Button>
        </Space>
      </Card>

      <Card
        title="卡密列表"
        extra={(
          <Select
            aria-label="状态筛选"
            value={status}
            style={{ width: 120 }}
            onChange={value => {
              setStatus(value)
              setPage(1)
            }}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'unused', label: '未使用' },
              { value: 'reserved', label: '使用中' },
              { value: 'active', label: '待回收' },
              { value: 'used', label: '已使用' },
            ]}
          />
        )}
      >
        <Table
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          scroll={{ x: 900 }}
          locale={{ emptyText: '暂无接码卡密，请先导入' }}
          pagination={{
            current: page,
            pageSize: 50,
            total,
            showSizeChanger: false,
            showTotal: value => `共 ${value} 张`,
            onChange: setPage,
          }}
        />
      </Card>
    </div>
  )
}
