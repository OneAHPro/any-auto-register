# MailAPI 密码提交与账号恢复设计

## 背景与根因

目标账号 `m82e48a8ff7f3f71b8358@o6f4.my` 通过 MailAPI 成功收取并验证邮箱 OTP，认证服务也成功完成密码重置，但本地凭据提交返回失败。

生产日志证明这是确定性的生命周期错误：

1. 登录开始时，`OutlookMailbox.get_email_by_address()` 从 `outlook_accounts` 取出记录并立即删除。
2. 密码重置成功后，`OutlookMailbox.commit_password_reset()` 再按邮箱查询同一张表。
3. 被取出的记录已经不存在，因此提交函数返回 `False`，上层将远端成功误判为本地失败。

目标账号此前由项目托管的 TOTP 密钥和恢复码随账号记录被删除。数据库在线表和数据库备份中已没有该账号记录，但当前生产进程内存仍保留一份完整的 `mailbox_login_context`；邮箱、签名 URL、32 位 TOTP 密钥和 30 位恢复码均能在同一 JSON 上下文中精确匹配。

## 目标

- 密码重置成功后，即使邮箱记录已被领取并从可用池删除，也能可靠保存新密码。
- 已领取记录不得重新成为可重复分配的可用邮箱。
- Microsoft/MailAPI 来源的密码重置继续使用 Outlook/MailAPI 后端，不误转到 AppleMail 文件池。
- 恢复账号 1377 的邮箱接码上下文、TOTP 密钥和恢复码，但不恢复任何已失效或无法确认的密码、Access Token 或 Refresh Token。
- 上线后只执行一次真实登录验证；成功后由正常持久化链路写入新密码和最新令牌。

## 方案

### 1. 修正 Outlook 邮箱记录生命周期

`OutlookMailbox.commit_password_reset()` 保持现有的输入校验。提交时：

- 如果 `outlook_accounts` 中仍有该邮箱，更新其密码和时间戳。
- 如果记录已被领取并删除，使用当前 `MailboxAccount.extra` 中的邮箱来源字段重新创建记录，写入新密码，并将 `enabled` 设为 `False`。
- 写回 `MailboxAccount.extra.password`，清除 `password_reset_required` 和临时 `new_password`。

禁用状态表示凭据已持久化但邮箱已消费。若后续登录步骤失败且属于可重试错误，现有 `requeue_account()` 会将同一记录更新为启用状态；成功路径不会让邮箱重新进入可分配池。

### 2. 保留 Microsoft/MailAPI 后端

`services.chatgpt_relogin._build_email_service()` 在处理 URL 密码重置凭据时，根据原始 `mailbox_login_context.provider` 选择后端：

- `microsoft` / `outlook` 使用 `OutlookMailbox`。
- 其他既有 URL 文件池凭据继续使用 `AppleMailMailbox`。

这避免 Microsoft 账号在强制重置时被错误路由到不存在的 AppleMail 池文件。

### 3. 恢复账号 1377

部署前对生产 SQLite 数据库做在线备份。随后从当前主应用进程内存重新定位完整邮箱上下文，并在写库前验证：

- 邮箱精确匹配目标邮箱。
- MailAPI URL 的签名匹配用户提供的地址。
- TOTP 密钥长度为 32，恢复码长度为 30。
- 恢复内容的哈希与调查阶段记录一致。

恢复 `accounts.id = 1377`，仅写入：

- `platform = chatgpt`
- 目标邮箱
- 空密码、空 Token、空用户 ID
- `status = registered`
- 包含已验证邮箱上下文的 `extra_json`

空密码会强制正常登录链路重新设置一个真实密码；不会把旧密码或本地生成但未确认的密码当成有效凭据。

### 4. 测试与验证

先添加失败测试，再实施代码：

1. 建立 MailAPI Outlook 记录。
2. 调用 `get_email_by_address()`，断言记录已从池中删除。
3. 调用 `commit_password_reset()`，修复前应失败。
4. 修复后断言提交成功、密码保存、MailAPI URL 保留、记录存在且禁用。
5. 覆盖失败回退：`requeue_account()` 能把禁用记录重新启用。
6. 覆盖 Microsoft URL 重置上下文选择 Outlook 后端。

运行 Outlook 邮箱、ChatGPT 重登和相关导入测试。部署后验证数据库完整性、服务状态、HTTP 健康检查和账号恢复字段，再对账号 1377 执行一次真实重登。

成功标准：

- 不再出现“密码已在认证服务重置，但本地凭据保存失败”。
- TOTP 验证使用恢复的项目密钥通过。
- 账号 1377 保存真实新密码和最新登录令牌。
- 邮箱池中保留禁用的凭据记录，不会被重复分配。

## 失败与回滚

- 任一内存上下文校验不符时停止数据恢复，不写入猜测值。
- 真实登录只尝试一次；外部认证异常时保留账号和可重试邮箱记录，不连续触发密码重置。
- 代码部署使用独立 release 目录和原子软链接切换。
- 数据回滚使用部署前 SQLite 在线备份；代码回滚切回上一 release。

## 不在本次范围

- 重构整个邮箱池为完整的 claimed/used 状态机。
- 批量恢复其他已删除账号。
- 改变全局邮箱提供商配置或其他账号的登录策略。
