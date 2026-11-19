"""End-to-end monitor flow with BookMyShow faked out (no network)."""
import asyncio
import os
import tempfile

os.environ["BMS_DATA_DIR"] = tempfile.mkdtemp()

from app import bms, db, monitor, notify  # noqa: E402
from tests import fixtures as fx  # noqa: E402


class FakeBms:
    def __init__(self, payloads):
        self.payloads = payloads  # list consumed per check (date "" call)
        self.calls = []

    async def showtimes(self, event_code, date_code, city, **kw):
        self.calls.append(date_code)
        p = self.payloads[0]
        if date_code:  # extra date: return only shows of that date
            return bms.Fetched({"data": {"showtimeWidgets": []}}, "http")
        return bms.Fetched(p, "http")


def test_alert_flow(monkeypatch):
    sent = []

    async def fake_send(title, body_html, body_plain, url=""):
        sent.append((title, body_plain))
        return {"dashboard": "sent"}

    monkeypatch.setattr(notify, "send_all", fake_send)
    fake = FakeBms([fx.NOT_OPEN])
    monkeypatch.setattr(monitor, "client", fake)

    wid = db.create_watch(title="ET00439706", city="chennai", slug="avengers-doomsday", event_code="ET00439706",
                          theatre="PVR Phoenix mall Velachery", interval_sec=120)
    run = lambda: asyncio.run(monitor.run_check(db.get_watch(wid)))  # noqa: E731

    w = run()
    assert w["status"] == "not_open" and sent == [] and w["title"] == "Avengers: Doomsday"

    fake.payloads = [fx.OPEN_ELSEWHERE]  # city open, theatre not yet
    w = run()
    assert w["status"] == "not_open" and "nothing matches" in w["status_detail"] and sent == []
    assert "AGS Cinemas: Maduravoyal" in w["last_result"]["all_venues"]

    fake.payloads = [fx.OPEN_PHOENIX]  # theatre opens -> one alert
    w = run()
    assert w["status"] == "open" and len(sent) == 1
    assert "tickets open" in sent[0][0] and "Phoenix MarketCity" in sent[0][1]
    assert w["alerts_sent"] == 1 and len(w["last_result"]["shows"]) == 2

    w = run()  # unchanged -> no duplicate alert
    assert len(sent) == 1 and w["checks"] == 4
    assert any(e["level"] == "alert" for e in db.list_events(watch_id=wid))


def test_is_due():
    w = {"active": True, "last_checked": None, "interval_sec": 120}
    assert monitor.is_due(w)
    w["last_checked"] = db.now_iso()
    assert not monitor.is_due(w)
    w["active"] = False
    assert not monitor.is_due(w)
