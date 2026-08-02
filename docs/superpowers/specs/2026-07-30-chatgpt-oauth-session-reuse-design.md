# ChatGPT 手机验证复用 OAuth 会话设计

## 目标

已有账号完成邮箱验证码登录并保存 AT 后，用户在同一后端进程内点击「接码」时，复用刚才建立的 `auth.openai.com` Cookie、设备 ID 和浏览器指纹，直接进入带 `offline_access` 的 OAuth 后续状态，避免再次验证邮箱。

## 约束

- 手机号绑定仍需进入 OpenAI OAuth 授权链路，因为 `add-phone`、workspace/consent 和 RT token exchange 属于同一条带 PKCE 的授权事务，现有 AT 不能单独替代它。
- 认证浏览器上下文只保存在后端内存中，不写数据库；默认 30 分钟过期，按标准化邮箱隔离。
- 服务重启、缓存过期或 OpenAI 拒绝旧会话时，自动回退到现有 passwordless 邮箱 OTP 登录，保证可用性。
- AT 与 RT 的持久化规则不变；手机流程失败不覆盖已有 AT。

## 数据流

1. 邮箱登录成功并读取 ChatGPT Session 后，将当前 HTTP Session、device ID、UA、Client Hints、语言和 impersonate 配置写入短期缓存。
2. 手机验证开始时按账号邮箱一次性取出缓存上下文。
3. 命中缓存时，`OAuthClient` 采用原 Session 和指纹，使用新的 PKCE 参数访问 `/oauth/authorize`。
4. 如果授权入口已进入 `add_phone`、consent、workspace、organization 或 OAuth callback，直接从该状态继续，跳过 `authorize/continue` 的邮箱提交和邮箱 OTP。
5. 如果缓存未命中或旧会话未被 OpenAI 接受，沿用现有新 Session + 邮箱 OTP 流程。
6. 手机验证码通过后换取并保存 RT，刷新账号状态。

## 交互

- 命中缓存：显示「正在复用邮箱登录状态并请求短信验证码」。
- 缓存失效：显示「登录状态已过期，正在重新验证邮箱并请求短信验证码」。
- 两条路径都只在用户点击「获取验证码」后执行，不自动发短信。

## 验证

- 缓存隔离、一次性取出和过期测试。
- AT 登录成功后写入缓存测试。
- 手机流程命中缓存后采用原 Session 且关闭 `force_new_browser` 测试。
- 已认证 OAuth 起点跳过邮箱提交测试。
- 未命中缓存仍走原邮箱 OTP 回退测试。
