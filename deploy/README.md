# `/www` no-downtime deployment

This deployment keeps the existing Docker daemon and all running new/Codex2API
containers unchanged. The application runs as its own systemd service and all
large or persistent files live below `/www/any-auto-register`.

## Layout

```text
/www/any-auto-register/
├── current -> releases/<git-commit>
├── releases/
├── shared/
│   ├── app.env
│   ├── cache/
│   ├── data/
│   ├── home/
│   └── logs/
└── venv/
```

The application binds only to `127.0.0.1:18081`; the solver binds only to
`127.0.0.1:18889`. Nginx publishes `accounts.anhepro.com`.

## Service installation

1. Create the `any-auto-register` system user with home
   `/www/any-auto-register/shared/home`.
2. Copy `deploy/systemd/app.env` to
   `/www/any-auto-register/shared/app.env` with mode `0600`.
3. Copy `deploy/systemd/any-auto-register.service` to
   `/etc/systemd/system/any-auto-register.service`.
4. Copy `deploy/logrotate/any-auto-register` to
   `/etc/logrotate.d/any-auto-register` with mode `0644`.
5. Run `systemd-analyze verify any-auto-register.service`, then
   `systemctl daemon-reload` and start only `any-auto-register.service`.

The service has a four-core CPU ceiling, a 4GB memory high-water mark, and a
6GB hard memory limit. Browser and Python caches remain on `/www`.

## Safe data cutover

The initial server copy must have `chatgpt_auto_relogin_enabled=0` so the local
and server schedulers never run together. Validate the server UI, SMTP settings,
solver, loopback API, Nginx, and HTTPS first. For final cutover:

1. Stop the local application so SQLite and mailbox session files are stable.
2. Create a SQLite backup and verify it with `PRAGMA integrity_check`.
3. Stop only `any-auto-register.service` on the server.
4. Synchronize the final database and mailbox/session files into `shared/data`.
5. Set the server recovery concurrency to 4 and enable the two-minute monitor.
6. Start the server service and verify two consecutive 64/64 cycles.

The existing new and Codex2API services are not restarted during this cutover.

## Nginx and TLS

Copy `deploy/nginx/accounts.anhepro.com.conf` to
`/etc/nginx/sites-available/accounts.anhepro.com`, enable it with a symlink, and
run `nginx -t` before reloading. Add the Cloudflare A record for
`accounts.anhepro.com` and obtain the certificate only after the origin HTTP
health check passes.

## Rollback

Stop `any-auto-register.service`, disable only the new Nginx site, and reload
Nginx after `nginx -t`. Existing Docker services continue running throughout.

