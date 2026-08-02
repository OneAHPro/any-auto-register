# ChatGPT TOTP Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复同时存在邮箱与 TOTP 因子时错误选择邮箱 MFA 的问题，让密码 + TOTP 导入记录使用已提供的密钥登录。

**Architecture:** 保持现有 MFA 提交函数不变，只在 `OAuthClient._submit_mfa_challenge` 调整分派优先级。用直接调用真实分派方法的单元测试锁定优先级和回退行为。

**Tech Stack:** Python 3、unittest、unittest.mock、Docker Compose

---

### Task 1: 锁定 MFA 选择策略

**Files:**
- Modify: `tests/test_chatgpt_register.py`
- Modify: `platforms/chatgpt/oauth_client.py`

- [ ] **Step 1: 写入失败测试**

在 `OAuthClientPasswordlessTests` 中增加三个测试：

```python
def test_submit_mfa_prefers_supplied_totp_over_email_factor(self):
    client = self._make_client()
    state = FlowState(
        page_type="mfa_challenge",
        payload={"factors": [
            {"id": "totp-1", "factor_type": "totp"},
            {"id": "email-1", "factor_type": "email"},
        ]},
    )
    expected = FlowState(page_type="consent")
    client._submit_totp_mfa_challenge = mock.Mock(return_value=expected)
    client._submit_email_mfa_challenge = mock.Mock()
    result = client._submit_mfa_challenge(
        state,
        email="user@example.com",
        skymail_client=mock.Mock(),
        totp_secret="JBSWY3DPEHPK3PXP",
        device_id="device-fixed",
    )
    self.assertIs(result, expected)
    client._submit_totp_mfa_challenge.assert_called_once()
    client._submit_email_mfa_challenge.assert_not_called()
```

另两个测试分别断言无密钥时回退邮箱、纯 TOTP 无密钥时保留明确错误。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `pytest -q tests/test_chatgpt_register.py -k 'submit_mfa_prefers_supplied_totp_over_email_factor or submit_mfa_falls_back_to_email_without_totp_secret or submit_mfa_totp_only_without_secret_reports_missing_secret'`

Expected: 第一项 FAIL，因为当前实现调用邮箱 MFA；另外两项 PASS。

- [ ] **Step 3: 实现最小修复**

在邮箱分支之前加入：

```python
if totp_factor is not None and str(totp_secret or "").strip():
    return self._submit_totp_mfa_challenge(
        state,
        totp_secret=totp_secret,
        device_id=device_id,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )
```

随后保留邮箱分支和纯 TOTP 缺密钥分支。

- [ ] **Step 4: 运行定向和全量测试**

Run: `pytest -q tests/test_chatgpt_register.py tests/test_chatgpt_existing_account_login.py`

Expected: 全部 PASS。

Run: `pytest -q`

Expected: 全量后端测试 PASS，只有项目既有 skip。

### Task 2: 部署并恢复 6 个账号

**Files:**
- Runtime input: `/Users/xuann/Documents/注册机/ChatGPT密码TOTP_首批6.txt`
- Runtime input: `/Users/xuann/Documents/注册机/当前可用接码卡密_6.txt`

- [ ] **Step 1: 重建并启动本地容器**

Run: `docker compose -f docker-compose.local.yml up -d --build`

Expected: `any-auto-register` 状态为 healthy/running，`http://127.0.0.1:18080/api/config` 返回 200。

- [ ] **Step 2: 重新导入 6 条密码 + TOTP 记录**

通过 `/api/mail-imports` 导入 `ChatGPT密码TOTP_首批6.txt`，绑定新的 AppleMail 池文件。

Expected: `success=6`、`failed=0`、池数量为 6。

- [ ] **Step 3: 启动登录接码任务并监控**

通过 `/api/tasks/register` 使用 6 张唯一 LeadBee 卡密创建任务。

Expected: 日志出现 `[stage=mfa] totp`，不出现“无法读取邮箱验证码”；每张卡仅在手机 OAuth 事务就绪后激活。

- [ ] **Step 4: 核对最终数据**

检查任务结果、`task_logs`、本地账户记录和 Codex2API 远端账户。

Expected: 对每个成功账号均有手机验证完成、Refresh Token 保存及 Codex2API `[OK]`；失败账号对应卡密状态按 `exchange_code_consumed` 单独保留。
