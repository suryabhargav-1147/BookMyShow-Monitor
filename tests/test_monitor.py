"""End-to-end monitor flow with BookMyShow faked out (no network)."""
import asyncio

from app import bms, db, monitor, notify
from tests import fixtures as fx


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
    assert "Tickets open" in sent[0][0] and "Phoenix MarketCity" in sent[0][1]
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


def test_concurrent_checks_alert_once(monkeypatch):
    """Bot's immediate check + scheduler check racing on a new watch must alert only once."""
    sent = []

    async def fake_send(title, body_html, body_plain, url=""):
        sent.append(body_plain)
        return {"dashboard": "sent"}

    class SlowFake(FakeBms):
        async def showtimes(self, *a, **k):
            await asyncio.sleep(0.05)  # both checks in flight at once
            return await super().showtimes(*a, **k)

    monkeypatch.setattr(notify, "send_all", fake_send)
    monkeypatch.setattr(monitor, "client", SlowFake([fx.OPEN_PHOENIX]))
    wid = db.create_watch(title="X", city="chennai", slug="avengers-doomsday", event_code="ET00439706",
                          theatre="PVR Phoenix Velachery", interval_sec=120)
    w = db.get_watch(wid)

    async def both():
        await asyncio.gather(monitor.run_check(w), monitor.run_check(w))

    asyncio.run(both())
    assert len(sent) == 1
    assert "TICKETS OPEN" in sent[0] and "📍 PVR · Phoenix MarketCity, Velachery" in sent[0]
    assert "Fri 18 Dec  10:45 AM · 7:30 PM" in sent[0]  # one line per date, times in order
    assert db.get_watch(wid)["checks"] == 2


def test_randomized_schedule():
    delays = [monitor.next_delay(180, 30) for _ in range(500)]
    assert all(150 <= d <= 210 for d in delays)
    assert max(delays) - min(delays) > 40  # actually spread out, not constant
    assert len({round(d) for d in delays}) > 30
    assert all(d >= monitor.MIN_INTERVAL for d in (monitor.next_delay(60, 300) for _ in range(200)))
    assert monitor.next_delay(180, 0) == 180


def test_next_check_drives_due():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    w = {"active": True, "last_checked": (now - timedelta(hours=1)).isoformat(), "interval_sec": 180,
         "next_check": (now + timedelta(seconds=40)).isoformat()}
    assert not monitor.is_due(w)  # scheduled time not reached, even though interval passed
    w["next_check"] = (now - timedelta(seconds=1)).isoformat()
    assert monitor.is_due(w)


def test_scans_all_open_dates_and_no_false_new_show(monkeypatch):
    """6 open dates: all get fetched (default limit 10); a date skipped later keeps its known shows."""
    calls = []

    def day(d):
        return fx.payload([(f"202612{x}", "date-selected") for x in range(17, 23)],
                          [fx.venue("RADM", "Radiance Cinema: Koodal Nagar, Madurai",
                                    [fx.show("07:00 PM", "1900", f"s{d}", f"202612{d}", [("GOLD", "200", "3")])])])

    class PerDate(FakeBms):
        async def showtimes(self, event_code, date_code, city, **kw):
            calls.append(date_code)
            return bms.Fetched(day(int(date_code[-2:]) if date_code else 17), "http")

    sent = []

    async def fake_send(title, body_html, body_plain, url=""):
        sent.append(body_plain)
        return {}

    monkeypatch.setattr(notify, "send_all", fake_send)
    monkeypatch.setattr(monitor, "client", PerDate([]))
    wid = db.create_watch(title="T", city="madurai", slug="d", event_code="ET00526312", theatre="Radiance", interval_sec=300)
    w = asyncio.run(monitor.run_check(db.get_watch(wid)))
    assert sorted({s["date_code"] for s in w["last_result"]["shows"]}) == [f"202612{d}" for d in range(17, 23)]
    assert "17–22 Dec" in w["summary"][0] and len(sent) == 1

    db.save_settings({"max_dates": "3"})  # now only 3 dates fit: the rest must not look "new" next time
    asyncio.run(monitor.run_check(db.get_watch(wid)))
    db.save_settings({"max_dates": "10"})
    w = asyncio.run(monitor.run_check(db.get_watch(wid)))
    assert len(sent) == 1, sent  # no false "new show" alerts
