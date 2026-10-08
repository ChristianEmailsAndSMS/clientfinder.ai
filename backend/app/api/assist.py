"""Pitch helper endpoints. Signed-in customers only; writes need the CSRF header."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import assist, credits
from ..config import settings
from ..db import get_db
from ..models import User
from ..security import csrf_guard, current_user, rate_limit_guard

router = APIRouter(prefix="/assist", tags=["assist"], dependencies=[Depends(rate_limit_guard)])


class AssistBody(BaseModel):
    mode: str = Field(max_length=20)
    job_id: int | None = None
    job_text: str = Field("", max_length=4000)
    thread: str = Field("", max_length=5000)
    image_b64: str | None = Field(None, max_length=4_400_000)       # about 3 MB of image
    note: str = Field("", max_length=300)


@router.get("/config")
def config(user: User = Depends(current_user)) -> dict:
    ready, why = assist.ready()
    return {"ready": ready, "reason": why, "price_usd": assist.typical_micro() / credits.MICRO, "unlimited": user.unlimited_credits,
            "balance_usd": credits.micro_to_usd(user.balance_micro), "has_profile": bool((user.profile or {}).get("about"))}


@router.post("")
def write(body: AssistBody, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    csrf_guard(request)
    try:
        out = assist.generate(db, user, body.mode, job_id=body.job_id, job_text=body.job_text, thread=body.thread,
                              image_b64=body.image_b64, note=body.note)
    except assist.AssistError as e:
        db.rollback()
        return JSONResponse(status_code=e.status, content={"detail": e.message})
    db.commit()
    db.refresh(user)
    out["balance_usd"] = credits.micro_to_usd(user.balance_micro)
    return out
