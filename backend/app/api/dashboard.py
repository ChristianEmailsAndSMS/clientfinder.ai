"""Serves the admin dashboard's static files. Public on purpose: the sign-in and account-setup screens live here.
Nothing in these files is secret and every data call they make needs a signed-in admin."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
_DIR = Path(__file__).resolve().parent.parent / "static" / "admin"
_FILES = {"dashboard.js": "text/javascript", "dashboard.css": "text/css"}     # allow-list: no path traversal possible


@router.get("/admin")
@router.get("/admin/")
def dashboard_page():
    return FileResponse(_DIR / "index.html", media_type="text/html")


@router.get("/admin/assets/{name}")
def dashboard_asset(name: str):
    if name not in _FILES:
        raise HTTPException(404)
    return FileResponse(_DIR / name, media_type=_FILES[name])
