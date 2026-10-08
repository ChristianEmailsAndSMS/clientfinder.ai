# VPS Inventory

Captured 2026-10-08 from the live server. No secrets in this file.
Items marked **TODO** still need confirming.

## 1. Host

| Item | Value |
|---|---|
| Hostname | `srv1546780` |
| OS | Ubuntu 24.04.5 LTS, kernel 6.8.0-146 (26.04 offered; do not upgrade casually) |
| RAM | 7.8 GiB total, ~6.4 GiB available with all apps running |
| Disk | 96 GB, 15 GB used, 82 GB free |
| IPv4 | 109.176.199.156 (matches the clientfinder.ai A record) |
| IPv6 | 2a02:4780:28:7253::1 |
| Access | root over SSH |
| Firewall | ufw active: 22, 80, 443 (+443/udp) allowed, 3000 denied. Leftover `Nginx Full` rule (no nginx installed) |

Pending: 1 security update. **No swap configured.**

## 2. Apps on this box

| App | Domain(s) | Process | Port | Location |
|---|---|---|---|---|
| Clientfinder.ai API | clientfinder.ai, www.clientfinder.ai | uvicorn, systemd unit `clientfinder-api.service` (enabled) | 127.0.0.1:8000 | `/root/clientfinder.ai/backend` (venv in `.venv`) |
| CopyProfit.ai (copybot) | copyprofit.ai, app.copyprofit.ai (www redirects) | python | 127.0.0.1:8090 | `/root/copybot` |
| EmailProfit.ai | app.emailprofit.ai | next-server | *:3000 (ufw denies external) | `/root/emailprofitai` |

Docker containers:

| Name | Image | Published |
|---|---|---|
| clientfinder_pg | postgres:16-alpine | 127.0.0.1:5433 (fixed 2026-10-08) |
| clientfinder_redis | redis:7-alpine | 127.0.0.1:6380 (fixed 2026-10-08) |
| copybot-db | pgvector/pgvector:pg16 | 127.0.0.1:5432 (correct) |

No Postgres or nginx on the host itself; databases are Docker-only.

## 3. Reverse proxy and TLS

- **Caddy** (pid on 80/443, admin API on 127.0.0.1:2019). Config `/etc/caddy/Caddyfile`, backups `Caddyfile.backup-2026-10-06` and `Caddyfile.bak`.
- TLS is automatic via Caddy; certbot is not used.
- Take a backup before editing: `cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%F)`, then `caddy validate --config /etc/caddy/Caddyfile` before `systemctl reload caddy`.

## 4. Ports

| Port | Owner | Public? |
|---|---|---|
| 22 | sshd | yes |
| 80, 443 | Caddy | yes |
| 3000 | EmailProfit (next-server) | bound on all interfaces, blocked by ufw |
| 8000 | Clientfinder API | localhost |
| 8090 | CopyProfit | localhost |
| 5432 | copybot-db | localhost |
| 5433 | clientfinder_pg | localhost |
| 6380 | clientfinder_redis | localhost |

Reserved for Clientfinder.ai: 8000 (API, in use), 5433 (Postgres), 6380 (Redis),
**3010 for the Next.js frontend** (3000 is EmailProfit's).

## 5. Security findings

1. **RESOLVED 2026-10-08:** Clientfinder's Postgres and Redis were published on 0.0.0.0 (Docker
   bypasses ufw) with the dev password. Both are now bound to 127.0.0.1 via `docker-compose.yml`,
   the Postgres password was rotated (stored in `/root/clientfinder.ai/.env` and `backend/.env`,
   both gitignored), and the API was restarted and verified. A check of roles, databases and
   Redis config found no sign of tampering. Rule going forward: always publish container ports
   as `127.0.0.1:host:container`.
2. Port 3000 listens on all interfaces; ufw denies it today, but binding it to 127.0.0.1 would
   remove the dependency on the firewall. EmailProfit's setting, so change only with care.
3. Apps live under `/root` and run as root. Plan a non-root service user before launch (Phase 11).
4. Stale ufw rule `Nginx Full` can be deleted: `ufw delete allow 'Nginx Full'`.

## 6. Domain and DNS

| Item | Value |
|---|---|
| DNS host | GoDaddy (`ns65/ns66.domaincontrol.com`) |
| A record | 109.176.199.156 (confirmed: this server) |
| www | Served directly by Caddy (no redirect to apex) |
| Resend domain | not verified yet (Phase 4) |

## 7. Capacity

RAM and disk are comfortable: ~6.4 GiB available, 82 GB free. Playwright at concurrency 1-2 fits.
No swap: a runaway Chromium could trigger the OOM killer and take a sibling app down. Add 2 GB:
`fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab`

## 8. Open items

- [x] Confirm server IPv4 matches the DNS A record
- [x] Fix public Postgres/Redis exposure and rotate the DB password
- [x] Find where EmailProfit lives (`/root/emailprofitai`)
- [x] Separate Postgres from copybot-db (done: own container)
- [ ] Add swap (command in section 7)
- [ ] Switch the server checkout back to the main branch after the Phase 0 branch is merged
- [ ] App runs as superuser `clientfinder`; create a limited app role before launch (Phase 11)
- [ ] Run apps as a non-root user (Phase 11)

## 9. Backups

| Item | Value |
|---|---|
| What | `pg_dump -Fc` of the `clientfinder` database from the `clientfinder_pg` container |
| When | daily 03:30 UTC (systemd timer `clientfinder-backup.timer`, `Persistent=true` so a missed run happens at next boot) |
| Where | `/var/backups/clientfinder/` on this VPS (dir mode 700, files 600) |
| Verified | every dump is restored into a scratch database and its `jobs` rows counted before it is kept; a failed check fails the run (non-zero exit, nothing kept, `last_run.json` untouched) |
| Retention | every backup for 14 days, plus the Sunday one for 8 weeks |
| Status | `GET /admin/backups` (header `X-Admin-Token`): `ok` / `stale` (newest > 36h) / `none`; `last_run.json` next to the dumps |
| Script | `deploy/backup.sh` (bash, no dependency on the Python app) |

Install (once):

```bash
cp /root/clientfinder.ai/deploy/clientfinder-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now clientfinder-backup.timer
systemctl start clientfinder-backup.service          # first backup now
journalctl -u clientfinder-backup -n 20 --no-pager    # expect: "ok: ... restore-verified (N jobs rows)"
systemctl list-timers clientfinder-backup.timer       # next run
```

Restore:

```bash
# Inspect a backup without touching production: restore into a separate database
docker exec clientfinder_pg createdb -U clientfinder restored
docker exec -i clientfinder_pg pg_restore -U clientfinder -d restored --no-owner < /var/backups/clientfinder/clientfinder-YYYY-MM-DD_HHMMSS.dump

# Disaster recovery onto the live database (STOP the API and scheduler first)
systemctl stop clientfinder-api clientfinder-scheduler
docker exec -i clientfinder_pg pg_restore -U clientfinder -d clientfinder --clean --if-exists --no-owner < FILE
systemctl start clientfinder-api clientfinder-scheduler

# Check a dump is restorable at any time
/root/clientfinder.ai/deploy/backup.sh --restore-test FILE
```

What this does NOT cover:

- **Losing the VPS or its disk loses the backups too.** They are on the same machine. Add an off-box copy
  (rclone to Backblaze B2 / S3, encrypted) before real customers pay (Phase 11).
- Up to 24h of new data between backups.
- Secrets: `.env` files are deliberately not in the backup. Keep a copy of the keys in a password manager.
- Redis holds nothing durable yet (no RQ jobs); it is not backed up.

