"""One-shot checker for cron / GitHub Actions: check once, alert on changes, save state, exit.

Configuration comes from env vars (or flags):
  BMS_MOVIE      BookMyShow movie URL or ET code               (required)
  BMS_CITY       city slug, default chennai
  BMS_THEATRE    words that must all appear in the venue name; comma = OR
  BMS_DATES      YYYYMMDD,YYYYMMDD                               (optional)
  BMS_TIME       morning,afternoon,evening,night                 (optional)
  BMS_REGION     region code override for unlisted cities         (optional)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NTFY_TOPIC, NTFY_SERVER  (alerts)
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import bms as bms_mod
from . import notify, parse

IST = ZoneInfo("Asia/Kolkata")


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def summary_md(movie: str, theatre: str, r: bms_mod.CheckResult) -> str:
    icon = {"open": "🟢", "not_open": "⚪", "blocked": "🟠", "error": "🔴"}.get(r.status, "")
    lines = [f"## {icon} {html.escape(movie)}: {r.status.replace('_', ' ')}", "",
             f"**Theatre filter:** {html.escape(theatre or 'any')}  ", f"**Detail:** {html.escape(r.detail)}  ",
             f"**Checked:** {datetime.now(IST):%d %b %Y %I:%M %p} IST via {r.via or '-'}", ""]
    if r.dates:
        lines.append("**Dates:** " + " ".join(
            f"`{parse.fmt_date(d['date_code'])}{'' if d['status'] == 'BOOKABLE' else ' (not open)'}`" for d in r.dates))
        lines.append("")
    if r.shows:
        lines += ["| Venue | Date | Time | Format | Seats |", "|---|---|---|---|---|"]
        for s in r.shows:
            seats = ", ".join(f"{c['name']} ₹{c['price']} {c['label']}" for c in s["categories"])
            lines.append(f"| {s['venue_name']} | {parse.fmt_date(s['date_code'])} | {s['time']} | "
                         f"{s['screen_attr']} | {seats} |")
    elif r.all_venues:
        lines += ["<details><summary>Venues currently showing it (" + str(len(r.all_venues)) + ")</summary>", "",
                  *[f"- {html.escape(v)}" for v in r.all_venues], "", "</details>"]
    return "\n".join(lines) + "\n"


async def main_async(a: argparse.Namespace) -> int:
    ref = parse.parse_movie_input(a.movie, a.city)
    if not ref:
        print(f"::error::BMS_MOVIE must be a BookMyShow movie URL or ET code, got {a.movie!r}")
        return 2
    state_path = Path(a.state)
    saved = json.loads(state_path.read_text()) if state_path.exists() else {}
    old_state, old_status = saved.get("state", {}), saved.get("status", "pending")

    client = bms_mod.Bms()
    try:
        r = await bms_mod.check(client, event_code=ref.event_code, city=ref.city, slug=ref.slug, theatre=a.theatre,
                                dates=a.dates, periods=a.time, region=a.region, max_dates=a.max_dates,
                                browser_fallback=a.browser, headless=True)
    finally:
        await client.aclose()

    movie = r.movie_name or saved.get("movie") or ref.title
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] {movie} ({ref.event_code}, {ref.city})"
          f" theatre={a.theatre or 'any'}")
    print(f"  status: {old_status} → {r.status}  ({r.detail})")
    for s in r.shows:
        cats = ", ".join(f"{c['name']}={c['label']}" for c in s["categories"])
        print(f"   • {s['venue_name']} | {s['date_code']} {s['time']} {s['screen_attr']} | {cats}")
    if r.status == "not_open" and r.all_venues:
        print(f"  {len(r.all_venues)} venue(s) in {ref.city} so far: " + "; ".join(r.all_venues[:15]))

    settings = {"telegram_bot_token": a.telegram_token, "telegram_chat_id": a.telegram_chat,
                "ntfy_topic": a.ntfy_topic, "ntfy_server": a.ntfy_server or "https://ntfy.sh"}
    alerts = 0

    async def send(title: str, plain: str, url: str) -> None:
        nonlocal alerts
        sent = []
        for name, coro in (("telegram", notify.telegram(f"<b>{html.escape(title)}</b>\n{html.escape(plain)}"
                                                         + (f'\n<a href="{url}">Book now →</a>' if url else ""), settings)),
                           ("ntfy", notify.ntfy(title, plain, url, settings))):
            try:
                ok, info = await coro
                sent.append(f"{name}: {info}")
                alerts += ok
            except Exception as e:  # noqa: BLE001
                sent.append(f"{name}: error {e}")
        print(f"  ALERT → {title} | " + " | ".join(sent))

    if r.status in ("open", "not_open"):
        changes = parse.detect_changes(old_state, r.state)
        first_open = r.status == "open" and not old_state.get("shows")
        if first_open or (changes and r.status == "open"):
            from .monitor import compose_alert  # reuse the dashboard's message format
            w = {"title": movie, "theatre": a.theatre, "city": ref.city}
            title, _, plain = compose_alert(w, r, changes, first_open)
            await send(title, plain, r.booking_url)
        new_state = r.state
    else:
        new_state = old_state  # keep the last good state through blocks/errors
        if old_status not in ("blocked", "error") and r.status != old_status:
            await send(f"⚠️ BMS monitor {r.status}", f"{movie}: {r.detail}", "")
        print(f"::warning::{r.status}: {r.detail}")

    if a.notify_test:
        await send("🔔 BMS monitor test", f"Alerts work. Watching {movie} @ {a.theatre or ref.city}.", r.booking_url)

    record = {"movie": movie, "event_code": ref.event_code, "city": ref.city, "theatre": a.theatre,
              "status": r.status, "detail": r.detail, "state": new_state}
    if record != {k: saved.get(k) for k in record}:  # only touch the file when something changed
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(record, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"  state saved → {state_path}")

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary_md(movie, a.theatre, r))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Check BookMyShow once and alert on changes.")
    p.add_argument("--movie", default=env("BMS_MOVIE"))
    p.add_argument("--city", default=env("BMS_CITY", "chennai"))
    p.add_argument("--theatre", default=env("BMS_THEATRE"))
    p.add_argument("--dates", default=env("BMS_DATES"))
    p.add_argument("--time", default=env("BMS_TIME"))
    p.add_argument("--region", default=env("BMS_REGION"))
    p.add_argument("--max-dates", type=int, default=int(env("BMS_MAX_DATES", "5")))
    p.add_argument("--state", default=env("BMS_STATE_FILE", "state/bms_state.json"))
    p.add_argument("--browser", action="store_true", help="allow Playwright fallback (needs browsers installed)")
    p.add_argument("--notify-test", action="store_true", default=env("BMS_NOTIFY_TEST") == "true",
                   help="also send a test alert")
    p.add_argument("--telegram-token", default=env("TELEGRAM_BOT_TOKEN"))
    p.add_argument("--telegram-chat", default=env("TELEGRAM_CHAT_ID"))
    p.add_argument("--ntfy-topic", default=env("NTFY_TOPIC"))
    p.add_argument("--ntfy-server", default=env("NTFY_SERVER"))
    a = p.parse_args()
    if not a.movie:
        p.error("set BMS_MOVIE or --movie")
    a.city = a.city.lower()
    sys.exit(asyncio.run(main_async(a)))


if __name__ == "__main__":
    main()
