# No-Downtime Server Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded history/log maintenance and deploy the application under `/www` beside the existing Docker services, then publish it at `accounts.anhepro.com`.

**Architecture:** Keep the existing Docker daemon and its data root unchanged. Run this project as a dedicated systemd service with persistent files under `/www/any-auto-register`, fronted by an isolated Nginx location. The existing scheduler performs conservative authentication checks and a daily, failure-isolated history cleanup.

**Tech Stack:** Python/FastAPI, SQLModel/SQLite, systemd, Xvfb, Nginx, logrotate, Cloudflare DNS, Certbot.

---

### Task 1: Add retention maintenance with failing tests

**Files:**
- Create: `services/task_history_retention.py`
- Create: `tests/test_task_history_retention.py`
- Modify: `core/config_store.py` only if environment-backed retention keys are needed

- [ ] **Step 1: Write tests for normal/failure retention and active preservation**

  Use a temporary SQLite engine, create `TaskLog` and `TaskRunModel` rows at
  29/31/89/91 days, and assert that normal rows older than 30 days and failed
  rows older than 90 days are deleted while pending/running rows remain.

- [ ] **Step 2: Run the focused test and observe the expected import failure**

  Run: `pytest -q tests/test_task_history_retention.py`

- [ ] **Step 3: Implement one transaction per table with configurable windows**

  Expose `cleanup_task_history(database_engine=..., now=..., normal_days=30,
  failure_days=90)` and return deleted counts. Select only terminal task runs;
  catch no exceptions inside the helper so the scheduler can log and isolate
  failures.

- [ ] **Step 4: Run the focused test until green**

  Run: `pytest -q tests/test_task_history_retention.py`

- [ ] **Step 5: Commit and push the tested retention change**

  Run: `git add services/task_history_retention.py tests/test_task_history_retention.py && git commit -m "feat: retain task history within bounded windows" && git push origin HEAD:main`

### Task 2: Integrate cleanup into the scheduler

**Files:**
- Modify: `core/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Add a failing scheduler isolation test**

  Patch the cleanup helper to raise once and assert `Scheduler.run_once` still
  completes the authentication tick and records the maintenance error without
  propagating it.

- [ ] **Step 2: Run the test and confirm it fails before integration**

  Run the focused scheduler test with `pytest -q`.

- [ ] **Step 3: Run cleanup once at startup, then at most daily**

  Add a monotonic timestamp and call the helper from `run_once`. Read optional
  `TASK_HISTORY_RETENTION_DAYS` and `TASK_HISTORY_FAILURE_RETENTION_DAYS`
  environment values, bounded to positive integers, with 30/90 defaults.

- [ ] **Step 4: Run focused scheduler tests and the complete backend suite**

  Run: `pytest -q tests/test_scheduler.py tests/test_task_history_retention.py`
  followed by the repository backend test command.

- [ ] **Step 5: Commit and push the scheduler integration**

  Run: `git add core/scheduler.py tests && git commit -m "feat: schedule bounded history cleanup" && git push origin HEAD:main`

### Task 3: Add no-downtime deployment assets

**Files:**
- Create: `deploy/systemd/any-auto-register.service`
- Create: `deploy/nginx/accounts.anhepro.com.conf`
- Create: `deploy/logrotate/any-auto-register`
- Create: `deploy/README.md`

- [ ] **Step 1: Add static deployment assets**

  Use `/www/any-auto-register/current`, `/www/any-auto-register/shared`,
  loopback port `18081`, solver port `18889`, recovery concurrency `4`, and
  explicit systemd resource limits. Configure Nginx websocket/SSE headers and
  logrotate `20M` with five compressed generations.

- [ ] **Step 2: Validate assets locally**

  Run shell syntax checks on helper snippets, inspect paths, and run the
  existing backend/frontend tests. No service is restarted during this step.

- [ ] **Step 3: Commit and push deployment assets**

  Run: `git add deploy && git commit -m "ops: add isolated www deployment assets" && git push origin HEAD:main`

### Task 4: Stage the server release without touching existing services

**Files/locations:**
- Server: `/www/any-auto-register/releases/<commit>`
- Server: `/www/any-auto-register/shared/data`
- Server: `/www/any-auto-register/shared/logs`

- [ ] **Step 1: Create the release directory and service user**

  Create the dedicated directories and a non-root service account. Do not stop
  Docker or modify `/var/lib/docker`.

- [ ] **Step 2: Upload the pushed commit and a consistent SQLite backup**

  Generate a SQLite backup from the local running container, transfer it over
  SSH, and verify its integrity before placing it in the shared data directory.

- [ ] **Step 3: Build the native Python/browser runtime under `/www`**

  Create the virtual environment and browser caches under `/www`; install only
  the required Ubuntu packages and Python requirements. Keep all runtime caches
  and logs off the system partition.

- [ ] **Step 4: Install and start only the new systemd unit**

  Validate the unit, start it, and check `127.0.0.1:18081` plus the automation
  status endpoint. Existing Docker containers and Nginx remain untouched until
  the local health check passes.

### Task 5: Add DNS/TLS and verify the public service

- [ ] **Step 1: Add/verify the Cloudflare A record**

  Create `accounts.anhepro.com -> 103.144.241.126` with the existing DNS
  provider, preserving any unrelated records.

- [ ] **Step 2: Install the Nginx site and obtain HTTPS**

  Run `nginx -t` before reload. Obtain a certificate for the new hostname and
  reload Nginx only after the certificate and proxy config validate.

- [ ] **Step 3: Verify end-to-end behavior**

  Check DNS, HTTPS, login page, `/api/automations/chatgpt-relogin`, SSE task
  logs, two-minute scheduling, and server resource/log growth. Confirm the
  existing new/Codex2API endpoints remain healthy.

- [ ] **Step 4: Record the deployed commit and rollback command**

  Keep the prior release directory and document `systemctl stop
  any-auto-register` plus Nginx site removal as the isolated rollback path.
