import { BrowserRouter, Navigate, Routes, Route, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { App as AntdApp, Breadcrumb, ConfigProvider, Layout, Menu, Button, Drawer, Spin, Tooltip } from 'antd'
import { DashboardOutlined, UserOutlined, GlobalOutlined, SettingOutlined, SunOutlined, MoonOutlined, LogoutOutlined, UnorderedListOutlined, MobileOutlined, ImportOutlined, CloudServerOutlined, ControlOutlined, MenuOutlined, MenuFoldOutlined, MenuUnfoldOutlined, AppstoreOutlined } from '@ant-design/icons'
import zhCN from 'antd/locale/zh_CN'
import Dashboard from '@/pages/Dashboard'
import Accounts from '@/pages/Accounts'
import Supply from '@/pages/Supply'
import Proxies from '@/pages/Proxies'
import Settings from '@/pages/Settings'
import TaskHistory from '@/pages/TaskHistory'
import RunningTasks from '@/pages/RunningTasks'
import Login from '@/pages/Login'
import SmsPool from '@/pages/SmsPool'
import Codex2APITargets from '@/pages/Codex2APITargets'
import Codex2APIScheduler from '@/pages/Codex2APIScheduler'
import { darkTheme, lightTheme } from './theme'
import { clearToken, getToken } from '@/lib/utils'

const primaryItems = [
  { key: '/', icon: <DashboardOutlined aria-hidden="true" />, label: '运营总览' },
  { key: '/accounts/chatgpt', icon: <UserOutlined aria-hidden="true" />, label: '账号池' },
  { key: '/supply', icon: <ImportOutlined aria-hidden="true" />, label: '补充账号' },
  { key: '/codex2api/targets', icon: <CloudServerOutlined aria-hidden="true" />, label: '实例管理' },
  { key: '/codex2api/scheduler', icon: <ControlOutlined aria-hidden="true" />, label: '号池调度' },
  { key: '/running-tasks', icon: <UnorderedListOutlined aria-hidden="true" />, label: '任务记录' },
]
const auxiliaryItems = [
  { key: '/proxies', icon: <GlobalOutlined aria-hidden="true" />, label: '代理资源' },
  { key: '/sms-pool', icon: <MobileOutlined aria-hidden="true" />, label: '接码资源' },
  { key: '/settings', icon: <SettingOutlined aria-hidden="true" />, label: '系统设置' },
]
function selectedRoute(path: string) {
  if (path.startsWith('/accounts')) return '/accounts/chatgpt'
  if (path === '/history') return '/running-tasks'
  if (path === '/codex2api') return '/settings'
  return path
}
function ChatGPTAccounts() {
  const { platform } = useParams()
  return platform === 'chatgpt' ? <Accounts /> : <Navigate to="/accounts/chatgpt" replace />
}
function ProtectedLayout() {
  const navigate = useNavigate()
  const [ready, setReady] = useState(false)
  useEffect(() => {
    let disposed = false
    fetch('/api/auth/status').then(r => r.json()).then(s => {
      if (disposed) return
      if (s.has_password && !getToken()) navigate('/login', { replace: true })
      else setReady(true)
    }).catch(() => { if (!disposed) setReady(true) })
    return () => { disposed = true }
  }, [navigate])
  if (!ready) return <div className="console-boot" role="status" aria-label="加载工作台"><Spin size="large" /></div>
  return <AppContent />
}
function AppContent() {
  const [themeMode, setThemeMode] = useState<'dark' | 'light'>(() => localStorage.getItem('theme') === 'light' ? 'light' : 'dark')
  const [collapsed, setCollapsed] = useState(false)
  const [mobile, setMobile] = useState(() => window.innerWidth < 960)
  const [menuOpen, setMenuOpen] = useState(false)
  const [hasPassword, setHasPassword] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const isLight = themeMode === 'light'
  const selected = selectedRoute(location.pathname)
  const currentLabel = [...primaryItems, ...auxiliaryItems].find(item => item.key === selected)?.label || '运营总览'
  useEffect(() => {
    document.documentElement.classList.toggle('light', isLight)
    localStorage.setItem('theme', themeMode)
  }, [isLight, themeMode])
  useEffect(() => {
    const resize = () => { setMobile(window.innerWidth < 960); if (window.innerWidth >= 960) setMenuOpen(false) }
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])
  useEffect(() => { fetch('/api/auth/status').then(r => r.json()).then(s => setHasPassword(Boolean(s.has_password))).catch(() => {}) }, [])
  const choose = (key: string) => { navigate(key); setMenuOpen(false) }
  const navigation = (compact: boolean) => <div className="console-nav-content">
    <a className="console-brand" href="/" onClick={event => { event.preventDefault(); choose('/') }} aria-label="Codex 中控，返回运营总览">
      <span className="console-brand-mark"><AppstoreOutlined /></span>
      {!compact && <span><strong>Codex 中控</strong><small>账号运营工作台</small></span>}
    </a>
    <div className="console-nav-scroll">
      <Menu mode="inline" selectedKeys={[selected]} items={primaryItems} inlineCollapsed={compact} onClick={({ key }) => choose(key)} aria-label="工作区导航" style={{ overflowY: 'auto' }} />
      <div className="console-nav-divider" />
      <Menu mode="inline" selectedKeys={[selected]} items={auxiliaryItems} inlineCollapsed={compact} onClick={({ key }) => choose(key)} aria-label="资源与设置" />
    </div>
    <div className="console-nav-footer">
      <Tooltip title={compact ? (isLight ? '切换到暗色模式' : '切换到亮色模式') : ''} placement="right">
        <Button type="text" block icon={isLight ? <MoonOutlined /> : <SunOutlined />} aria-label={isLight ? '切换到暗色模式' : '切换到亮色模式'} onClick={() => setThemeMode(isLight ? 'dark' : 'light')}>
          {!compact && (isLight ? '暗色模式' : '亮色模式')}
        </Button>
      </Tooltip>
      {hasPassword && <Button type="text" block icon={<LogoutOutlined />} aria-label="退出登录" onClick={() => { clearToken(); navigate('/login') }}>{!compact && '退出登录'}</Button>}
      {!mobile && <Button type="text" block icon={compact ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} aria-label={compact ? '展开导航' : '折叠导航'} onClick={() => setCollapsed(!collapsed)}>{!compact && '折叠导航'}</Button>}
    </div>
  </div>
  return <ConfigProvider theme={isLight ? lightTheme : darkTheme} locale={zhCN}><AntdApp>
    <a className="console-skip-link" href="#console-main">跳转到主要内容</a>
    <Layout className="console-shell" style={{ minHeight: '100vh' }}>
      {!mobile && <Layout.Sider className="console-sidebar" width={216} collapsedWidth={72} collapsed={collapsed} trigger={null} style={{ position: 'sticky', top: 0, alignSelf: 'flex-start', height: '100vh' }}>{navigation(collapsed)}</Layout.Sider>}
      <Drawer title="工作区导航" open={mobile && menuOpen} placement="left" width={280} onClose={() => setMenuOpen(false)} destroyOnHidden className="console-mobile-nav" styles={{ body: { padding: 0 } }}>{navigation(false)}</Drawer>
      <Layout className="console-main-layout" style={{ minWidth: 0 }}>
        <header className="console-topbar"><div className="console-topbar-location">
          {mobile && <Button type="text" icon={<MenuOutlined />} aria-label="打开导航" onClick={() => setMenuOpen(true)} />}
          <Breadcrumb items={[{ title: '工作区' }, { title: currentLabel }]} />
        </div><Button size="small" icon={<ImportOutlined />} onClick={() => choose('/supply')}>补充账号</Button></header>
        <Layout.Content className="app-content console-content" id="console-main" tabIndex={-1} style={{ minWidth: 0 }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/accounts" element={<Navigate to="/accounts/chatgpt" replace />} />
            <Route path="/accounts/:platform" element={<ChatGPTAccounts />} />
            <Route path="/register" element={<Navigate to="/supply?method=login" replace />} />
            <Route path="/supply" element={<Supply />} />
            <Route path="/running-tasks" element={<RunningTasks />} />
            <Route path="/history" element={<TaskHistory />} />
            <Route path="/proxies" element={<Proxies />} />
            <Route path="/sms-pool" element={<SmsPool />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/codex2api" element={<Settings page="codex2api" />} />
            <Route path="/codex2api/targets" element={<Codex2APITargets />} />
            <Route path="/codex2api/scheduler" element={<Codex2APIScheduler />} />
            <Route path="/mail-import" element={<Navigate to="/supply?method=mail" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout.Content>
      </Layout>
    </Layout>
  </AntdApp></ConfigProvider>
}
export default function App() {
  return <BrowserRouter><Routes><Route path="/login" element={<Login />} /><Route path="/*" element={<ProtectedLayout />} /></Routes></BrowserRouter>
}
