# ChatGPT 自动化 Bark 强提醒设计

## 目标

在保留现有 SMTP 邮件告警的同时，为 iPhone 增加 Bark 强提醒。现有两类业务告警沿用原触发条件，并在触发时并行尝试邮件和 Bark：

- `Codex 重登失败账号告警`
- `Codex 剩余额度不足告警`

Bark 通知固定使用 `level=critical`、`call=1` 和 `sound=alarm`，用于在夜间持续响铃约 30 秒，并在 Bark 已取得 iOS 紧急通知权限时穿透静音与勿扰模式。

## 方案选择

第一版采用项目服务器直接调用 Bark HTTP API。

- 相比 Pushover Emergency，不增加订阅、回执状态和账号体系。
- 相比 Twilio 电话，不增加电话号码、语音脚本、通话费用和重拨状态机。
- 相比 Home Assistant，本项目无需依赖额外家庭自动化服务。

本版不实现人工确认、电话升级或外部心跳监控；这些能力可在后续独立设计。

## 后台配置

在现有“ChatGPT 自动重登”设置卡片中，将“邮件告警”调整为“告警通知”，保留全部 SMTP 字段，并新增：

- `bark_enabled`：是否启用 Bark 强提醒，默认关闭。
- `bark_endpoint`：Bark App 提供的完整推送地址，例如 `https://api.day.app/DEVICE_KEY`。
- “发送测试 Bark 通知”按钮：使用当前表单中尚未保存的开关与地址发送测试通知。

`bark_endpoint` 包含设备密钥，按敏感凭证处理：

- 配置读取接口始终返回空字符串，不把已保存地址发送到浏览器。
- 保存表单时，空值表示保留已有地址；关闭 Bark 不删除已保存地址。
- 测试接口中空地址沿用服务器已保存值。
- 地址、设备密钥、请求正文不进入任务日志、异常文本或 API 响应。
- 只接受 Bark 官方 `https://api.day.app/DEVICE_KEY` 地址；拒绝内网主机、用户信息、查询参数、畸形 URL 和显式重定向；去除末尾 `/` 后发送。

## Bark 请求

项目使用 Python 标准库向配置地址发送 JSON `POST`，不新增运行时依赖。请求字段固定为：

```json
{
  "title": "业务告警标题",
  "body": "精简后的告警汇总",
  "group": "Any Auto Register · Codex",
  "level": "critical",
  "call": "1",
  "sound": "alarm"
}
```

网络超时为 20 秒，响应体限制为 64 KiB。HTTP 非 2xx、显式重定向、返回体不是 JSON、或 Bark 返回 `code != 200` 均记为发送失败。对外只返回净化后的失败类型。

## 触发与内容

### 重登失败告警

仍以 `quota_eligible_failure_count >= chatgpt_auto_relogin_alert_threshold` 为触发条件。Bark 标题与邮件标题保持一致，正文包含：

- 仍有额度的重登失败数量；
- 其中封禁或删除数量；
- 正常可用账号数量；
- 当前估算剩余额度；
- 任务 ID。

### 剩余额度不足告警

仍以配置阈值大于零且 `estimated_remaining_usd < chatgpt_auto_relogin_quota_alert_threshold_usd` 为触发条件。连续低于阈值时，每轮自动任务都发送。正文包含：

- 当前估算剩余额度；
- 配置阈值；
- 正常可用账号数量；
- Codex2API 账号总数；
- 任务 ID。

额度查询失败、手工任务、停止任务和异常终止任务继续跳过额度告警。

## 渠道隔离与任务元数据

邮件与 Bark 是两个独立发送渠道：

- SMTP 未配置或发送失败时仍尝试 Bark。
- Bark 未配置或发送失败时仍保留邮件结果。
- 两个渠道的异常都不改变已完成的自动任务状态。
- 现有邮件元数据保持兼容，新增 Bark 专用结果字段：
  - `bark_alert_sent`、`bark_alert_reason`、`bark_alert_error_type`
  - `bark_quota_alert_sent`、`bark_quota_alert_reason`、`bark_quota_alert_error_type`
- 任务日志分别写明邮件和 Bark 的发送、未启用、未达到阈值和净化后的失败状态。

## 模块边界

- `services/chatgpt_bark_alerts.py`：Bark 配置解析、请求发送、两类告警策略和测试通知。
- `api/tasks.py`：在现有邮件调用旁独立调用 Bark 服务，持久化结果和日志。
- `api/config.py`：配置白名单、敏感字段保留、地址校验和测试接口。
- `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`：Bark 表单与测试按钮。
- `frontend/src/pages/Settings.tsx`：加载时清空敏感地址、保存时保持表单行为。

## 测试

后端覆盖：

- 两类阈值边界和每轮重复发送；
- `critical`、`call=1`、`alarm`、标题和正文；
- HTTP、JSON 和 Bark 业务状态失败；
- Bark 地址、设备密钥和请求正文不泄漏；
- 测试接口使用未保存值、空值保留已保存地址；
- SMTP 与 Bark 任一失败时另一渠道仍执行；
- 停止任务与额度查询失败不误发 Bark。

前端覆盖：

- 新字段、帮助文案和默认值；
- 配置加载后不回显 Bark 地址；
- 保存空地址不覆盖服务器凭证；
- 测试按钮成功、失败和重复点击保护。

部署验收使用测试按钮向用户 iPhone 发送一条明确标注为测试的强提醒；业务告警不通过伪造生产故障验证。
