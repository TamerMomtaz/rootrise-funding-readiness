import pytest


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("API_KEY", "hunter-key")
    monkeypatch.setenv("TEAM_PASSCODE", "team-pass")
    monkeypatch.setenv("AUTH_SECRET", "s3cret")


def token(client):
    return client.post("/api/auth/login", json={"passcode": "team-pass"}).json()["token"]


def test_off_is_default_and_open(client, secured):
    assert client.post("/api/opportunities", json={"name": "a", "type": "grant"}).status_code == 200
    assert client.get("/api/auth/status").json()["mode"] == "off"


def test_writes_mode(client, secured, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "writes")
    assert client.get("/api/opportunities").status_code == 200
    assert client.post("/api/opportunities", json={"name": "a", "type": "grant"}).status_code == 401
    assert client.post("/api/opportunities", json={"name": "a", "type": "grant"},
                       headers={"X-API-Key": "hunter-key"}).status_code == 200
    h = {"Authorization": f"Bearer {token(client)}"}
    assert client.put("/api/opportunities/1", json={"fit_score": 1}, headers=h).status_code == 200
    assert client.delete("/api/opportunities/1", headers={"X-API-Key": "wrong"}).status_code == 401


def test_full_mode(client, secured, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "full")
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200                  # shell loads to show the passcode screen
    for path in ("/api/opportunities", "/api/readiness-items", "/api/dashboard-stats",
                 "/api/activity-log", "/api/opportunities/counts-by-type", "/openapi.json", "/docs"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/opportunities", headers={"X-API-Key": "hunter-key"}).status_code == 200
    assert client.get("/api/opportunities", headers={"Authorization": f"Bearer {token(client)}"}).status_code == 200


def test_public_default_key_never_accepted(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "rootrise-dev-key-2024")
    monkeypatch.setenv("AUTH_MODE", "writes")
    r = client.post("/api/opportunities", json={"name": "a", "type": "grant"},
                    headers={"X-API-Key": "rootrise-dev-key-2024"})
    assert r.status_code == 401


def test_bad_and_tampered_tokens(client, secured, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "full")
    assert client.post("/api/auth/login", json={"passcode": "nope"}).status_code == 401
    t = token(client)
    assert client.get("/api/opportunities", headers={"Authorization": f"Bearer {t[:-2]}xx"}).status_code == 401
    monkeypatch.setenv("AUTH_SECRET", "rotated")
    assert client.get("/api/opportunities", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_expired_token(client, secured, monkeypatch):
    import auth
    monkeypatch.setenv("AUTH_MODE", "full")
    old = auth.issue_token(now=1_000_000)["token"]
    assert client.get("/api/opportunities", headers={"Authorization": f"Bearer {old}"}).status_code == 401


def test_login_throttle(client, secured, monkeypatch):
    import auth
    monkeypatch.setattr(auth, "_failures", __import__("collections").defaultdict(__import__("collections").deque))
    for _ in range(10):
        assert client.post("/api/auth/login", json={"passcode": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"passcode": "team-pass"}).status_code == 429


def test_reset_database_is_gone(client):
    assert client.post("/api/reset-database").status_code in (404, 405)


def test_cors_is_not_wildcard(client):
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") is None
    r = client.get("/api/health", headers={"Origin": "https://rootrise-funding-readiness.onrender.com"})
    assert r.headers.get("access-control-allow-origin") == "https://rootrise-funding-readiness.onrender.com"
    assert r.headers.get("access-control-allow-credentials") is None
