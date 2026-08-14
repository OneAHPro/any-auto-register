# LeadBee 接码路由与 SMS 页面整合设计

## 目标

把现有账号登录接码整理为三个明确模式，并让批量任务在 LeadBee Open
API 余额耗尽时安全地降级到本地 SMS 卡密池：

1. `API 优先`：默认模式；API 有容量时创建 Open API 订单，明确没有余额时
   自动为当前邮箱切换一张卡密。
2. `仅卡密池`：完全不创建 Open API 订单，只使用已经导入的 LeadBee 卡密。
3. `无需接码`：只完成邮箱登录，不进入 `add_phone` 手机验证。

同时把 LeadBee API 配置从“全局设置”移动到统一的 `SMS接码` 页面，展示
可用余额、预留余额、当前产品单价和预计还能创建的订单数。

## 已确认的外部约束

- LeadBee Open API 账户限制按服务方为本账户提供的额度执行：每分钟最多
  创建 60 个订单、每分钟最多 900 个请求、同时最多 50 个活动订单。
- 旧卡密走网页会话接口。LeadBee 会返回 `ACTIVE_LIMIT_REACHED`，最多同时
  处理 5 张卡密。
- Open API 的 `POST /orders` 不接收卡密，公开接口也没有卡密兑换接口。
  因而本次保留两个独立执行通道，不能把旧卡密伪装成 Open API 订单。
- 当前部署是单应用进程。进程内余额预留和限速器足以协调本应用发起的并发
  请求；LeadBee 返回的余额和订单状态仍是最终事实来源。

## 用户界面

### 导航与 SMS 页面

- 左侧菜单 `SMS接码池` 改名为 `SMS接码`，路由继续使用 `/sms-pool`，避免
  破坏书签和现有链接。
- 页面标题同步改为 `SMS接码`。
- 页面顶部放置 LeadBee Open API 配置卡片，下面保留卡密统计、导入和列表。
- API 卡片包含启用开关、API Key、API Secret、产品 ID、保存和连接测试。
  Key/Secret 继续保持只写：加载页面时为空，空值保存不覆盖服务器凭据。
- 全局设置页面不再渲染、校验或提交 LeadBee API 字段，防止同一配置出现
  两个编辑入口。

### API 状态摘要

连接测试以及已启用配置的自动状态查询只返回经过清洗的字段：

```json
{
  "configured_product_available": true,
  "balance_available": "35.70",
  "balance_reserved": "0.00",
  "unit_price": "1.30",
  "estimated_order_capacity": 27,
  "currency": "CNY"
}
```

金额使用十进制字符串计算，预计次数为
`floor(balance_available / unit_price)`。单价缺失、为零或格式无效时，预计
次数返回 `null`，而不是猜测价格。响应不透传产品对象、API 凭据、签名、
手机号、验证码或服务方原始错误。

SMS 页面和登录弹窗统一展示：

```text
API 可用余额 ¥35.70 · 单价 ¥1.30/次 · 预计可接 27 次
卡密池可用 18 张
```

数据加载失败时保留接码方式选择，但显示“余额暂未获取”；是否允许启动仍由
服务端配置校验决定。

### 已有账号登录弹窗

当“登录并接码获取 RT”启用时，显示单选的 `接码方式`：

- `API优先`：LeadBee API，余额不足自动切换卡密池。API 配置完整时默认选中。
- `仅卡密池`：显示当前可用卡密数，不创建 API 订单。
- `无需接码`：等价于关闭手机验证，只保存普通登录得到的 token。

弹窗不再提供逐任务粘贴原始卡密的文本框；卡密统一先导入 `SMS接码` 页面。
后端继续接受旧字段，保证历史请求和重试记录可读取。

新增显式请求字段：

```text
chatgpt_existing_account_sms_mode = api_fallback_pool | pool | none
```

新前端始终发送该字段。后端将它规范化成现有 phone/API/pool 状态；字段缺失
时继续兼容旧版 `bind_phone`、`leadbee_api` 和 `use_sms_pool` 请求。显式选择
`pool` 时不会因为全局 API 已启用而被强制切回 API。

接码任务的并发输入最大为 50。这里表示账号登录 worker 数量，不代表卡密
同时激活数；卡密实际并发仍由独立的 5 槽位队列控制。

## 后端调度架构

### 独立并发通道

把当前共享的 `BoundedSemaphore(5)` 拆成：

```text
LeadBee Open API flow slots = 50
LeadBee legacy card flow slots = 5
```

API 和卡密互不占用对方槽位。卡密队列取消当前 30 秒排队失败规则，改为
可取消的 FIFO 等待：

- 等待期间反复检查停止/跳过指令；
- 只有拿到卡密槽位后才激活卡密并启动卡密流程期限；
- 任一终态、异常或取消都在 `finally` 中释放槽位；
- 排队本身不消耗卡密，也不启动 540 秒 provider 结算期限。

因此 25 个账号从 API 降级时，最多 5 个同时接码，其余 20 个排队。它们只
会变慢，不会因为 50 与 5 的差异自动失败。

### API 容量协调

新增一个进程级 `LeadBeeCapacityCoordinator`，在 API worker 消费 OAuth
resume context 之前完成以下原子操作：

1. 在互斥区内获取当前产品价格与可用余额。
2. 从可用余额中扣除本进程已经承诺但尚未创建订单的本地预留。
3. 容量足够时，为当前 `client_order_id` 创建一份临时容量租约。
4. 容量不足时抛出内部 `LeadBeeCapacityExhausted`，此时尚未请求
   `POST /orders`，也尚未消费 OAuth resume context。
5. API 订单创建成功后提交租约；创建前退出则释放租约。

同一个 `client_order_id` 的重试复用同一租约。金额只用 `Decimal`，不使用
浮点数。连接测试和运行时容量解析共用同一个小型解析模块，避免 UI 与调度
对价格字段的解释不一致。产品目录和单价最多缓存 60 秒；每次容量分配都读取
新余额，并扣除尚未落到服务方余额中的本地租约。

API 客户端增加进程级滑动窗口限速：创建请求不超过 60 次/分钟，全部请求
不超过 900 次/分钟。等待限速时遵守 provider deadline 和任务取消。轮询
间隔至少为 4 秒，并继续尊重服务方返回的更大
`next_poll_after_seconds`/`Retry-After`，保证 50 个活动订单的常规轮询低于
900 次/分钟。

### 安全回退

只有以下两种“确定没有创建 API 订单”的结果允许自动回退：

- 容量协调器在 `POST /orders` 前确定可用余额小于产品单价；
- LeadBee 返回明确、非歧义的余额不足业务拒绝，且客户端确认没有订单 ID。

以下结果不回退：网络超时、连接中断、HTTP 5xx、损坏的 2xx JSON、幂等
冲突、未知订单状态或任何已经标记 `card_at_risk/order_at_risk` 的结果。此时
保留 API 客户端订单标识并失败/隔离，避免远端实际已扣费后又消耗一张卡密。

API 容量不足的批量账号执行以下流程：

1. 记录不含余额原文和凭据的安全状态 `api_capacity_exhausted`。
2. 原子调用 SMS 池 `reserve(task_id, count=1)`，将卡密绑定到当前邮箱/attempt。
3. 没有卡密时，以“API 余额不足且卡密池无可用卡密”结束该账号。
4. 有卡密时，使用同一账号已保存的 OAuth resume context 启动 legacy 卡密
   接码；进入 5 槽位 FIFO 队列。
5. 任务停止、跳过或启动失败时，未激活的卡密恢复为可用；已激活卡密继续
   使用现有 restored/consumed/unusable/active_unknown 结算规则。

容量预检失败发生在 OAuth resume context 被取出前。若服务方在 `POST
/orders` 时才返回明确余额拒绝，runner 必须保留一份同 attempt 的 prepared
context 快照，确认无订单后再交给卡密 runner；不得退回到重新发送邮箱验证码
的完整登录流程。

回退只适用于 `api_fallback_pool`。`pool` 从一开始就使用卡密，`none` 从不
启动 phone provider。

### 持久化和日志

- API 尝试先持久化 `client_order_id`，容量不足回退后再把实际 SMS pool
  item、脱敏卡密提示和 provider mode 写入同一 attempt binding。
- 只要 binding 无法持久化，就在调用 provider 前 fail closed，避免产生无法
  审计的付费订单或卡密消耗。
- 对用户可见日志使用固定文案：`API 余额不足，已切换 SMS 卡密接码`、
  `卡密接码排队中`、`API 余额不足且卡密池无可用卡密`。
- 日志、任务快照、账号 extra、HTTP 响应不得出现 API Key、Secret、签名、
  完整卡密、完整手机号或短信验证码。

## 兼容性

- 单账号手机验证弹窗继续支持 API、旧卡密和手动手机号，不自动消费批量
  SMS 池；本次自动回退只用于已有账号批量登录任务。
- 旧版显式 API 请求仍表现为 API-only；只有新的
  `api_fallback_pool` 模式启用自动回退。
- 旧版 `pool` 请求继续在任务入队时预留所需卡密，保证原有“数量不足立即
  409”的行为。
- API 关闭时，旧卡密、SMS pool、重试绑定与恢复语义保持不变。

## 测试策略

后端测试至少覆盖：

- API 50 槽位与卡密 5 槽位互相独立；
- 25 个降级账号按 5 个一批执行，等待超过 30 秒也不因旧排队期限失败；
- 等待卡密槽位时停止/跳过可立即退出，且卡密正确释放；
- 本地余额租约阻止 50 个线程用同一份余额超额下单；
- 60 create/min、900 request/min、最小 4 秒轮询及取消/deadline；
- POST 前余额不足安全降级、明确余额拒绝安全降级；
- transport/5xx/malformed 2xx/unknown/idempotency conflict 不降级；
- 卡密不足时只让没有卡密的账号失败，已经获得卡密的账号继续；
- API→卡密 binding 切换、卡密终态、任务停止及重试保持可审计；
- 旧 API-only、旧 SMS pool、直接卡密和无需接码回归。

前端测试至少覆盖：

- 菜单和页面标题为 `SMS接码`，路由不变；
- LeadBee 设置只出现在 SMS 页面，全局设置不再出现；
- 写入型凭据不会被回填或渲染；
- 余额、单价、预计次数和卡密池可用数展示；
- API 启用时默认 `API优先`，三种模式生成互斥且最小的请求 payload；
- `仅卡密池` 在全局 API 启用时仍保持 pool 模式；
- API 状态加载失败、保存、延迟响应和组件卸载不产生陈旧状态；
- 前端构建及现有登录、Settings、SMS pool 测试全部通过。

## 上线验证

先运行所有后端和前端测试并构建静态资源。部署前确认没有正在运行的任务，
创建数据库在线备份，发布精确 commit 到新 release，原子切换 symlink 并只
重启应用服务。上线后先做只读产品/余额检查，再做：

1. 一个 API-only 账号；
2. 一个 `API优先` 且容量充足的账号；
3. 使用测试容量注入验证 API→卡密回退，不消耗真实重复订单；
4. 一个 `仅卡密池` 账号；
5. 一个 `无需接码` 账号。

任何健康检查、启动日志或任务回归失败都切回上一 release。
