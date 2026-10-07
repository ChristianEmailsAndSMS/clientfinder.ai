from fastapi import FastAPI
from .api.jobs import router as jobs_router

app = FastAPI(title="Clientfinder.ai API", version="0.1.0")

app.include_router(jobs_router)


@app.get("/health")
def health():
    return {"ok": True, "service": "clientfinder.ai"}
