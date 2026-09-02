#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$repo_root/docker-compose.codex2api-test.yml"
project_name="any-auto-register-codex2api-test"
admin_secret="${CODEX2API_TEST_ADMIN_SECRET:-test-admin-secret}"

cleanup() {
  docker compose -p "$project_name" -f "$compose_file" down -v --remove-orphans >/dev/null 2>&1 || true
}

if [[ "${RUN_REAL_CODEX2API:-0}" != "1" ]]; then
  PYTHONPATH="$repo_root" "$repo_root/.venv/bin/pytest" -q "$repo_root/tests/test_codex2api_two_instance.py"
  exit 0
fi

trap cleanup EXIT INT TERM
docker compose -p "$project_name" -f "$compose_file" up -d --pull=missing

for port in 18080 18081; do
  ready=0
  for _attempt in $(seq 1 60); do
    if curl --fail --silent --show-error "http://127.0.0.1:${port}/health" >/dev/null; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ "$ready" != "1" ]]; then
    echo "Codex2API test target on port ${port} did not become healthy" >&2
    exit 1
  fi
  curl --fail --silent --show-error \
    -H "X-Admin-Key: ${admin_secret}" \
    "http://127.0.0.1:${port}/api/admin/accounts" >/dev/null
done

CODEX2API_TEST_ADMIN_SECRET="$admin_secret" PYTHONPATH="$repo_root" \
  "$repo_root/.venv/bin/python" - <<'PY'
from __future__ import annotations

import os
from typing import Any

from services.codex2api_target_client import Codex2APITargetClient, TargetConfig


def remote_id(payload: Any) -> int:
    if isinstance(payload, dict):
        for key in ("id", "remote_id"):
            try:
                value = int(payload.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        accounts = payload.get("accounts")
        if isinstance(accounts, list):
            for item in accounts:
                value = remote_id(item)
                if value > 0:
                    return value
    return 0


admin_key = os.environ["CODEX2API_TEST_ADMIN_SECRET"]
for target_id, port in ((1, 18080), (2, 18081)):
    client = Codex2APITargetClient(
        TargetConfig(
            id=target_id,
            name=f"fixture-{target_id}",
            base_url=f"http://127.0.0.1:{port}",
            admin_key=admin_key,
        )
    )
    client.health()
    client.capabilities()
    before = client.list_accounts()
    payload = {
        "email": f"codex2api-contract-{target_id}@example.test",
        "access_token": (
            "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
            "eyJzdWIiOiJjb250cmFjdCIsImV4cCI6MjAwMDAwMDAwMH0."
        ),
        "account_id": f"contract-workspace-{target_id}",
    }
    imported = client.import_access_token(payload)
    remote = remote_id(imported)
    rows = client.list_accounts()
    if remote <= 0:
        remote = max((remote_id(row) for row in rows), default=0)
    if remote <= 0:
        raise RuntimeError(f"target {target_id} import returned no remote id")
    client.set_locked(remote, True)
    client.set_enabled(remote, False)
    client.update_scheduler(remote, {})
    client.test_account(remote)
    client.delete_account(remote)
    client.restore_account(remote)
    client.set_locked(remote, False)
    client.set_enabled(remote, True)
    print(f"target {target_id}: read/import/lock/disable/test/delete/restore/enable passed; before={len(before)}")
PY

PYTHONPATH="$repo_root" "$repo_root/.venv/bin/pytest" -q "$repo_root/tests/test_codex2api_two_instance.py"
echo "two Codex2API target contract checks passed"
