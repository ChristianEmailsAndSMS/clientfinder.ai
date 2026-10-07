from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    database_url: str = "postgresql+psycopg://clientfinder:dev_only_change_me@localhost:5433/clientfinder"

    dev_fixtures: bool = True

    serpapi_api_key: str = ""
    serper_api_key: str = ""
    google_search_provider: str = "serpapi"

    firecrawl_api_key: str = ""

    anthropic_api_key: str = ""
    extraction_model: str = "claude-haiku-4-5-20251001"

    jwt_secret: str = "change-me-dev-only"
    jwt_alg: str = "HS256"

    resend_api_key: str = ""
    email_from: str = "noreply@clientfinder.ai"

    whop_api_key: str = ""
    whop_webhook_secret: str = ""


settings = Settings()
