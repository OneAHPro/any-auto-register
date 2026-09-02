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

PYTHONPATH="$repo_root" "$repo_root/.venv/bin/pytest" -q "$repo_root/tests/test_codex2api_two_instance.py"
echo "two Codex2API target contract checks passed"
