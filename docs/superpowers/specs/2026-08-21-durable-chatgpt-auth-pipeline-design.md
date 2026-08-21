# 单机持久化 ChatGPT 认证与自动维护设计

## 目标

在不引入 Redis、Celery、Temporal 或额外服务器的前提下，把现有登录、MFA、邮箱接码和自动重登链路改造成可恢复、可幂等、可退避的单机持久化系统。

成功标准：

- 进程重启后，已入库任务能够继续执行或进入明确终态。
- 同一 ChatGPT 账号同时只允许一个会改变认证状态的操作。
- 只有已确认生效的 TOTP 可以参与正常登录；未确认的 staged 密钥不得覆盖有效密钥。
- 邮箱验证码保留为服务端风险验证和 MFA 修复兜底；兜底成功后必须修复为密码加 TOTP 的正常状态。
- 邮箱领取使用租约，不再通过删除数据库记录表达占用。
- 暂时错误使用指数退避和随机抖动；确定性凭据错误进入隔离，不按周期重复等待 OTP。
- Access Token、Refresh Token、密码、TOTP、恢复码、MailAPI Token 和签名 URL 不进入任务日志。
- Codex2API 同步失败不得回滚已经确认的本地认证状态。

## 部署约束

- 只有一台生产服务器。
- 保留 FastAPI、SQLModel 和 SQLite WAL。
- API 进程继续负责请求和任务展示。
- 持久任务执行器先作为独立运行循环接入现有服务生命周期；状态与租约全部写入 SQLite，因此以后拆成独立 systemd Worker 时不改变领域模型。
- 数据规模约百级账号，SQLite 单写者能力足够；通过短事务、CAS 和有限并发避免写竞争。

## 权威状态边界

### 账号认证状态

新增每账号唯一的 `chatgpt_auth_states`：

- `account_id`：唯一账号引用。
- `auth_version`：每次成功提交认证凭据递增，用于乐观并发控制。
- `primary_state`：`absent | pending | confirmed | rejected`。
- `mfa_state`：`absent | active | suspect | repair_required | blocked`。
- `active_mfa_generation`：当前生效的不可变 MFA 代数。
- `email_recovery_state`：`unavailable | unverified | exclusive_verified | shared_receiver`。
- `credential_revision`：权威凭据内容的不可逆摘要，仅用于变更检测，不用于恢复密钥。
- 最近成功、失败分类、更新时间。

认证密钥在迁移期继续保存在账号记录的兼容凭据快照中，但其有效性只由 `chatgpt_auth_states` 决定。`mailbox_login_context` 降为兼容投影；所有写入必须由同一个提交函数生成，调用方不得分别修改多份 JSON。

### MFA 操作与代数

新增 operation-scoped 的 `chatgpt_mfa_operations`，替换按邮箱唯一的 journal：

- 主键 `operation_id`，并保存 `account_id`、`generation`、`base_auth_version`。
- 状态：`staged -> activating -> activated_remote -> committed`。
- 异常终态：`activation_unknown | aborted | superseded | quarantined`。
- TOTP 和恢复码绑定同一 operation/generation。
- 所有状态更新必须包含 `operation_id + expected_state`；旧回调不能标记或删除新操作。

只有 `activated_remote` 能在账号版本 CAS 成功后晋升为 `active`。`staged` 永远不作为正常登录凭据。远端激活响应不确定时进入 `activation_unknown`，通过独立新鲜登录或邮箱修复确认，不能猜测。

现有 `chatgpt_mfa_rotation_journal` 仅用于一次性兼容迁移：

- legacy `staged` 全部标记为隔离候选，普通登录不读取。
- 有账号且与账号密钥冲突的记录进入修复队列。
- 无账号记录进入 orphan 隔离，清除秘密后只保留无密钥审计事件。

### 恢复码

恢复码状态为 `available | reserved | consumed | unknown`：

- 提交前原子预留。
- 明确成功后消费。
- 网络或响应不确定时标记 `unknown`，禁止重复使用。
- legacy staged 恢复码不自动尝试。

## 认证决策状态机

自动维护先做 Refresh Token/Codex2API 探针，只有确认失效才进入完整登录。

完整登录顺序：

1. 必须具备已确认 primary credential。只有 TOTP、没有密码的账号进入 `blocked_missing_primary` 修复；若邮箱可用，先通过一次邮箱验证建立并提交密码。
2. 读取服务端实际提供的验证因子。
3. 优先使用账号当前 `active` generation 的 TOTP。
4. TOTP 只有在服务端明确返回 `credential_rejected` 时才进入恢复码；timeout、429、5xx、会话失效、页面解析失败均停止并重试，不降级。
5. 恢复码失败或服务端直接要求邮箱时，在策略允许且邮箱通道健康时使用新鲜邮箱验证码。
6. 邮箱兜底成功后，把原 MFA 标记为 `suspect`，在同一新鲜会话中修复或轮换 TOTP。
7. 新密码加新 TOTP 的独立登录验证成功后，以 `auth_version` CAS 提交，随后才能清理 MFA operation。

邮箱兜底条件：

- 服务端明确提供 email factor 或独立 email risk challenge。
- 邮箱恢复通道不是 `unavailable`。
- OTP 必须属于本次 challenge，具有新鲜 message id 或 received time；无结构化时间时不得返回基线中已出现的相同验证码。
- 每账号同一失败窗口只允许一次自动邮箱兜底，避免每个调度周期反复取码。
- 共享接码地址允许作为用户明确要求的兜底，但记录 `shared_receiver` 风险，并采用更长冷却时间。

## 邮箱租约

`outlook_accounts` 增加：

- `state`：`available | leased | bound | quarantined | disabled`。
- `lease_owner`、`lease_expires_at`、`lease_version`。
- `bound_account_id`、`bound_at`。

领取流程使用短事务 CAS：

`available -> leased -> bound`

- 登录成功并保存账号后转为 `bound`，保留作为该账号邮箱兜底，但不再分配给其他账号。
- 登录明确失败且未改变远端账号状态时释放为 `available`。
- 远端状态不确定时转为 `quarantined`。
- 服务启动时回收过期 `leased`；`bound` 永不自动回收。
- 兼容旧 `enabled` 字段，迁移完成后由 `state` 决定资格。

## 持久任务与调度

新增：

### `chatgpt_maintenance_jobs`

保存任务来源、类型、优先级、状态、到期时间、租约、心跳、去重键和取消请求。

状态：`pending -> leased -> running -> completed | partial | cancelled`。租约过期且无有效心跳时回到 `pending`。

### `chatgpt_maintenance_items`

每个账号每个操作一行，状态：

`queued -> leased -> running -> succeeded | retry_wait | quarantined | superseded`

包含 `account_id`、operation、attempt、due time、结构化错误、幂等键、lease 和 fencing token。

### `chatgpt_maintenance_account_states`

保存账号级：

- `healthy | due | backoff | circuit_open | half_open | quarantined`
- `next_eligible_at`
- `consecutive_failures`
- `failure_domain`、`error_code`
- 熔断结束时间、最后成功时间、账号版本和 fencing token。

`TaskRun` 保留为 UI 投影，不再作为执行真相。`TaskLog` 增加 job/item/attempt 关联，并改为同步事务写入，禁止 daemon 日志线程。

### 优先级与抢占

- 手工任务优先级 100，自动任务优先级 10。
- 有手工积压时暂停领取新的自动 item。
- 同账号的未开始自动 item 标记 `superseded`。
- 正在运行的自动 item 在安全检查点停止续租并让出；无关账号允许收尾。
- 删除自动停止 watchdog 的 `os._exit(75)` 路径。

### 退避与熔断

使用 full jitter：

`delay = random(0, min(cap, base * 2 ** (failures - 1)))`

- 锁竞争：5 到 30 秒。
- Codex2API 暂时失败：1 分钟起步，最长 1 小时。
- 登录或邮箱暂时失败：15 分钟起步，最长 24 小时。
- 连续 5 次同域失败后熔断 6 小时；到期只允许一次 half-open。
- 缺凭据、MFA 冲突、账号停用等确定性结果直接隔离。
- Codex2API 网络故障使用独立服务级熔断器，不能给所有账号分别累计凭据失败。

## 幂等、事务和同步

远端调用采用 saga，不跨网络请求持有数据库事务：

1. DB lease + operation id + base auth version。
2. 短事务持久化意图。
3. 执行远端请求并记录结构化结果。
4. 以 fencing token 和 auth version CAS 提交结果。

账号凭据更新使用单一提交函数，同时：

- 晋升或退休 MFA generation。
- 更新密码和令牌。
- 递增 `auth_version`。
- 生成兼容 `mailbox_login_context`。
- 标记 operation committed。
- 写入 Codex2API outbox。

Codex2API outbox 以 `(account_id, auth_version, destination)` 唯一。同步失败只重试 outbox，不回滚认证状态。

## 现有数据迁移

迁移账本不保存邮箱或秘密，只保存账号 ID、布尔能力、关系枚举、状态和安全错误码。

顺序：

1. 先部署“staged 不参与登录”的硬门禁。
2. 只读审计并生成账号分类。
3. 完整密码加 TOTP 账号建立 canonical auth state。
4. 有 MFA 无密码账号通过一次邮箱兜底建立并确认密码；TOTP 可用时不轮换。
5. journal 冲突账号先验证账号保存 TOTP；明确失败后再验证 staged 候选；两者都明确失败才用邮箱修复并生成第三个 TOTP。
6. 不确定响应进入 deferred，不切换候选。
7. orphan journal 清除秘密，仅保留审计终态。

灰度批次：1 个代表性账号，然后 5、10、25、50、剩余；远端修改并发从 1 开始，稳定后最多 2。每批要求无悬挂 lease、无 secret 日志、密码加 TOTP 独立登录成功。

## 可观测性

记录：

- 队列深度和最老任务等待时间。
- lease/heartbeat 年龄及回收次数。
- 按 source、stage、failure domain 的尝试、成功、退避和隔离数量。
- TOTP、恢复码、邮箱兜底选择结果，但不记录值。
- 邮箱 provider 等待耗时与新鲜度判定。
- Codex2API 探针延迟和熔断状态。
- 幂等命中、CAS 冲突和 auth version。
- 进程级强退次数，目标为零。

## 测试

- MFA journal：staged/activated/unknown、并发 generation、旧回调 ABA、崩溃点恢复。
- 登录路由：TOTP 成功、明确拒绝、网络异常、恢复码消费、服务端 email challenge、邮箱自愈。
- 密码 bootstrap：远端成功本地提交失败、重启恢复和重复执行。
- 邮箱租约：领取竞争、过期回收、bound 不重分配、进程中断。
- 持久任务：去重、租约、心跳、重启续跑、手工优先、自动让出、退避、熔断和 half-open。
- 并发：手工/自动/MFA 迁移同账号互斥，fencing token 和 auth version 阻止旧结果覆盖。
- 日志：使用 sentinel 扫描所有日志、API、TaskRun、TaskLog、异常和迁移账本。
- 回归：现有全量 pytest、前端构建、生产数据库迁移副本和只读审计。

## 上线与回滚

1. Git、生产 SQLite、当前 release、环境和 systemd 配置先备份并校验。
2. 新 schema 采用加法迁移，旧列和旧表暂不删除。
3. 部署后先禁用远端 MFA 批量修改，只启用 staged 门禁、任务租约和审计。
4. 对代表性账号执行一次新鲜登录验证，再启用小批迁移。
5. 所有批次稳定后恢复自动维护。

回滚先停止领取新任务并等待运行中 operation 到安全终点。尚未改变远端 MFA 的操作可切回旧 release；已经进入 `activated_remote` 或结果不确定的操作必须前向恢复，禁止用整库备份覆盖已改变的远端凭据。
