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
| IPv4 | **TODO** (`curl -4 ifconfig.me`); DNS for clientfinder.ai points at 109.176.199.156 |
| IPv6 | 2a02:4780:28:7253::1 |
| Access | root over SSH |
| Firewall | ufw active: 22, 80, 443 (+443/udp) allowed, 3000 denied. Leftover `Nginx Full` rule (no nginx installed) |

Pending: 1 security update. No swap info captured (**TODO**).

## 2. Apps on this box

| App | Domain(s) | Process | Port | Location |
|---|---|---|---|---|
| Clientfinder.ai API | clientfinder.ai, www.clientfinder.ai | uvicorn | 127.0.0.1:8000 | `/root/clientfinder.ai/backend` |
| CopyProfit.ai (copybot) | copyprofit.ai, app.copyprofit.ai (www redirects) | python | 127.0.0.1:8090 | `/root/copybot` |
| EmailProfit.ai | app.emailprofit.ai | next-server | *:3000 (ufw denies external) | **TODO** |

Docker containers:

| Name | Image | Published |
|---|---|---|
| clientfinder_pg | postgres:16-alpine | **0.0.0.0:5433 and [::]:5433** |
| clientfinder_redis | redis:7-alpine | **0.0.0.0:6380 and [::]:6380** |
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
| 5433 | clientfinder_pg | **public, fix below** |
| 6380 | clientfinder_redis | **public, fix below** |

Reserved for Clientfinder.ai: 8000 (API, in use), 5433 (Postgres), 6380 (Redis),
**3010 for the Next.js frontend** (3000 is EmailProfit's).

## 5. Security findings

1. **Postgres and Redis for Clientfinder are published on 0.0.0.0.** Docker writes its own
   iptables rules, so ufw does not block them. The compose file used the dev password
   `dev_only_change_me` and Redis has no auth. Fix: bind to 127.0.0.1 (done in
   `docker-compose.yml`), recreate the containers, rotate the DB password, and check the logs
   for outside connections. Steps are in the Phase 0 hand-off notes.
2. Port 3000 listens on all interfaces; ufw denies it today, but binding it to 127.0.0.1 would
   remove the dependency on the firewall. EmailProfit's setting, so change only with care.
3. Apps live under `/root` and run as root. Plan a non-root service user before launch (Phase 11).
4. Stale ufw rule `Nginx Full` can be deleted: `ufw delete allow 'Nginx Full'`.

## 6. Domain and DNS

| Item | Value |
|---|---|
| DNS host | GoDaddy (`ns65/ns66.domaincontrol.com`) |
| A record | 109.176.199.156 (**TODO** confirm this is this server's IPv4) |
| www | Served directly by Caddy (no redirect to apex) |
| Resend domain | not verified yet (Phase 4) |

## 7. Capacity

RAM and disk are comfortable: ~6.4 GiB available, 82 GB free. Playwright at concurrency 1-2 fits.
Swap: **TODO**.

## 8. Open items

- [ ] Confirm server IPv4 matches the DNS A record
- [ ] Fix public Postgres/Redis exposure and rotate the DB password
- [ ] Find where EmailProfit lives (`ls -l /proc/$(pgrep -f next-server | head -1)/cwd`)
- [ ] Decide whether Clientfinder's Postgres stays separate from copybot-db (recommended: yes)
- [ ] Add swap if none
