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
3010 was reserved for a Next.js frontend that was dropped (3000 is EmailProfit's).

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

## 8b. Security

See `docs/SECURITY.md` (protocol, findings, runbooks) and run `deploy/security_audit.sh` after each deploy. Services still run as root
from `/root`; the plan to change that is in SECURITY.md section 4.

## 9. Backups

| Item | Value |
|---|---|
| What | `pg_dump -Fc` of the `clientfinder` database from the `clientfinder_pg` container |
| When | daily 03:30 UTC (systemd timer `clientfinder-backup.timer`, `Persistent=true` so a missed run happens at next boot) |
| Where | `/var/backups/clientfinder/` on this VPS (dir mode 700, files 600) |
| Verified | every dump is restored into a scratch database and its `jobs` rows counted before it is kept; a failed check fails the run (non-zero exit, nothing kept, `last_run.json` untouched) |
| Retention | every backup for 14 days, plus the Sunday one for 8 weeks |
| Status | `GET /admin/backups` (signed-in admin session): `ok` / `stale` (newest > 36h) / `none`; `last_run.json` next to the dumps |
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

- The local dumps alone do not survive losing the server: use the off-server copy below.
- Up to 24h of new data between backups.
- Secrets: the local backup does not include `.env`; the off-server copy does (encrypted). Also keep the keys in a password manager.
- Redis holds nothing durable yet (no RQ jobs); it is not backed up.



### Off-server backup (encrypted, Backblaze B2)

Every night at 04:15 UTC `deploy/offsite_backup.sh` copies the new dumps and a copy of `backend/.env` to a Backblaze B2 bucket. rclone's **crypt**
remote encrypts file names and contents on this server first, so B2 only holds scrambled files. It verifies the newest dump arrived at the right size,
keeps 60 days of dumps and 30 days of `.env` copies, and writes `offsite.json` next to the dumps. The admin dashboard shows "Needs attention" until
this has run, and `deploy/security_audit.sh` reports it.

**Keep in a password manager (not on the server only): the B2 key, and the crypt password.** Without the crypt password the off-server files cannot be
read, even by you.

One-time setup (copy the commands; they ask for the secrets and never put them on the command line or in shell history):

1. Backblaze: create an account, a **private** bucket (any unique name), and an **Application Key** limited to that bucket (Read and Write). Note the
   keyID and the application key. In the bucket settings set Lifecycle Rules to **"Keep only the last version of the file"** (otherwise deleted old
   dumps are kept and billed). Expect about $1 a month at this size.
2. On the server:
   ```bash
   apt install -y rclone
   read -r -p "B2 keyID: " KID; read -rs -p "B2 application key: " AK; echo
   rclone config create cfb2 b2 account "$KID" key "$AK"
   read -r -p "Bucket name: " BUCKET
   read -rs -p "Choose a LONG encryption password (save it in your password manager): " P1; echo
   rclone config create cfcrypt crypt remote "cfb2:$BUCKET/clientfinder" password "$(rclone obscure "$P1")"
   unset KID AK P1; chmod 600 /root/.config/rclone/rclone.conf
   ```
3. First copy, by hand: `/root/clientfinder.ai/deploy/offsite_backup.sh` (expect `ok: clientfinder-....dump copied and verified`).
   In the B2 website you should see only scrambled names.
4. Switch on the nightly timer:
   ```bash
   cp /root/clientfinder.ai/deploy/clientfinder-offsite.{service,timer} /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now clientfinder-offsite.timer
   systemctl list-timers clientfinder-offsite.timer
   ```

Restore from the off-server copy (new server or after a loss): install rclone, run the `rclone config create` lines above with the SAME bucket and the
SAME crypt password, then:

```bash
rclone lsf cfcrypt:db | tail -3                              # newest dumps
rclone copy cfcrypt:db/clientfinder-YYYY-MM-DD_HHMMSS.dump /var/backups/clientfinder/
rclone copy cfcrypt:env /root/env-backup/                    # the .env copies
/root/clientfinder.ai/deploy/backup.sh --restore-test /var/backups/clientfinder/clientfinder-YYYY-MM-DD_HHMMSS.dump
# then the "Disaster recovery onto the live database" steps above
```

Test the restore once now, before you need it. Limits: this protects against losing the server, not against someone who already controls the server
(they would find the rclone config); keep the B2 key restricted to this one bucket.
