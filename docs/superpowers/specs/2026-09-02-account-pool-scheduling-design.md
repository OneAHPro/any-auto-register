# 账号资产与多 Codex2API 号池调度设计

## 目标

在不修改 Codex2API 源码的前提下，把 `any-auto-register` 扩展为账号资产控制面：管理多个 Codex2API 实例，保持账号身份和额度历史连续，支持公共/企业/浮动号池之间的计划、人工确认和可恢复迁移，并保留现有登录、2FA、自动重登录和凭证同步流程。

首期扩缩容均生成计划，由操作员确认后执行；自动扩缩容属于后续开关，不在首期默认开启。

## 已确认的边界

- 控制面代码只提交到 `any-auto-register`。
- Codex2API 是外部执行节点，不增加其 Go/React/数据库代码。
- 当前生产实例作为第一个目标导入；第二个实例部署后，在管理台添加目标即可接入。
- 当前生产地址仍由现有 Nginx 和 systemd 服务提供，不改变登录端口和现有运行方式。
- 现有 ChatGPT 登录、密码、MFA、邮箱和自动重登录状态机继续作为凭证生命周期的唯一入口。
- 外部请求失败、身份不明确、额度过期或目标能力不足时，系统宁可暂停动作，也不删除或覆盖账号。

## 方案概览

```text
React 管理台
      │
FastAPI 控制面
      ├─ 目标注册表 / 密钥引用
      ├─ 稳定账号身份
      ├─ 全局额度账本
      ├─ 号池归属与租约
      ├─ 纯函数容量规划器
      └─ 持久化迁移 Worker
              │
       Codex2API Target Client
          ┌───┴───┐
          ▼       ▼
       目标 A    目标 B
```

`any-auto-register` 的数据库是身份、额度、归属和迁移状态的业务真相源。Codex2API 只保存和执行本节点的凭证与请求，不承担跨节点去重或全局额度累计。

所有远端调用经过一个显式接收 `target_id` 的客户端。旧的单目标调用保留兼容适配器，未指定目标时解析为迁移生成的默认目标 A；新代码不得再直接读取全局 `codex2api_api_url` 来决定目标。

## Codex2API 外部契约

客户端只依赖上游开源仓库已经存在的管理接口，并在目标注册时记录能力探测结果。

| 能力 | 接口 | 用途 |
|---|---|---|
| 目标健康 | `GET /api/admin/health`、`GET /api/admin/settings` | 连通性、版本和能力探测 |
| 账号清单 | `GET /api/admin/accounts?channel=codex` | 远端 ID、邮箱、身份别名、状态、额度和 `active_requests` |
| 用量探针 | `POST /api/admin/accounts/usage/probe`、`GET /api/admin/runtime-status` | 触发并等待一轮批量用量采集 |
| 凭证导入 | `POST /api/admin/accounts`、`POST /api/admin/accounts/at`、`POST /api/admin/accounts/import` | 分别导入 RT、AT 或带完整身份的 JSON |
| 凭证验证 | `GET /api/admin/accounts/{id}/test` | 验证身份和远端凭证；额度限制响应视为“凭证已鉴权” |
| 调度开关 | `POST /api/admin/accounts/{id}/enable` | `enabled=false` 设置 `DispatchPaused`，停止新请求分配 |
| 自动清理保护 | `POST /api/admin/accounts/{id}/lock` | 锁定只用于保护迁移中的账号，不能代替排空 |
| 远端刷新 | `POST /api/admin/accounts/{id}/refresh` | 可选的远端 RT 自刷新 |
| 远端删除/恢复 | `DELETE /api/admin/accounts/{id}`、`POST /api/admin/accounts/{id}/restore` | 迁移提交后的源副本清理和回滚 |
| 账号调度元数据 | `PATCH /api/admin/accounts/{id}/scheduler` | 需要时设置分组、API Key 许可和调度覆盖 |

Codex2API 没有独立的 drain API。排空流程必须先调用 `enable=false`，再反复读取账号清单，直到 `active_requests == 0`。`locked=true` 不会阻止新请求。

目标能力分为 `read_only`、`sync_only`、`migratable` 三档：缺少禁用、测试、恢复或活动请求字段的目标可以查看和 dry-run，但不能执行迁移。

## 业务对象

### 账号身份

`accounts` 继续保存现有凭证和平台字段；新增独立的稳定身份记录，避免账号本地行被删除或重新登录后改变身份。

稳定身份的匹配优先级：

1. `platform + email + workspace_id`
2. `platform + email + chatgpt_account_id`
3. `platform + email + credential_fingerprint`

身份别名（邮箱、workspace、ChatGPT account ID、凭证指纹）保留首次和最近发现时间。别名冲突进入 `ambiguous`，不自动合并。重新登录只更新 `credential_revision` 和凭证，不创建新的稳定身份。

### 目标与绑定

每个 Codex2API 实例有名称、类型（公共/企业/浮动/备用）、服务器标识、Base URL、密钥引用、默认号池、启用状态、健康状态、能力状态和最近探针/同步时间。

同一个稳定身份在每个目标最多一条绑定，绑定保存远端账号 ID、远端邮箱、远端状态、最近同步结果、凭证版本和最后发现时间。远端 ID 只在目标范围内有意义，不能拿来跨目标去重。

### 号池与租约

号池是控制面的业务分组：`PUBLIC_POOL`、企业池、`FLOAT_POOL`、`STANDBY_POOL`。账号当前归属使用租约而非永久搬迁，保存开始/到期时间、所有者、原因和版本号。一个身份同一时间只能有一条 `active` 归属；迁移期间使用 `draining`。

Codex2API 的 account group/API Key 限制可以作为目标内的流量隔离手段，但不能替代控制面 assignment。目标不支持可靠分组限制时，仍以远端 `enable` 状态和本地 assignment 为准。

### 额度账本

每次目标探针产生 `account_quota_snapshots`，至少记录 5 小时、7 天和可用时的月度窗口：使用百分比、已计费美元、剩余美元、重置时间、来源、采集时间、新鲜度和原始摘要。长期历史按小时/日汇总，避免无限增长。

账本按稳定身份和重置窗口累计，不按 Codex2API 目标累计。迁移时永远保留源目标的最后快照：

- 目标返回相同重置窗口且计数更高：采用更高的供应商计数。
- 目标返回相同窗口但计数更低：视为节点内计数重置，沿用已累计值并增加目标侧增量。
- 重置时间或计数无法判断：保留旧总额，状态标为 `stale/unknown`，暂停自动扩缩容，等待人工确认或下一次可比快照。

因此目标池内的显示可以分段，但账号在控制面上的 7 天额度不会因换池回到零。5 小时/7 天使用供应商快照；月度累计在目标没有对应字段时只记录可验证的本地用量，不伪造供应商额度。

### 调度计划与迁移记录

每次计划有唯一运行 ID、触发来源、输入快照、计划动作、预计账号租金/带宽/收入/毛利变化、状态和完成时间。每个动作单独保存原因、源/目标、预期 assignment 版本、预期凭证版本和执行结果。

迁移是独立的持久化 Saga，保存当前步骤、远端 ID、重试次数、幂等键、错误摘要和回滚状态。网络请求期间不持有数据库事务；每一步都先写入状态，再执行远端调用，之后用版本条件更新结果。

## 迁移状态机

```text
planned
  → locking
  → draining
  → uploading
  → target_disabled
  → verifying
  → assignment_committing
  → source_cleaning
  → target_enabling
  → committed
```

可从任一步进入 `rollback_required`，再进入 `rolled_back`；提交后源副本清理失败进入 `cleanup_pending`，不重复执行未知结果。

执行顺序：

1. 用本地身份锁和数据库 CAS 锁住账号，确认 assignment 版本、凭证版本和邮箱仍未变化。
2. 在源目标设置 `locked=true`，再设置 `enabled=false`，停止新请求。
3. 轮询源账号清单，等待 `active_requests` 归零。默认排空超时 10 分钟；超时则恢复源账号并将迁移置为失败。
4. 将最新 RT/AT 和身份字段上传到目标。导入返回的远端账号必须再次按邮箱和身份别名唯一匹配。
5. 立即对目标账号调用 `enabled=false`；若目标无法确认禁用成功，迁移进入人工处理，不继续删除源副本。
6. 调用目标账号测试接口，并读取目标额度快照。HTTP 成功但身份、邮箱或凭证状态不匹配时视为验证失败。
7. 在本地事务中以 `expected_assignment_version` 和 `expected_credential_revision` 提交目标 assignment、绑定和迁移事件；版本不符则回滚，不能覆盖并发重登录结果。
8. 删除源目标的旧凭证（上游删除进入回收站）；删除失败记录 `cleanup_pending`，不隐藏事实。
9. 启用目标账号并确认远端清单显示为可调度；启用失败时恢复源账号和本地 assignment。
10. 写入完成审计，释放本地锁。

提交前任何失败都执行：删除或恢复目标临时副本、恢复源账号的 `enabled` 状态、恢复源锁定状态、保留所有远端响应摘要。服务重启后 Worker 根据持久化步骤先做远端清单对账，再决定继续、回滚或暂停；不会依靠内存状态重复删除。

## 容量规划

规划器是无副作用的纯计算模块。输入包括企业过去 7 天用量、未来 24～48 小时预测、账号安全额度、重置时间、有效并发、健康状态、目标状态、租约和成本配置；输出只描述动作，不触碰远端。

安全额度：

```text
safe_7d_quota = min(
  账号历史 P25 七日产出（数据足够时）,
  账号配置上限,
  供应商当前可验证额度
)
```

历史不足时使用 1800 USD 的保守默认值，并在计划中标注“估算”。

```text
required_by_quota = ceil(forecast_7d_usd / safe_7d_quota)
required_by_concurrency = ceil(peak_concurrency / safe_concurrency_per_account)
desired_count = max(required_by_quota, required_by_concurrency, pool_min_accounts)
```

账号选择顺序：健康正常、剩余额度充足、已在目标上、距离重置较近、历史产出稳定，最后才使用浮动账号。最小租约默认 6 小时；连续两个周期低于 60% 利用率才生成缩容建议。

计划默认 `dry_run`。扩容和缩容都必须由操作员点击确认；执行接口只接受仍然新鲜、版本匹配且目标健康的计划。未来自动扩容/缩容必须分别配置开关，不能由一次总开关隐式打开。

成本模型先作为计划展示和审计输入：客户收入按实际美元用量乘客户单价，账号月租按租约占用比例分摊，带宽按目标配置分摊；缺少客户用量或带宽数据时显示“未估算”，不得把零当作真实毛利。

## 调度周期

现有 60 秒 Scheduler 保留，只增加轻量触发器；网络和长任务由后台 Worker 执行。

推荐顺序：

```text
目标健康检查
→ 批量额度采集
→ 账号/凭证异常处理
→ 生成 dry-run 计划
→ 等待人工确认
→ 串行执行迁移动作
→ 写入运行结果和告警
```

目标健康每目标最多一个并行任务；额度采集沿用目标级批量接口；重登录继续使用现有并发上限 3；同一账号的登录、迁移、删除和凭证更新互斥；同一目标的迁移串行提交。

过期额度只用于展示，不参与扩缩容。目标连续两次健康检查失败时暂停该目标的迁移计划；连续两次成功后才恢复生成可执行计划。

## API 与管理台

新增控制面接口：

```text
GET    /api/codex2api/targets
POST   /api/codex2api/targets
PATCH  /api/codex2api/targets/{target_id}
POST   /api/codex2api/targets/{target_id}/health

GET    /api/accounts/{account_id}/quota
GET    /api/accounts/{account_id}/quota/history
POST   /api/accounts/{account_id}/quota/refresh

GET    /api/scheduler/plan
POST   /api/scheduler/plan
POST   /api/scheduler/apply
GET    /api/scheduler/runs

POST   /api/accounts/{account_id}/assignment
GET    /api/accounts/{account_id}/migrations
POST   /api/migrations/{migration_id}/rollback
```

所有写接口返回操作 ID 和当前状态，不返回 Admin Key、RT、AT、Cookie 或完整远端响应。

管理台增加三个视图：

- 目标管理：目标名称、类型、健康度、能力、账号数、最近同步时间；密钥只显示掩码。
- 调度计划：当前/建议账号数、扩缩容原因、涉及账号、租约、预计成本和毛利；确认前可查看完整动作列表。
- 账号详情：当前目标和号池、5 小时/7 天额度、重置时间、绑定状态、迁移时间线和失败原因。

账号列表增加目标、号池、7 天剩余、下次重置、租约和最近同步状态列。现有单目标 `extra_json.sync_statuses` 继续展示为兼容信息，但新页面优先读取结构化绑定表。

## 代码边界

首轮实现沿用现有文件职责，并把新逻辑放到小模块：

- `core/db.py`：模型、幂等 SQLite 迁移、索引和事务辅助。
- `core/config_store.py`：新调度参数和目标密钥引用的兼容读取。
- `services/codex2api_target_client.py`：所有目标级 HTTP 调用、响应清洗和能力探测。
- `services/account_identity.py`：身份生成、别名索引和重登录 upsert。
- `services/quota_ledger.py`：快照、去重、窗口连续性和新鲜度判断。
- `services/account_migration.py`：Saga、排空、对账、回滚和审计。
- `services/pool_scheduler.py`：安全额度、容量规划、成本展示和计划 CAS。
- `services/external_sync.py`、`services/chatgpt_codex2api_health.py`：改为调用目标客户端，保留旧签名适配。
- `core/scheduler.py`：只增加周期触发和 Worker 唤醒，不承载 HTTP 编排。
- `api/codex2api_control.py`：目标、额度、计划、assignment 和迁移路由。
- `api/config.py`、`api/accounts.py`：兼容字段和账号响应扩展。
- `frontend/src/App.tsx`、`frontend/src/pages/Accounts.tsx`：导航、列表和详情扩展；新增目标/调度页面与 API 类型。

不在首轮修改 Codex2API 仓库、生产 Nginx、现有登录流程或凭证格式。

## 测试与验收

### 单元测试

- 稳定身份优先级、别名冲突和重登录 upsert。
- 目标客户端对 JSON/SSE、401/403、超时和敏感字段清洗。
- 多目标账号去重和额度窗口连续性（包括目标计数归零、重置时间变化和不确定状态）。
- 安全额度、扩容/缩容阈值、最小租约、健康/新鲜度保护和成本公式。
- Saga 每个状态转换、幂等重试、版本冲突和回滚。

### 集成与契约测试

- 用固定的 Codex2API 开源版本录制/模拟账号清单、导入、测试、启用、删除/恢复和用量探针响应。
- 验证旧单目标配置迁移后仍能完成现有自动重登录和上传测试。
- 验证 Admin Key、凭证、Cookie、计划原文和远端响应正文不进入日志或 API 响应。

### 两实例端到端测试

在本机或隔离环境启动两套未修改的 Codex2API 容器，使用独立数据库和端口：

1. 在目标 A 导入测试账号并采集额度。
2. 在 any-auto-register 建立身份、绑定和初始 assignment。
3. 生成 dry-run 扩容计划，确认后执行 A→B。
4. 验证源账号先停止新调度并等待活动请求结束。
5. 验证目标账号身份/凭证/额度通过后才启用，源账号进入回收站。
6. 验证全局额度历史连续，目标池内局部统计可以分段。
7. 注入目标上传失败、验证失败、排空超时、启用失败、网络中断和服务重启，确认源账号恢复且迁移可继续或回滚。
8. 测试 1、20、100 和 500 个账号的批量探针与 SQLite 写入冲突。

### 验收标准

- 同一账号重登录、重新导入或换目标不生成重复稳定身份。
- 迁移前后控制面 5 小时/7 天账本连续；过期或不确定数据不会触发扩缩容。
- 任何迁移动作都有可查询的计划、步骤、远端 ID 和审计结果。
- 目标异常、身份重复或版本冲突时不会批量删除账号。
- 首期扩容和缩容均需人工确认；未确认计划不产生远端写操作。
- 现有自动重登录、邮箱、MFA、同步和账号列表回归测试保持通过。
- Codex2API 源码仓库无改动。

## 上线顺序

1. 数据结构和默认目标迁移，功能开关关闭，只读采集。
2. 多目标健康与全局额度账本，观察快照连续性。
3. dry-run 规划与成本展示，不执行远端动作。
4. 手动迁移和回滚，先用两套临时容器验证，再迁移 1～3 个真实账号。
5. 观察 7～14 天后再评估自动扩容；自动缩容仍保持人工确认，除非另行确认。

每次生产发布前备份 `account_manager.db`，创建不可变 release，保留上一版本回滚入口；发布后检查 systemd、Nginx、目标健康、额度采集和一条只读账号详情链路。

## 明确不做的事

- 不修改或重新编译 Codex2API。
- 不在没有人工确认的情况下移动生产账号。
- 不把远端账号 ID 当作跨实例永久身份。
- 不把过期/缺失额度当成零额度。
- 不把 `locked` 当作排空机制。
- 不把缺失的成本数据当作零成本。
- 不在迁移中打印或返回任何凭证、密钥、Cookie 或完整远端响应。
