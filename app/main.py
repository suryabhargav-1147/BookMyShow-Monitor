"""FastAPI app: dashboard + REST API (OpenAPI at /docs) + live event stream."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import bms as bms_mod
from . import db, monitor, notify, parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).resolve().parent.parent / "static"
API_TOKEN = os.getenv("BMS_API_TOKEN", "")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    task = None if os.getenv("BMS_DISABLE_MONITOR") == "1" else asyncio.create_task(monitor.loop())
    yield
    if task:
        task.cancel()
    await monitor.client.aclose()


app = FastAPI(
    title="BookMyShow Monitor API",
    version="1.0.0",
    description="Watch BookMyShow for ticket openings at a city or a specific theatre. "
                "Alerts go to Telegram, ntfy and the dashboard. Set BMS_API_TOKEN to require "
                "`Authorization: Bearer <token>` from non-local clients.",
    lifespan=lifespan,
)


def auth(request: Request) -> None:
    """Local clients are trusted; remote ones need the bearer token when BMS_API_TOKEN is set."""
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1", "localhost", "testclient"):
        return
    if not API_TOKEN:
        return
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
        or request.query_params.get("token", "")
    if not hmac.compare_digest(supplied, API_TOKEN):
        raise HTTPException(401, "missing or bad bearer token")


api = Depends(auth)


# ---------------------------------------------------------------- models
class WatchIn(BaseModel):
    movie: str = Field(..., description="BookMyShow movie/buytickets URL, an ET code, or a movie name",
                       examples=["https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706"])
    city: str = Field("chennai", examples=["chennai"])
    theatre: str = Field("", description="Words that must all appear in the venue name; comma = OR",
                         examples=["PVR Phoenix Velachery"])
    dates: str = Field("", description="Only these dates, comma-separated YYYYMMDD", examples=[""])
    periods: str = Field("", description="morning, afternoon, evening, night (comma-separated)")
    region: str = Field("", description="Override BookMyShow region code for cities not built in, e.g. COIM")
    interval_sec: int = Field(180, ge=monitor.MIN_INTERVAL, le=86400)


class WatchPatch(BaseModel):
    title: str | None = None
    theatre: str | None = None
    dates: str | None = None
    periods: str | None = None
    region: str | None = None
    interval_sec: int | None = Field(None, ge=monitor.MIN_INTERVAL, le=86400)
    active: bool | None = None


class RunIn(BaseModel):
    movie: str = Field(..., examples=["ET00439706"])
    city: str = "chennai"
    theatre: str = ""
    dates: str = ""
    periods: str = ""
    region: str = ""


class SettingsIn(BaseModel):
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    ntfy_server: str | None = None
    ntfy_topic: str | None = None
    headless: bool | None = None
    browser_fallback: bool | None = None
    max_dates: int | None = Field(None, ge=1, le=14)


# ---------------------------------------------------------------- helpers
async def resolve_movie(movie: str, city: str) -> parse.MovieRef:
    city = city.strip().lower()
    ref = parse.parse_movie_input(movie, city)
    if ref:
        return ref
    s = db.get_settings()
    found = await monitor.client.search_movies(city, movie, headless=s["headless"] == "1")
    if not found:
        raise HTTPException(404, f"No movie matching '{movie}' in {city}. Paste the BookMyShow URL instead.")
    return found[0]


def public_settings() -> dict[str, Any]:
    s = db.get_settings()
    out: dict[str, Any] = dict(s)
    tok = s.get("telegram_bot_token", "")
    out["telegram_bot_token"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else ("set" if tok else "")
    out["telegram_configured"] = bool(tok and s.get("telegram_chat_id"))
    out["ntfy_configured"] = bool(s.get("ntfy_topic"))
    out["headless"] = s["headless"] == "1"
    out["browser_fallback"] = s["browser_fallback"] == "1"
    out["max_dates"] = int(s["max_dates"] or 5)
    out["api_token_required"] = bool(API_TOKEN)
    out["cities"] = sorted(k for k in parse.REGIONS if k not in ("delhi", "bangalore"))
    return out


# ---------------------------------------------------------------- dashboard
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- API v1
@app.get("/api/v1/watches", dependencies=[api], tags=["watches"])
def list_watches() -> list[dict]:
    return db.list_watches()


@app.post("/api/v1/watches", dependencies=[api], tags=["watches"], status_code=201)
async def create_watch(body: WatchIn) -> dict:
    ref = await resolve_movie(body.movie, body.city)
    wid = db.create_watch(title=ref.title, city=ref.city, slug=ref.slug, event_code=ref.event_code,
                          theatre=body.theatre.strip(), dates=body.dates.strip(), periods=body.periods.strip(),
                          region=body.region.strip(), interval_sec=body.interval_sec)
    db.log_event(wid, "info", f"Watching {ref.title} ({ref.event_code}) in {ref.city}"
                              + (f" at '{body.theatre}'" if body.theatre else ""))
    w = db.get_watch(wid)
    notify.broadcast("watch", {"watch": w})
    return w or {}


@app.get("/api/v1/watches/{wid}", dependencies=[api], tags=["watches"])
def get_watch(wid: int) -> dict:
    w = db.get_watch(wid)
    if not w:
        raise HTTPException(404, "watch not found")
    return w


@app.patch("/api/v1/watches/{wid}", dependencies=[api], tags=["watches"])
def patch_watch(wid: int, body: WatchPatch) -> dict:
    if not db.get_watch(wid):
        raise HTTPException(404, "watch not found")
    db.update_watch(wid, **body.model_dump(exclude_none=True))
    w = db.get_watch(wid)
    notify.broadcast("watch", {"watch": w})
    return w or {}


@app.delete("/api/v1/watches/{wid}", dependencies=[api], tags=["watches"], status_code=204)
def delete_watch(wid: int) -> None:
    db.delete_watch(wid)
    notify.broadcast("deleted", {"watch_id": wid})


@app.post("/api/v1/watches/{wid}/check", dependencies=[api], tags=["watches"])
async def check_now(wid: int) -> dict:
    w = db.get_watch(wid)
    if not w:
        raise HTTPException(404, "watch not found")
    return await monitor.run_check(w)


@app.post("/api/v1/watches/{wid}/reset", dependencies=[api], tags=["watches"])
def reset_watch(wid: int) -> dict:
    """Forget what was already alerted so the next check alerts again."""
    db.update_watch(wid, state={}, status="pending")
    return db.get_watch(wid) or {}


@app.get("/api/v1/dataset", dependencies=[api], tags=["results"])
def dataset() -> list[dict]:
    """Flat list of every currently matching show across all watches (one item per show)."""
    items = []
    for w in db.list_watches():
        for s in (w["last_result"] or {}).get("shows", []):
            items.append({"watch_id": w["id"], "movie": w["title"], "event_code": w["event_code"],
                          "city": w["city"], "checked_at": w["last_checked"], **s})
    return items


@app.post("/api/v1/run-sync", dependencies=[api], tags=["results"])
async def run_sync(body: RunIn) -> dict:
    """One-shot check without creating a watch: resolve the movie, fetch, filter, return results."""
    ref = await resolve_movie(body.movie, body.city)
    s = db.get_settings()
    r = await bms_mod.check(monitor.client, event_code=ref.event_code, city=ref.city, slug=ref.slug,
                            theatre=body.theatre, dates=body.dates, periods=body.periods, region=body.region,
                            max_dates=int(s["max_dates"] or 5), browser_fallback=s["browser_fallback"] == "1",
                            headless=s["headless"] == "1")
    out = {k: v for k, v in r.__dict__.items() if k != "state"}
    out["movie"] = {"title": r.movie_name or ref.title, "event_code": ref.event_code, "city": ref.city, "url": ref.url}
    return out


@app.get("/api/v1/movies/search", dependencies=[api], tags=["movies"])
async def search_movies(q: str, city: str = "chennai") -> list[dict]:
    s = db.get_settings()
    refs = await monitor.client.search_movies(city.lower(), q, headless=s["headless"] == "1")
    return [{**r.__dict__, "url": r.url} for r in refs]


@app.get("/api/v1/events", dependencies=[api], tags=["activity"])
def events(limit: int = 100, watch_id: int | None = None) -> list[dict]:
    return db.list_events(min(limit, 500), watch_id)


@app.get("/api/v1/stats", dependencies=[api], tags=["activity"])
def stats() -> dict:
    return db.stats()


@app.get("/api/v1/settings", dependencies=[api], tags=["settings"])
def get_settings() -> dict:
    return public_settings()


@app.put("/api/v1/settings", dependencies=[api], tags=["settings"])
async def put_settings(body: SettingsIn) -> dict:
    vals: dict[str, str] = {}
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "telegram_bot_token" and ("…" in v or v == "set"):
            continue  # masked value echoed back unchanged
        vals[k] = ("1" if v else "0") if isinstance(v, bool) else str(v).strip()
    db.save_settings(vals)
    return public_settings()


@app.post("/api/v1/notify/test", dependencies=[api], tags=["settings"])
async def notify_test() -> dict:
    return await notify.send_all("🔔 BMS Monitor test", "Notifications are working.",
                                 "Notifications are working.", "https://in.bookmyshow.com")


@app.post("/api/v1/telegram/detect-chat", dependencies=[api], tags=["settings"])
async def telegram_detect() -> list[dict]:
    tok = db.get_settings().get("telegram_bot_token", "")
    if not tok:
        raise HTTPException(400, "Save the bot token first")
    try:
        return await notify.telegram_detect_chat(tok)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Telegram error: {e}") from e


@app.get("/api/v1/stream", dependencies=[api], tags=["activity"])
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events: watch updates, checks and alerts in real time."""
    q = notify.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            notify.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


DEFAULT_PORT = 8642
SERVER_FILE = db.DATA_DIR / "server.json"


def _free_port(host: str, start: int, tries: int = 20) -> int:
    import socket
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port in {start}-{start + tries - 1} on {host}. Set PORT=<free port>.")


def run() -> None:
    import json

    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    wanted = int(os.getenv("PORT", DEFAULT_PORT))
    port = _free_port(host, wanted)
    if port != wanted:
        print(f"Port {wanted} is in use; using {port} instead.")
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"\n  BMS Monitor dashboard → http://{shown}:{port}\n")
    SERVER_FILE.write_text(json.dumps({"url": f"http://{shown}:{port}"}))  # lets bms-mcp find us
    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    run()
