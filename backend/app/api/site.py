"""The customer-facing pages: home, sign in, and the app. Static files with no inline script or style (strict CSP)."""
import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ..config import settings
from ..models import User
from ..security import session_user

router = APIRouter(include_in_schema=False)
_DIR = Path(__file__).resolve().parent.parent / "static" / "site"
_ASSETS = {"site.css": "text/css", "common.js": "text/javascript", "home.js": "text/javascript",
           "login.js": "text/javascript", "app.js": "text/javascript",
           "logo.png": "image/png", "logo-icon.png": "image/png", "favicon.png": "image/png", "apple-touch-icon.png": "image/png", "og.png": "image/png"}          # allow-list: no path traversal possible


_hashes: dict[str, tuple[tuple[int, int], str]] = {}


def _version(name: str) -> str:
    """Short content hash of a static file (recomputed only when the file changes)."""
    st = (_DIR / name).stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _hashes.get(name)
    if hit is None or hit[0] != key:
        hit = (key, hashlib.sha256((_DIR / name).read_bytes()).hexdigest()[:10])
        _hashes[name] = hit
    return hit[1]


def _page(name: str) -> HTMLResponse:
    """A page whose scripts, styles and images are addressed by content hash (/assets/app.js?v=ab12cd34ef). After a deploy the page names
    new URLs, so a browser or Cloudflare can never pair new HTML with an old cached script (that mismatch broke sign-in messages)."""
    html = (_DIR / name).read_text(encoding="utf-8")
    for asset_name in _ASSETS:
        html = html.replace(f'"/assets/{asset_name}"', f'"/assets/{asset_name}?v={_version(asset_name)}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})        # always revalidate the page itself


def not_found_page() -> HTMLResponse:
    """The branded 404 a browser gets for an unknown address (API clients keep the JSON answer)."""
    r = _page("notfound.html")
    r.status_code = 404
    return r


@router.get("/")
def home():
    return _page("home.html")


@router.get("/robots.txt")
def robots():
    return FileResponse(_DIR / "robots.txt", media_type="text/plain")


@router.get("/sitemap.xml")
def sitemap():
    base = settings.public_url.rstrip("/")
    return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{base}/</loc></url></urlset>\n',
                    media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/favicon.ico")
def favicon_ico():
    return FileResponse(_DIR / "favicon.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/login")
def login_page():
    return _page("login.html")


@router.get("/app")
@router.get("/app/")
def app_page(user: User | None = Depends(session_user)):
    if user is None:                                           # not signed in: go to the sign-in page, then come back
        return RedirectResponse("/login?next=/app", status_code=302)
    return _page("app.html")


@router.get("/assets/{name}")
def asset(name: str, request: Request):
    if name not in _ASSETS:
        raise HTTPException(404)
    if request.query_params.get("v") == _version(name):            # the URL names this exact content: safe to keep for a year
        cache = "public, max-age=31536000, immutable"
    else:                                                           # an old or unversioned URL: short, so a stale one fixes itself fast
        cache = "public, max-age=60, must-revalidate"
    return FileResponse(_DIR / name, media_type=_ASSETS[name], headers={"Cache-Control": cache})
