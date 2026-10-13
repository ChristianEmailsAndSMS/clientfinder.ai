from fastapi.testclient import TestClient

from app.main import app


def test_lone_surrogate_in_json_body_is_422_not_500():
    c = TestClient(app, raise_server_exceptions=False)
    body = b'{"email":"a@b.com","password":"x\\ud800"}'
    r = c.post("/auth/login", content=body, headers={"content-type": "application/json", "X-Requested-With": "clientfinder"})
    assert r.status_code in (401, 422)
    r = c.post("/auth/login", content=b'{"email":"a@b.com"}', headers={"content-type": "application/json", "X-Requested-With": "clientfinder"})
    assert r.status_code == 422 and "detail" in r.json()
