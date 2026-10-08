# Clientfinder.ai — Master Checklist

Build phases. Each item is independently checkable; later phases depend on earlier ones. Start order recommendation at the bottom.

---

## Phase 0 — Infrastructure prep & decisions

- [x] Document the existing VPS (see `docs/INFRA.md`) (OS, Docker vs bare metal, reverse proxy, ports in use, how CopyProfit.ai and EmailProfit.ai are deployed — so Clientfinder.ai lands as a sibling without stepping on them)
- [x] Confirm domain `clientfinder.ai` is on the VPS or where DNS points (GoDaddy DNS -> this VPS)
- [ ] Pick primary stack. Default proposal:
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

- [ ] **SerpAPI** account (~$75/mo for 5K searches) OR **Serper.dev** (~$50/mo for 2.5K) — the Google-search layer
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
- [ ] **LLM extractor** (Claude Sonnet or Haiku): single prompt that reads any page HTML and returns `{is_real_job, title, company_or_poster, pay, type, apply_url, posted_at, platform, raw_snippet}`. Haiku is cheaper per call; Sonnet is more accurate — benchmark both.
- [ ] **Durable sources, direct ingestion** (no Google needed, free): RemoteOK API, Remotive API, We Work Remotely RSS, ProBlogger, Mediabistro, Braintrust, Greenhouse/Lever/Ashby public boards filtered to copywriter/email/marketing, Upwork public RSS. These give us a clean baseline the Google-based layer can't miss.
- [ ] Dedupe (hash of normalized title + company + poster)
- [ ] Postgres schema: `jobs` table + `job_sources` enum + `job_tags` + `search_queries` + `scrape_runs` (audit)
- [ ] Scraper runs scheduled via **Celery Beat** or **APScheduler** (hourly for Google-search layer, 4x/day for durable sources)
- [ ] Admin: "re-run source X now" + "requeue failed URLs" endpoints

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
- [ ] Rate limiting per user
- [ ] API documented (FastAPI auto-generates OpenAPI)

## Phase 3 — Frontend MVP (job list only, no payments yet)

- [ ] Next.js app, clean landing page explaining what Clientfinder does
- [ ] Signup / login / forgot-password flows wired to Resend
- [ ] **Living job list** inside the account (not a spreadsheet — the user's own request update): sortable, filterable table of jobs with click-through links
- [ ] Filters: pay range (slider), job type (contract/FT/social-post/hourly), platform, posted-within (24h/7d/30d/all), location, keyword search
- [ ] Each row: clickable `Source URL` button that opens the original job post or social media post
- [ ] Mobile responsive
- [ ] CSV export button (keep the spreadsheet option as a fallback)
- [ ] Saved searches → user's dashboard shows "new jobs matching your filter since last visit"

## Phase 4 — Account system + Resend

- [ ] Resend account + verified sending domain for `clientfinder.ai`
- [ ] Email templates: welcome, verify email, forgot password, password changed, admin-reset-your-password, daily job digest
- [ ] Email verification required before unlocking features
- [ ] Password rules + 2FA option (TOTP via authenticator)
- [ ] Account deletion flow (GDPR hygiene)

## Phase 5 — Whop payment + credit system

- [ ] Whop account configured, Clientfinder.ai product set up
- [ ] Credits = **1.5× Claude API cost** (`CREDIT_MARKUP` in `backend/app/pricing.py`, the single source of truth). Metering:
  - On every Claude API call, read `response.usage.input_tokens` and `output_tokens`
  - Convert to cost via the price table in `app/pricing.py` (verify prices against Anthropic's pricing page; recheck on each model change)
  - `charge_usd()` multiplies by 1.5 and rounds UP; an unpriced model must be refused, never charged at a guess
  - Debit atomically (row lock or single `UPDATE ... WHERE balance >= x`) so concurrent requests cannot overdraw a balance
  - Charge for failed/unparseable calls too: we still paid Anthropic for the tokens
  - If balance insufficient → block the operation + prompt to buy more
- [ ] **Margin math (1.5× = 50% of cost per call).** Scraping (SerpAPI + extraction, roughly $35–$45/month at full speed, unmeasured) is a *shared fixed* cost that does not scale with users. Credit margin covers it only if users' raw Claude spend reaches about 2× that figure per month (~$70–$90). Whop/processor fees also come out of the margin; confirm their rate. Re-run `scripts/cost_report.py` monthly. If margin falls short, options: a small platform/subscription fee, or a higher markup. Scraping features stay free for all users.
- [ ] Credit purchase flow via Whop checkout
- [ ] Webhook from Whop → increment credits on successful payment
- [ ] Transaction log table (every debit, every top-up) — audit trail
- [ ] "Low credits" email at 20% remaining

## Phase 6 — Admin interface

- [ ] Admin-only area at `/admin` behind an allowlist of your email(s) + 2FA
- [ ] Table of all customers: email, signup date, credit balance, lifetime spend, last-login, Whop subscription status
- [ ] Per-customer actions: grant credits, revoke credits, view activity log, trigger password reset email (Resend), suspend/unban, delete account
- [ ] Scraper status dashboard: last run per source, success/fail counts, # new jobs added today
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
- [ ] Claude drafts personalized outreach messages tailored to a specific job listing
- [ ] Variants: cold DM, cold email, response-to-post
- [ ] Credit cost per generation

## Phase 10 — Chat coaching + metrics ticker

- [ ] Users upload screenshot of a chat conversation
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
- [ ] Backups: Postgres daily to S3/B2, encrypted, 30-day retention
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
