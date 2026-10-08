"""What a signed-in customer can see about their own credits."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..credits import micro_to_usd
from ..db import get_db
from ..models import CreditEntry, User
from ..security import current_user

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
