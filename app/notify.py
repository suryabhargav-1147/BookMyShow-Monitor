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


async def telegram(text: str, settings: dict[str, str] | None = None) -> tuple[bool, str]:
    s = settings or db.get_settings()
    token, chat = s.get("telegram_bot_token", ""), s.get("telegram_chat_id", "")
    if not token or not chat:
        return False, "Telegram not configured"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True})
    ok = r.status_code == 200 and r.json().get("ok")
    return bool(ok), "sent" if ok else r.text[:300]


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
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": "urgent", "Tags": "tickets"}
    if url:
        headers["Click"] = url
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{server}/{topic}", content=body.encode(), headers=headers)
    return r.status_code < 300, "sent" if r.status_code < 300 else r.text[:300]


async def send_all(title: str, body_html: str, body_plain: str, url: str = "") -> dict[str, str]:
    s = db.get_settings()
    out: dict[str, str] = {}
    broadcast("alert", {"title": title, "body": body_plain, "url": url})
    out["dashboard"] = "sent"
    for name, coro in (("telegram", telegram(f"<b>{title}</b>\n{body_html}" + (f'\n<a href="{url}">Book now →</a>' if url else ""), s)),
                       ("ntfy", ntfy(title, body_plain, url, s))):
        try:
            ok, info = await coro
            out[name] = info
        except Exception as e:  # noqa: BLE001
            out[name] = f"error: {e}"
    return out
