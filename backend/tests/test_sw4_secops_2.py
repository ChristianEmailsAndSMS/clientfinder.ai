"""/health must be 503 when the database cannot be reached, so an uptime monitor sees the outage."""
import authkit


class _DeadEngine:
    class dialect:
        name = "postgresql"

    def connect(self):
        raise OSError("connection refused")


def test_health_is_503_when_database_unreachable(monkeypatch):
    env = authkit.make_env(monkeypatch)
    import app.main as m
    monkeypatch.setattr(m, "engine", _DeadEngine())
    r = env.client.get("/health")
    assert r.status_code == 503
    assert r.json() == {"ok": False, "service": "clientfinder.ai", "schema": "unreachable"}
