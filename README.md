# Clientfinder.ai

SaaS that finds marketing / copywriting / email / funnel-builder jobs across the web (job boards + social media), filters them, and gives users a living feed they can sort by pay, type, and platform.

## Status

Live at https://clientfinder.ai (FastAPI app on a VPS). For how it runs, how changes get deployed and the rules for contributors (human or AI), read [AGENTS.md](AGENTS.md). Build plan and open items: [CHECKLIST.md](CHECKLIST.md).

## Approach

"Manus-style" scraping — rather than custom per-site scrapers, we:

1. Query Google Search (SerpAPI / Serper.dev) for `"hiring copywriter"`, `"hiring email marketer"`, etc. — Google already indexes Twitter, Reddit, LinkedIn posts, blog hiring pages.
2. Fetch each result URL with Playwright (handles JS-rendered pages).
3. Feed the HTML to Claude, which extracts structured job data: title, company, pay, type, apply-URL, posted-at.
4. Store in Postgres, serve via FastAPI, and show it in a plain-JS site that FastAPI itself serves (no Next.js, no build step).
5. Supplement with **direct ingestion** from durable sources that don't block us: RemoteOK, Remotive, We Work Remotely, ProBlogger, and Greenhouse/Lever/Ashby boards. (Upwork is only reached through the Google layer, not by RSS.)

## Local dev

```bash
# 1. Start Postgres (docker-compose also starts a Redis container; the app does not use Redis yet)
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
# → open http://localhost:8000/ . The interactive API docs at /docs are hidden (404) unless you set ENABLE_DOCS=1 in .env.
```

See [CHECKLIST.md](CHECKLIST.md) for everything else.
