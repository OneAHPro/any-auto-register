# 订单前 OAuth 会话恢复重试设计

## 目标

当 ChatGPT 手机 OAuth 在 LeadBee 订单创建前因会话回到 `log_in` 或授权上下文失效而抛错时，自动重新登录一次并重建授权上下文；旧账号缺少完整重登凭据时，回退到已认证浏览器快照生成新 PKCE。若仍失败，明确标记为“订单未创建”，不把它当成已扣费的 provider 失败。

## 约束与不变量

- 只有 `provider_started` 为假时才允许这条重试路径。
- 最多自动重试一次；API 模式优先使用已保存的账号密码、MFA 或邮箱接码凭据建立全新登录会话，随后生成新的 PKCE/OAuth 事务。
- 进入 `add_phone` 并开始 LeadBee provider 后，继续沿用现有订单终态、释放和重试逻辑，不重复创建同一订单。
- 不把邮箱、密码、Cookie、手机号、验证码或 provider 凭据写入诊断；公开诊断只使用固定安全码和阶段。
- 重试期间保留当前 provider 锁与 API 容量租约，最终只在所有尝试结束后结算。

## 数据流

1. `run_leadbee_phone_oauth_flow` 使用缓存/持久化 OAuth 上下文执行一次手机授权。
2. `_take_phone_oauth_resume_context` 或 `login_and_get_tokens` 在 provider 尚未启动时抛出专用的预下单异常。
3. `ChatGPTPhoneVerificationManager._run_automatic` 识别该异常，发布 `oauth_prepare` / `OPENAI_OAUTH_CONTEXT_NOT_READY` 诊断，短暂退避后将 broker 标记为恢复重试，再调用 runner 一次。
4. API runner 使用持久化凭据重新走已有账号 Access Token 登录，取得新的 OAuth 事务；持久化凭据不适用时，才从认证浏览器快照创建新 PKCE。
5. 第二次成功后继续正常 token 持久化；第二次仍失败则以明确的订单未创建消息结束。

## 测试策略

- manager 单元测试：预下单专用异常第一次失败、第二次成功；确认只调用两次、未触发 provider；第二次仍失败时不再调用第三次。
- phone flow 单元测试：重试标记会跳过旧的 prepared context，使用浏览器快照建立新事务。
- 现有全量测试保持通过，尤其是已下单后的 LeadBee 释放/订单替换测试。
