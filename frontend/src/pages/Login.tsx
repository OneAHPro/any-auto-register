import { useEffect, useState } from 'react'
import { App, ConfigProvider, Form, Input, Button } from 'antd'
import { LockOutlined, SafetyCertificateOutlined, AppstoreOutlined } from '@ant-design/icons'
import { setToken } from '@/lib/utils'
import { darkTheme, lightTheme } from '@/theme'
import './operations-workspace.css'

type Step = 'password' | '2fa'

function LoginContent() {
  const { message } = App.useApp()
  const [step, setStep] = useState<Step>('password')
  const [tempToken, setTempToken] = useState('')
  const [loading, setLoading] = useState(false)

  const handleLogin = async (values: { password: string }) => {
    setLoading(true)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: values.password }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '登录失败')
      if (data.requires_2fa) {
        setTempToken(data.temp_token)
        setStep('2fa')
      } else {
        setToken(data.access_token)
        window.location.href = '/'
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '请求失败，请重试')
    } finally {
      setLoading(false)
    }
  }

  const handleTotp = async (values: { code: string }) => {
    setLoading(true)
    try {
      const res = await fetch('/api/auth/verify-totp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ temp_token: tempToken, code: values.code }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '验证失败')
      setToken(data.access_token)
      window.location.href = '/'
    } catch (error) {
      message.error(error instanceof Error ? error.message : '请求失败，请重试')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="console-login">
      <div className="console-login-workspace">
        <div className="console-login-intro">
          <div className="console-login-brand"><AppstoreOutlined /><span>Codex2API 运营中控台</span></div>
          <p>管理已有账号，查看实例状态，跟进每一次补号与调度。</p>
          <div className="console-login-scope"><span>账号池</span><span>实例管理</span><span>任务记录</span></div>
        </div>
        <section className="console-login-form" aria-labelledby="login-heading">
          <header><h1 id="login-heading">{step === '2fa' ? '双因素验证' : '登录中控台'}</h1><p>{step === '2fa' ? '请输入验证器 App 中的 6 位验证码。' : '使用访问密码进入运营工作台。'}</p></header>
          {step === '2fa' ? (
            <Form key="totp" name="totp" layout="vertical" onFinish={handleTotp} requiredMark={false}>
              <Form.Item name="code" label="验证码" rules={[{ required: true, message: '请输入验证码' }, { pattern: /^\d{6}$/, message: '验证码为 6 位数字' }]}>
                <Input prefix={<SafetyCertificateOutlined />} placeholder="000000" maxLength={6} inputMode="numeric" autoComplete="one-time-code" autoFocus className="console-login-code" />
              </Form.Item>
              <Button type="primary" htmlType="submit" block loading={loading}>验证并登录</Button>
              <Button type="link" className="console-login-back" onClick={() => { setStep('password'); setTempToken('') }}>返回密码登录</Button>
            </Form>
          ) : (
            <Form key="password" name="password" layout="vertical" onFinish={handleLogin} requiredMark={false}>
              <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
                <Input.Password prefix={<LockOutlined />} placeholder="请输入访问密码" autoComplete="current-password" autoFocus />
              </Form.Item>
              <Button type="primary" htmlType="submit" block loading={loading}>登录</Button>
            </Form>
          )}
          <p className="console-login-note"><LockOutlined />登录信息仅用于此中控台的访问验证</p>
        </section>
      </div>
    </main>
  )
}

export default function Login() {
  const [isLight] = useState(() => localStorage.getItem('theme') === 'light')
  useEffect(() => { document.documentElement.classList.toggle('light', isLight) }, [isLight])
  return <ConfigProvider theme={isLight ? lightTheme : darkTheme}><App><LoginContent /></App></ConfigProvider>
}
