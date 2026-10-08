"""Serves the admin dashboard's static files. Nothing in them is secret and every data call they make needs a signed-in
admin; the page itself just sends visitors without a session to the sign-in page."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from ..models import User
from ..security import session_user

router = APIRouter(include_in_schema=False)
_DIR = Path(__file__).resolve().parent.parent / "static" / "admin"
_FILES = {"dashboard.js": "text/javascript", "dashboard.css": "text/css"}     # allow-list: no path traversal possible


@router.get("/admin")
@router.get("/admin/")
def dashboard_page(user: User | None = Depends(session_user)):
    if user is None:                                           # sign-in lives on /login; it sends you back here
        return RedirectResponse("/login?next=/admin", status_code=302)
    return FileResponse(_DIR / "index.html", media_type="text/html")


@router.get("/admin/assets/{name}")
def dashboard_asset(name: str):
    if name not in _FILES:
        raise HTTPException(404)
    return FileResponse(_DIR / name, media_type=_FILES[name])
