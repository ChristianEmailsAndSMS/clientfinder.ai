from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from .api.jobs import router as jobs_router

app = FastAPI(title="Clientfinder.ai API", version="0.1.0")

app.include_router(jobs_router)

_LANDING_HTML = (Path(__file__).resolve().parent / "templates" / "landing.html").read_text()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
    return _LANDING_HTML


@app.get("/health")
def health():
    return {"ok": True, "service": "clientfinder.ai"}
