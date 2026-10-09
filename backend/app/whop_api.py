"""Creates a Whop checkout link for a credit bundle, on demand, through Whop's API.

The request shape follows Whop's "create checkout configuration" reference (POST /api/v1/checkout_configurations, Bearer key, a `plan`
with company_id / initial_price / plan_type / currency, plus metadata and redirect_url) but could not be tried against the live API
from here. So failures are never swallowed: Whop's own error message comes back to the caller (secrets redacted), and
`scripts/whop_check.py` shows it on the server in one command."""
from __future__ import annotations

import logging

import httpx

from .config import settings
from .security_utils import redact

log = logging.getLogger(__name__)
API = "https://api.whop.com/api/v1/checkout_configurations"
BUNDLES = (27, 47, 97)


class WhopApiError(Exception):
    """`str(e)` is the detail for logs and scripts/whop_check.py. Customers only ever see PUBLIC_MESSAGE."""


PUBLIC_MESSAGE = "Checkout is temporarily unavailable. Try again in a few minutes, or email christian@emailsandsms.com."


def configured() -> bool:
    return bool(settings.whop_api_key.strip() and settings.whop_company_id.strip())


def _clean(text: str) -> str:
    """Whop's error text, with anything credential-like removed, and our own key removed whatever shape it has."""
    key = settings.whop_api_key.strip()
    out = redact(text)
    return out.replace(key, "[key]") if key else out


def _post(payload: dict) -> httpx.Response:
    return httpx.post(API, json=payload, headers={"Authorization": f"Bearer {settings.whop_api_key.strip()}"}, timeout=15.0)


def create_checkout(user_id: int, email: str, usd: int) -> str:
    """The https checkout URL for exactly `usd` dollars of credit, tagged with who is buying."""
    if usd not in BUNDLES:
        raise WhopApiError("That amount is not available.")
    if not configured():
        raise WhopApiError("Checkout is not set up yet.")
    plan = {"company_id": settings.whop_company_id.strip(), "initial_price": usd, "plan_type": "one_time", "currency": "usd",
            "title": f"${usd} Clientfinder credits"}
    if settings.whop_product_id.strip():
        plan["product_id"] = settings.whop_product_id.strip()
    payload = {"plan": plan, "metadata": {"cf_user_id": str(user_id), "cf_email": email, "cf_bundle_usd": str(usd)},
               "redirect_url": settings.public_url.rstrip("/") + "/app"}
    try:
        r = _post(payload)
    except httpx.HTTPError as e:
        log.warning("whop checkout request failed: %s", type(e).__name__)
        raise WhopApiError("Could not reach the payment service. Try again in a minute.") from e
    if r.status_code >= 300:
        log.warning("whop checkout refused: %s %s", r.status_code, _clean(r.text)[:300])
        raise WhopApiError(f"Payment service said {r.status_code}: {_clean(r.text)[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        raise WhopApiError("Unreadable answer from the payment service.") from e
    data = data.get("data", data) if isinstance(data, dict) else {}
    url = data.get("purchase_url") or data.get("url")
    if not (isinstance(url, str) and url.startswith("https://")):
        raise WhopApiError("The payment service did not return a checkout link.")
    return url
