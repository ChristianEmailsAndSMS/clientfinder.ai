from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from .api.account import router as account_router
from .api.admin import router as admin_router
from .api.auth import router as auth_router
from .api.dashboard import router as dashboard_router
from .config import settings
from .logging_setup import setup_logging
from .api.jobs import router as jobs_router

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
# The admin dashboard loads only its own files (no inline script or third-party code), and must never be cached.
_ADMIN_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
              "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _BASE_HEADERS.items():
        response.headers.setdefault(k, v)
    if request.url.path.startswith(("/admin", "/auth", "/account")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = _ADMIN_CSP
    return response

app.include_router(jobs_router)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(admin_router)

_LANDING_HTML = (Path(__file__).resolve().parent / "templates" / "landing.html").read_text()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
    return _LANDING_HTML


@app.get("/health")
def health():
    return {"ok": True, "service": "clientfinder.ai"}
