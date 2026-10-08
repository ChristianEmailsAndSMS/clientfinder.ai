#!/usr/bin/env python3
"""Try Whop checkout creation from the server and print exactly what Whop answers. Never prints the API key.

    .venv/bin/python scripts/whop_check.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import whop_api  # noqa: E402
from app.config import settings  # noqa: E402

print("WHOP_API_KEY set:", bool(settings.whop_api_key.strip()), "| WHOP_COMPANY_ID:", settings.whop_company_id.strip() or "(missing)",
      "| WHOP_PRODUCT_ID:", settings.whop_product_id.strip() or "(none)")
if not whop_api.configured():
    sys.exit("Set WHOP_API_KEY and WHOP_COMPANY_ID (biz_...) in .env first.")
try:
    print("OK, checkout link:", whop_api.create_checkout(0, "check@example.com", 27))
except whop_api.WhopApiError as e:
    sys.exit(f"FAILED: {e}")
