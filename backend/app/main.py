from fastapi import FastAPI, Request
from .api.account import router as account_router
from .api.admin import router as admin_router
from .api.webhooks import router as webhooks_router
from .api.auth import router as auth_router
from .api.dashboard import router as dashboard_router
from .config import settings
from .logging_setup import setup_logging
from .api.jobs import router as jobs_router
from .api.public import router as public_router
from .api.site import router as site_router
from .api.searches import router as searches_router

setup_logging()

# Interactive docs and the schema are an attack map. Off unless ENABLE_DOCS=1 (local dev).
app = FastAPI(
    title="Clientfinder.ai API", version="0.1.0",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

_BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}
# Every page loads only its own files (no inline script or style, no third-party code).
_PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
              "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _BASE_HEADERS.items():
        response.headers.setdefault(k, v)
    response.headers["Content-Security-Policy"] = _PAGE_CSP
    if request.url.path.startswith(("/admin", "/auth", "/account", "/app", "/login", "/searches", "/jobs")):
        response.headers["Cache-Control"] = "no-store"
    return response

app.include_router(jobs_router)
app.include_router(public_router)
app.include_router(searches_router)
app.include_router(site_router)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(admin_router)
app.include_router(webhooks_router)

@app.get("/health")
def health():
    return {"ok": True, "service": "clientfinder.ai"}
