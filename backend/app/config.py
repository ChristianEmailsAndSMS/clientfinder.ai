from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    database_url: str = "postgresql+psycopg://clientfinder:dev_only_change_me@localhost:5433/clientfinder"

    # Fixtures are fake data. Off unless a dev explicitly sets DEV_FIXTURES=1 in .env.
    # Optional: a separate, more privileged login used ONLY by `alembic` to change the schema. When set, DATABASE_URL can be a
    # limited role (deploy/db_least_privilege.sql) that can read/write rows but never alter or drop tables.
    migration_database_url: str = ""

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
    # Chromium would run as root with no sandbox on pages from the open web. Keep OFF until the scraper runs as an
    # unprivileged user (docs/SECURITY.md, "Run as a non-root user").
    playwright_fallback: bool = False
    enable_docs: bool = False        # /docs, /redoc, /openapi.json. Off in production.
    durable_sources_interval_hours: int = 6

    # Who may create an admin account (via a one-time setup code printed on the server). Comma-separated.
    admin_emails: str = "christian@emailsandsms.com"
    cookie_secure: bool = True       # session cookie only over HTTPS. Tests/local dev over http set COOKIE_SECURE=0.
    session_hours: int = 12
    # The public job feed. Turn JOBS_REQUIRE_LOGIN=1 on before launch: otherwise anyone can read (or scrape) the whole product
    # for free. Left off only because customer sign-up (Phase 4) does not exist yet.
    jobs_require_login: bool = False
    public_rate_limit_per_min: int = 120      # per IP, on the public job endpoints (0 = off)
    max_credit_adjust_usd: float = 1000.0     # sanity cap on a single admin grant/revoke
    backup_dir: str = "/var/backups/clientfinder"

    # Master secret: signs sessions and derives the key that encrypts 2FA secrets. >= 32 random chars, never the default.
    # Generate: python3 -c "import secrets; print(secrets.token_urlsafe(48))". Rotating it logs everyone out.
    # Encrypts 2FA secrets at rest. Separate from JWT_SECRET so rotating the session secret never breaks anyone's 2FA.
    # >= 32 random chars. To rotate: put the new value here and move the old one to DATA_ENCRYPTION_KEY_PREVIOUS.
    # If unset, a key derived from JWT_SECRET is used (then rotating JWT_SECRET WILL break 2FA: set this).
    data_encryption_key: str = ""
    data_encryption_key_previous: str = ""
    jwt_secret: str = "change-me-dev-only"
    jwt_alg: str = "HS256"

    resend_api_key: str = ""
    email_from: str = "noreply@clientfinder.ai"

    whop_api_key: str = ""
    whop_webhook_secret: str = ""


settings = Settings()
