"""Only allowed Telegram user IDs (or local / bearer) can reach the dashboard and API."""
import os

os.environ["BMS_DISABLE_MONITOR"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, main, notify  # noqa: E402

ALLOWED, OTHER = "111222333", "999888777"
REMOTE = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}  # as nginx sends it


def setup_module(_):
    db.save_settings({"telegram_chat_id": ALLOWED, "telegram_bot_token": "x"})


def client():
    return TestClient(main.app, follow_redirects=False, base_url="https://testserver")


def test_remote_requests_are_blocked_without_session():
    c = client()
    assert c.get("/api/v1/watches", headers=REMOTE).status_code == 401
    r = c.get("/", headers=REMOTE)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/docs", headers=REMOTE).status_code == 303
    assert c.get("/openapi.json", headers=REMOTE).status_code == 303
    assert c.get("/login", headers=REMOTE).status_code == 200
    assert c.get("/healthz", headers=REMOTE).status_code == 200


def test_local_direct_is_trusted():
    assert client().get("/api/v1/watches").status_code == 200


def test_code_flow_only_for_allowed_ids(monkeypatch):
    sent = []

    async def fake_tg(text, settings=None):
        sent.append((settings["telegram_chat_id"], text))
        return True, "#1 sent"

    monkeypatch.setattr(notify, "telegram", fake_tg)
    auth.codes.last_sent.clear()
    c = client()

    r = c.post("/auth/code", json={"user_id": OTHER}, headers=REMOTE)
    assert r.status_code == 200 and sent == []  # same reply, nothing sent

    c.post("/auth/code", json={"user_id": ALLOWED}, headers=REMOTE)
    assert len(sent) == 1 and sent[0][0] == ALLOWED
    code = sent[0][1].split("<code>")[1].split("</code>")[0]

    c.post("/auth/code", json={"user_id": ALLOWED}, headers=REMOTE)
    assert len(sent) == 1  # rate-limited

    assert c.post("/auth/verify", json={"user_id": ALLOWED, "code": "000000" if code != "000000" else "111111"},
                  headers=REMOTE).status_code == 401
    assert c.post("/auth/verify", json={"user_id": OTHER, "code": code}, headers=REMOTE).status_code == 401
    r = c.post("/auth/verify", json={"user_id": ALLOWED, "code": code}, headers=REMOTE)
    assert r.status_code == 200 and auth.COOKIE in r.cookies
    assert "secure" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()

    assert c.get("/api/v1/watches", headers=REMOTE).status_code == 200
    assert c.get("/api/v1/me", headers=REMOTE).json()["user"] == "#1"
    # the code is single use
    assert c.post("/auth/verify", json={"user_id": ALLOWED, "code": code}, headers=REMOTE).status_code == 401

    # removing the ID from the allowed list revokes the session immediately
    db.save_settings({"telegram_chat_id": OTHER})
    assert c.get("/api/v1/watches", headers=REMOTE).status_code == 401
    db.save_settings({"telegram_chat_id": ALLOWED})


def test_magic_link_single_use_and_forgery():
    tok = auth.codes.link_token(ALLOWED)
    c = client()
    r = c.get(f"/auth/link?t={tok}", headers=REMOTE)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    c2 = client()
    assert c2.get(f"/auth/link?t={tok}", headers=REMOTE).status_code == 401  # reused
    forged = tok.replace(ALLOWED, OTHER, 1)
    assert c2.get(f"/auth/link?t={forged}", headers=REMOTE).status_code == 401
    c2.cookies.set(auth.COOKIE, "111222333.9999999999.bad")
    assert c2.get("/api/v1/watches", headers=REMOTE).status_code == 401


def test_bearer_token(monkeypatch):
    monkeypatch.setattr(auth, "API_TOKEN", "s3cret")
    c = client()
    assert c.get("/api/v1/watches", headers={**REMOTE, "Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/api/v1/watches", headers={**REMOTE, "Authorization": "Bearer nope"}).status_code == 401
