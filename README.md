# Clientfinder.ai

SaaS that finds marketing / copywriting / email / funnel-builder jobs across the web (job boards + social media), filters them, and gives users a living feed they can sort by pay, type, and platform.

## Status

Early scaffold — Phase 1 (data pipeline MVP) in progress. See [CHECKLIST.md](CHECKLIST.md) for the full build plan.

## Approach

"Manus-style" scraping — rather than custom per-site scrapers, we:

1. Query Google Search (SerpAPI / Serper.dev) for `"hiring copywriter"`, `"hiring email marketer"`, etc. — Google already indexes Twitter, Reddit, LinkedIn posts, blog hiring pages.
2. Fetch each result URL with Playwright (handles JS-rendered pages).
3. Feed the HTML to Claude, which extracts structured job data: title, company, pay, type, apply-URL, posted-at.
4. Store in Postgres, serve via FastAPI, display in a Next.js frontend.
5. Supplement with **direct ingestion** from durable sources that don't block us: RemoteOK, ProBlogger, Greenhouse/Lever boards, Upwork RSS.

## Local dev

```bash
# 1. Start Postgres + Redis
docker-compose up -d

# 2. Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys when you have them; defaults work in dev-fixture mode
alembic upgrade head

# 3. Run the pipeline against fixtures (no API keys needed)
python scripts/run_pipeline.py --query "hiring copywriter"

# 4. Boot the API
uvicorn app.main:app --reload
# → open http://localhost:8000/docs
```

See [CHECKLIST.md](CHECKLIST.md) for everything else.
