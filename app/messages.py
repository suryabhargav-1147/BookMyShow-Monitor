"""Readable Telegram (HTML) messages: watch cards and alerts.

Layout rules: one idea per line, showtimes grouped by date (one line per day), sold-out
shows struck through, the movie version on its own line, and booking as a button.
"""
from __future__ import annotations

import html
import re
from datetime import date

from . import parse

ICON = {"open": "🟢", "not_open": "⚪", "blocked": "🟠", "error": "🔴", "pending": "⏳"}
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_DATES, MAX_TIMES, MAX_VENUES = 8, 6, 3

e = html.escape


# ---------------------------------------------------------------- small formatters
def day_label(code: str) -> str:
    """'20261217' → 'Thu 17 Dec'."""
    try:
        d = date(int(code[:4]), int(code[4:6]), int(code[6:8]))
    except (ValueError, TypeError):
        return code or "?"
    return f"{_DAYS[d.weekday()]} {d.day:>2} {_MON[d.month - 1]}"


def short_time(t: str) -> str:
    """'06:05 AM' → '6:05 AM'."""
    return re.sub(r"^0(\d:)", r"\1", (t or "").strip())


def pretty_format(fmt: str) -> str:
    """'MS-Infinity Vsn 3d' → 'MS-Infinity 3D'; 'R EPIQ DOLBY ATMOS' → 'EPIQ Dolby Atmos'."""
    f = re.sub(r"\bvsn\b", "", fmt or "", flags=re.I)
    f = re.sub(r"\b([234])d\b", lambda m: m.group(1) + "D", f, flags=re.I)
    f = re.sub(r"^R\s+", "", f.strip())
    f = re.sub(r"\bDOLBY\b", "Dolby", f)
    f = re.sub(r"\bATMOS\b", "Atmos", f)
    return re.sub(r"\s{2,}", " ", f).strip(" ,")


def venue_name(v: str) -> str:
    """'Radiance Cinema: Koodal Nagar, Madurai' → 'Radiance Cinema · Koodal Nagar, Madurai'."""
    return (v or "").replace(": ", " · ", 1)


def movie_and_version(w: dict) -> tuple[str, str]:
    """Clean name + version line from the last check, falling back to the stored title."""
    r = w.get("last_result") or {}
    lang, fmt = parse.split_language(r.get("language", ""))
    name = r.get("movie_name") or re.sub(r"\s*\(.*\)$", "", w.get("title", "")) or w.get("title", "")
    version = " · ".join(filter(None, [lang, pretty_format(fmt)]))
    if not version:
        m = re.search(r"\(([^)]*)\)$", w.get("title", ""))
        version = m.group(1) if m else ""
    return name, version


def every(sec: int, jitter: int) -> str:
    return f"every {sec / 60:g} min" + (f" (±{jitter}s)" if jitter else "")


# ---------------------------------------------------------------- schedules
def schedule_html(shows: list[dict], max_venues: int = MAX_VENUES) -> tuple[str, bool]:
    """Per venue: one line per date with its times; sold-out times struck through.

    Returns (html, any_sold_out)."""
    by_venue: dict[str, dict[str, list[dict]]] = {}
    for s in shows:
        by_venue.setdefault(s.get("venue_name") or "Venue", {}).setdefault(s.get("date_code", ""), []).append(s)
    blocks, sold_out = [], False
    for venue, days in list(by_venue.items())[:max_venues]:
        lines = [f"📍 <b>{e(venue_name(venue))}</b>"]
        attrs = {pretty_format(s.get("screen_attr", "")) for ss in days.values() for s in ss} - {""}
        if len(attrs) == 1:
            lines[0] += f"\n<i>{e(attrs.pop())}</i>"
        for d in sorted(days)[:MAX_DATES]:
            times = []
            for s in sorted(days[d], key=lambda s: _time_key(s.get("time", "")))[:MAX_TIMES]:
                t = e(short_time(s.get("time", "")))
                if s.get("available", True):
                    times.append(t)
                else:
                    times.append(f"<s>{t}</s>")
                    sold_out = True
            extra = len(days[d]) - MAX_TIMES
            lines.append(f"<code>{day_label(d)}</code>  " + " · ".join(times) + (f" +{extra}" if extra > 0 else ""))
        if len(days) > MAX_DATES:
            lines.append(f"<i>+{len(days) - MAX_DATES} more dates</i>")
        blocks.append("\n".join(lines))
    if len(by_venue) > max_venues:
        blocks.append(f"<i>+{len(by_venue) - max_venues} more venues</i>")
    return "\n\n".join(blocks), sold_out


# ---------------------------------------------------------------- watch card
def card(w: dict, jitter: int) -> str:
    r = w.get("last_result") or {}
    name, version = movie_and_version(w)
    lines = [f"{ICON.get(w['status'], '')} <b>{e(name)}</b>  <code>#{w['id']}</code>"]
    if version:
        lines.append(f"<i>{e(version)}</i>")
    lines.append("")
    lines.append(f"🎯 {e(w['theatre'] or 'Any theatre')} · {e(w['city'].title())}")
    lines.append(f"⏱ {every(w['interval_sec'], jitter)}" + ("" if w["active"] else "  ⏸ <b>paused</b>"))
    extra = [x for x in [w.get("dates") and "📅 only " + ", ".join(day_label(d).strip() for d in w["dates"].split(",")),
                         w.get("periods") and "🕑 " + w["periods"].replace(",", ", ")] if x]
    lines += [e(x) for x in extra]
    lines.append("")
    if w["status"] in ("open", "not_open") and r.get("dates") is not None:
        lines += [e(x) for x in parse.date_summary(r, w["theatre"], w.get("dates", ""))]
    else:
        status = {"blocked": "🟠 Blocked by BookMyShow", "error": "🔴 Check failed", "pending": "⏳ Not checked yet"}
        lines.append(f"{status.get(w['status'], w['status'])}: {e(w['status_detail'])}")
    if w["status"] == "open" and r.get("shows"):
        sched, sold = schedule_html(r["shows"], max_venues=2)
        lines += ["", sched]
        if sold:
            lines.append("<i><s>struck</s> = sold out</i>")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- alerts
def alert(w: dict, r, changes: list[dict], first_open: bool) -> tuple[str, str, str]:
    """(title, telegram html body, plain text) for a first opening or a set of changes."""
    res = {"movie_name": r.movie_name, "language": r.language, "dates": r.dates, "shows": r.shows}
    name, version = movie_and_version({**w, "last_result": res})
    if first_open:
        title = f"🎟 Tickets open · {name}"
        head = "<b>🎟 TICKETS OPEN</b>"
    else:
        title = f"🔔 {len(changes)} update{'s' if len(changes) != 1 else ''} · {name}"
        head = f"<b>🔔 {len(changes)} update{'s' if len(changes) != 1 else ''}</b>"
    parts = [head, f"<b>{e(name)}</b>" + (f"\n<i>{e(version)}</i>" if version else "")]

    if first_open:
        sched, sold = schedule_html(r.shows)
        parts.append(sched)
        if sold:
            parts.append("<i><s>struck</s> = sold out</i>")
    else:
        parts.append(changes_html(changes))

    summary = parse.date_summary(res, w.get("theatre", ""), w.get("dates", ""))
    parts.append("\n".join(e(x) for x in summary))
    body = "\n\n".join(p for p in parts[1:] if p)
    full = head + "\n" + body
    plain = re.sub(r"<[^>]+>", "", full).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return title, full, plain


def changes_html(changes: list[dict]) -> str:
    new: dict[tuple[str, str], list[str]] = {}
    back: dict[str, list[str]] = {}
    opened: list[str] = []
    for c in changes:
        if c["kind"] == "new":
            new.setdefault((c["venue"], c["date"]), []).append(short_time(c["time"]))
        elif c["kind"] == "back":
            back.setdefault(c["venue"], []).append(
                f"<code>{day_label(c['date'])}</code>  {e(short_time(c['time']))} · {e(c['cat'])} "
                f"{e(parse.AVAIL.get(c['status'], '').lower())}")
        elif c["kind"] == "date":
            opened.append(c["date"])
    out = []
    if opened:
        out.append(f"📅 <b>Dates opened:</b> {e(parse.date_ranges(opened))}")
    venues = list(dict.fromkeys(v for v, _ in new))
    for v in venues:
        rows = [f"<code>{day_label(d)}</code>  " + " · ".join(e(t) for t in sorted(set(ts), key=_time_key))
                for (vv, d), ts in sorted(new.items()) if vv == v]
        out.append(f"🆕 <b>New shows</b> · {e(venue_name(v))}\n" + "\n".join(rows[:MAX_DATES]))
    for v, rows in back.items():
        out.append(f"🟢 <b>Seats back</b> · {e(venue_name(v))}\n" + "\n".join(rows[:MAX_DATES]))
    return "\n\n".join(out)


def _time_key(t: str) -> tuple[int, int]:
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", t, re.I)
    if not m:
        return (99, 0)
    h = int(m[1]) % 12 + (12 if m[3].upper() == "PM" else 0)
    return (h, int(m[2]))
