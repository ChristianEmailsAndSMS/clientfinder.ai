"""A lone surrogate or Infinity in a JSON body must give a plain 422, never a 500, and the submitted password must not reach the log."""
import logging

import pytest

from tests.authkit import CSRF, make_env

H = {**CSRF, "content-type": "application/json"}


@pytest.fixture()
def env(monkeypatch):
    return make_env(monkeypatch)


@pytest.mark.parametrize("path,body", [
    ("/auth/signup", b'{"email":"leak@example.com","password":"Sup3rSecretPassw0rd!\\ud800"}'),
    ("/auth/login", b'{"email":"a@b.co","password":"x","totp_code":Infinity}'),
    ("/auth/login", b'{"email":"a@b.co","password":"x","totp_code":1e999}'),
])
def test_bad_json_values_give_422(env, caplog, path, body):
    with caplog.at_level(logging.DEBUG):
        r = env.client.post(path, content=body, headers=H)
    assert r.status_code == 422, r.text
    assert "Sup3rSecret" not in r.text
    assert "Sup3rSecret" not in caplog.text


def test_422_does_not_echo_input(env):
    r = env.client.post("/auth/login", json={"email": "a@b.co", "password": ["hunter2hunter2"]}, headers=CSRF)
    assert r.status_code == 422
    assert "hunter2" not in r.text and "input" not in r.json()["detail"][0]

