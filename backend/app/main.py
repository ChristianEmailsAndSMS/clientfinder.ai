from contextlib import asynccontextmanager

from fastapi.responses import JSONResponse
from fastapi import FastAPI, HTTPException, Request
from .api.account import router as account_router
from .api.admin import router as admin_router
from .api.webhooks import router as webhooks_router
from .api.assist import router as assist_router
from .api.auth import router as auth_router
from .api.dashboard import router as dashboard_router
from . import schema_check
from .config import settings
from .db import engine
from .logging_setup import setup_logging
from .api.jobs import router as jobs_router
from .api.public import router as public_router
from .api.site import router as site_router
from .api.searches import router as searches_router

setup_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A skipped `alembic upgrade head` after a pull must stop the deploy here, not 500 on a customer's first request.
    schema_check.require_current(engine, settings.schema_check_strict)
    yield


# Interactive docs and the schema are an attack map. Off unless ENABLE_DOCS=1 (local dev).
app = FastAPI(
    lifespan=lifespan,
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


MAX_BODY_DEFAULT = 1_000_000
MAX_BODY_ASSIST = 6_000_000                 # a screenshot as base64


class BodyLimitMiddleware:
    """Caps the request body. Checks Content-Length up front AND counts the bytes actually received, because a chunked
    request (Transfer-Encoding: chunked) carries no Content-Length and would otherwise be read in full."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        limit = MAX_BODY_ASSIST if scope.get("path") == "/assist" else MAX_BODY_DEFAULT
        cl = dict(scope.get("headers") or []).get(b"content-length", b"")
        if cl.isdigit() and int(cl) > limit:
            return await JSONResponse(status_code=413, content={"detail": "Request too large"})(scope, receive, send)
        seen = 0

        async def counted():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise HTTPException(413, "Request too large")
            return msg
        await self.inner(scope, counted, send)


app.add_middleware(BodyLimitMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _BASE_HEADERS.items():
        response.headers.setdefault(k, v)
    response.headers["Content-Security-Policy"] = _PAGE_CSP
    if request.url.path.startswith(("/admin", "/auth", "/account", "/app", "/login", "/searches", "/jobs", "/assist")):
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
app.include_router(assist_router)

@app.get("/health")
def health():
    """200 only when the database schema matches the code. 503 means a migration was not applied (see the server log).
    Revisions are not shown here (public); the admin overview has them."""
    r = schema_check.check(engine)
    ok = r["state"] != "behind"
    body = {"ok": ok, "service": "clientfinder.ai", "schema": "ok" if ok else "behind"}
    return JSONResponse(status_code=200 if ok else 503, content=body)
