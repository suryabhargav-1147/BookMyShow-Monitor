"""Outbound notifications: Telegram bot, ntfy.sh push, and the dashboard (SSE)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from . import db

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def broadcast(kind: str, payload: dict[str, Any]) -> None:
    msg = json.dumps({"kind": kind, **payload})
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


async def telegram(text: str, settings: dict[str, str] | None = None, url: str = "",
                   button: str = "🎟 Book now") -> tuple[bool, str]:
    s = settings or db.get_settings()
    token = s.get("telegram_bot_token", "").strip()
    chats = [c.strip() for c in s.get("telegram_chat_id", "").replace(";", ",").split(",") if c.strip()]
    if not token or not chats:
        return False, "Telegram not configured"
    results = []
    async with httpx.AsyncClient(timeout=15) as c:
        for i, chat in enumerate(chats, 1):  # several user IDs: comma-separated
            payload: dict[str, Any] = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": True}
            if url:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": button, "url": url}]]}
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            ok = r.status_code == 200 and r.json().get("ok")
            # never echo chat IDs (they'd show in public CI logs); refer to them by position
            results.append((ok, f"#{i} sent" if ok else f"#{i} failed: {r.json().get('description', r.status_code)}"))
    return any(ok for ok, _ in results), ", ".join(m for _, m in results)


async def telegram_detect_chat(token: str) -> list[dict[str, str]]:
    """Return chats that recently messaged the bot (user sends /start first)."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"https://api.telegram.org/bot{token}/getUpdates")
    r.raise_for_status()
    chats: dict[str, dict[str, str]] = {}
    for u in r.json().get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        ch = msg.get("chat") or {}
        if ch.get("id") is not None:
            name = ch.get("title") or " ".join(filter(None, [ch.get("first_name"), ch.get("last_name")])) or ch.get("username", "")
            chats[str(ch["id"])] = {"id": str(ch["id"]), "name": name, "type": ch.get("type", "")}
    return list(chats.values())


async def ntfy(title: str, body: str, url: str = "", settings: dict[str, str] | None = None) -> tuple[bool, str]:
    s = settings or db.get_settings()
    topic = s.get("ntfy_topic", "")
    if not topic:
        return False, "ntfy not configured"
    server = (s.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    # JSON publishing keeps emoji/Unicode intact (HTTP headers must be ASCII)
    msg: dict[str, Any] = {"topic": topic, "title": title, "message": body, "priority": 5, "tags": ["tickets"]}
    if url:
        msg["click"] = url
        msg["actions"] = [{"action": "view", "label": "Book now", "url": url}]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(server, json=msg)
    return r.status_code < 300, "sent" if r.status_code < 300 else r.text[:300]


async def send_all(title: str, body_html: str, body_plain: str, url: str = "") -> dict[str, str]:
    """body_html is the complete Telegram message (it carries its own heading)."""
    s = db.get_settings()
    out: dict[str, str] = {}
    broadcast("alert", {"title": title, "body": body_plain, "url": url})
    out["dashboard"] = "sent"
    for name, coro in (("telegram", telegram(body_html, s, url)), ("ntfy", ntfy(title, body_plain, url, s))):
        try:
            ok, info = await coro
            out[name] = info
        except Exception as e:  # noqa: BLE001
            out[name] = f"error: {e}"
    return out
