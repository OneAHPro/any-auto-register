# ChatGPT 已有账号登录模式设计

## 目标

为已注册 ChatGPT 的微软邮箱提供登录专用流程。任务从本地 Outlook 邮箱池加载凭据，使用邮箱 OTP 登录 OpenAI，并且仅在同时取得 Access Token 和 Refresh Token 时保存账号。

## 范围

- 新增任务配置 `chatgpt_existing_account_login_only`，默认关闭。
- 配置开启时跳过 ChatGPT 注册状态机、随机注册资料和 `about_you` 提交。
- 直接调用现有 `OAuthClient.login_and_get_tokens()`，使用 `screen_hint=login` 和 passwordless OTP。
- 登录专用模式不改变默认的新账号注册流程。
- 当前批次通过任务 API 传入该配置，不新增前端控件。

## 数据流

1. 从 Outlook 邮箱池加载一条账号并记录现有邮件 UID 基线。
2. 创建邮箱验证码适配器。
3. 以已有账号登录模式启动 OAuth 会话。
4. 轮询新邮件并提交 OTP。
5. 获取 OAuth Token 后校验 Access Token 和 Refresh Token 均为非空。
6. 校验通过后复用现有账号持久化逻辑写入本地账号库；缺少任一 Token 时返回失败，不保存半成品。

## 日志与错误处理

- 登录专用模式使用“加载邮箱凭据”“登录已有账号”等措辞，不再将邮箱加载或登录标记为创建账号。
- 保留 OAuthClient 的阶段日志，便于区分 Bootstrap、Sentinel、OTP、workspace 和 token exchange 错误。
- 批量实跑使用单并发；出现连续出口风控时停止任务并恢复未成功邮箱池。

## 测试与验收

- 回归测试证明登录专用模式不调用注册状态机。
- 验证 OAuth 调用使用 `screen_hint=login`、passwordless OTP，并关闭 `about_you` 补全。
- 验证 AT 与 RT 同时存在时成功，任一缺失时失败。
- 验证默认注册模式行为保持不变。
- 容器内目标测试通过后重建服务，先实跑一条，再处理其余邮箱。
