# VPS Inventory — fill in before deploying

Goal: land Clientfinder.ai as a sibling of CopyProfit.ai and EmailProfit.ai without
colliding on ports, databases, nginx server blocks, or system packages.

Run the commands in each section on the VPS and paste the output / answers below.
Do NOT paste passwords, API keys, or private keys here. This file is committed.

## 1. Host

| Item | Value |
|---|---|
| Provider | |
| OS + version (`lsb_release -a`) | |
| CPU / RAM / disk (`nproc; free -h; df -h /`) | |
| Public IPv4 | |
| Deploy user / SSH access | |
| Firewall in use (ufw / iptables / provider panel) | |

## 2. Runtime model

| Item | Value |
|---|---|
| Docker installed? (`docker --version`) | |
| docker compose v2? (`docker compose version`) | |
| Apps run in Docker, systemd, pm2, or bare processes? | |
| Python versions available (`python3 --version`) | |
| Node version (`node --version`) | |

Running containers (`docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'`):

```
(paste)
```

systemd units for the apps (`systemctl list-units --type=service --state=running | grep -iE 'copy|email|app'`):

```
(paste)
```

## 3. Ports in use

`ss -tlnp` output, with PIDs/processes:

```
(paste)
```

Clientfinder.ai needs these (proposed, pick free ones if taken):

| Service | Proposed host port | Bind to |
|---|---|---|
| FastAPI backend | 8010 | 127.0.0.1 |
| Next.js frontend | 3010 | 127.0.0.1 |
| Postgres (if separate instance) | 5433 | 127.0.0.1 |
| Redis (if separate instance) | 6380 | 127.0.0.1 |

Only 80/443 are public. Everything else binds to localhost.

## 4. Reverse proxy and TLS

| Item | Value |
|---|---|
| Proxy (nginx / Caddy / Traefik) | |
| Config location (`/etc/nginx/sites-enabled/`) | |
| TLS method (certbot / Caddy auto / Cloudflare) | |
| Existing server blocks (domains) | |

## 5. Databases

| Item | Value |
|---|---|
| Postgres installed? Version, host/container | |
| Existing databases (`\l`) | |
| Redis installed? Used by CopyProfit/EmailProfit? | |
| Plan: new database `clientfinder` + role on the shared instance, or separate container? | |

Default recommendation: a separate database and role on the shared Postgres if one
exists; otherwise the compose file in this repo. Never share a role with the sibling apps.

## 6. How the sibling apps are deployed

| | CopyProfit.ai | EmailProfit.ai |
|---|---|---|
| Stack (language / framework) | | |
| Process manager | | |
| Repo + deploy method (git pull, CI, manual) | | |
| Reverse-proxy upstream port | | |
| Database | | |
| Anything that must not be touched | | |

## 7. Domain and DNS

| Item | Value |
|---|---|
| Registrar | |
| Current A/AAAA records for `clientfinder.ai` | |
| DNS host (registrar / Cloudflare / other) | |
| `www` handling | |
| Resend sending domain verified? (Phase 4) | |

## 8. Capacity check for the scraper

Playwright + Chromium needs roughly 500 MB–1 GB RAM per concurrent page. Confirm:

- [ ] Free RAM after the sibling apps are running: ______
- [ ] Free disk for Chromium + Postgres growth: ______
- [ ] Swap configured: ______

If RAM is tight, run the scraper worker at concurrency 1 or move it off-box.

## 9. Open questions / risks

-
