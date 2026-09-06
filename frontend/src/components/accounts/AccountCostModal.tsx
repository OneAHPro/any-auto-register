import { useRef, useState } from 'react'
import { Alert, Button, Form, InputNumber, Modal, Typography } from 'antd'
import { apiFetch } from '@/lib/utils'

export interface AccountCostAccount {
  id: number
  email: string
  extra?: { purchase_cost_cny?: string | number | null }
}

interface AccountCostModalProps {
  account: AccountCostAccount
  onCancel: () => void
  onSaved: (id: number, cost: string | null) => void
}

const COST_VALIDATION_MESSAGE = '请输入 0 至 999999999.99 元，最多两位小数'

export function AccountCostModal({ account, onCancel, onSaved }: AccountCostModalProps) {
  const [form] = Form.useForm<{ purchase_cost_cny?: string | null }>()
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const [error, setError] = useState('')
  const storedCost = account.extra?.purchase_cost_cny

  const save = async () => {
    if (savingRef.current) return
    savingRef.current = true
    let values: { purchase_cost_cny?: string | null }
    try {
      values = await form.validateFields()
    } catch {
      savingRef.current = false
      return
    }
    const cost = values.purchase_cost_cny == null || values.purchase_cost_cny === ''
      ? null
      : String(values.purchase_cost_cny)
    if (!Number.isSafeInteger(account.id) || account.id <= 0) {
      setError('请先刷新账号列表，完成同步入库后设置成本')
      savingRef.current = false
      return
    }
    setSaving(true)
    setError('')
    try {
      await apiFetch(`/accounts/${account.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ purchase_cost_cny: cost }),
      })
      onSaved(account.id, cost)
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '保存失败，请重试')
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }

  return (
    <Modal
      title="设置账号成本"
      open
      width={400}
      onCancel={onCancel}
      closable={!saving}
      maskClosable={false}
      keyboard={!saving}
      footer={[
        <Button key="clear" disabled={saving} onClick={() => form.setFieldsValue({ purchase_cost_cny: null })}>清空成本</Button>,
        <Button key="cancel" disabled={saving} onClick={onCancel}>取消</Button>,
        <Button key="save" type="primary" loading={saving} onClick={() => { void save() }}>保存</Button>,
      ]}
    >
      <Typography.Paragraph type="secondary" style={{ overflowWrap: 'anywhere' }}>{account.email}</Typography.Paragraph>
      <Form
        form={form}
        layout="vertical"
        initialValues={{ purchase_cost_cny: storedCost == null ? null : String(storedCost) }}
        onFinish={() => { void save() }}
      >
        <Form.Item
          name="purchase_cost_cny"
          label="购入成本（元）"
          extra="留空保存后显示“未设置”。"
          rules={[{
            validator: (_, value: unknown) => {
              if (value === undefined || value === null || value === '') return Promise.resolve()
              const text = String(value)
              const amount = Number(text)
              return /^\d+(?:\.\d{1,2})?$/.test(text) && Number.isFinite(amount) && amount >= 0 && amount <= 999999999.99
                ? Promise.resolve()
                : Promise.reject(new Error(COST_VALIDATION_MESSAGE))
            },
          }]}
        >
          <InputNumber
            autoFocus
            stringMode
            precision={2}
            step="0.01"
            changeOnBlur={false}
            placeholder="未设置"
            prefix="¥"
            disabled={saving}
            style={{ width: '100%' }}
          />
        </Form.Item>
        <button type="submit" style={{ display: 'none' }} tabIndex={-1} aria-hidden="true" />
      </Form>
      {error ? <Alert type="error" showIcon message={error} role="alert" style={{ marginTop: 12 }} /> : null}
    </Modal>
  )
}
