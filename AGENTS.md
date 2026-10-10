# Clientfinder.ai: instructions for AI coding agents (Cursor, Claude Code, others)

Read this first. The README is older than the code; this file and `docs/` are current.

## What this is
A SaaS that collects hiring posts for copywriters, email marketers, funnel builders and creative strategists (job boards and social posts) into
Postgres. Customers browse and filter for free, and pay credits to run live searches and to use the "Pitch help" writer. Live at https://clientfinder.ai.

## How it runs and how changes get there (no auto-deploy)
```
you / agent  --push-->  GitHub (ChristianEmailsAndSMS/clientfinder.ai)  --manual git pull-->  the VPS  -->  Cloudflare (DNS proxy)  -->  visitors
```
- The app runs on the owner's VPS in `/root/clientfinder.ai` (FastAPI + uvicorn as `clientfinder-api`, scheduler as `clientfinder-scheduler`,
  Postgres in Docker on 127.0.0.1:5433, Caddy in front for HTTPS). Cloudflare only proxies DNS. It is NOT a Cloudflare Worker.
- **Nothing deploys automatically.** An agent can only push to GitHub. The owner then runs on the server:
  ```
  cd /root/clientfinder.ai && git pull
  cd backend && .venv/bin/pip install -r requirements.txt && .venv/bin/alembic upgrade head
  systemctl restart clientfinder-api clientfinder-scheduler
  ```
  Say in your final message when a change needs `alembic upgrade head`, new `.env` settings, or a Caddy change.
- Agents have no access to the server, its database or its `.env`. Never ask for keys in chat; tell the owner which setting to add.
- Working branch today: `claude/quirky-edison-h61hvc` (the server pulls this). `main` is behind. Do not push to `main` without being asked.
  Work on one branch at a time, and commit small. Do not open a pull request unless asked.

## Layout (`backend/` is the whole app)
- `app/api/` routers: `auth`, `account` (credits, prefs, profile, checkout), `jobs` (filters, facets), `searches` (live search), `assist` (Pitch help),
  `admin`, `webhooks` (Whop), `site` (HTML pages and `/assets/*`).
- `app/static/site/` customer UI (plain JS, no framework, no build step). `app/static/admin/` admin dashboard.
- `app/scrapers/` collection pipeline; `app/live_search.py`; `app/assist.py`; `app/whop.py` (webhook) and `app/whop_api.py` (checkout links).
- `app/credits.py` credit ledger; `app/pricing.py` model prices and the 1.5x markup; `app/geo.py` and `app/platforms.py` job classification.
- `alembic/versions/` migrations (0001 to 0008 so far). `deploy/` server files and the audit script. `docs/` COSTS, SECURITY, INFRA, STACK.
- `CHECKLIST.md` = the build plan and what the owner still has to do.

## Run the tests (needs no database, no keys, no network)
```
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest -q
```
All tests must pass before you push (385 at the time of writing). Tests use SQLite and stub every outside service.

## Rules that must not be broken
1. **Money.** Balances are integers in micro-USD. Change them only through `credits.apply(...)` (atomic, append-only, idempotent via `ref`). Customers pay
   `CREDIT_MARKUP` (1.5x) of our real cost, computed in `pricing.py` with exact Decimal math. Never use floats for money.
2. **Security headers / CSP.** Pages allow no inline script or inline style and load nothing from other sites. Do not use `innerHTML`; build DOM with
   `CF.h` and `textContent`. New static files must be added to the allow-list in `app/api/site.py`.
3. **Every write endpoint** needs a signed-in user and `csrf_guard`. Admin routes sit behind `current_admin`. Webhooks are authenticated by signature only.
4. **Secrets** live only in the server's `.env` (see `backend/.env.example`). Never commit `.env`, keys, or customer data. Do not log secrets
   (`security_utils.redact`).
5. **Database changes** need an Alembic migration (never edit an old one) plus a model change, and tests.
6. **Untrusted text** (job posts, pasted messages, screenshots) goes to the model as data, never as instructions, and is shown as plain text.
7. **No guessing external APIs.** Whop's payload fields and the 3/6/9-month Google date filters are unverified; if a service's docs are unreachable, say so.
8. Add or update tests with every behaviour change, and keep the docs in step (`docs/SECURITY.md` for anything touching auth, money or uploads).

## Where things are still open
See `CHECKLIST.md` (server hardening list) and `docs/SECURITY.md` section 9 (known and accepted risks).
