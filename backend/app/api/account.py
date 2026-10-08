"""What a signed-in customer can see about their own credits."""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..credits import micro_to_usd
from ..db import get_db
from ..models import CreditEntry, User
from ..tagging import CATEGORY_RULES
from .. import geo, platforms
from ..security import csrf_guard, current_user, utcnow

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/credits")
def my_credits(limit: int = Query(50, ge=1, le=200), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(CreditEntry).where(CreditEntry.user_id == user.id).order_by(CreditEntry.id.desc()).limit(limit)).all()
    buy = settings.whop_checkout_url.strip()
    return {
        "buy_url": buy if buy.startswith("https://") else None,          # only https links from the owner's config
        "balance_usd": micro_to_usd(user.balance_micro),
        "entries": [{"at": r.created_at.isoformat(), "kind": r.kind, "amount_usd": micro_to_usd(r.delta_micro),
                     "balance_after_usd": micro_to_usd(r.balance_after_micro), "reason": r.reason} for r in rows],
    }


class PrefsBody(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=12)       # job categories: keys of tagging.CATEGORY_RULES
    regions: list[str] = Field(default_factory=list, max_length=12)     # geo.BANDS keys, or "unspecified"
    kinds: list[str] = Field(default_factory=list, max_length=3)        # board | social | web
    remote: str = Field("", max_length=5)                               # "" | "true" | "false"


@router.put("/prefs")
def save_prefs(body: PrefsBody, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    csrf_guard(request)
    user.prefs = {
        "roles": [r for r in dict.fromkeys(body.roles) if r in CATEGORY_RULES],
        "regions": [r for r in dict.fromkeys(body.regions) if r in geo.REGION_KEYS],
        "kinds": [k for k in dict.fromkeys(body.kinds) if k in platforms.KIND_LABEL],
        "remote": body.remote if body.remote in ("true", "false") else "",
    }
    user.onboarded_at = user.onboarded_at or utcnow()
    db.commit()
    return {"onboarded": True, "prefs": user.prefs}
