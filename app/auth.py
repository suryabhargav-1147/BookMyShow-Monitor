"""Web access limited to the allowed Telegram user IDs.

Sign-in: enter your Telegram user ID → the bot sends a 6-digit code (only to allowed IDs) →
enter it → signed session cookie (30 days). Or send /web to the bot for a one-tap login link.
Every request re-checks that the cookie's user ID is still allowed, so removing an ID from
the list locks that person out immediately.

Always allowed: direct local requests (127.0.0.1 without proxy headers, e.g. the MCP server,
cron, curl on the box) and requests with `Authorization: Bearer $BMS_API_TOKEN`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Request

from . import db

COOKIE = "bms_session"
SESSION_DAYS = 30
CODE_TTL = 300
LINK_TTL = 600
API_TOKEN = os.getenv("BMS_API_TOKEN", "")


def _secret() -> bytes:
    env = os.getenv("BMS_SESSION_SECRET")
    if env:
        return env.encode()
    path = db.DATA_DIR / "session.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def allowed_ids() -> set[str]:
    raw = db.get_settings().get("telegram_chat_id", "")
    return {c.strip() for c in raw.replace(";", ",").split(",") if c.strip()}


# ---------------------------------------------------------------- request classification
def local_direct(request: Request) -> bool:
    host = request.client.host if request.client else ""
    proxied = any(h in request.headers for h in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip"))
    return host in ("127.0.0.1", "::1", "testclient") and not proxied


def bearer_ok(request: Request) -> bool:
    if not API_TOKEN:
        return False
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bool(supplied) and hmac.compare_digest(supplied, API_TOKEN)


def session_user(request: Request) -> str | None:
    raw = request.cookies.get(COOKIE, "")
    try:
        uid, exp, sig = raw.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(f"{uid}.{exp}")) or int(exp) < time.time():
        return None
    return uid if uid in allowed_ids() else None


def make_session(uid: str) -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{uid}.{exp}.{_sign(f'{uid}.{exp}')}"


def is_authorized(request: Request) -> bool:
    return local_direct(request) or bearer_ok(request) or session_user(request) is not None


# ---------------------------------------------------------------- one-time codes and links
@dataclass
class _Pending:
    code: str
    expires: float
    attempts: int = 0


@dataclass
class Codes:
    pending: dict[str, _Pending] = field(default_factory=dict)
    last_sent: dict[str, float] = field(default_factory=dict)
    used_links: dict[str, float] = field(default_factory=dict)

    def issue(self, uid: str) -> str | None:
        """New code for an allowed uid; None if not allowed or asked again within 30 s."""
        now = time.time()
        if uid not in allowed_ids() or now - self.last_sent.get(uid, 0) < 30:
            return None
        self.last_sent[uid] = now
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.pending[uid] = _Pending(code, now + CODE_TTL)
        return code

    def verify(self, uid: str, code: str) -> bool:
        p = self.pending.get(uid)
        if not p or p.expires < time.time() or uid not in allowed_ids():
            return False
        p.attempts += 1
        if p.attempts > 5:
            self.pending.pop(uid, None)
            return False
        if hmac.compare_digest(p.code, code.strip()):
            self.pending.pop(uid, None)
            return True
        return False

    def link_token(self, uid: str) -> str:
        exp = str(int(time.time()) + LINK_TTL)
        nonce = secrets.token_hex(8)
        return f"{uid}.{exp}.{nonce}.{_sign(f'link:{uid}.{exp}.{nonce}')}"

    def redeem_link(self, token: str) -> str | None:
        try:
            uid, exp, nonce, sig = token.split(".")
        except ValueError:
            return None
        now = time.time()
        self.used_links = {n: t for n, t in self.used_links.items() if t > now}
        if (not hmac.compare_digest(sig, _sign(f"link:{uid}.{exp}.{nonce}")) or int(exp) < now
                or nonce in self.used_links or uid not in allowed_ids()):
            return None
        self.used_links[nonce] = int(exp)
        return uid


codes = Codes()


def public_url() -> str:
    return os.getenv("BMS_PUBLIC_URL", "").rstrip("/")
