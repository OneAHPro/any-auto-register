# 远端 Codex2API 账号展示与实时额度设计

## 目标

在不复制 Codex2API 凭据的前提下，将直接导入 Codex2API 的 JSON 账号同步到项目账号列表，展示远端状态与实时额度，并让这些账号进入项目号池调度。

## 方案

Codex2API 继续作为完整 JSON 凭据的导入和保存位置。项目列表请求 ChatGPT 远端账号清单，先触发一次有界的用量探针，再读取账号摘要。远端账号按 `target_id + remote_id` 建立稳定的项目身份；已经存在的本地账号优先按身份/邮箱复用，只有远端独有账号才使用 `local_account_id=0` 的远端托管绑定。远端摘要只保存邮箱、账号 ID、套餐、状态、启用/锁定状态、用量和时间戳。

远端账号默认归入目标的默认号池，但只有远端状态为 `active`、`ready` 或 `rate_limited` 且启用时才建立 active assignment。未授权、停用和锁定账号保留绑定并进入 standby，避免被调度。项目内同目标号池调整调用现有 assignment CAS；跨目标迁移继续要求本地凭据或原始 JSON，远端摘要不参与凭据复制。

## 列表与卡片

`GET /accounts?platform=chatgpt&include_live=1` 返回本地账号和远端独有账号的合并列表，并标记 `account_source=codex2api`、`remote_only=true`、目标和远端 ID。远端托管卡片隐藏密码/Refresh Token 操作，详情使用已返回摘要，不请求不存在的本地账号详情接口。卡片采用固定高度、固定底部操作区和统一额度区，缺少某项数据时显示占位符而不改变整体高度。

实时额度使用 Codex2API 的 `usage_percent_7d`、`usage_7d_detail.requests` 和 `usage_7d_detail.account_billed` 作为展示回退；`billed_7d` 仍保留原有重置窗口语义，不把滚动计费值写入额度估算账本。卡片显示数据采集时间，远端请求或探针失败时标记为不可用并保留最近一次时间。

## 验证

- 后端单测覆盖远端独有账号投影、身份/绑定/assignment 建立、滚动计费展示回退和远端状态过滤。
- 前端单测覆盖远端托管卡片、无凭据操作、计费显示和卡片等高样式。
- 运行后端相关 pytest、前端相关 Vitest、TypeScript/Vite 构建和 ESLint。
