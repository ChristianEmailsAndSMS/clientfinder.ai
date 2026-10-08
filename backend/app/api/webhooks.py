"""Payment webhooks. Authenticated by signature, not by cookie/CSRF."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import whop
from ..config import settings
from ..db import get_db
from ..security import log_event

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/whop")
async def whop_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    if not settings.whop_webhook_secret:
        raise HTTPException(503, "Webhook not configured")
    body = await request.body()
    if len(body) > whop.MAX_BODY:
        raise HTTPException(413, "Too large")
    h = request.headers
    msg_id = h.get("webhook-id", "")
    if not whop.verify(settings.whop_webhook_secret, msg_id, h.get("webhook-timestamp", ""), h.get("webhook-signature", ""), body):
        log_event(db, "webhook_bad_signature", request)
        db.commit()
        raise HTTPException(401, "Bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Not JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Unexpected payload")
    ev = whop.process(db, msg_id, payload)
    db.commit()
    return {"ok": True, "status": ev.status}          # 200 even when held for review: a retry would not change the outcome
