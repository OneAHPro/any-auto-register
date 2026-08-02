# ChatGPT 分阶段账号登录设计

## 目标

在 ChatGPT 账号管理页提供批量“已有账号登录”和单账号“接码”能力：后台从已导入的邮箱池自动读取邮箱 OTP，先通过 ChatGPT Web 会话获取并保存 Access Token；之后由用户在账号行内输入手机号和短信验证码，继续 OAuth 登录并补齐 Refresh Token。

## 现有能力复用

- 邮箱来源继续使用 `mail_provider` 与本地邮箱池；Outlook/Hotmail/MailAPI 账号由 `OutlookMailbox` 自动取出并轮询 OTP。
- 批量执行继续使用 `/api/tasks/register`、任务并发、任务日志和账号保存逻辑。
- RT 阶段继续使用 `OAuthClient.login_and_get_tokens()`、现有 add-phone 发码/验码请求和状态机。
- 登录完成后的状态刷新继续使用 `probe_local_chatgpt_status()` 与 `apply_chatgpt_status_policy()`。

## 第一阶段：批量邮箱登录并保存 AT

1. ChatGPT 账号页顶部新增“登录”按钮。
2. 登录弹窗展示本地邮箱池数量，并允许设置登录数量、并发数与启动间隔。
3. 提交后创建普通注册任务，但携带：
   - `chatgpt_existing_account_login_only=true`
   - `chatgpt_existing_account_login_stage=access_token`
   - `chatgpt_existing_account_allow_phone_verification=false`
4. 后端从邮箱池取出账号，自动请求、轮询并提交邮箱 OTP。
5. 使用 ChatGPT Web 登录会话读取 `/api/auth/session`，只持久化 `access_token`、`session_token` 与账号信息，不写入 `refresh_token`。
6. 将本次使用的邮箱接码凭据随 ChatGPT 账号保存，供第二阶段再次自动读取邮箱 OTP。
7. 保存账号后立即执行一次本地状态探测并刷新账号状态信息。

## 第二阶段：人工手机接码并补齐 RT

1. 当账号已有 AT 且缺少 RT 时，在行内“详情”之前展示“接码”按钮。
2. 用户输入 E.164 国际格式手机号并点击“获取验证码”。
3. 后端为该账号启动一个有过期时间的内存登录会话：
   - 使用账号保存的邮箱接码凭据自动完成邮箱 OTP；
   - 进入 add-phone 后发送短信；
   - 保留同一 OAuth Session、PKCE 和流程状态，等待前端提交短信验证码。
4. 短信发送成功后开始 60 秒倒计时；到期后允许重发。
5. 验证码错误时保留当前登录会话，用户可继续输入；验证码正确后状态机继续完成 OAuth Token Exchange。
6. 成功后合并更新账号的 RT、ID Token、Session Token；只有完整成功时才更新 AT，任何手机阶段错误均保留第一阶段已经保存的 AT。
7. 完成后执行本地状态探测，关闭弹窗并刷新列表。

## 会话与错误处理

- 同一账号同时只允许一个手机验证会话。
- 手机验证会话默认 10 分钟过期；服务重启后需重新发起。
- 发码、重发、验证码校验均有独立加载状态，后端也拒绝重复命令。
- 明确区分邮箱池为空、邮箱 OTP 超时、手机号格式错误、短信发码失败、验证码错误、会话过期和 Token Exchange 失败。
- 邮箱池账号在第一阶段失败时沿用当前任务失败语义；成功后凭据保存在账号记录中，不再依赖已被取出的池记录。

## 测试

- 后端测试覆盖 AT-only 登录结果、邮箱接码凭据保存、AT/RT 独立合并、手机命令的发码/重发/错误验证码/成功验证码和会话过期。
- 前端测试覆盖“接码”按钮显示条件、手机号校验、倒计时与 API 请求状态。
- 完整验证包含 Python 单元测试、前端 Vitest、ESLint、TypeScript/Vite 构建和本地页面交互检查。
