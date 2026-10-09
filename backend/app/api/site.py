"""The customer-facing pages: home, sign in, and the app. Static files with no inline script or style (strict CSP)."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from ..models import User
from ..security import session_user

router = APIRouter(include_in_schema=False)
_DIR = Path(__file__).resolve().parent.parent / "static" / "site"
_ASSETS = {"site.css": "text/css", "common.js": "text/javascript", "home.js": "text/javascript",
           "login.js": "text/javascript", "app.js": "text/javascript",
           "logo.png": "image/png", "logo-icon.png": "image/png", "favicon.png": "image/png", "apple-touch-icon.png": "image/png", "og.png": "image/png"}          # allow-list: no path traversal possible


@router.get("/")
def home():
    return FileResponse(_DIR / "home.html", media_type="text/html")


@router.get("/login")
def login_page():
    return FileResponse(_DIR / "login.html", media_type="text/html")


@router.get("/app")
@router.get("/app/")
def app_page(user: User | None = Depends(session_user)):
    if user is None:                                           # not signed in: go to the sign-in page, then come back
        return RedirectResponse("/login?next=/app", status_code=302)
    return FileResponse(_DIR / "app.html", media_type="text/html")


@router.get("/assets/{name}")
def asset(name: str):
    if name not in _ASSETS:
        raise HTTPException(404)
    return FileResponse(_DIR / name, media_type=_ASSETS[name], headers={"Cache-Control": "public, max-age=" + ("86400" if name.endswith(".png") else "300")})
