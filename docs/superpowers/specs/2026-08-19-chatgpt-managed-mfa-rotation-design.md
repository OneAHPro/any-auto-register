# ChatGPT 登录后托管 MFA 轮换设计

## 目标

在“登录已有 ChatGPT 账号”任务中提供默认开启的 MFA 托管开关：

- 未启用 MFA 的账号，登录成功后新增 TOTP MFA。
- 已启用 MFA 的账号，先通过现有 MFA 或邮箱验证码完成登录，再轮换为项目生成的新 TOTP MFA。
- 新 TOTP 密钥和恢复码只写入账号的邮箱登录上下文，不写入任务日志。
- 旧 TOTP 失效但服务端同时提供邮箱验证因子时，自动回退到邮箱验证码继续登录。
- 共享接码地址不等于邮箱控制权；任务结果明确记录该风险，不宣称账号已完全接管。

## 用户流程

登录弹窗新增“登录后新增/轮换 MFA”开关，默认开启。任务流程显示为：

`邮箱登录 -> MFA 新增/轮换 ->（可选）手机验证 -> 保存令牌`

开启后，MFA 轮换是严格步骤：远端操作失败或新密钥未能持久化时，该账号任务失败并显示 `[stage=mfa_rotate]` 具体原因，避免远端状态与本地凭据不一致。

## 后端协议

新建独立 `ChatGPTMfaManager`，复用刚完成登录的浏览器会话与新鲜 Access Token：

1. `GET /backend-api/accounts/mfa_info` 查询现有因子。
2. 若存在旧 TOTP，先确认邮箱验证码渠道可用；缺少邮箱恢复渠道时停止，不删除旧因子。确认后调用 `POST /backend-api/accounts/mfa/user/disable_in_house` 删除旧 TOTP（不误删 passkey）。
3. `POST /backend-api/accounts/mfa/user/request_mfa_token_in_house` 获取短期 enrollment token。
4. `POST https://auth.openai.com/api/mfa/public/enroll` 获取新 TOTP secret。
5. 本地生成六位 TOTP，调用 `POST .../activate_enrollment` 激活。
6. 服务端协议发生差异时兼容 `/backend-api/accounts/mfa/enroll` + `/accounts/mfa/user/activate_enrollment` 的 in-house 形状；通过 public 协议时继续创建恢复码。
7. 新 secret 生成后、远端激活前先写入 SQLite 写前日志；激活后标记 journal 状态，再读取 `mfa_info` 并写入账号邮箱登录上下文。账号落库成功后才清理 journal，进程中断时下次登录自动恢复。

所有响应错误只保留 HTTP 状态、结构化错误码和脱敏消息；TOTP secret、验证码、恢复码、Bearer Token 永不进入日志。

## 凭据持久化

成功后在 `mailbox_login_context.extra` 中保存；激活到账号落库之间由 `chatgpt_mfa_rotation_journal` 耐久暂存：

- `totp_secret`
- `mfa_recovery_code`
- `chatgpt_mfa_managed: true`
- `mfa_rotated_at`
- `mailbox_control_risk: shared_receiver | managed_mailbox`

同时删除供货商的 `totp_url`，后续登录优先使用本地 TOTP。邮箱收件 URL 继续保留，用于邮箱 OTP 备用验证。没有密码的账号保持原账号类型，避免凭空制造密码凭据。

## 故障与恢复

- 原 TOTP 返回 `incorrect_code` 且存在邮箱因子：自动切到邮箱 OTP。
- 禁用旧因子之前失败：远端状态未改变，可安全重试。
- 无邮箱恢复渠道且已有旧 TOTP：在远端变更前失败，避免锁号。
- 禁用后、新因子激活前失败：写前日志保留已生成 secret；仍可凭邮箱 OTP 再次执行修复。
- 新因子激活后、账号保存前进程退出：下次任务从 SQLite journal 恢复 secret；任务绑定表不复制 MFA secret 或恢复码。
- 仅有共享接码 URL：记录“邮箱控制权未接管”，提醒该账号仍可能被供货商找回。

## 测试

- 无 MFA 新增、已有 MFA 轮换、恢复码保存、响应脱敏。
- 旧 TOTP 错误后邮箱回退。
- 不同邮箱凭据类型的密钥写入与旧 `totp_url` 清理。
- 前端默认开关、请求字段和风险说明。
- 线上用已授权测试账号做一次真实新增，并验证数据库只存密钥、不泄露日志。
