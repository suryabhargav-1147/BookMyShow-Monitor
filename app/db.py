"""SQLite persistence: watches, event log, settings."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("BMS_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "monitor.db"

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    city TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT 'movie',
    event_code TEXT NOT NULL,
    theatre TEXT NOT NULL DEFAULT '',
    dates TEXT NOT NULL DEFAULT '',
    periods TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    interval_sec INTEGER NOT NULL DEFAULT 180,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    status_detail TEXT NOT NULL DEFAULT '',
    last_checked TEXT,
    next_check TEXT,
    last_result TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT '{}',
    alerts_sent INTEGER NOT NULL DEFAULT 0,
    checks INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "headless": "1",
    "browser_fallback": "1",
    "max_dates": "5",
    "default_city": "chennai",
    "jitter_sec": "30",
}
SECRET_SETTINGS = {"telegram_bot_token"}

with _lock:
    _conn.executescript(SCHEMA)
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(watches)")}
    if "next_check" not in cols:  # databases created before randomized scheduling
        _conn.execute("ALTER TABLE watches ADD COLUMN next_check TEXT")
    _conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
        _conn.commit()
        return rows


def _x(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid or 0


JSON_COLS = ("last_result", "state")


def _watch(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["active"] = bool(d["active"])
    for c in JSON_COLS:
        d[c] = json.loads(d.get(c) or "{}")
    from .parse import date_summary  # local import: parse has no db dependency
    d["summary"] = date_summary(d["last_result"], d["theatre"], d["dates"]) \
        if d["status"] in ("open", "not_open") else []
    return d


# ---- watches -------------------------------------------------------------
def list_watches() -> list[dict[str, Any]]:
    return [_watch(r) for r in _q("SELECT * FROM watches ORDER BY id DESC")]


def get_watch(wid: int) -> dict[str, Any] | None:
    rows = _q("SELECT * FROM watches WHERE id=?", (wid,))
    return _watch(rows[0]) if rows else None


CREATABLE = ("title", "city", "slug", "event_code", "theatre", "dates", "periods", "region", "interval_sec")
UPDATABLE = {"title", "slug", "event_code", "city", "theatre", "dates", "periods", "region", "interval_sec", "active", "status",
             "status_detail", "last_checked", "next_check", "last_result", "state", "alerts_sent", "checks"}


def create_watch(**f: Any) -> int:
    cols = [c for c in CREATABLE if c in f]
    return _x(f"INSERT INTO watches({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
              (*[f[c] for c in cols], now_iso()))


def update_watch(wid: int, **fields: Any) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k not in UPDATABLE:
            raise ValueError(f"field {k} not updatable")
        if k in JSON_COLS:
            v = json.dumps(v)
        elif k == "active":
            v = int(bool(v))
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        _x(f"UPDATE watches SET {', '.join(sets)} WHERE id=?", (*vals, wid))


def delete_watch(wid: int) -> None:
    _x("DELETE FROM watches WHERE id=?", (wid,))
    _x("DELETE FROM events WHERE watch_id=?", (wid,))


# ---- events --------------------------------------------------------------
def log_event(watch_id: int | None, level: str, message: str) -> dict[str, Any]:
    ts = now_iso()
    eid = _x("INSERT INTO events(watch_id, level, message, created_at) VALUES (?,?,?,?)",
             (watch_id, level, message, ts))
    _x("DELETE FROM events WHERE id <= ?", (eid - 2000,))
    return {"id": eid, "watch_id": watch_id, "level": level, "message": message, "created_at": ts}


def list_events(limit: int = 100, watch_id: int | None = None) -> list[dict[str, Any]]:
    if watch_id:
        rows = _q("SELECT * FROM events WHERE watch_id=? ORDER BY id DESC LIMIT ?", (watch_id, limit))
    else:
        rows = _q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def stats() -> dict[str, int]:
    r = _q("""SELECT COUNT(*) total, SUM(active) active, SUM(status='open') open,
              COALESCE(SUM(checks),0) checks, COALESCE(SUM(alerts_sent),0) alerts FROM watches""")[0]
    return {k: int(r[k] or 0) for k in r.keys()}


# ---- settings ------------------------------------------------------------
def get_settings() -> dict[str, str]:
    s = dict(DEFAULT_SETTINGS)
    for r in _q("SELECT key, value FROM settings"):
        if r["key"] in s:
            s[r["key"]] = r["value"]
    # env vars win (handy for Docker / headless servers)
    for k in s:
        env = os.getenv(f"BMS_{k.upper()}")
        if env is not None:
            s[k] = env
    return s


def save_settings(values: dict[str, str]) -> None:
    for k, v in values.items():
        if k in DEFAULT_SETTINGS:
            _x("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (k, str(v)))
