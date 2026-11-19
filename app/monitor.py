"""Scheduler: checks each active watch on its interval and alerts on openings/changes."""
from __future__ import annotations

import asyncio
import html
import logging
import random
from datetime import datetime, timezone

from . import bms as bms_mod
from . import db, notify, parse

log = logging.getLogger("monitor")
MIN_INTERVAL = 60
client = bms_mod.Bms()


def is_due(w: dict) -> bool:
    if not w["active"]:
        return False
    if not w["last_checked"]:
        return True
    last = datetime.fromisoformat(w["last_checked"])
    return (datetime.now(timezone.utc) - last).total_seconds() >= max(MIN_INTERVAL, int(w["interval_sec"]))


def compose_alert(w: dict, r: bms_mod.CheckResult, changes: list[str], first_open: bool) -> tuple[str, str, str]:
    name = r.movie_name or w["title"]
    title = f"🎟️ {name}: tickets open!" if first_open else f"🎬 {name}: {len(changes)} update(s)"
    where = w["theatre"] or w["city"].title()
    lines: list[str] = []
    if first_open:
        lines.append(f"Booking is live for {where}.")
    lines += changes[:15]
    if len(changes) > 15:
        lines.append(f"…and {len(changes) - 15} more")
    # compact current summary
    by_venue: dict[str, list[str]] = {}
    for s in r.shows:
        tag = "" if s["available"] else " (full)"
        by_venue.setdefault(s["venue_name"], []).append(f"{parse.fmt_date(s['date_code'])} {s['time']}{tag}")
    if by_venue:
        lines.append("")
        for v, times in list(by_venue.items())[:5]:
            lines.append(f"📍 {v}: " + ", ".join(times[:8]) + (" …" if len(times) > 8 else ""))
    plain = "\n".join(lines)
    return title, html.escape(plain), plain


async def run_check(w: dict) -> dict:
    s = db.get_settings()
    notify.broadcast("checking", {"watch_id": w["id"]})
    r = await bms_mod.check(
        client, event_code=w["event_code"], city=w["city"], slug=w["slug"], theatre=w["theatre"],
        dates=w["dates"], periods=w["periods"], region=w["region"],
        max_dates=int(s.get("max_dates") or 5), browser_fallback=s.get("browser_fallback", "1") == "1",
        headless=s.get("headless", "1") == "1",
    )
    prev_status, old_state = w["status"], w["state"]
    updates: dict = {"status": r.status, "status_detail": r.detail, "last_checked": db.now_iso(),
                     "checks": w["checks"] + 1}
    result = {k: v for k, v in r.__dict__.items() if k != "state"}
    updates["last_result"] = result
    if r.movie_name and (w["title"] in (w["event_code"], "Movie") or w["title"].startswith("ET")):
        updates["title"] = r.movie_name

    if r.status != prev_status:
        level = {"open": "success", "blocked": "warning", "error": "error"}.get(r.status, "info")
        db.log_event(w["id"], level, f"{w['title']}: {prev_status} → {r.status}. {r.detail}")

    if r.status in ("open", "not_open"):
        changes = parse.detect_changes(old_state, r.state)
        first_open = r.status == "open" and not old_state.get("shows")
        if first_open or (changes and r.status == "open"):
            title, body_html, body_plain = compose_alert(w, r, changes, first_open)
            out = await notify.send_all(title, body_html, body_plain, r.booking_url)
            updates["alerts_sent"] = w["alerts_sent"] + 1
            db.log_event(w["id"], "alert", f"{title}\n{body_plain}\n→ delivery: {out}")
        updates["state"] = r.state

    db.update_watch(w["id"], **updates)
    w2 = db.get_watch(w["id"]) or {}
    notify.broadcast("watch", {"watch": w2})
    return w2


async def loop() -> None:
    await asyncio.sleep(2)
    while True:
        try:
            for w in db.list_watches():
                if is_due(w):
                    await run_check(w)
                    await asyncio.sleep(random.uniform(2, 6))  # spread requests out
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("monitor loop error")
        await asyncio.sleep(5)
