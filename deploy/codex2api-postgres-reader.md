# Codex2API PostgreSQL 只读连接

本项目的数据库读取器只查询 Codex2API 的账号和用量数据，不写入远端数据库。
建议在 Codex2API PostgreSQL 中创建单独的登录角色，并只授予必要表的读取权限。

以下 SQL 需要 Codex2API 数据库 owner 或具备建角色/建 schema/建视图权限的运维账号。
把 `READER_PASSWORD` 换成随机生成的强密码，
不要把它提交到仓库或写入前端设置。

```bash
set -euo pipefail
: "${READER_PASSWORD:?set READER_PASSWORD in the shell, without committing it}"

if docker exec codex2api-postgres psql -U codex2api -d codex2api -Atqc \
  "SELECT 1 FROM pg_roles WHERE rolname = 'account_manager_reader'" | grep -qx '1'; then
  docker exec -i codex2api-postgres psql -U codex2api -d codex2api \
    -v reader_password="$READER_PASSWORD" <<'SQL'
ALTER ROLE account_manager_reader LOGIN PASSWORD :'reader_password';
SQL
else
  docker exec -i codex2api-postgres psql -U codex2api -d codex2api \
    -v reader_password="$READER_PASSWORD" <<'SQL'
CREATE ROLE account_manager_reader LOGIN PASSWORD :'reader_password';
SQL
fi

docker exec -i codex2api-postgres psql -U codex2api -d codex2api <<'SQL'
GRANT CONNECT ON DATABASE codex2api TO account_manager_reader;
CREATE SCHEMA IF NOT EXISTS account_manager_reader;

-- The role resolves ``accounts`` to this projection through its search_path.
-- It never receives SELECT on public.accounts, whose credentials JSONB contains
-- refresh/access/session tokens.
CREATE OR REPLACE VIEW account_manager_reader.accounts
WITH (security_barrier = true) AS
SELECT
  a.id,
  a.name,
  a.platform,
  a.type,
  jsonb_strip_nulls(jsonb_build_object(
    'email', a.credentials->>'email',
    'user_email', a.credentials->>'user_email',
    'account_id', a.credentials->>'account_id',
    'chatgpt_account_id', a.credentials->>'chatgpt_account_id',
    'user_id', a.credentials->>'user_id',
    'workspace_id', a.credentials->>'workspace_id',
    'effective_workspace_id', a.credentials->>'effective_workspace_id',
    'plan_type', a.credentials->>'plan_type',
    'upstream_type', a.credentials->>'upstream_type',
    'subscription_expires_at', a.credentials->>'subscription_expires_at',
    'expires_at', a.credentials->>'expires_at',
    'codex_5h_used_percent', a.credentials->>'codex_5h_used_percent',
    'codex_7d_used_percent', a.credentials->>'codex_7d_used_percent',
    'codex_5h_reset_at', a.credentials->>'codex_5h_reset_at',
    'codex_7d_reset_at', a.credentials->>'codex_7d_reset_at',
    'codex_usage_updated_at', a.credentials->>'codex_usage_updated_at',
    'codex_5h_usage_updated_at', a.credentials->>'codex_5h_usage_updated_at',
    'codex_7d_usage_updated_at', a.credentials->>'codex_7d_usage_updated_at',
    'workspace_name', a.credentials->>'workspace_name',
    'codex_credits', jsonb_strip_nulls(jsonb_build_object(
      'balance', a.credentials->'codex_credits'->>'balance',
      'has_credits', a.credentials->'codex_credits'->>'has_credits'
    ))
  )) AS credentials,
  a.status,
  a.enabled,
  a.locked,
  a.cooldown_reason,
  a.cooldown_until,
  a.created_at,
  a.updated_at,
  a.deleted_at,
  a.error_message
FROM public.accounts AS a;

GRANT USAGE ON SCHEMA account_manager_reader TO account_manager_reader;
GRANT USAGE ON SCHEMA public TO account_manager_reader;
GRANT SELECT ON account_manager_reader.accounts TO account_manager_reader;
ALTER ROLE account_manager_reader SET search_path = account_manager_reader, public;
REVOKE ALL ON TABLE public.accounts FROM account_manager_reader;

-- Usage logs contain no credential JSON. Keep the grant column-scoped so a
-- future migration adding private fields does not silently widen access.
GRANT SELECT (account_id, status_code, total_tokens, account_billed,
              user_billed, created_at) ON TABLE public.usage_logs
  TO account_manager_reader;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'usage_logs'
      AND column_name = 'internal_reason'
  ) THEN
    EXECUTE 'GRANT SELECT (internal_reason) ON TABLE public.usage_logs TO account_manager_reader';
  END IF;
END
$$;
SQL
```

如果 Codex2API 的迁移新增了读取表，应在评估字段语义后再单独授予权限。不要直接
授予整个数据库的 `ALL` 权限。

应用服务必须通过稳定地址连接。当前推荐把 PostgreSQL 端口只发布到宿主机回环
地址，例如 `127.0.0.1:15432:5432`，或者把应用容器加入 Codex2API 的 Docker
网络；不要把数据库端口暴露到公网，也不要把容器临时 IP 写进配置。

读取器使用未限定 schema 的 `accounts` 名称。上面的角色级 `search_path` 会把它解析到
安全投影视图；如果部署环境已有自定义 `search_path`，请把
`account_manager_reader` 放在最前面，或在连接 DSN 中设置 `options=-csearch_path=account_manager_reader,public`。
如果投影视图已经由其他 owner 管理，请由该 owner 发布新版本的视图后再执行授权；
PostgreSQL 不允许用 `CREATE OR REPLACE VIEW` 改变既有列的顺序。

systemd 部署示例：

```text
CODEX2API_DATABASE_URL=postgresql://account_manager_reader:PASSWORD@127.0.0.1:15432/codex2api
CODEX2API_DATABASE_TARGET_ID=1
```

多实例使用 `CODEX2API_DATABASE_URL_2` 等带目标 ID 的变量。读取器会先批量查询
`accounts`，再按 `accounts.id` 聚合 `usage_logs.account_billed`；`status_code=499`
的请求与 Codex2API 账号用量接口保持同样的排除口径。查询结果写入本项目的本地
`operations_billing_snapshots` 表，远端不可用时页面继续显示最近一次快照并标记
数据时间。

`CODEX2API_DATABASE_TIMEZONE` 决定“今日”和历史日的切分，默认是控制台业务时区
`Asia/Shanghai`。如果目标 API 按 UTC 统计日用量，请将它设为 `UTC`，再做一次 API
与数据库的跨日对账；累计总额不受这个设置影响。

启用前先执行只读连通性检查，并对比一个已知账号的 API 与数据库结果：

```bash
psql "$CODEX2API_DATABASE_URL" -c 'select 1;'
psql "$CODEX2API_DATABASE_URL" -c \
  'select id, status, enabled from accounts order by id desc limit 5;'
```

确认对账一致后，再把环境变量加入应用服务并重启应用服务；Codex2API 本身不需要
重启。
