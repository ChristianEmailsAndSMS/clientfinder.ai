# Stack Decision Record

Status: **Proposed**. Becomes final once the open items in `docs/INFRA.md` confirm no conflict
with CopyProfit.ai / EmailProfit.ai.

## Decision

| Layer | Choice | Status |
|---|---|---|
| Backend | Python + FastAPI | Already scaffolded in `backend/` |
| ORM / migrations | SQLAlchemy 2 + Alembic | Already scaffolded |
| Database | Postgres 16, separate database and role from the sibling apps | Scaffolded (docker-compose) |
| Queue / cache | Redis; RQ for scraper jobs | Redis in compose, RQ not yet added |
| Scheduler | APScheduler (in-process) first; Celery Beat only if we outgrow it | Not yet added |
| Page fetching | Playwright, Firecrawl as fallback | Playwright in requirements |
| Extraction | Claude (Haiku vs Sonnet, benchmark both) | Anthropic SDK in requirements |
| Search layer | SerpAPI or Serper.dev | Pick one in Phase 1 |
| Frontend | Next.js + Tailwind | `frontend/` is a placeholder; a server-rendered landing page is served by FastAPI at `/` for now |
| Email | Resend | Confirmed |
| Payments | Whop | Confirmed |

## Why this stack

- **Python for the backend**: scraping, Playwright, and the Anthropic SDK are all first-class
  in Python, and Phase 1 code already exists.
- **Postgres**: relational data (users, credits ledger, jobs) with auditability; pgvector is
  available later for the Phase 8 RAG store.
- **Separate database and role**: isolation from the sibling apps; a bad migration or runaway
  scraper cannot touch their data.
- **RQ over Celery**: smaller surface area for a solo-run product. Revisit if we need complex
  schedules or retries.
- **APScheduler first**: the checklist allows Celery Beat or APScheduler; the latter needs no
  extra broker semantics for hourly and 4x/day jobs.

## Alternatives considered

| Option | Why not (for now) |
|---|---|
| Reuse the sibling apps' stack | Unknown until INFRA.md section 6 is filled. If they are Python/Postgres, we reuse infra (proxy, backups), not code. If they are Node-only, we still keep Python for the scraper. |
| Next.js-only (API routes) | Scraping and LLM extraction are heavier in Python; two services is fine. |
| Skip the Next.js frontend, keep server-rendered templates | Cheaper to start. Revisit at Phase 3 if the filterable job table does not need rich client interactivity. |
| Celery + Beat | More moving parts than the load requires today. |

## Decisions that block later phases

| Decision | Needed by | Owner | Status |
|---|---|---|---|
| Final stack sign-off | Phase 2 | Christian | Open |
| Shared vs separate Postgres instance | Phase 1 deploy | Christian | Separate (own container already running; copybot-db is CopyProfit's) |
| Search provider (SerpAPI vs Serper) | Phase 1 | Christian | Open |
| Extraction model (Haiku vs Sonnet) | Phase 1 | Benchmark | Open |
| Rights to Dylan's chat-closing course content | Phase 8 | Christian | Open |

## IP note (Phase 8)

Until rights to Dylan's course content are confirmed, treat it as off-limits: do not ingest it
into the RAG store and do not show it to users. Christian's own content (Vault, Full Stack
Email Marketer, Whop transcripts) is fine to ingest.
