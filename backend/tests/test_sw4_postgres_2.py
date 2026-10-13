"""PostgreSQL cannot store a NUL byte in text; admin filters and the credit reason must answer 422, not 500."""
import pytest

from test_accounts import admin, customer, env, signed_in  # noqa: F401  (fixtures)
from authkit import CSRF


@pytest.mark.parametrize("path", ["/admin/users?q=%00x", "/admin/scrape-runs?status=%00x",
                                  "/admin/security/events?event=%00x", "/admin/payments?status=%00x"])
def test_admin_filters_refuse_nul_bytes(env, admin, path):
    c = signed_in(env, admin)
    assert c.get(path).status_code == 422


def test_admin_credit_reason_refuses_nul_byte(env, admin):
    c, uid = signed_in(env, admin), customer(env)
    r = c.post(f"/admin/users/{uid}/credits", json={"amount_usd": 1, "kind": "grant", "reason": "a\u0000b"}, headers=CSRF)
    assert r.status_code == 422
    ok = c.post(f"/admin/users/{uid}/credits", json={"amount_usd": 1, "kind": "grant", "reason": "ab c"}, headers=CSRF)
    assert ok.status_code == 200
