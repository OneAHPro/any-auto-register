# ChatGPT Sentinel 浏览器 SDK 加载修复设计

## 目标

消除已有账号登录任务中反复出现的：

```text
Sentinel Browser 异常: Page.wait_for_function: Timeout 15000ms exceeded
```

让浏览器通道从 OpenAI 官方 Sentinel frame 加载 SDK，保留 HTTP PoW 回退和
现有任务停止能力，并避免每个相关登录阶段固定浪费 15 秒。

## 根因证据

当前 `get_sentinel_token_via_browser()` 打开调用方传入的 Auth 页面，例如
`https://auth.openai.com/log-in/password`，随后假设该页面会定义：

```javascript
window.SentinelSDK.token
```

实际最小探测结果：

```text
auth page:  HTTP 403, SDK 5 秒内未出现
frame page: HTTP 200, SDK 约 1 秒就绪
frame token: password_verify token 约 1.9 秒生成成功
```

官方 frame 页面明确加载当前版本 SDK：

```html
<script src="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"></script>
```

因此异常不是接码逻辑造成的，也不是 Playwright 自身随机故障；根因是浏览器
助手在错误的页面等待一个不保证存在的全局对象。

当前异常会被捕获并返回 `None`，OAuth 调用方随后尝试 HTTP PoW。因此很多
账号仍能登录，但每次先损失 15 秒，并在 HTTP PoW 同时失败时扩大为登录失败。

## 方案

### 固定官方 frame

在 `sentinel_browser.py` 定义与当前 SDK 版本对应的：

```text
https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6
```

浏览器始终访问该 frame 获取 SDK，不再把 Auth 登录页面作为 SDK 宿主。
`page_url` 参数为兼容现有调用方暂时保留，但不再决定 SDK 页面；日志只记录
flow，不记录可能带 OAuth 查询参数的完整调用方 URL。

浏览器 context 同时设置 `sentinel.openai.com` 和 `auth.openai.com` 的
`oai-did` cookie，并继续复用当前代理、User-Agent、headless/headed 选择和
TLS 设置。

加载顺序为：

1. `goto(frame, wait_until="load")`；
2. 有界等待 `window.SentinelSDK.token`；
3. 使用现有 `Promise.race` 有界调用 `SentinelSDK.token(flow)`；
4. 成功后返回 token；失败后返回 `None`，由调用方继续 HTTP PoW。

不从网络下载后执行未固定版本的脚本，不增加动态 SDK 发现逻辑。

### 错误和日志

- frame 导航、SDK 加载或 token 生成失败仍是可恢复通道失败，不直接结束账号。
- 浏览器通道失败的日志改为“Sentinel 浏览器通道未就绪，准备使用 HTTP PoW”，
  避免把已成功降级的情况显示成最终任务异常。
- HTTP PoW 成功时继续记录现有“已通过 HTTP PoW 获取 token”。
- 两种通道都失败时，保留现有明确的 `无法获取 sentinel token (<flow>)`。
- 日志不输出 Sentinel token、OAuth URL 查询参数、device ID 或 cookie。

### 停止与资源清理

保留现有 attempt-scoped Playwright driver interrupt、JavaScript Promise
timeout、`browser.close()`、`playwright.stop()` 和最终 checkpoint。任务停止只
终止当前 attempt 的 driver，不影响其他并发账号。

## 测试

- 新回归测试让假 Auth 页面永远没有 SDK，证明实现访问固定 frame，而不是
  等满 15 秒后返回空值。
- 断言 `goto` 使用 frame URL、`wait_until="load"` 且 timeout 有界。
- 断言 frame 就绪后仍执行包含 `Promise.race` 的 token 调用。
- frame 导航失败、SDK wait 失败、token 返回错误时仍返回 `None` 并完成
  browser/playwright 清理。
- 任务 stop/skip 中断、driver 隔离和 cleanup 阻塞测试保持通过。
- OAuth 测试证明浏览器返回空值时仍调用 HTTP PoW，浏览器成功时不重复调用。
- 使用本机 Chrome 的无凭据 smoke probe 验证 frame SDK 和 token 生成；自动
  测试不依赖外网或本机 Chrome。

## 上线观察

部署后检查新任务日志：不再出现 Auth 页面 `wait_for_function` 15 秒超时；
正常路径应在数秒内出现 Sentinel Browser 成功，浏览器通道失败时应紧接 HTTP
PoW 成功或明确的双通道失败。该修复不改变邮箱、密码、OTP、接码或 token
持久化逻辑。
