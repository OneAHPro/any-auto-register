# 邮箱导入、OAuth 预建与 LeadBee 重试加固设计

## 目标

在不改变现有三种接码模式和结算边界的前提下，修复三类已在线上复现的问题：

1. 邮箱文本导入同时接受完整的 `---` 与 `----` 分隔符。
2. 手机 OAuth 事务预建失败时延迟重试，并从已认证浏览器快照恢复一次新的 PKCE 事务，不重复发送邮箱 OTP。
3. LeadBee/OpenAI 手机链路保留安全结构化失败阶段，对可重试请求进行有界重试，并且只在旧订单已明确释放后自动重试一次账号。

生产任务 `task_3e91c78a80654eb8a7dd53b1affef754` 的 14 个订单中，12 个完成，2 个在分配号码后约 7–8 秒被取消并释放。当前日志丢失了 `add-phone/send` 与第一次订单读取之间的具体失败阶段，因此本次先补可观测性，再补有严格结算门槛的恢复。

## 邮箱分隔符

新增一个无状态共享解析模块，使用 `(?<!-)-{3,4}(?!-)` 识别完整的三个或四个横线。自动检测、AppleMail 与 Microsoft 正式导入共用该模块；Tab、空白与 JSON 路径保持原样。

匹配不会切开字段中的单横线、双横线或五个以上连续横线。同一批内容可逐行混用三横线和四横线。前端说明只描述两种受支持格式，不在浏览器中复制解析逻辑。

## OAuth 预建与恢复

首次邮箱 OTP 通过后只尝试一次预建；Access Token 获取后再使用短暂有界退避重试，避免在认证 Cookie 尚未稳定时立即连续请求三次。每次尝试创建独立的 OAuthClient、Session 克隆、PKCE verifier 与 state，不共享可变对象。

无论预建是否成功，Access Token 登录阶段都会保存一份仅供服务端使用的已认证浏览器快照。预建成功时继续保存 v2 prepared context；预建仍未就绪时，手机 runner 从浏览器快照恢复会话并只建立一次新的 PKCE 事务。这个 fallback 不提交邮箱、不调用邮箱 OTP，并在调用 LeadBee 前完成。

日志和元数据只保留 `stage`、`page_type`、HTTP 状态、attempt 与 `recovered/deferred`，不保留 URL query、cookie、邮箱、PKCE、state 或 token。账号 API 继续删除所有 OAuth 快照中的 Cookie 值。

## LeadBee/OpenAI 重试

LeadBee 单次请求对 transport、408、425、429、500、502、503、504 做最多三次的指数退避；429 优先使用 `Retry-After`。GET 始终读取同一 `order_id`，写请求始终复用同一幂等键。

创建订单收到损坏的 2xx、最终 transport/5xx 或幂等冲突时，先调用官方 `GET /orders`，按已经持久化的 `client_order_id` 查找现有订单。找到后接管原订单；没有找到时保持隔离，不生成新业务单号。认证、签名、权限、产品与 IP 拒绝立即失败。

OpenAI `add-phone/send` 对 transport、408、425、429 与 5xx 使用同一 Session 和手机号做最多三次有界重试。明确的 invalid/unsupported/already-used phone 才触发同一 LeadBee 订单的 replace；限流不换号。错误信息不包含 OpenAI 原始响应体。

## 安全诊断

broker 只接受白名单字段：

- `failure_stage`
- `safe_error_code`
- `http_status`
- `provider_retry_count`
- `order_status`
- `billing_status`
- `replacement_count`
- `recovery_status`

阶段限于 OAuth、LeadBee 创建/读取/换号/取消与 OpenAI 发码/验码。错误码映射到本地固定枚举。诊断进入任务详情和 attempt binding 的现有 JSON 字段，不增加包含原始响应的数据库列，也不暴露 order ID/client order ID。

## 账号级自动重试

API 手机流程失败后仅在以下条件全部成立时自动再试一次：旧订单是 `CANCELED` 或 `EXPIRED`、`billing_status=RELEASED`、手机号尚未验证、没有后台所有权歧义、错误不是容量不足。新 `client_order_id` 必须先写入 binding，随后恢复 OAuth context，再执行容量预检和订单创建。

若旧订单为 `CAPTURED`、仍 active、状态不明或清理未完成，则不创建第二单。自动重试仍失败后保留现有 failed binding 与人工“重试失败账号”入口。只有明确 `LEADBEE_API_CAPACITY_EXHAUSTED` 才允许切换卡密池。

## 验证与上线

每项变更先写失败测试并确认 RED，再做最小实现。运行聚焦后端测试、后端全量测试、前端 Vitest、构建、compileall、格式/差异与凭证扫描。

上线前确认没有 pending/running 任务，在线备份 SQLite，创建不可变 release，原子切换 symlink 并只重启 `any-auto-register.service`。健康检查通过后，对原任务的两条失败 binding 发起受控重试，并核对新旧订单状态、billing 与预留余额；异常立即回滚。
