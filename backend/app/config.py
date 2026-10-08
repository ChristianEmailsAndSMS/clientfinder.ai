from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    database_url: str = "postgresql+psycopg://clientfinder:dev_only_change_me@localhost:5433/clientfinder"

    # Fixtures are fake data. Off unless a dev explicitly sets DEV_FIXTURES=1 in .env.
    dev_fixtures: bool = False

    serpapi_api_key: str = ""
    serper_api_key: str = ""
    google_search_provider: str = "serpapi"

    firecrawl_api_key: str = ""

    anthropic_api_key: str = ""
    extraction_model: str = "claude-haiku-4-5-20251001"

    # Scheduler cadence (app/scheduler.py)
    google_search_interval_minutes: int = 60     # how often the scheduler checks for due queries
    google_query_cycle_hours: int = 12           # each query in the plan re-runs this often
    google_max_queries_per_tick: int = 6         # cap per scheduler tick (spreads load, bounds a backlog)
    serpapi_monthly_budget: int = 4500           # hard stop. SerpAPI free tier is ~100/mo: set 100 until you upgrade
    playwright_fallback: bool = True
    durable_sources_interval_hours: int = 6

    jwt_secret: str = "change-me-dev-only"
    jwt_alg: str = "HS256"

    resend_api_key: str = ""
    email_from: str = "noreply@clientfinder.ai"

    whop_api_key: str = ""
    whop_webhook_secret: str = ""


settings = Settings()
