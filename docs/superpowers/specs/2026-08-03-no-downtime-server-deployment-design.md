# No-Downtime Server Deployment Design

## Goal

Deploy Any Auto Register beside the existing new/Codex2API services without
restarting Docker or moving the system-wide Docker data root, while keeping the
two-minute Codex2API authentication monitor safe for long-term operation.

## Boundaries

- Existing Docker containers, ports, volumes, and `/var/lib/docker` remain
  untouched.
- The new application runtime, browser cache, SQLite database, backups, and
  application logs live under `/www/any-auto-register`.
- The public hostname is `accounts.anhepro.com` and proxies to a loopback-only
  application port.
- The normal monitor calls only the Codex2API `wham_only` probe. It must not
  invoke a model `/responses` fallback or refresh healthy accounts.
- A confirmed authentication failure may start recovery, but recovery
  concurrency is capped by the service configuration so a burst cannot starve
  existing services.

## Components

1. **Runtime service** — a dedicated systemd unit starts the FastAPI process
   under Xvfb and the existing local solver. It has explicit CPU, memory, and
   process limits and restarts independently of Docker.
2. **Retention maintenance** — the scheduler removes finished task runs and
   task-history rows after configurable normal/failure retention windows. Active
   tasks are never removed.
3. **File rotation** — a logrotate policy bounds `solver.log` to five 20MB
   generations. Systemd deployment logs are stored under the `/www` release
   directory.
4. **Reverse proxy** — an Nginx site for `accounts.anhepro.com` forwards HTTP,
   websocket, and SSE traffic to the loopback service; it is validated before
   reload.
5. **DNS/TLS** — the Cloudflare A record points to `103.144.241.126`; DNS
   propagation and HTTPS are checked before exposing the application.

## Data flow and failure handling

- Every monitor cycle first probes Codex2API and waits for its runtime probe to
  finish. Healthy or rate-limited account states are recorded without token
  writes. Only fresh, explicit authentication failures enter the existing RT
  refresh / full-login path.
- Network, timeout, unknown, and temporary upstream errors are deferred to the
  next cycle. They never trigger OTP login.
- A scheduler mutex prevents overlapping monitor/relogin tasks.
- Retention failures are logged and do not stop the authentication scheduler.
- Nginx configuration is tested with `nginx -t`; reload is performed only after
  a successful test. The service is health-checked on its loopback port before
  DNS is changed.

## Operational defaults

- Monitor interval: 2 minutes.
- Recovery concurrency on the server: 4.
- Normal task-history retention: 30 days.
- Failed task-history retention: 90 days.
- Solver log rotation: 20MB × 5.
- Service limits: `CPUQuota=400%`, `MemoryHigh=4G`, `MemoryMax=6G`.

## Verification

- Unit tests cover retention boundaries, active-row preservation, and scheduler
  error isolation.
- Existing backend and frontend suites, compile checks, and production build
  remain green.
- Before DNS cutover, verify loopback health, task scheduler state, the
  `/api/automations/chatgpt-relogin` response, and the HTTPS hostname.

