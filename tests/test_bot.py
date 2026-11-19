"""Telegram chat flow with the Telegram API and BookMyShow faked."""
import asyncio

from app import db, monitor
from app.telegram_bot import Bot

OWNER = "111222333"


def make_bot(monkeypatch):
    b = Bot()
    sent = []

    async def api(method, **p):
        if method == "sendMessage":
            sent.append((p["chat_id"], p["text"], p.get("reply_markup")))
        return {}

    async def run_check(w):
        db.update_watch(w["id"], status="not_open", status_detail="Not open for booking yet", checks=w["checks"] + 1)
        return db.get_watch(w["id"])

    monkeypatch.setattr(b, "api", api)
    monkeypatch.setattr(monitor, "run_check", run_check)
    db.save_settings({"telegram_chat_id": OWNER, "telegram_bot_token": "x", "default_city": "chennai"})
    return b, sent


def msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}}


def btn(chat, data):
    return {"update_id": 2, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": int(chat)}}}}


def test_add_list_delete(monkeypatch):
    b, sent = make_bot(monkeypatch)
    run = lambda u: asyncio.run(b.handle(u))  # noqa: E731
    for w in db.list_watches():
        db.delete_watch(w["id"])

    run(msg("999", "/start"))
    assert "private" in sent[-1][1] and "999" in sent[-1][1]

    run(msg(OWNER, "/help"))
    assert "/add" in sent[-1][1] and "chennai" in sent[-1][1]

    run(msg(OWNER, "https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706"))
    assert "Which theatre" in sent[-1][1]

    run(msg(OWNER, "PVR Grand Mall Velachery"))
    ws = db.list_watches()
    assert len(ws) == 1 and ws[0]["theatre"] == "PVR Grand Mall Velachery" and ws[0]["event_code"] == "ET00439706"
    assert "Not open yet" in sent[-1][1] and sent[-1][2]["inline_keyboard"][0][2]["callback_data"] == f"del:{ws[0]['id']}"

    run(msg(OWNER, "/add ET00526306"))
    run(btn(OWNER, "th:any"))
    assert len(db.list_watches()) == 2 and db.list_watches()[0]["theatre"] == ""

    run(btn(OWNER, f"tog:{ws[0]['id']}"))
    assert db.get_watch(ws[0]["id"])["active"] is False and "paused" in sent[-1][1]

    run(msg(OWNER, "/list"))
    assert sum("#" in t for _, t, _ in sent[-2:]) == 2

    run(btn(OWNER, f"del:{ws[0]['id']}"))
    assert db.get_watch(ws[0]["id"]) is None and "Deleted" in sent[-1][1]

    run(msg(OWNER, "/city Mumbai"))
    assert db.get_settings()["default_city"] == "mumbai"
    asyncio.run(b.aclose())
