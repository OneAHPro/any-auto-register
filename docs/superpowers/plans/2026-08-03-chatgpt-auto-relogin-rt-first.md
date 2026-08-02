# ChatGPT RT 优先自动认证维护实施计划

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将现有全量验证码自动重登替换为 10 分钟 RT 优先维护，修复忘记密码账号的回退登录，并增加任务记录、倒计时和阈值 SMTP 告警。

**Architecture:** OAuth 刷新层只返回 `valid`、`invalid`、`transient_error` 三态；服务层在单账号锁内负责编排刷新、原子持久化、必要的完整登录和 Codex2API 覆盖验证；自动任务选择新编排入口，手工重登保持强制完整登录。登录凭据同时保存在账号自包含上下文、账号密码字段和对应邮箱池记录中。

**Tech Stack:** Python 3、FastAPI、SQLModel、curl_cffi、React、Ant Design、Vitest、Docker Compose。

---

### Task 1: 为 OAuth Refresh Token 建立可判定的三态结果

**Files:**
- Modify: `platforms/chatgpt/token_refresh.py`
- Create: `tests/test_chatgpt_token_refresh.py`

**Steps:**
1. 先写失败测试，覆盖成功并轮换 RT、`invalid_grant`/`token_invalidated`、429、5xx、未知 4xx、网络异常和响应缺少 Access Token。
2. 运行 `python -m pytest -q tests/test_chatgpt_token_refresh.py`，确认测试因缺少三态字段失败。
3. 扩展 `TokenRefreshResult`，增加结构化状态、HTTP 状态和脱敏错误码；解析 JSON 错误但不记录响应正文或令牌。
4. 再运行同一测试文件，确认全部通过。

### Task 2: 实现 RT 优先的单账号自动编排

**Files:**
- Modify: `services/chatgpt_relogin.py`
- Modify: `tests/test_chatgpt_relogin.py`

**Steps:**
1. 先写失败测试：RT 成功时保存轮换 AT/RT、同步 Codex2API且不完整登录；明确失效时才完整登录；暂时失败时不写数据库、不登录；同步失败使用独立阶段。
2. 增加自动账号列表，使所有带 RT 的可见 ChatGPT 账号都进入自动检测；运行时发现 RT 缺失则按明确失效处理，手工完整重登资格判断保持原语义。
3. 在现有单账号锁内新增 `refresh_or_relogin_chatgpt_account`，复用原子持久化和 Codex2API 覆盖验证，确保暂时失败不修改旧令牌。
4. 运行 `python -m pytest -q tests/test_chatgpt_relogin.py`，修复至通过。

### Task 3: 自动任务与手工任务选择不同入口

**Files:**
- Modify: `api/tasks.py`
- Modify: `tests/test_chatgpt_relogin_task.py`

**Steps:**
1. 先写失败测试，断言 `automation=true` 调用 RT 优先入口，普通任务仍调用 `relogin_chatgpt_account`。
2. 从任务元数据选择账号处理函数，并让日志/持久化详情区分 RT 刷新、完整登录和延后重试。
3. 运行 `python -m pytest -q tests/test_chatgpt_relogin_task.py`，确认互斥、停止和并发测试继续通过。

### Task 4: 修复忘记密码账号的凭据持久化与旧数据恢复

**Files:**
- Modify: `platforms/chatgpt/plugin.py`
- Modify: `services/chatgpt_relogin.py`
- Modify: `core/applemail_pool.py`
- Modify: `core/base_mailbox.py`
- Modify: `tests/test_chatgpt_plugin.py`
- Modify: `tests/test_chatgpt_relogin.py`
- Modify: `tests/test_icloud_mailbox.py`
- Modify: `tests/test_accounts_api_sanitization.py`

**Steps:**
1. 先写失败测试：注册成功后上下文包含当前密码和收件 URL；历史记录优先使用主表密码；完全缺少密码但有收件 URL 时生成新密码并更新已使用记录；保存密码被服务端以 401 / `invalid_credentials` 明确拒绝时强制重置一次，而超时不重置；API 输出仍隐藏嵌套秘密。
2. 将 ChatGPT 凭据元数据改为最小自包含快照，排除 claim ID、临时新密码和运行缓存。
3. URL 凭据恢复按“上下文 → 邮箱池 → 主表密码”合并；主表密码存在时覆盖错误的 `password_reset_required=true`。
4. 为已使用的指定邮箱记录增加原子密码更新路径，保持 `pool_state=used` 与 `enabled=false`；明确的旧密码 401 触发单次忘记密码兜底；登录成功后同步账号主表密码与上下文。
5. 运行上述四个测试文件，确认新旧账号路径都通过且无秘密泄露。

### Task 5: 将单一自动任务周期改为 10 分钟

**Files:**
- Modify: `services/chatgpt_auto_relogin.py`
- Modify: `api/config.py`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `tests/test_chatgpt_auto_relogin.py`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`

**Steps:**
1. 先把测试期望改为默认/最小 10 分钟，并验证 9 分钟被拒绝、10 分钟被接受。
2. 运行后端相关测试与 `npm test -- --run` 的两个前端测试文件，确认红灯。
3. 修改后端默认值、校验下限、前端初始值和归一化边界；页面说明明确 RT 优先和仅失效才验证码重登。
4. 重跑相关测试直至通过。

### Task 6: 增加每轮阈值邮件、任务历史统计和下次执行倒计时

**Files:**
- Create: `services/chatgpt_auto_relogin_alerts.py`
- Create: `tests/test_chatgpt_auto_relogin_alerts.py`
- Modify: `core/task_runtime.py`
- Modify: `api/config.py`
- Modify: `api/tasks.py`
- Modify: `tests/test_task_runtime.py`
- Modify: `tests/test_chatgpt_relogin_task.py`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `frontend/src/pages/RunningTasks.tsx`
- Create: `frontend/src/pages/RunningTasks.test.tsx`
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `frontend/src/pages/Accounts.test.tsx`

**Steps:**
1. 先写邮件边界测试：两项都低于阈值不连接 SMTP，任一项达到阈值发送一次，587 使用 STARTTLS，465 使用 SMTP SSL，发送异常不泄露凭证。
2. 增加 SMTP 运行时配置与默认阈值 5；配置 GET 始终隐藏密码，PUT 空密码保留旧值。
3. 自动任务按线程安全方式汇总 RT 明确失效和完整重登失败，任务元数据持久化统计与邮件结果；手工或停止任务不发送。
4. 在“任务运行”标记自动认证任务并显示统计，在 ChatGPT 账号数后轮询状态并显示倒计时。
5. 运行对应后端和前端测试直至通过。

### Task 7: 全量验证、部署和仓库交付

**Files:**
- Verify all changed files
- Runtime data: `/database/account_manager.db` and `/runtime/mail/*.json` (backup before deployment)

**Steps:**
1. 运行后端相关组合测试，再运行后端全量 `python -m pytest -q`。
2. 在 `frontend` 运行 `npm test`、`npm run build` 和 `npm run lint`。
3. 检查 Git 差异和敏感信息，确认只包含本次功能文件。
4. 备份数据库和邮箱池，Docker Compose 重新构建并启动服务。
5. 把 SMTP 凭证写入运行时配置，并设置阈值 5；确认配置读取不回传 SMTP 密码，执行受控邮件发送验证。
6. 把运行配置设置为启用、间隔 10、并发 10；验证状态接口仅存在一套任务并显示 10 分钟排期。
7. 对受控账号验证 RT 快速路径；确认两个历史账号已被新恢复逻辑判定为可完整重登。
8. 暂存、提交并执行 `git push origin HEAD:main`，确认远端 main 指向新提交。
