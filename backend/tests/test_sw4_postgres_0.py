"""Over-long ledger refs (long e-mail, long webhook id) must be shortened, not overflow credit_ledger.ref (PostgreSQL enforces it)."""
from app import credits
from app.api.auth import mailbox_key
from app.models import CreditEntry


def test_make_ref_keeps_short_and_fits_long():
    assert credits.make_ref("welcome", "a@b.com") == "welcome:a@b.com"
    long = "a" * 60 + "@" + "b" * 52 + ".example.com"
    r = credits.make_ref("welcome", mailbox_key(long))
    assert len(r) <= 128 and r == credits.make_ref("welcome", mailbox_key(long.upper()))
    assert r != credits.make_ref("welcome", mailbox_key("x" + long))
    assert len(credits.make_ref("whop", "e" * 128)) <= 128


def test_ledger_ref_column_width_matches():
    assert CreditEntry.__table__.c.ref.type.length == credits.REF_MAX
