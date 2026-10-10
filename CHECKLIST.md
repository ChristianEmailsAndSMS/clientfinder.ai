# Clientfinder.ai — Master Checklist

Build phases. Each item is independently checkable; later phases depend on earlier ones. Start order recommendation at the bottom.

## Where we are and the game plan (updated 2026-10-10)

**Built and live on the VPS (pull and restart to get the latest):** home page with sign-up and sign-in, customer app with onboarding, filters
(job boards vs social, location and time zones), live paid searches (1.5x), Pitch help, credit bundles, admin dashboard (customers, unlimited credits,
limits, sources, payments, margin), Whop checkout and webhook, daily local backups, logo.

**Not yet proven with the real services:** a real Whop payment end to end, a live SerpApi + Claude search on the server, Google's 3/6/9-month date filters.

### Do next, in this order
1. [ ] **Deploy the latest** (`git pull`, `pip install`, `alembic upgrade head`, restart) and copy the new Caddy `request_body` block so screenshots work.
2. [ ] **Real test purchase:** sign up as a customer, buy $27 on Whop, confirm the balance rises and the payment shows in `/admin/payments`. Fix field names if it is held.
3. [ ] **Real test search** on the server (give yourself unlimited credits first); check SerpApi shows one search used and the dashboard margin reads about 1.50x.
4. [ ] **Save `backend/.env` in a password manager** (it is not in GitHub). Then the **off-server encrypted database backup** (Backblaze B2 via rclone).
5. [ ] **Server hardening:** SSH key-only, run as a non-root user, least-privilege database login (list at the bottom of this file).
6. [ ] **Resend emails:** verify email, forgot password, "low credits" at 20%, welcome. Then a small welcome credit if you want one.
7. [ ] **Legal and trust:** terms, privacy policy, refund policy, pricing page. Uptime monitor and error alerts.
8. [ ] **Merge `claude/quirky-edison-h61hvc` into `main`** and point the server at `main`, so GitHub's default branch holds the real code.
9. [ ] Turn admin 2FA back on (`ADMIN_REQUIRE_2FA=1`) before launch.

### First month after launch
- [ ] CSV export and saved searches ("new jobs matching your filter since your last visit")
- [ ] Customer lifetime spend and Whop status in the admin table; delete-account and password-reset-email actions
- [ ] Refund and chargeback handling (events are held for review today; decide the policy)
- [ ] More sources: Reddit, X/Twitter, IndieHackers (Phase 7)
- [ ] Decide whether searches should let customers choose how many results to pull (needs paging through Google; extra cost per page)
- [ ] Rebuild the matching rules in `app/geo.py` after seeing how many real jobs state a location

### Later
- [ ] Course content and the "getting your first client" flow (Phase 8, check the IP question first)
- [ ] Reply-rate and close-rate metrics, benchmarks, money-made ticker (Phase 10)
- [ ] Profile-screenshot outreach (the rest of Phase 9)
- [ ] Automatic deploys from GitHub (GitHub Action, only after tests pass)

### Open decisions for the owner
- Refund policy and what to do about chargebacks
- Email wording and sender address for Resend
- Whether to keep Pitch help's daily cap at 60 drafts and live search at 20 per day for paying customers

---

## Phase 0 — Infrastructure prep & decisions

- [x] Document the existing VPS (see `docs/INFRA.md`) (OS, Docker vs bare metal, reverse proxy, ports in use, how CopyProfit.ai and EmailProfit.ai are deployed — so Clientfinder.ai lands as a sibling without stepping on them)
- [x] Confirm domain `clientfinder.ai` is on the VPS or where DNS points (GoDaddy DNS -> this VPS)
- [x] Pick primary stack. **Decided and built:** FastAPI + plain JS served by the API (no Next.js, no build step), Postgres, APScheduler (no Redis), Claude Haiku 5.5, Whop, Resend (DNS set; no emails sent yet). Original proposal:
  - Backend: **Python + FastAPI** (great for scraping, Claude API, async)
  - Frontend: **Next.js (React) + Tailwind**, server-side auth
  - DB: **Postgres** (same instance, separate database for isolation)
  - Queue: **Redis + RQ** for scraper jobs
  - Email: **Resend** (confirmed)
  - Payments: **Whop** (confirmed)
- [ ] Decide: are CopyProfit.ai and EmailProfit.ai on this stack already, or will Clientfinder.ai introduce new tech? (reusing = faster)
- [ ] Resolve IP question for Dylan's chat-closing course content (see Phase 8)

## Phase 1 — Scraper / data layer MVP (Manus-style: Google-first + LLM extraction)

The approach: Google is already indexing Twitter/X, Reddit, LinkedIn posts, random hiring blogs, Discord listings — everything. We query Google for the keywords, fetch the result pages with a headless browser, and let Claude extract structured data. One LLM extractor replaces dozens of fragile per-site scrapers.

- [x] **SerpAPI** account (key in the server `.env`; customer-paid searches only, the scheduled Google plan stays off) (~$75/mo for 5K searches) OR **Serper.dev** (~$50/mo for 2.5K) — the Google-search layer
- [ ] Keyword query library:
  - `"hiring copywriter"` scoped to last 24h / 7d
  - `"hiring email marketer"`, `"hiring email copywriter"`
  - `"hiring creative strategist"`, `"hiring creative director"`
  - `"hiring landing page builder"`, `"hiring funnel builder"`
  - `"looking for a copywriter"`, `"need a copywriter"`, `"copywriter wanted"`
  - Scoped searches: `site:twitter.com`, `site:x.com`, `site:reddit.com`, `site:linkedin.com/posts`, `site:indeed.com`, `site:upwork.com`
  - Rerun every N hours; dedupe URLs we've already extracted
- [ ] **Page fetcher**: Playwright on VPS (free, handles JS) OR Firecrawl API ($19/mo starter — cleaner but costs scale). Default to Playwright, fall back to Firecrawl when a site blocks us.
- [ ] **For hostile sites** (LinkedIn in particular): optional **Bright Data** / **Apify** residential proxy integration. Add later if LinkedIn consistently fails from the VPS IP.
- [x] **LLM extractor** (Claude Haiku 5.5, billed at 1.5x; was: Claude Sonnet or Haiku): single prompt that reads any page HTML and returns `{is_real_job, title, company_or_poster, pay, type, apply_url, posted_at, platform, raw_snippet}`. Haiku is cheaper per call; Sonnet is more accurate — benchmark both.
- [x] **Durable sources, direct ingestion** (no Google needed, free): RemoteOK API, Remotive API, We Work Remotely RSS, ProBlogger, Mediabistro, Braintrust, Greenhouse/Lever/Ashby public boards filtered to copywriter/email/marketing, Upwork public RSS. These give us a clean baseline the Google-based layer can't miss.
- [x] Dedupe (hash of normalized title + company + poster)
- [x] Postgres schema: `jobs` + `sources` + `job_tags` + `search_queries` + `scrape_runs` + `failed_urls` (migrations 0001-0003, tested on Postgres 16)
- [x] Scraper runs scheduled via **APScheduler** (`clientfinder-scheduler`) (hourly for Google-search layer, 4x/day for durable sources)
- [x] Admin: "re-run source X now" + "requeue failed URLs" endpoints (`/admin/*`, X-Admin-Token; swap for real admin auth in Phase 2/6)

## Phase 2 — Backend API

- [ ] FastAPI app with endpoints:
  - `POST /auth/signup`, `POST /auth/login`, `POST /auth/forgot-password`, `POST /auth/reset-password` (triggers Resend)
  - `GET /jobs?filter=...` — paginated job list
  - `GET /jobs/:id` — full job + source URL
  - `POST /jobs/:id/track` — user says "I replied to this" (feeds reply-rate tracking later)
  - `GET /me` — current user info, credit balance
  - `POST /credits/purchase` — Whop checkout URL
  - Admin endpoints (gated): `GET /admin/users`, `POST /admin/users/:id/credits`, `POST /admin/users/:id/reset-password`
- [ ] JWT session + refresh tokens
- [x] Rate limiting per user (per-IP, plus per-account caps on searches, drafts and checkouts)
- [x] API documented (OpenAPI exists but is hidden in production on purpose)

## Phase 3 — Frontend MVP (job list only, no payments yet)

- [x] Landing page explaining what Clientfinder does (plain HTML/JS served by the API, strict CSP; home, sign in, app. Next.js not needed for this)
- [ ] Signup / login / forgot-password flows wired to Resend  _(signup + login built and tested; forgot-password and email verification still need Resend)_
- [x] **Living job list** inside the account (`/app`): sortable, filterable cards with click-through links
- [x] Filters: pay (min + period), job type, platform, topics, remote, posted-within, keyword search  _(plus platform groups: job boards vs social, location text, time-zone bands)_
- [x] Each job: "Open post" button to the original post (new tab, noopener/nofollow)
- [x] Mobile responsive (filter drawer; checked at 390px, no horizontal overflow)
- [ ] CSV export button (keep the spreadsheet option as a fallback)
- [ ] Saved searches → user's dashboard shows "new jobs matching your filter since last visit"

## Phase 4 — Account system + Resend

- [ ] Resend account + verified sending domain for `clientfinder.ai`
- [ ] Email templates: welcome, verify email, forgot password, password changed, admin-reset-your-password, daily job digest
- [ ] Email verification required before unlocking features
- [ ] Password rules + 2FA option (TOTP via authenticator)  _(built and tested for admin accounts; customer sign-up flow still to do)_
- [ ] Account deletion flow (GDPR hygiene)

## Phase 5 — Whop payment + credit system

- [x] Whop account configured, Clientfinder.ai product set up
- [x] Credits = **1.5× Claude API cost**  _(live search and Pitch help both bill through it; dashboard shows the real margin)_ (`CREDIT_MARKUP` in `backend/app/pricing.py`, the single source of truth). Metering:
  - On every Claude API call, read `response.usage.input_tokens` and `output_tokens`
  - Convert to cost via the price table in `app/pricing.py` (verify prices against Anthropic's pricing page; recheck on each model change)
  - `charge_usd()` multiplies by 1.5 and rounds UP; an unpriced model must be refused, never charged at a guess
  - Debit atomically (row lock or single `UPDATE ... WHERE balance >= x`) so concurrent requests cannot overdraw a balance
  - Charge for failed/unparseable calls too: we still paid Anthropic for the tokens
  - If balance insufficient → block the operation + prompt to buy more
- [ ] **Margin math (1.5× = 50% of cost per call).** Scraping (SerpAPI + extraction, roughly $35–$45/month at full speed, unmeasured) is a *shared fixed* cost that does not scale with users. Credit margin covers it only if users' raw Claude spend reaches about 2× that figure per month (~$70–$90). Whop/processor fees also come out of the margin; confirm their rate. Re-run `scripts/cost_report.py` monthly. If margin falls short, options: a small platform/subscription fee, or a higher markup. Scraping features stay free for all users.
- [x] Credit purchase flow via Whop checkout ($27 / $47 / $97 bundles, links made by the Whop API per click; needs the first real test purchase)
- [x] Webhook from Whop → increment credits on successful payment (built and tested with simulated requests; **not yet tried with a real payment**)
- [x] Transaction log table (every debit, every top-up): `credit_ledger`, append-only for the app role, balance reconciles with the ledger (tested under concurrency on Postgres)
- [ ] "Low credits" email at 20% remaining

## Phase 6 — Admin interface

- [x] Admin-only area at `/admin` behind an allowlist of your email(s) + 2FA (`ADMIN_EMAILS`, password + TOTP, first-run account setup screen; docs/SECURITY.md)
- [ ] Table of all customers: email, signup date, credit balance, lifetime spend, last-login, Whop subscription status  _(built: email, signup, balance, last login, status. Missing: lifetime spend, Whop status.)_
- [ ] Per-customer actions: grant credits, revoke credits, view activity log, trigger password reset email (Resend), suspend/unban, delete account  _(built: grant, revoke, credit history, suspend/unban, unlock, sign out everywhere. Missing: password-reset email, delete account.)_
- [x] Scraper status dashboard: last run per source, success/fail counts, # new jobs added today (dashboard: Overview + Sources tabs; run-now and requeue buttons)
- [ ] Error log viewer (recent exceptions)
- [ ] Monthly revenue dashboard

## Phase 7 — Social media scraping (paid tier — Twitter/X first)

- [ ] Twitter/X API Basic plan ($200/mo) or weigh paid tier that works
- [ ] Search queries for `hiring copywriter`, `hiring email marketer`, `hiring creative strategist`, `hiring landing page builder`, `hiring funnel builder`, `looking for a copywriter`, `need a copywriter`, etc.
- [ ] Reddit (free API): /r/forhire, /r/HireACopywriter, /r/jobbit, /r/slavelabour (for small gigs), /r/DesignJobs
- [ ] IndieHackers: scrape #hiring posts
- [ ] Public Discord servers you have access to: bot that watches `#hiring` channels in copywriting/marketing communities (needs server admin invites)
- [ ] LinkedIn: **expect failures.** Try the public jobs search HTML with rotating headers; mark as "best effort, often empty" in the UI
- [ ] Instagram: no path. Document as not-supported, suggest users search manually with a saved query link
- [ ] Classifier on social posts: filter spam, filter already-filled, extract contact method (DM, email, form link)

## Phase 8 — Course content & intro flow

- [ ] Decide IP strategy for Dylan's chat-closing course transcripts (confirm Christian's rights first):
  - Option A: use only as internal LLM context (reference, never shown)
  - Option B: user-visible, requires license or rebuild in Christian's own words
- [ ] Ingest Christian's own course content ($57M Vault, Full Stack Email Marketer, Whop transcripts) into a RAG store (Postgres + pgvector or similar)
- [ ] "Getting your first client" intro flow on signup: curated lessons from Christian's content, playlisted
- [ ] In-app lesson viewer

## Phase 9 — Outreach draft generator (future)

- [ ] User uploads screenshot of their social profile (IG, LinkedIn, X)
- [ ] OCR + vision model extracts bio, follower count, niche
- [x] Claude drafts personalized outreach messages tailored to a specific job listing (**Pitch help**: first message, cover letter, resume pitch, what to send)
- [ ] Variants: cold DM, cold email, response-to-post
- [ ] Credit cost per generation

## Phase 10 — Chat coaching + metrics ticker

- [x] Users upload screenshot of a chat conversation (**Pitch help → They replied**: reads it and suggests three replies)
- [ ] Vision model extracts turns, counts replies vs messages sent
- [ ] Dashboard metrics per user:
  - Reply rate (replies received / messages sent)
  - Call-booking rate (calls booked / replies received)
  - Close rate (clients closed / calls booked)
  - Money-made ticker (user self-reports deals won, dollar amount)
- [ ] Compare each user's metrics to platform benchmarks → "you're below average on reply rate, here's a lesson from Christian's course on improving your DMs"

## Phase 11 — Launch

- [ ] Firewall rules on VPS (UFW/iptables), only ports 80/443 open, SSH key-only
- [ ] Let's Encrypt SSL via nginx reverse proxy
- [ ] Backups: Postgres daily to S3/B2, encrypted, 30-day retention  _(local daily verified backups on the VPS are in place: `deploy/backup.sh`, see docs/INFRA.md §9. Still needed: the off-box encrypted copy.)_
- [ ] Error monitoring (Sentry free tier)
- [ ] Uptime monitoring (BetterUptime or UptimeRobot free)
- [ ] Terms of service, privacy policy, cookie policy (GDPR-safe default)
- [ ] Pricing page
- [ ] Marketing: launch sequence to your Full Stack Email Marketer students first

---

## Recommended start order

**Week 1 (Phase 0 + 1):** VPS inventory + scraper MVP for 3 sources (ProBlogger + RemoteOK + Greenhouse). Prove the data pipeline works end-to-end into Postgres before building any UI.

**Week 2–3 (Phase 2 + 3):** FastAPI backend + Next.js frontend showing the job list. No auth yet, just so you can see it work.

**Week 4 (Phase 4):** Auth + Resend wired up. You use it yourself.

**Week 5–6 (Phase 5 + 6):** Whop + credits + admin dashboard. Soft-launch to yourself + a handful of students.

**Week 7+ (Phase 7):** Add Twitter/X, Reddit, public Discord. This is where it gets really valuable for your users.

**Later (Phase 8–10):** Course content, outreach generator, chat coaching.

---

## Server hardening to do before real customers (owner chose to do these later)

Walk-through for each is in `docs/SECURITY.md` section 4. Run `bash deploy/security_audit.sh` afterwards.

- [ ] **SSH key-only login** (two red FAILs in the audit). Owner uses the hosting browser console; do this before sharing the server password with anyone.
- [ ] **Run the API and scheduler as a non-root user.**
- [ ] **Least-privilege database login** (`deploy/db_least_privilege.sql`).
- [ ] **Encrypted off-server backups** (today they live on the same server).
- [ ] Copy the new `request_body` block from `deploy/Caddyfile.security.snippet` into `/etc/caddy/Caddyfile` (screenshots in the pitch helper need the 6 MB allowance on `/assist`).
- [ ] Turn admin 2FA back on (`ADMIN_REQUIRE_2FA=1`).
