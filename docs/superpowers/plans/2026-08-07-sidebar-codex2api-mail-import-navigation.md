# Codex2API 与邮箱导入主导航拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Codex2API 设置和邮箱导入操作从全局配置二级标签移动为左侧主导航独立页面。

**Architecture:** `App.tsx` 增加两个主菜单项和独立路由；`Settings` 通过 `page` 属性支持普通设置、Codex2API、邮箱导入三种渲染模式。三个模式继续共用现有配置加载、默认值、保存、敏感字段和错误处理，邮箱导入继续复用 `MailImportPanel` 与现有后端接口。

**Tech Stack:** React 19、React Router、Ant Design、TypeScript、Vitest、Testing Library、Vite。

---

### Task 1: 主侧栏入口与路由

**Files:**
- Create: `frontend/src/App.test.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: 编写失败的主导航测试**

在 `App.test.tsx` 中 mock 登录状态和平台请求，渲染根路由并验证两个新入口。点击入口后验证浏览器路径：

```tsx
it('shows Codex2API and mail import as primary navigation routes', async () => {
  const user = userEvent.setup()
  render(<App />)

  await user.click(await screen.findByText('Codex2API'))
  expect(window.location.pathname).toBe('/codex2api')

  await user.click(screen.getByText('邮箱导入'))
  expect(window.location.pathname).toBe('/mail-import')
})
```

测试初始化必须提供 `matchMedia`、`ResizeObserver`、`fetch('/api/auth/status')` 和 `apiFetch('/platforms')` 的稳定 mock，避免页面业务请求影响导航断言。

- [ ] **Step 2: 运行测试并确认 RED**

```bash
cd frontend
npm test -- src/App.test.tsx
```

预期：找不到 `Codex2API` 和 `邮箱导入` 主菜单项。

- [ ] **Step 3: 添加菜单项、选中态与路由**

在 `App.tsx` 引入 `ApiOutlined`、`ImportOutlined`，在 `SMS接码池` 与 `全局配置` 之间加入：

```tsx
{
  key: '/codex2api',
  icon: <ApiOutlined />,
  label: 'Codex2API',
},
{
  key: '/mail-import',
  icon: <ImportOutlined />,
  label: '邮箱导入',
},
```

扩展 `getSelectedKey()`：

```tsx
if (path === '/codex2api') return ['/codex2api']
if (path === '/mail-import') return ['/mail-import']
```

增加路由，复用下一任务定义的页面模式：

```tsx
<Route path="/codex2api" element={<Settings page="codex2api" />} />
<Route path="/mail-import" element={<Settings page="mail-import" />} />
```

- [ ] **Step 4: 运行主导航测试并确认 GREEN**

```bash
cd frontend
npm test -- src/App.test.tsx
```

预期：测试通过，点击两个菜单项分别进入对应路径。

- [ ] **Step 5: 提交主导航改动**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "feat(navigation): add Codex2API and mail import pages"
```

### Task 2: Settings 独立页面模式

**Files:**
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`

- [ ] **Step 1: 编写失败的页面模式测试**

在 `Settings.test.tsx` 增加三个行为测试，并为 `/mail-imports/providers` 与 `/mail-imports/snapshot` 提供最小响应：

```tsx
it('renders Codex2API as a standalone page', async () => {
  render(<Settings page="codex2api" />)
  expect(await screen.findByRole('heading', { name: 'Codex2API' })).toBeTruthy()
  expect(screen.getByText('管理面板')).toBeTruthy()
  expect(screen.getByText('告警通知')).toBeTruthy()
  expect(screen.queryByText('注册设置')).toBeNull()
})

it('renders mail import as a standalone page', async () => {
  render(<Settings page="mail-import" />)
  expect(await screen.findByRole('heading', { name: '邮箱导入' })).toBeTruthy()
  expect(await screen.findByRole('button', { name: '确认导入' })).toBeTruthy()
  expect(screen.queryByText('注册设置')).toBeNull()
})

it('removes moved features from global settings', async () => {
  const user = userEvent.setup()
  render(<Settings />)
  expect(await screen.findByText('注册设置')).toBeTruthy()
  expect(screen.queryByText('Codex2API')).toBeNull()
  await user.click(screen.getByText('邮箱服务'))
  expect(screen.queryByRole('button', { name: '确认导入' })).toBeNull()
})
```

现有 Codex2API、自动重登、删除联动和 Bark 测试改为渲染 `<Settings page="codex2api" />`，不再点击已移除的内部标签。

- [ ] **Step 2: 运行 Settings 测试并确认 RED**

```bash
cd frontend
npm test -- src/pages/Settings.test.tsx
```

预期：独立标题不存在，普通设置仍显示 Codex2API 标签与邮箱导入面板。

- [ ] **Step 3: 实现三种页面模式**

在 `Settings.tsx` 定义：

```tsx
type SettingsPageMode = 'settings' | 'codex2api' | 'mail-import'

interface SettingsProps {
  page?: SettingsPageMode
}

export default function Settings({ page = 'settings' }: SettingsProps) {
```

用页面模式决定有效标签、标题和说明：

```tsx
const [activeTab, setActiveTab] = useState(page === 'codex2api' ? 'codex2api' : 'register')
const effectiveTab = page === 'codex2api' ? 'codex2api' : activeTab
const pageTitle = page === 'codex2api' ? 'Codex2API' : page === 'mail-import' ? '邮箱导入' : '全局配置'
const pageDescription = page === 'codex2api'
  ? '管理自动上传、删除联动、鉴权巡检与告警通知'
  : page === 'mail-import'
    ? '导入并管理 Outlook、Hotmail、MailAPI URL 与 iCloud 邮箱池'
    : '配置将持久化保存，注册任务自动使用'
```

普通设置的标签列表过滤 `codex2api`，且只在 `page === 'settings'` 时渲染左侧 `Tabs`：

```tsx
const settingsTabs = TAB_ITEMS.filter((item) => item.key !== 'codex2api')
```

为邮箱导入模式构造完整内容，并把它放在现有内容分支之前：

```tsx
const mailImportPageContent = page === 'mail-import' ? (
  <>
    <MailImportPanel form={form} />
    <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} disabled={saveDisabled} block>
      {saved ? '已保存 ✓' : '保存配置'}
    </Button>
  </>
) : null
```

普通 `mailbox` 标签移除 `<MailImportPanel form={form} />`。Codex2API 模式继续渲染原 `ConfigSection`、`ChatGPTAutoReloginSection` 和保存按钮。不要复制配置加载或保存逻辑。

- [ ] **Step 4: 运行 Settings 测试并确认 GREEN**

```bash
cd frontend
npm test -- src/pages/Settings.test.tsx src/components/settings/ChatGPTAutoReloginSection.test.tsx
```

预期：独立页面、入口移除、Bark、删除联动和自动重登测试全部通过。

- [ ] **Step 5: 提交页面模式改动**

```bash
git add frontend/src/pages/Settings.tsx frontend/src/pages/Settings.test.tsx
git commit -m "feat(settings): split Codex2API and mail import pages"
```

### Task 3: 回归验证与生产发布

**Files:**
- Verify: `frontend/src/App.tsx`
- Verify: `frontend/src/pages/Settings.tsx`
- Build output: `static/`

- [ ] **Step 1: 运行完整前端测试**

```bash
cd frontend
npm test -- --run
```

预期：全部测试通过，无失败用例。

- [ ] **Step 2: 运行生产构建**

```bash
cd frontend
npm run build
```

预期：TypeScript 与 Vite 构建退出码为 0；`static/assets` 中的 JS 包含 `Codex2API`、`邮箱导入` 与新路由。

- [ ] **Step 3: 检查提交与工作树**

```bash
git diff --check
git status --short
git log --oneline -5
```

预期：无空白错误，功能改动均已提交，工作树干净。

- [ ] **Step 4: 生产预检和不可变 release**

通过 SSH 确认 `any-auto-register.service` 为 active、回环接口为 200、数据库 `PRAGMA quick_check=ok`，并确认 `task_runs` 中没有 `pending` 或 `running`。从已验证提交创建新 release，覆盖为刚构建的 `static/`，上传后以服务用户完成 Python 导入检查和静态资源文案检查。

- [ ] **Step 5: 原子切换与验收**

保留当前 `/www/any-auto-register/releases/bark-critical-20260807-1715-680de84` 作为回滚目标，原子切换 `current`，只重启 `any-auto-register.service`。要求：

```text
systemctl is-active = active
loopback /api/auth/status = 200
public / = 200
public bundle contains /codex2api and /mail-import
database quick_check = ok
Codex2API/Postgres/Redis container IDs and StartedAt unchanged
```

浏览器验收左侧菜单可进入两个新页面，全局配置中不再显示对应入口。
