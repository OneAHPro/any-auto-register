import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Alert, App, Button, Form, Skeleton } from 'antd'
import { ArrowRightOutlined, FileTextOutlined, LoginOutlined, MailOutlined, ReloadOutlined } from '@ant-design/icons'
import { ConsolePageHeader } from '@/components/console/ConsolePageHeader'
import { ChatGPTExistingAccountLoginModal } from '@/components/ChatGPTExistingAccountLoginModal'
import { CodexAccountImportModal } from '@/components/CodexAccountImportModal'
import MailImportPanel from '@/components/settings/MailImportPanel'
import { apiFetch } from '@/lib/utils'

const methods = [
  { key: 'mail', icon: <MailOutlined />, label: '导入邮箱资料', detail: '密码与 2FA · MailAPI 收件链接' },
  { key: 'login', icon: <LoginOutlined />, label: '登录已有账号', detail: '使用邮箱池，完成登录与接码' },
  { key: 'json', icon: <FileTextOutlined />, label: '导入 JSON / 凭据', detail: 'JSON、RT、AT 与 Session' },
]
const mailboxKeys = ['mail_provider', 'applemail_pool_dir', 'applemail_pool_file', 'outlook_backend', 'luckmail_email_type', 'luckmail_domain']

export default function Supply() {
  const navigate = useNavigate()
  const { message } = App.useApp()
  const [params, setParams] = useSearchParams()
  const method = methods.some(item => item.key === params.get('method')) ? params.get('method')! : 'mail'
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [loginOpen, setLoginOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const loadConfig = useCallback(async () => {
    setLoading(true)
    setError('')
    try { const config = await apiFetch('/config'); form.setFieldsValue(config || {}) }
    catch (e) { setError(e instanceof Error ? e.message : '读取邮箱配置失败，请重试') }
    finally { setLoading(false) }
  }, [form])
  useEffect(() => { void loadConfig() }, [loadConfig])
  const continueLogin = async () => {
    setSaving(true)
    try {
      const values = form.getFieldsValue(true)
      if (values.mail_provider === 'mail_import') {
        values.mail_provider = values.mail_import_source === 'applemail' ? 'applemail' : 'microsoft'
      }
      const data = Object.fromEntries(mailboxKeys.filter(key => values[key] !== undefined).map(key => [key, values[key]]))
      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data }) })
      setParams({ method: 'login' })
    } catch (e) { message.error(e instanceof Error ? e.message : '保存邮箱配置失败，请重试') }
    finally { setSaving(false) }
  }

  return <div className="console-page supply-workspace">
    <ConsolePageHeader title="补充账号" description="导入已购账号，完成登录并加入 Codex2API 号池。"
      actions={<Button onClick={() => navigate('/running-tasks')}>查看任务记录</Button>} />
    <div className="supply-layout">
      <nav className="supply-methods" aria-label="选择补号方式">
        {methods.map(item => <button type="button" key={item.key} className={`supply-method${method === item.key ? ' is-active' : ''}`}
          aria-current={method === item.key ? 'step' : undefined} onClick={() => setParams({ method: item.key })}>
          <span className="supply-method-icon">{item.icon}</span><span><strong>{item.label}</strong><small>{item.detail}</small></span>
          {method === item.key && <ArrowRightOutlined className="supply-method-arrow" />}
        </button>)}
        <div className="supply-method-note">已有 JSON 或令牌可直接导入。邮箱资料先入库，再执行账号登录。</div>
      </nav>
      <Form form={form} component="section" layout="vertical" className="console-panel supply-body" aria-label={methods.find(item => item.key === method)?.label}>
        {method === 'mail' && <>
          <div className="console-section-heading"><div><h2>邮箱资料</h2><p>粘贴已购账号资料或收件链接，识别格式后批量导入。</p></div>
            <Button type="primary" loading={saving} disabled={loading || Boolean(error)} onClick={() => void continueLogin()}>保存并继续登录<ArrowRightOutlined /></Button></div>
          {error ? <Alert type="error" showIcon message="邮箱配置读取失败" description={error} action={<Button icon={<ReloadOutlined />} onClick={() => void loadConfig()}>重试</Button>} /> :
            loading ? <Skeleton active paragraph={{ rows: 7 }} /> : <div className="supply-mail-content"><MailImportPanel form={form} compact /></div>}
        </>}
        {method === 'login' && <>
          <div className="console-section-heading"><div><h2>登录已有账号</h2><p>从已导入的邮箱池执行登录，按需完成手机号验证。</p></div></div>
          <ol className="supply-steps">
            <li><strong>确认邮箱资料</strong><p>邮箱密码、2FA 密钥或 MailAPI 收件链接已导入。</p></li>
            <li><strong>执行账号登录</strong><p>设置本次数量与并发，选择接码方式。</p></li>
            <li><strong>核对入池结果</strong><p>在任务记录查看登录和同步结果，在账号池检查状态。</p></li>
          </ol>
          <div className="console-toolbar"><Button type="primary" icon={<LoginOutlined />} onClick={() => setLoginOpen(true)}>设置登录任务</Button>
            <Button onClick={() => setParams({ method: 'mail' })}>补充邮箱资料</Button></div>
        </>}
        {method === 'json' && <>
          <div className="console-section-heading"><div><h2>JSON 与账号凭据</h2><p>上传已有凭据，选择目标实例和号池后导入。</p></div></div>
          <dl className="supply-format-list"><div><dt>JSON 文件</dt><dd>支持完整账号 JSON、仅 Access Token 的 JSON 和 Session JSON。</dd></div>
            <div><dt>文本凭据</dt><dd>支持每行一个 Refresh Token 或 Access Token。</dd></div>
            <div><dt>批量文件</dt><dd>可选择多个文件或文件夹，查看导入进度与结果。</dd></div></dl>
          <div className="console-toolbar"><Button type="primary" icon={<FileTextOutlined />} onClick={() => setImportOpen(true)}>选择文件或粘贴凭据</Button>
            <Button onClick={() => navigate('/accounts/chatgpt')}>查看账号池</Button></div>
        </>}
      </Form>
    </div>
    <ChatGPTExistingAccountLoginModal open={loginOpen} onClose={() => setLoginOpen(false)} onDone={() => navigate('/accounts/chatgpt')} />
    <CodexAccountImportModal open={importOpen} onClose={() => setImportOpen(false)} onCompleted={() => navigate('/accounts/chatgpt')} />
  </div>
}
