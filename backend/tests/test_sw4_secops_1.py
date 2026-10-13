import time

import pyotp

from app import security, whop
from app.models import User


def test_totp_non_ascii_digits_rejected_not_crash():
    secret = pyotp.random_base32()
    u = User(email="a@b.co", totp_secret_enc=security.encrypt_secret(secret))
    for code in ("٠٠٠٠٠٠", "１２３４５６"):
        assert security.check_totp(u, code) is False


def test_whop_non_ascii_signature_is_just_bad():
    body = b"{}"
    ts = str(int(time.time()))
    assert whop.verify("ws_secret", "m1", ts, "v1,café", body) is False
