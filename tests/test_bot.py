"""Telegram chat flow with the Telegram API and BookMyShow faked."""
import asyncio
from datetime import date

from app import db, monitor, parse
from app import telegram_bot as tb
from app.bms import Variant
from app.telegram_bot import Bot, parse_dates_text

VARIANTS = [Variant("ET00526312", "Avengers: Doomsday", "Tamil", "MS-Infinity Vsn 3d", "avengers-doomsday-msinfinity-vsn-3dtamil"),
            Variant("ET00526306", "Avengers: Doomsday", "English", "MS-Infinity Vsn 3d", "avengers-doomsday-msinfinity-vsn-3d"),
            Variant("ET00439706", "Avengers: Doomsday", "English", "2D, 3D", "avengers-doomsday")]

OWNER = "111222333"


def make_bot(monkeypatch):
    b = Bot()
    sent = []
    checks = []

    async def api(method, **p):
        if method == "sendMessage":
            sent.append((p["chat_id"], p["text"], p.get("reply_markup")))
        return {}

    async def run_check(w):
        checks.append(w["id"])
        db.update_watch(w["id"], status="not_open", status_detail="Not open for booking yet", checks=w["checks"] + 1,
                        last_result={"all_venues": ["PVR: Grand Mall, Velachery", "Radials Cinemas: Madurai"]})
        return db.get_watch(w["id"])

    async def search_movies(city, q, **kw):
        return [parse.MovieRef("Avengers Doomsday", city, "avengers-doomsday", "ET00439706")]

    monkeypatch.setattr(b, "api", api)
    monkeypatch.setattr(monitor, "run_check", run_check)
    monkeypatch.setattr(monitor.client, "search_movies", search_movies)

    async def fake_variants(bms, refs, city, region="", limit=8):
        return list(VARIANTS)

    monkeypatch.setattr(tb, "variants", fake_variants)
    db.save_settings({"telegram_chat_id": OWNER, "telegram_bot_token": "x", "default_city": "chennai"})
    for w in db.list_watches():
        db.delete_watch(w["id"])
    return b, sent, checks


def msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}}


def btn(chat, data):
    return {"update_id": 2, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": int(chat)}}}}


def buttons(entry):
    return [b["callback_data"] for row in (entry[2] or {}).get("inline_keyboard", []) for b in row]


def test_add_flow_with_city_and_timing(monkeypatch):
    b, sent, checks = make_bot(monkeypatch)
    run = lambda u: asyncio.run(b.handle(u))  # noqa: E731

    run(msg("999", "/start"))
    assert "private" in sent[-1][1] and "999" in sent[-1][1]

    run(msg(OWNER, "https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706"))
    assert "Which theatre" in sent[-1][1] and "addcity" in buttons(sent[-1])
    run(btn(OWNER, "addcity"))
    run(msg(OWNER, "Madurai"))
    assert "Madurai" in sent[-1][1] and "Which theatre" in sent[-1][1]
    run(msg(OWNER, "radials"))
    assert "How often" in sent[-1][1] and "ni:300" in buttons(sent[-1])
    run(btn(OWNER, "ni:300"))
    ws = db.list_watches()
    assert len(ws) == 1
    w = ws[0]
    assert (w["city"], w["theatre"], w["interval_sec"], w["event_code"]) == ("madurai", "radials", 300, "ET00439706")
    assert checks == [w["id"]] and "every 5 min" in sent[-1][1]
    assert {"chk:%d" % w["id"], "edit:%d" % w["id"], "tog:%d" % w["id"], "del:%d" % w["id"]} <= set(buttons(sent[-1]))


def test_name_search_asks_language_then_format(monkeypatch):
    b, sent, checks = make_bot(monkeypatch)
    run = lambda u: asyncio.run(b.handle(u))  # noqa: E731
    run(msg(OWNER, "doomsday"))
    assert "Which language" in sent[-1][1]
    labels = [bt["text"] for row in sent[-1][2]["inline_keyboard"] for bt in row]
    assert labels[0] == "English ⭐ default" and "Tamil" in labels
    run(btn(OWNER, "lang:0"))
    assert "Which format" in sent[-1][1]
    fl = [bt["text"] for row in sent[-1][2]["inline_keyboard"] for bt in row]
    assert fl[0] == "2D, 3D ⭐ default"  # standard before premium
    run(btn(OWNER, "fmt:0"))
    assert "Which theatre" in sent[-1][1] and "English · 2D, 3D" in sent[-1][1]
    run(msg(OWNER, "PVR Grand Mall Velachery"))
    run(btn(OWNER, "ni:180"))
    w = db.list_watches()[0]
    assert w["event_code"] == "ET00439706" and w["title"] == "Avengers: Doomsday (English · 2D, 3D)"

    run(msg(OWNER, "doomsday"))
    run(btn(OWNER, "lang:1"))  # Tamil: only one format → straight to theatre
    assert "Which theatre" in sent[-1][1] and "Tamil" in sent[-1][1]


def test_one_line_add(monkeypatch):
    b, sent, checks = make_bot(monkeypatch)
    asyncio.run(b.handle(msg(OWNER, "/add doomsday | madurai | radials | 5")))
    w = db.list_watches()[0]
    assert (w["city"], w["theatre"], w["interval_sec"]) == ("madurai", "radials", 300)
    assert w["event_code"] == "ET00439706"  # English, standard by default
    asyncio.run(b.handle(msg(OWNER, "/add doomsday tamil | madurai | radials | 5")))
    assert db.list_watches()[0]["event_code"] == "ET00526312"
    # unknown city → warning, region override accepted
    asyncio.run(b.handle(msg(OWNER, "/add ET00439706 | salem SALM | any | 10")))
    w2 = db.list_watches()[0]
    assert (w2["city"], w2["region"], w2["interval_sec"]) == ("salem", "SALM", 600)
    asyncio.run(b.handle(msg(OWNER, "/add ET00439706 | erode | | 3")))
    assert any("region code" in t for _, t, _ in sent)


def test_edit_everything(monkeypatch):
    b, sent, checks = make_bot(monkeypatch)
    run = lambda u: asyncio.run(b.handle(u))  # noqa: E731
    wid = db.create_watch(title="Avengers Doomsday", city="chennai", slug="avengers-doomsday",
                          event_code="ET00439706", theatre="PVR", interval_sec=120)
    db.update_watch(wid, last_result={"all_venues": ["PVR: Grand Mall, Velachery", "INOX: Chennai Citi Centre"]})

    run(btn(OWNER, f"edit:{wid}"))
    assert {f"e:{wid}:th", f"e:{wid}:ci", f"e:{wid}:iv", f"e:{wid}:mv", f"e:{wid}:dt", f"e:{wid}:pt"} <= set(buttons(sent[-1]))

    run(btn(OWNER, f"e:{wid}:iv"))
    run(btn(OWNER, f"iv:{wid}:300"))
    assert db.get_watch(wid)["interval_sec"] == 300 and checks == []  # timing change: no re-check
    run(btn(OWNER, f"e:{wid}:iv"))
    run(msg(OWNER, "4"))
    assert db.get_watch(wid)["interval_sec"] == 240

    run(btn(OWNER, f"e:{wid}:th"))
    assert f"tv:{wid}:0" in buttons(sent[-1])  # venues seen offered as buttons
    run(btn(OWNER, f"tv:{wid}:0"))
    assert db.get_watch(wid)["theatre"] == "PVR: Grand Mall, Velachery" and checks == [wid]

    run(btn(OWNER, f"e:{wid}:ci"))
    run(msg(OWNER, "madurai"))
    assert db.get_watch(wid)["city"] == "madurai"

    run(btn(OWNER, f"e:{wid}:dt"))
    run(msg(OWNER, "18 dec, 19/12"))
    assert db.get_watch(wid)["dates"].endswith("1218,") is False and len(db.get_watch(wid)["dates"].split(",")) == 2

    run(btn(OWNER, f"e:{wid}:pt"))
    run(btn(OWNER, f"pt:{wid}:evening"))
    run(btn(OWNER, f"pt:{wid}:night"))
    run(btn(OWNER, f"pt:{wid}:evening"))
    assert db.get_watch(wid)["periods"] == "night"

    run(btn(OWNER, f"e:{wid}:mv"))
    run(msg(OWNER, "https://in.bookmyshow.com/movies/madurai/avengers-doomsday-msinfinity-vsn-3d/ET00526306"))
    assert db.get_watch(wid)["event_code"] == "ET00526306"
    run(btn(OWNER, f"e:{wid}:mv"))
    run(msg(OWNER, "doomsday"))
    assert db.get_watch(wid)["event_code"] == "ET00439706"  # by name → English standard

    run(btn(OWNER, f"tog:{wid}"))
    assert db.get_watch(wid)["active"] is False and "paused" in sent[-1][1]
    run(btn(OWNER, f"del:{wid}"))
    assert db.get_watch(wid) is not None and f"delok:{wid}" in buttons(sent[-1])  # asks first
    run(btn(OWNER, f"delok:{wid}"))
    assert db.get_watch(wid) is None
    asyncio.run(b.aclose())


def test_parse_dates_text():
    today = date(2026, 11, 19)
    assert parse_dates_text("18 dec, 19/12", today) == "20261218,20261219"
    assert parse_dates_text("dec 20 and 20261221", today) == "20261220,20261221"
    assert parse_dates_text("5 jan", today) == "20270105"  # already past this year → next year
    assert parse_dates_text("any", today) == ""
    assert parse_dates_text("tomorrow-ish", today) is None
    assert parse_dates_text("31/02", today) is None


def test_card_shows_date_summary(monkeypatch):
    b, sent, checks = make_bot(monkeypatch)
    wid = db.create_watch(title="D", city="chennai", slug="d", event_code="ET00439706", theatre="PVR Nexus", interval_sec=180)
    db.update_watch(wid, status="open", status_detail="2 shows", last_result={
        "dates": [{"date_code": "20261217", "status": "BOOKABLE"}, {"date_code": "20261218", "status": "BOOKABLE"},
                  {"date_code": "20261219", "status": "BOOKABLE"}, {"date_code": "20261220", "status": "NOT_OPEN"}],
        "shows": [{"date_code": "20261217"}, {"date_code": "20261218"}]})
    text = b.describe(db.get_watch(wid))
    assert "Booking open: 17–19 Dec (at your theatre: 17–18 Dec)" in text
    assert "Watching for: 20 Dec onwards" in text
    assert db.get_watch(wid)["summary"][0].startswith("📅 Booking open")
