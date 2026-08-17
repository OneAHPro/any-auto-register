# ChatGPT TOTP + MailAPI 重登修复设计

## 问题

导入的 `chatgpt_password_totp` 账号可以同时保存 ChatGPT 密码、TOTP 密钥和可选的 `mail_api_url`。首次登录链路会保留这三类凭据，但已保存账号的重登链路始终构造 `_PasswordTotpEmailService`。该服务不暴露 `mail_api_url`，并在读取邮箱验证码时主动抛错，导致 OpenAI 先要求邮箱 OTP 时始终得到 0 个候选验证码。

## 设计

- 没有 `mail_api_url` 的密码 + TOTP 账号保持现有行为，只使用本地 TOTP，不尝试收件。
- 存在 `mail_api_url` 时，重登链路构造现有的 AppleMail/MailAPI 邮箱后端和 `_PersistedEmailService`，从同一个账号上下文同时暴露密码、TOTP 密钥与收件地址。
- 邮箱 OTP 继续复用现有的新邮件基线、等待预算、后台等待和任务中断逻辑；TOTP 仍由本地密钥生成，不发送给收件服务。
- 外层 OAuth 状态机无需改变：邮箱 OTP 状态读取 MailAPI，MFA 状态读取 TOTP 密钥。

## 验证

- 回归测试证明混合凭据构造为可收件服务，并能返回邮箱 OTP。
- 回归测试证明 `create_email()` 同时保留 `mail_api_url`、密码和 TOTP 密钥。
- 现有纯密码 + TOTP、URL OTP、邮箱等待预算及完整重登测试继续通过。

