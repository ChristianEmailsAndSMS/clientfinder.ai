"""A lone surrogate in a request body must give a 422, not a 500 (the error body used to echo it)."""
from test_customer_api import env, member  # noqa


def test_lone_surrogate_in_json_body_is_a_422_not_a_crash(env):
    c = member(env)
    r = c.post("/searches/estimate", content=b'{"query":"hiring \\ud800 x","freshness":"w"}',
               headers={"content-type": "application/json", "X-Requested-With": "clientfinder"})
    assert r.status_code in (200, 422)
    r = c.post("/auth/login", content=b'{"email":"a\\ud800@x.com","password":"x"}',
               headers={"content-type": "application/json", "X-Requested-With": "clientfinder"})
    assert r.status_code == 422
    assert "\ud800" not in r.text
    assert r.json()["detail"][0]["loc"]
