# 手工 ChatGPT 任务可中断设计

## 背景与现场证据

2026-08-10 的一轮 5 账号手工登录接码任务完成 4 个账号后长期停留在
`running`。任务库显示 `progress=5/5`、`success=4`、`active_attempts=1`，手工
跳过和停止请求都已写入控制器，但最后一个执行单元没有退出。对应绑定记录仍在
`stage=login`，没有创建本地账号。

该账号最后一条执行日志是 `password_verify: Sentinel Browser 启动`。服务进程
下仍有该账号独占的 Playwright driver 和 Chromium 进程。终止这一个 driver 后，
调用立即抛错，任务按现有收尾路径变为 `stopped`，浏览器进程、任务槽位和 SMS
卡密绑定均被释放，主服务和自动重登调度器没有重启。

根因位于 `get_sentinel_token_via_browser()`：`page.goto()` 和
`page.wait_for_function()` 有超时，但随后执行的异步 `page.evaluate()` 直接等待
`window.SentinelSDK.token(flow)`。Playwright 的导航默认超时不会限制这个异步
Promise；当 SDK Promise 或 driver 通信永久不返回时，Python 线程也永久阻塞，
现有协作式停止检查点无法运行。

## 目标

- Sentinel SDK Promise 在有限时间内结束，失败后继续走现有 HTTP PoW 回退。
- 手工停止或跳过在浏览器 driver 自身失去响应时也能中断当前执行单元。
- 只终止当前任务已登记的临时浏览器 driver，不重启主服务，不影响 Codex2API、
  自动重登调度器或独立 Turnstile solver。
- 维持已有成功登录、MFA、邮箱回池、SMS 卡密结算和自动任务停止行为。

## 方案比较

### 仅增加 JavaScript Promise 超时

实现简单，能够覆盖本次 SDK Promise 永不结束的根因。但若 Playwright driver
本身失去响应，`page.evaluate()` 仍可能不返回，手工停止也只能等底层进程恢复。

### 仅扩大服务级停止看门狗

可以最终清除任何卡死，但需要回收整个应用进程。虽然 systemd 会恢复服务，仍会
短暂影响无关 API 和自动化，不符合本次“只停止当前任务”的要求。

### 双层账号级保护（采用）

第一层在页面内用 `Promise.race` 限制 Sentinel SDK 调用；第二层把当前
Playwright driver 的终止回调登记到当前 attempt 的任务控制器。停止或跳过请求会
立即调用该 attempt 的回调，使阻塞中的 Playwright 调用抛错并进入原有异常与收尾
路径。自动任务现有服务级看门狗保留为最终兜底。

## 组件设计

### attempt 中断资源登记

`RegisterTaskControl` 增加按 `attempt_id` 管理的中断回调：

- 登记返回幂等注销函数；
- `request_stop_once()` 在设置粘性停止标志后，锁外调用所有活跃 attempt 回调；
- `request_skip_current()` 只调用本次被标记跳过的活跃 attempt 回调；
- `finish_attempt()` 清除该 attempt 的所有回调，防止后续请求误杀已经复用的资源；
- 回调异常被隔离，停止标志和其他回调仍然生效。

`bind_task_attempt_context()` 用线程本地上下文把 `_do_one()` 当前的
`RegisterTaskControl` 与 `attempt_id` 暴露给深层浏览器帮助函数，不修改 OAuth
客户端的公共参数链。

### Playwright driver 中断

Sentinel 浏览器启动 Playwright 后读取该实例 driver 子进程 PID，并在当前 attempt
登记一个只向该 PID 发送 `SIGTERM` 的幂等回调。PID 必须是正整数，回调只持有
本次 `sync_playwright()` 实例的 PID。正常退出时注销回调；停止时 driver 终止会让
同步 Playwright 调用抛出异常，现有 `finally`、邮箱回池、binding 失败更新和任务
收尾继续执行。

无法解析 driver PID 时不做进程终止，仍由页面内 Promise 超时提供第一层保护。

### Sentinel Promise 超时

`page.evaluate()` 参数增加 `timeoutMs`，页面脚本通过 `Promise.race` 在
`SentinelSDK.token(flow)` 和定时拒绝之间竞争。超时值受现有 `timeout_ms` 控制，
最大 45 秒；超时按当前函数的失败路径返回 `None`，上层继续尝试 HTTP PoW，避免
把一次 Sentinel 浏览器故障直接扩大为账号失败。

## 测试策略

- 任务控制单元测试证明停止会立即调用活跃 attempt 的中断回调且只调用一次。
- 跳过测试证明只中断被标记的活跃 attempt，注销或结束后的回调不再执行。
- Sentinel 浏览器测试使用假 Playwright 对象，证明传给页面的脚本包含有限的
  Promise 超时，并证明当前 attempt 会登记/注销 driver 中断资源。
- 注册任务测试模拟浏览器调用被停止后抛错，证明任务进入 `stopped`、attempt 归零、
  binding 失败收尾与邮箱/SMS 资源释放保持现状。
- 运行任务控制、ChatGPT 注册/登录/接码、自动重登以及完整后端测试；前端没有行为
  变更，只运行现有前端测试和生产构建作为兼容验证。

## 上线与回滚

提交同步到 `origin/main` 后创建新的不可变 release。部署前要求无活动手工任务并
创建 SQLite 在线备份；切换后验证主服务、内外网 HTTP、数据库完整性和自动重登
状态。使用合成的卡死 Playwright 测试验证停止回调，不触发真实账号登录。若服务
健康或调度状态异常，原子切回上一 release 并只重启 `any-auto-register.service`。
