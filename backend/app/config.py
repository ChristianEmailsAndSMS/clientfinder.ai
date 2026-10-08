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
    # claude-haiku-5-5: $0.10 / $0.50 per million tokens. Must have an entry in app/pricing.py or customer searches refuse to run.
    extraction_model: str = "claude-haiku-5-5"

    # Scheduler cadence (app/scheduler.py)
    # The scheduled Google plan (app/scrapers/query_plan.py) spends YOUR money on a timer. It is OFF by default: having the API keys
    # only enables customer-paid searches. Set GOOGLE_SCHEDULE_ENABLED=1 to also run the scheduled plan at your own cost.
    google_schedule_enabled: bool = False
    google_search_interval_minutes: int = 60     # how often the scheduler checks for due queries
    google_query_cycle_hours: int = 12           # each query in the plan re-runs this often
    google_max_queries_per_tick: int = 6         # cap per scheduler tick (spreads load, bounds a backlog)
    # What one Google search costs us (SerpAPI plan price / searches). Customers pay this x CREDIT_MARKUP. Check your plan.
    serpapi_cost_per_search_usd: float = 0.015
    search_cache_hours: int = 6              # same search within this window is served from our database, free
    user_searches_per_day: int = 20          # live (non-cached) searches per customer per 24h
    user_search_results: int = 10            # Google results processed per live search
    user_search_max_concurrent: int = 3      # live searches running at once, whole site
    serpapi_monthly_budget: int = 4500           # hard stop. SerpAPI free tier is ~100/mo: set 100 until you upgrade
    # Chromium would run as root with no sandbox on pages from the open web. Keep OFF until the scraper runs as an
    # unprivileged user (docs/SECURITY.md, "Run as a non-root user").
    playwright_fallback: bool = False
    enable_docs: bool = False        # /docs, /redoc, /openapi.json. Off in production.
    durable_sources_interval_hours: int = 6

    # Only these emails can be admin, and only via `scripts/admin_cli.py create-admin` on the server (browser sign-up for them is blocked,
    # because without email verification anyone could register the owner's address first). Comma-separated.
    admin_emails: str = "christian@emailsandsms.com"
    cookie_secure: bool = True       # session cookie only over HTTPS. Tests/local dev over http set COOKIE_SECURE=0.
    session_hours: int = 12
    # Free credit given to a new account. Keep 0 until sign-up requires a verified email: otherwise throwaway
    # accounts can farm free credit (live searches cost real money).
    signup_bonus_usd: float = 0.0
    # Where customers pay for credits (your Whop checkout page). Shown as a "Buy credits" button. Until the Whop webhook exists,
    # YOU add the credits after you see the payment (dashboard > Customers > Manage, or `admin_cli.py grant`).
    whop_checkout_url: str = ""
    # Admin 2FA is ON by default. Setting this to 0 lets the owner sign in with email + password only (weaker: one leaked
    # password = full admin access). Customers never have 2FA either way.
    admin_require_2fa: bool = True
    # Signing secret from Whop's webhook settings. Without it the webhook endpoint refuses everything.
    whop_webhook_secret: str = ""
    whop_amount_unit: str = "dollars"          # "dollars" or "cents": how Whop's payment amount field is expressed
    whop_max_topup_usd: float = 500.0          # a single payment above this is held for review instead of auto-credited
    # The job feed needs a signed-in account (otherwise anyone could read or scrape the whole product for free).
    # The home page shows only a small, redacted preview. Set JOBS_REQUIRE_LOGIN=0 only for local development.
    jobs_require_login: bool = True
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
