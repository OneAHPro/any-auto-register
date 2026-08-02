# ChatGPT TOTP 优先选择设计

## 背景

ChatGPT 密码 + TOTP 导入记录不具备邮箱收件能力。当前 MFA 页面同时返回 `totp, email` 时，登录器优先选择邮箱 MFA，随后调用邮箱收件接口并立即失败，导致有效的 TOTP 密钥未被使用。

## 目标

- 当页面提供 TOTP 且导入记录带有非空 TOTP 密钥时，优先本地生成并提交 TOTP。
- 仅在没有 TOTP 密钥、页面提供邮箱因子且邮箱客户端可用时，使用邮箱 MFA。
- 保持纯 TOTP 缺密钥、未知 MFA 因子等现有错误语义不变。
- 修复不得提前启动 LeadBee；登录失败时卡密继续保持未消耗。

## 设计

只修改 `OAuthClient._submit_mfa_challenge` 的因子选择顺序：先解析 `totp` 和 `email` 因子；若 `totp` 存在且 `totp_secret` 非空，调用 `_submit_totp_mfa_challenge`；否则尝试邮箱因子；最后保留纯 TOTP 分支，让缺密钥错误继续由 `_submit_totp_mfa_challenge` 统一生成。

不增加配置开关，不修改导入格式，也不改变单因子账号的行为。

## 验证

- 同时存在 `totp, email` 且带密钥：必须调用 TOTP，不能访问邮箱。
- 同时存在 `totp, email` 但无密钥：继续调用邮箱 MFA。
- 仅有 TOTP 且无密钥：继续返回“缺少 MFA 密钥”。
- 运行 ChatGPT 登录相关测试及全量后端测试，然后重建容器。
- 重新导入原 6 个账号，确认登录接码、Refresh Token 和 Codex2API 上传结果。
