"""MCP server (stdio) so Claude / any MCP client can drive the monitor.

It talks to the running monitor's REST API (BMS_MONITOR_URL, else the URL bms-monitor
recorded in data/server.json at startup).

  claude mcp add bms-monitor -- uv --directory /path/to/BookMyShow-Monitor run bms-mcp
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer


def _base_url() -> str:
    if os.getenv("BMS_MONITOR_URL"):
        return os.environ["BMS_MONITOR_URL"].rstrip("/")
    try:  # written by bms-monitor at startup with the port it actually bound
        from .db import DATA_DIR
        return json.loads((DATA_DIR / "server.json").read_text())["url"].rstrip("/")
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:8642"


BASE = _base_url()
TOKEN = os.getenv("BMS_API_TOKEN", "")
mcp = MCPServer("bms-monitor", instructions="Manage BookMyShow ticket-opening watches and query live showtimes.")


def _call(method: str, path: str, **kw: Any) -> Any:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    r = httpx.request(method, f"{BASE}/api/v1{path}", headers=headers, timeout=180, **kw)
    if r.status_code >= 400:
        return {"error": r.status_code, "detail": r.text[:500]}
    return r.json() if r.content else {"ok": True}


def _slim(w: dict) -> dict:
    lr = w.get("last_result") or {}
    return {k: w.get(k) for k in ("id", "title", "event_code", "city", "theatre", "dates", "periods",
                                  "interval_sec", "active", "status", "status_detail", "last_checked")} | {
        "booking_url": lr.get("booking_url"), "shows": len(lr.get("shows") or [])}


@mcp.tool()
def list_watches() -> list[dict]:
    """List every BookMyShow watch with its current status."""
    return [_slim(w) for w in _call("GET", "/watches")]


@mcp.tool()
def add_watch(movie: str, city: str = "chennai", theatre: str = "", dates: str = "", periods: str = "",
              interval_sec: int = 180) -> dict:
    """Start watching a movie. movie: BookMyShow URL, ET code or name. theatre: words that must all appear in
    the venue name (comma = OR). dates: YYYYMMDD list. periods: morning/afternoon/evening/night."""
    return _call("POST", "/watches", json=dict(movie=movie, city=city, theatre=theatre, dates=dates,
                                                 periods=periods, interval_sec=interval_sec))


@mcp.tool()
def check_now(watch_id: int) -> dict:
    """Run a check for one watch immediately and return its updated status."""
    return _slim(_call("POST", f"/watches/{watch_id}/check"))


@mcp.tool()
def set_active(watch_id: int, active: bool) -> dict:
    """Pause (false) or resume (true) a watch."""
    return _slim(_call("PATCH", f"/watches/{watch_id}", json={"active": active}))


@mcp.tool()
def remove_watch(watch_id: int) -> dict:
    """Delete a watch."""
    return _call("DELETE", f"/watches/{watch_id}")


@mcp.tool()
def check_showtimes(movie: str, city: str = "chennai", theatre: str = "", dates: str = "",
                    periods: str = "") -> dict:
    """One-shot: fetch current showtimes/availability for a movie (optionally one theatre) without saving a watch."""
    return _call("POST", "/run-sync", json=dict(movie=movie, city=city, theatre=theatre, dates=dates, periods=periods))


@mcp.tool()
def search_movies(query: str, city: str = "chennai") -> list[dict]:
    """Find a movie's BookMyShow event code by name in a city."""
    return _call("GET", "/movies/search", params={"q": query, "city": city})


@mcp.tool()
def recent_activity(limit: int = 20) -> list[dict]:
    """Recent status changes and alerts."""
    return _call("GET", "/events", params={"limit": limit})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
