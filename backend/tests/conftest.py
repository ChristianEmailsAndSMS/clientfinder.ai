"""Keep the test suite hermetic: whatever is in a real backend/.env (the server's keys, a switched-off 2FA, a Whop key) must never leak
into tests. Environment variables beat the .env file, so set them BEFORE the app's settings are imported.
(DEV_FIXTURES is left alone on purpose: the suite is run both ways.)"""
import os

for key in ("SERPAPI_API_KEY", "SERPER_API_KEY", "ANTHROPIC_API_KEY", "WHOP_API_KEY", "WHOP_WEBHOOK_SECRET", "WHOP_COMPANY_ID",
            "WHOP_PRODUCT_ID", "WHOP_CHECKOUT_URL", "WHOP_CHECKOUT_27", "WHOP_CHECKOUT_47", "WHOP_CHECKOUT_97",
            "DATA_ENCRYPTION_KEY", "DATA_ENCRYPTION_KEY_PREVIOUS", "RESEND_API_KEY", "MIGRATION_DATABASE_URL"):
    os.environ[key] = ""
os.environ["ADMIN_REQUIRE_2FA"] = "1"
os.environ["GOOGLE_SCHEDULE_ENABLED"] = "0"
os.environ["GOOGLE_SEARCH_PROVIDER"] = "serpapi"
