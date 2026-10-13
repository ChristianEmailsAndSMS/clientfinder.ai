# Clientfinder.ai Security Protocol

Last reviewed: 2026-10-08. Run `deploy/security_audit.sh` on the server after every deploy and once a month; it checks most
of this file automatically and prints PASS / WARN / FAIL.

## 1. What we protect, and from whom

| Asset | Worst case | Main controls |
|---|---|---|
| Customer accounts and credit balances (money) | Free credit, stolen balances, account takeover | Argon2id passwords, 2FA for admins (on by default; the owner can switch it off with `ADMIN_REQUIRE_2FA=0`, see section 9), lockout + per-IP throttle, append-only ledger, atomic balance updates |
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

- Automated tests (run `pytest` in `backend/` and read the last line for the current count), including tampered/forged/expired/`alg=none` tokens, 2FA replay, lockout, CSRF, role checks, SSRF redirect
  to an internal host, secret redaction, and credit ledger invariants.
- Mutation checks: I deliberately broke the 2FA replay check, session revocation, the admin email allow-list, the CSRF check
  and the ledger's overdraw guard; each break made a test fail.
- On real PostgreSQL 16 (at the time, migrations 0001-0004; 0005-0008 have not been round-tripped on Postgres by this review): the migrations (and downgrade/upgrade round trip), 40 concurrent spends against a $1.00 balance
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
- Whop payment webhooks: now built (`app/whop.py`, signature + 5-minute window + dedupe by event id) but **never exercised with a real Whop payment**; field names are unverified (section 9).
- A penetration test by a third party. This is a careful self-review, not a certification.

## 3. Authentication design (admin now, customers in Phase 4)

- Passwords: argon2id; unknown emails burn the same work as known ones; generic error messages ("Invalid email, password or code").
- 2FA: TOTP, secret encrypted at rest, each code usable once, +-30s drift. Required for admins while `ADMIN_REQUIRE_2FA=1` (the code default): an admin without 2FA cannot
  sign in. With `ADMIN_REQUIRE_2FA=0` (the owner's current choice, section 9) the admin account is password-only.
- Admin = `is_admin` flag AND email in `ADMIN_EMAILS` AND 2FA on (the 2FA part is skipped when `ADMIN_REQUIRE_2FA=0`). Checked on every request.
- Lockout: 5 failures locks the account 15 minutes; 20 failures from one IP in 10 minutes blocks that IP. Both are stored in the
  database, so a restart does not reset them. An attacker can use lockout to annoy you; clear it with
  `scripts/admin_cli.py unlock EMAIL`.
- Sessions: 12-hour signed token in an HttpOnly, Secure, SameSite=Strict cookie. Signing out (which also ends the user's other sessions), "Sign out everywhere" and suspending a user
  invalidate every token immediately (server-side version counter).
- Customer accounts: anyone can sign up with email + password (12+ characters, no 2FA required). Sign-ups are limited to 5 per IP per
  hour and 200 per hour site-wide. New accounts get **$0 credit** unless the owner sets `SIGNUP_BONUS_USD` (default 0).
  With it on, the welcome credit is only loosely protected (section 12): sign-up does not verify email addresses, so any made-up address can claim it.
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
8. Install the scheduler unit and the backup timer (`deploy/clientfinder-scheduler.service`, `deploy/clientfinder-backup.*`). There is no `clientfinder-api.service` in the repo: it exists only on the server (`systemctl cat clientfinder-api`).

**Before launch**
9. Run the services as a non-root user (below). Until then, keep `PLAYWRIGHT_FALLBACK` off.
10. ~~`JOBS_REQUIRE_LOGIN`~~ is now on by default; the job feed needs an account.
11. Off-server, encrypted backups (e.g. `rclone` to Backblaze B2 with a crypt remote) plus an alert when a backup fails.
12. ~~Whop webhook~~ built; still needs one real test purchase.
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
| A customer runs up our SerpAPI / Claude bill | Needs credit first (balance must cover the estimate), 20 new searches per day, one running search per user, 3 at once site-wide, the monthly search budget still applies, and the price is charged from the ledger. One default customer search is 20 Google searches (about $0.30 to us, about $0.66 to the customer), so one customer at the daily limit can use 400 Google searches a day if they have the credit |
| Cache poisoning / one customer's search showing another's data | Cached results are shared jobs only; a search record is visible only to its owner (404 for anyone else) |
| Search text abused to hit our servers | The text only goes to Google as a query. Fetching the result pages goes through the SSRF guard |
| Double-charging | One ledger entry per search, `ref=usersearch:<id>`; a retry cannot charge twice. A search where every Google search failed charges nothing; if only some failed, only the ones that ran are charged and the customer is told the list may be incomplete. A search that timed out stops running more Google searches |
| A customer reads other accounts | Every `/searches` and `/account` call is scoped to the signed-in user; admin-only data stays behind the 2FA admin check |
| Open redirect after sign-in | `next=` accepts only same-site relative paths (tested) |
| Stored XSS from scraped text | The interface never uses `innerHTML`; every value is inserted as text; strict CSP (no inline script/style). Enforced by tests that scan all scripts |

## 9. Review of the later features (onboarding, pitch helper, Whop checkout and webhook, per-user limits)

**Checked and fine:** every new write endpoint needs a signed-in account and the CSRF header; admin endpoints sit behind the admin dependency; filters
are parameterised (region and kind values are checked against fixed lists, `location` escapes LIKE wildcards); the screenshot is decoded, size-limited
and identified by its real file signature, never by the name the browser gives it; job posts, pasted conversations and screenshots go to the model as
data with an instruction to ignore anything inside them, and the answer is shown as plain text only; the Whop webhook verifies the signature and a
5-minute timestamp window, dedupes by event id, and credits the account id our own server put in the checkout (a buyer cannot forge it); Whop's error
text is stripped of the API key; unlimited accounts still obey the daily caps.

**Fixed in this review:**
1. The proxy's 1 MB body limit would have rejected screenshots. It is now 6 MB for `/assist` only (Caddy snippet) and the API enforces the same caps itself.
2. Customers no longer see Whop's raw error (it named which API permission was missing). They get a generic message; the detail goes to the log and to `scripts/whop_check.py`.
3. Repeated clicks on a bundle reuse the same checkout link for 20 minutes instead of creating a new Whop plan each time; the endpoint also has the per-IP rate limit.
4. One pitch draft at a time per customer, so parallel requests cannot slip past the balance check.
5. Refund, dispute and chargeback events are held for the owner and counted under "Needs attention" on the dashboard. Nothing is revoked automatically.
6. The audit script now flags duplicate settings in `.env` (it caught two `WHOP_WEBHOOK_SECRET` lines) and a Whop key without a webhook secret.

**Known and accepted:**
- **Admin 2FA is off** (owner's choice). One leaked or guessed admin password is full access. Use a long unique password, keep the lockout, and turn `ADMIN_REQUIRE_2FA=1` back on when ready.
- **Chargebacks:** if a customer buys credit, spends it, then disputes the payment, the credit is gone. The dashboard now tells you; you decide.
- **Counters live in memory** (pitch helper daily cap, checkout link limits). A restart resets them. The caps protect cost, not accounts, so this is acceptable on one server.
- **Payment amount:** we credit the amount Whop reports as paid. If Whop's field includes tax the customer gets slightly more than the base price. Check the first real payment in `/admin/payments`.
- **Screenshots** are sent to Anthropic's API to be read and are not stored by us. They may contain other people's private details; the UI says so.
- **Not verified against the live services:** Whop's payload field names, Google's 3/6/9-month date filter.


## 10. Full-product audit by nine independent auditors (2026-10-12)

The owner reported inaccurate behaviour. Nine auditors (money, job data, web UI, docs vs code, API, payments, live search, security,
migrations) each had to reproduce every finding and write a tested fix; nine skeptics then tried to refute each one. 63 findings, all confirmed
as real by the skeptics. Fixed in this commit series (each is its own commit, tagged `pre-swarm` before the first):

- **Critical:** with only a Serper key (or provider=serpapi but no SerpApi key) customers were charged for searches that never ran; fixed so an
  unconfigured provider refuses the search and bills nothing.
- **High:** two simultaneous searches by one customer could both start (now a database rule: one queued/running search per user); a search
  slower than the 10-minute timeout was marked "not charged" and then charged; `/health` said ok when the database was behind the code (it now
  returns 503 and the API refuses to start unless `SCHEMA_CHECK_STRICT=0`); long-prompt pricing was applied to the SUM of a search's tokens instead of per call;
  place names matched ordinary words ("Join us", "or", "in"); Google-found jobs were filed under the wrong platform (wix.com counted as X);
  "On-site" and "has pay" filters included unknown/DOE jobs; Lever monthly pay was stored as yearly; stale price quotes could show "Free" for the wrong window.
- **Medium and below:** refunds could be turned into credit by the admin button; odd webhook amounts returned 500; any payment by a customer's email
  was credited even for another Whop product (now only our credit packs unless the payment carries our account id); chunked requests bypassed the body cap;
  account enumeration; audit-log flooding; admin Payments tab added; many stale statements in the docs corrected, and a test now fails when `.env.example` drifts from `config.py`.

**Deliberately not done yet:** JD-15 (deduplicating the same job under http/https, www, x.com/twitter.com needs a migration that recomputes
every `dedupe_hash` and merges duplicates; do it as one reviewed change), proper token accounting on `scrape_runs` (cost_report now labels extraction
spend as a lower bound).

**After deploying, run once:** `POST /admin/regions/rebuild` (Admin), `python scripts/backfill_tags.py`, `python scripts/recompute_platforms.py --apply`,
and delete Lever jobs whose `pay_period='year'` and `pay_max < 20000` so the next run re-adds them with the right pay.


## 11. Second audit swarm (2026-10-12, on the code after Cursor's merge)

Four of seven auditors finished before the session limit; the docs, security and end-to-end auditors and all skeptics did not run and are
scheduled to be re-run. The findings of the four that did finish were checked by reading the evidence and running the tests, then fixed:
the "[object HTMLDivElement]" and "null" text in dialogs, "Newest first" sorting by scrape date instead of the shown date, the "Live: updated"
badge being kept alive by a customer's paid search, a false admin alarm for the customer-search source, `recompute_platforms.py` relabelling
posts as "web" (it now uses the page URL that Google returned), the Reddit/open-web title filter dropping real posts (plural, -ing, "copy writer",
customer words, hybrid roles, employers who mention a keyword), the monthly search budget being checked once instead of before every Google search,
old rows (or rows kept as not-a-real-job) being attached to a customer's search, partial searches being handed out free from the cache, and
platform labels missing for X, Mastodon and others. Remaining low-priority filter findings (F5 to F7) are not fixed.

## 12. Welcome credit, password help and the public pages (2026-10-12)

- **Welcome credit (`SIGNUP_BONUS_USD`, off by default).** Without email verification, throwaway sign-ups could farm free searches, which cost real money.
  Limits: one grant per real mailbox (`name+tag@`, gmail dots and googlemail.com count as the same mailbox; enforced by the ledger `ref` `welcome:<mailbox>`),
  and a site-wide cap `SIGNUP_BONUS_DAILY_CAP` (default 25) per rolling 24 hours. When the cap is used up the offer disappears from the pages and no credit is
  given. The count and the grant are serialised with a Postgres advisory lock (held until the sign-up commits), so simultaneous sign-ups cannot
  overshoot the cap. Residual risk: someone with many real mailboxes (different domains) can still collect it; worst case per day is cap x one search cost. Add email
  verification (Resend) before raising the cap.
  Accepted risks while the credit is on: (1) addresses are not verified and the mailbox key only folds `+tag`, case and gmail dots, so look-alikes such as
  `zed@gmail.com` and `zed@gmail.com` plus a zero-width space count as different mailboxes and each get the credit; (2) the daily cap is first come, first served,
  so a spammer can use up the 25 grants and real visitors get no credit until the rolling 24 hours pass (the sign-up IP limits of section 3 slow this down;
  they trust the forwarded address from Caddy/Cloudflare). Worst case is still cap x credit per day. Fix properly with email verification.
- **Forgot password.** There is no self-serve reset yet (no email sender). The sign-in form tells the customer to email the owner, who runs
  `admin_cli.py reset-user EMAIL` (new password, signed out everywhere, never makes the account an admin; `reset-admin` would). The owner must be
  reasonably sure the request comes from the account's own address.
- **Public pages.** The two-factor screen is no longer in the public HTML (it is built in the browser only when an admin needs it). `/auth/status` now says
  `signed_in` so visitors do not trigger a 401 from `/auth/me`. Error lines start hidden and silent.
- **Cloudflare Web Analytics.** The strict CSP (no third-party scripts) correctly blocks Cloudflare's auto-injected beacon. The CSP stays as it is; to stop the
  console message, turn off automatic Web Analytics for the site in the Cloudflare dashboard.
- **Saved jobs (migration 0010).** `saved_jobs` is private to each customer: every endpoint (`GET /account/saved`, `GET /account/saved/ids`,
  `PUT`/`DELETE /account/saved/{job_id}`) needs a signed-in user and the writes need `csrf_guard`. Only real, listed jobs can be saved, at most 500 per
  customer, and a job removed from the database drops out of the list. Nothing is shared between customers.
- **Sign-out.** The page now confirms with `/auth/status` that the session is really gone and signs out a second time if it is not (a customer reported
  that the first click sometimes did not stick on the live site; the cause was not reproduced locally, so this verifies instead of assuming).
- **Believable dates and "Open post" addresses (2026-10-13, migration 0011).** `app/dates.py` rejects posting dates before 2015 or more than a day ahead
  at ingest and at read time (a customer saw "691 months ago"); migration 0011 clears the bad stored values. Google results on Indeed, jobgether,
  LinkedIn and Glassdoor are accepted only in their single-job address shape; `scripts/purge_listing_urls.py` (dry run by default) hides stored rows
  that open a search or list page. The same company, title and place within 90 days is one job even when reached by two addresses.
