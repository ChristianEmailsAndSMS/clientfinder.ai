# Clientfinder.ai Security Protocol

Last reviewed: 2026-10-08. Run `deploy/security_audit.sh` on the server after every deploy and once a month; it checks most
of this file automatically and prints PASS / WARN / FAIL.

## 1. What we protect, and from whom

| Asset | Worst case | Main controls |
|---|---|---|
| Customer accounts and credit balances (money) | Free credit, stolen balances, account takeover | Argon2id passwords, mandatory 2FA for admins, lockout + per-IP throttle, append-only ledger, atomic balance updates |
| Admin dashboard | Attacker grants themselves credit, reads customers | Allow-listed email + password + 2FA, HttpOnly/Secure/SameSite=Strict cookie, CSRF header + Origin check, strict CSP, audit log |
| The job database (the product) | Competitor copies everything | Per-IP rate limit, optional `JOBS_REQUIRE_LOGIN` |
| API keys (SerpAPI, Anthropic, Resend, Whop) | Someone else spends your money | `.env` mode 600, secrets redacted from logs and stored errors, never printed |
| The server itself | Full takeover, data theft | Firewall, localhost-only databases, SSRF guard on everything the scraper fetches, least-privilege DB role, unprivileged services (see section 4) |
| Backups | Customer data leak | Directory mode 700, restore-verified daily |

## 2. Findings from this review, and what was done

Each item below was reproduced or tested; the tests live in `backend/tests/` and fail if the fix is removed.

| # | Finding | Risk | Fix | Status |
|---|---|---|---|---|
| 1 | Postgres and Redis published on `0.0.0.0` (Docker bypasses ufw) with the dev password | Database readable from the internet | Bound to `127.0.0.1`, password rotated, contents checked for tampering | **Fixed and verified on the server** |
| 2 | httpx logged every request URL at INFO, including `?api_key=...` | SerpAPI key written to the system journal | httpx/httpcore logging silenced; a redacting filter scrubs every log record | Fixed, tested |
| 3 | `raise_for_status()` text contains the full URL with the key; we stored it in `search_queries.error` and showed it in the admin API | Key stored in the database and shown in the UI | Search errors now carry only `provider HTTP 401`; every stored error passes through `redact()` | Fixed, tested |
| 4 | Scraper followed redirects to any address | **SSRF**: a page indexed by Google could redirect the server to `127.0.0.1:8000`, internal services, cloud metadata | Every URL and every redirect hop must resolve only to public addresses on ports 80/443; manual redirect handling; 2 MB response cap | Fixed, tested (residual risk below) |
| 5 | Model output and third-party APIs could put `javascript:` / `data:` links in the stored job URL | Stored XSS / phishing link shown to customers | `safe_job_url()`: plain http(s) only, no credentials or control characters. Also fixed ProBlogger storing relative links | Fixed, tested |
| 6 | Interactive API docs and the OpenAPI schema were public | Free map of every endpoint | Off unless `ENABLE_DOCS=1` | Fixed, tested |
| 7 | No security headers | Clickjacking, MIME sniffing | HSTS, nosniff, DENY framing, referrer policy on every response; strict CSP + `no-store` on the admin area | Fixed, tested |
| 8 | Playwright fallback on by default | Chromium running as **root with no sandbox** on untrusted web pages | Default off. Enable only after section 4 | Fixed (disabled) |
| 9 | Static `ADMIN_TOKEN` header for admin access (and it appeared in a screenshot) | Anyone holding the token is admin; no 2FA, no audit trail | Removed. Replaced by session login + 2FA | **Fixed. Delete `ADMIN_TOKEN` from `.env`** |
| 10 | App connected to Postgres as a superuser | A single SQL bug = full control of the database server | `deploy/db_least_privilege.sql` creates a role that cannot alter/drop/create anything; verified refused on 9 destructive statements | Provided; **needs applying on the server (section 4)** |
| 11 | Public `/jobs` had no rate limit and no login option | Entire product scrapeable | Login required by default; 120 requests/min per IP; the home page shows only a 6-row redacted preview | Fixed |
| 12 | 2FA secrets keyed to `JWT_SECRET` | Rotating the session secret would break everyone's 2FA | Separate `DATA_ENCRYPTION_KEY`, with `..._PREVIOUS` for rotation | Fixed, tested; **set the key** |
| 13 | Password policy accepted repeats such as `passwordpasswordpassword` | Weak admin password | Rejects repeats, common words plus a few characters, email name; 14+ chars for admins | Fixed, tested |
| 14 | Unbounded credit amounts (`1e400`, `nan`) | Crash or absurd balance | Exact decimal parsing, finite, max 6 decimals, hard ceiling, plus a $1,000 cap per admin adjustment | Fixed, tested |
| 15 | Dependency vulnerabilities | Known CVEs | `pip-audit` on `requirements.txt`: none found today. `deploy/security_audit.sh` re-runs it on the server | Clean at review time |

### Verified how

- 293 automated tests (run `pytest` in `backend/`), including tampered/forged/expired/`alg=none` tokens, 2FA replay, lockout, CSRF, role checks, SSRF redirect
  to an internal host, secret redaction, and credit ledger invariants.
- Mutation checks: I deliberately broke the 2FA replay check, session revocation, the admin email allow-list, the CSRF check
  and the ledger's overdraw guard; each break made a test fail.
- On real PostgreSQL 16: all four migrations (and downgrade/upgrade round trip), 40 concurrent spends against a $1.00 balance
  (exactly 20 accepted, balance 0, never negative), 30 simultaneous replays of one payment (credited once), 60 mixed
  operations (balance always equals the ledger sum), and the full browser account-creation flow in headless Chromium with no
  CSP violations.

### What was NOT verified

- The server-side pieces I cannot reach from here: the systemd timer and hardened unit files running under real systemd, Caddy
  config, ufw, SSH config, fail2ban. `deploy/security_audit.sh` checks these on the server.
- DNS rebinding for the scraper (the guard resolves, then the HTTP client resolves again; an attacker controlling DNS with a
  zero TTL could in theory differ between the two). The scraper only feeds an extractor and nothing it fetches is returned to a
  caller, so the realistic impact is small. Full removal needs connecting to the checked IP; revisit if the scraper is ever
  exposed to user-supplied URLs.
- Whop payment webhooks: **not built**. Do not accept payments until the webhook is added with signature verification written
  against Whop's own documentation (section 6).
- A penetration test by a third party. This is a careful self-review, not a certification.

## 3. Authentication design (admin now, customers in Phase 4)

- Passwords: argon2id; unknown emails burn the same work as known ones; generic error messages ("Invalid email, password or code").
- 2FA: TOTP, secret encrypted at rest, each code usable once, +-30s drift. Mandatory for admins: an admin without 2FA cannot
  sign in, and an admin session stops working if 2FA is switched off.
- Admin = `is_admin` flag AND email in `ADMIN_EMAILS` AND 2FA on. All three, every request.
- Lockout: 5 failures locks the account 15 minutes; 20 failures from one IP in 10 minutes blocks that IP. Both are stored in the
  database, so a restart does not reset them. An attacker can use lockout to annoy you; clear it with
  `scripts/admin_cli.py unlock EMAIL`.
- Sessions: 12-hour signed token in an HttpOnly, Secure, SameSite=Strict cookie. "Sign out everywhere" and suspending a user
  invalidate every token immediately (server-side version counter).
- Customer accounts: anyone can sign up with email + password (12+ characters, no 2FA required). Sign-ups are limited to 5 per IP per
  hour and 200 per hour site-wide. New accounts get **$0 credit** (`SIGNUP_BONUS_USD`): until sign-up verifies email addresses, free credit
  could be farmed with throwaway accounts, and live searches cost real money.
- The owner address (`ADMIN_EMAILS`) **cannot be registered in the browser**. Without email verification, whoever registered it first
  would own the admin account. The admin account is created on the server with `scripts/admin_cli.py create-admin` (password typed in the
  terminal, never shown), then the first browser sign-in forces 2FA enrolment through a 10-minute token that is not a session. Once Resend
  email verification exists (Phase 4) this can become a normal sign-up.
- Audit log: every sign-in, failure, lockout, setup step and admin action (including every credit change) is recorded and
  visible in the dashboard's Security log.

## 4. Do next (prioritised)

**Today**
1. Remove `ADMIN_TOKEN` from `/root/clientfinder.ai/backend/.env` (it was exposed in a screenshot and no longer does anything).
2. Add to `.env`, then restart the API (`systemctl restart clientfinder-api`):
   ```
   JWT_SECRET=<python3 -c "import secrets; print(secrets.token_urlsafe(48))">
   DATA_ENCRYPTION_KEY=<a different value, same command>
   ```
   Then `chmod 600 .env`. Keep a copy of both values in a password manager: losing `DATA_ENCRYPTION_KEY` means 2FA must be re-enrolled.
3. Create your admin account: `cd /root/clientfinder.ai/backend && .venv/bin/python scripts/admin_cli.py create-admin` (choose a password;
   you will not see it), then sign in at `https://clientfinder.ai/login` and scan the 2FA QR code.
4. Run `deploy/security_audit.sh` and fix FAILs.

**This week**
5. Least-privilege database role (see `deploy/db_least_privilege.sql` header). Then set `DATABASE_URL` to the new role and
   `MIGRATION_DATABASE_URL` to the owner login, restart the services.
6. Add `import cf_security` to the clientfinder.ai block of the Caddyfile (`deploy/Caddyfile.security.snippet`).
7. SSH: key-only login (`PasswordAuthentication no` in `/etc/ssh/sshd_config.d/`; keep a second session open while you test);
   `apt install fail2ban unattended-upgrades`; `ufw delete allow 'Nginx Full'`.
8. Install the hardened unit files and the backup timer (`deploy/*.service`, `deploy/*.timer`).

**Before launch**
9. Run the services as a non-root user (below). Until then, keep `PLAYWRIGHT_FALLBACK` off.
10. ~~`JOBS_REQUIRE_LOGIN`~~ is now on by default; the job feed needs an account.
11. Off-server, encrypted backups (e.g. `rclone` to Backblaze B2 with a crypt remote) plus an alert when a backup fails.
12. Whop webhook with signature verification and idempotent crediting (`credits.apply(..., ref=payment_id)` is ready for it).
13. Email verification and password reset (Resend) for customers; then grant a small welcome credit and let the owner register normally.
14. Pin dependency versions with hashes (`pip-compile --generate-hashes`) and run `pip-audit` in CI.
15. Add API workers behind Caddy only after moving the rate limiter to Redis (it is per-process today).

### Run as a non-root user (do in a maintenance window)

```bash
useradd --system --create-home --home-dir /opt/clientfinder --shell /usr/sbin/nologin clientfinder
git clone <repo> /opt/clientfinder/app && chown -R clientfinder: /opt/clientfinder
sudo -u clientfinder bash -c 'cd /opt/clientfinder/app/backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
install -o clientfinder -g clientfinder -m 600 /root/clientfinder.ai/backend/.env /opt/clientfinder/app/backend/.env
# in the two unit files set: User=clientfinder, WorkingDirectory/ExecStart under /opt/clientfinder/app/backend
# let the API read backup status: chgrp clientfinder /var/backups/clientfinder && chmod 750 it, and UMask=0027 in the backup unit
systemctl daemon-reload && systemctl restart clientfinder-api clientfinder-scheduler
```
Nothing here needs the Docker socket: the app reaches Postgres on `127.0.0.1:5433`. Only `backup.sh` needs Docker, and it stays root.

## 5. Runbooks

**Create or recover the admin account** (lost phone, forgot password, locked out): on the server run
`.venv/bin/python scripts/admin_cli.py reset-admin christian@emailsandsms.com` (choose a new password), then sign in at `/login` and scan
the new QR code. This signs out every session and disables the old 2FA device.

**Locked out after failed attempts:** wait 15 minutes, or `scripts/admin_cli.py unlock EMAIL`.

**A secret was exposed** (screenshot, chat, git, log):
1. Assume it is public. Rotate it now; do not wait to see if it was used.
2. Where to rotate: SerpAPI / Anthropic / Resend / Whop dashboards (create a new key, delete the old one, update `.env`, restart).
   `JWT_SECRET`: new value, restart (signs everyone out; 2FA is unaffected because of `DATA_ENCRYPTION_KEY`).
   `DATA_ENCRYPTION_KEY`: put the new value in `DATA_ENCRYPTION_KEY`, the old in `DATA_ENCRYPTION_KEY_PREVIOUS`, restart.
   Postgres password: `ALTER ROLE ... PASSWORD`, update `DATABASE_URL`, restart.
3. Check the dashboard Security log and the provider's usage page for activity you do not recognise.

**Suspected compromise of the server:**
1. Do not reboot (you lose evidence). `ss -tnp` and `last -20` and `journalctl -u clientfinder-api --since "-24h"`.
2. Rotate everything in the list above. `scripts/admin_cli.py reset-admin` and "Sign out everywhere" for every account.
3. Check `crontab -l`, `/etc/cron.d`, `~/.ssh/authorized_keys`, unknown systemd units, unknown listening ports.
4. If in doubt, rebuild the server from a clean image and restore the latest verified backup (`docs/INFRA.md` section 9).

**Customer reports wrong credit:** open Customers & credits, Manage, read the credit history (every change has who, when and
why). Correct it with an adjustment; never edit the table by hand (the app role cannot, by design).

## 6. Credits and payments: rules for future code

- All balance changes go through `app/credits.py`. Never `UPDATE users SET balance...` anywhere else.
- Charge **after** the model call using the returned token counts, but call `require_balance()` **before**.
- An unpriced model raises; do not add a fallback price.
- Payments must be credited with `ref=<provider payment id>` so a webhook retry cannot pay twice.
- A Whop webhook must verify the provider's signature against the raw request body using the secret from Whop, reject if the
  secret is not configured, and be written from Whop's current documentation and a real sample payload (not guessed field names).
- Prices live in `app/pricing.py` and are **unverified**: check them against Anthropic's pricing page before charging anyone.

## 7. Routine

| When | What |
|---|---|
| Every deploy | `deploy/security_audit.sh`; `pytest` |
| Weekly | Read the dashboard Security log (look for repeated `login_fail`, `setup_fail`, `lockout`); confirm backup status is `ok` |
| Monthly | `deploy/backup.sh --restore-test <newest dump>`; `pip-audit`; `apt upgrade` and reboot if required; review who has access to the server and the Whop/Resend/SerpAPI/Anthropic accounts |
| Quarterly | Rotate `JWT_SECRET`; review this file |

## 8. Customer sign-up and live search: what can go wrong

| Risk | Control |
|---|---|
| Bots mass-create accounts | 5 per IP per hour, 200 per hour overall, password policy, $0 starting credit (nothing to farm) |
| Someone registers the owner's email | Blocked for every address in `ADMIN_EMAILS` |
| A customer runs up our SerpAPI / Claude bill | Needs credit first (balance must cover the estimate), 20 new searches per day, one running search per user, 3 at once site-wide, the monthly search budget still applies, and the price is charged from the ledger |
| Cache poisoning / one customer's search showing another's data | Cached results are shared jobs only; a search record is visible only to its owner (404 for anyone else) |
| Search text abused to hit our servers | The text only goes to Google as a query. Fetching the result pages goes through the SSRF guard |
| Double-charging | One ledger entry per search, `ref=usersearch:<id>`; a retry cannot charge twice. Failed searches charge nothing |
| A customer reads other accounts | Every `/searches` and `/account` call is scoped to the signed-in user; admin-only data stays behind the 2FA admin check |
| Open redirect after sign-in | `next=` accepts only same-site relative paths (tested) |
| Stored XSS from scraped text | The interface never uses `innerHTML`; every value is inserted as text; strict CSP (no inline script/style). Enforced by tests that scan all scripts |
