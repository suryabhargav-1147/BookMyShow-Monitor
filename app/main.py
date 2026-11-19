"""FastAPI app: dashboard + REST API (OpenAPI at /docs) + live event stream."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import bms as bms_mod
from . import auth, db, monitor, notify, parse
from .telegram_bot import bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, and Telegram URLs contain the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
STATIC = Path(__file__).resolve().parent.parent / "static"


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [] if os.getenv("BMS_DISABLE_MONITOR") == "1" else [
        asyncio.create_task(monitor.loop()), asyncio.create_task(bot.run())]
    yield
    for t in tasks:
        t.cancel()
    await bot.aclose()
    await monitor.client.aclose()


app = FastAPI(
    title="BookMyShow Monitor API",
    version="1.0.0",
    description="Watch BookMyShow for ticket openings at a city or a specific theatre. "
                "Alerts go to Telegram, ntfy and the dashboard. Access is limited to the allowed Telegram "
                "user IDs (sign in at /login), local requests, or `Authorization: Bearer $BMS_API_TOKEN`.",
    lifespan=lifespan,
)


OPEN_PATHS = ("/login", "/auth/", "/favicon.ico", "/healthz")


@app.middleware("http")
async def guard(request: Request, call_next):
    """Only allowed Telegram users (session), local direct calls, or the bearer token get in."""
    path = request.url.path
    if path.startswith(OPEN_PATHS) or auth.is_authorized(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "sign in required"}, status_code=401, headers={"Cache-Control": "no-store"})
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


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
    jitter_sec: int | None = Field(None, ge=0, le=600)


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
    out["max_dates"] = int(s["max_dates"] or 10)
    out["jitter_sec"] = int(s.get("jitter_sec") or 0)
    out["api_token_required"] = bool(auth.API_TOKEN)
    out["telegram_bot_status"] = bot.status
    out["cities"] = sorted(k for k in parse.REGIONS if k not in ("delhi", "bangalore"))
    return out


# ---------------------------------------------------------------- dashboard
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- sign-in (allowed Telegram users only)
class CodeRequest(BaseModel):
    user_id: str = Field(..., max_length=20)


class CodeVerify(BaseModel):
    user_id: str = Field(..., max_length=20)
    code: str = Field(..., max_length=10)


def _set_session(resp, request: Request, uid: str):
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(auth.COOKIE, auth.make_session(uid), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, secure=secure, samesite="lax", path="/")
    return resp


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if auth.is_authorized(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/auth/code", include_in_schema=False)
async def auth_code(body: CodeRequest) -> dict:
    uid = body.user_id.strip()
    code = auth.codes.issue(uid) if uid.lstrip("-").isdigit() else None
    if code:
        ok, _ = await notify.telegram(
            f"🔐 <b>BMS Monitor sign-in code: <code>{code}</code></b>\nValid 5 minutes. "
            "If you didn't try to sign in, ignore this.",
            {**db.get_settings(), "telegram_chat_id": uid})
        db.log_event(None, "info", f"Sign-in code requested for user #{sorted(auth.allowed_ids()).index(uid) + 1}"
                                   + ("" if ok else " (Telegram delivery failed)"))
    # same answer either way: don't reveal which IDs are allowed
    return {"ok": True, "message": "If that ID is allowed, a code was sent to it on Telegram."}


@app.post("/auth/verify", include_in_schema=False)
def auth_verify(body: CodeVerify, request: Request):
    uid = body.user_id.strip()
    if not auth.codes.verify(uid, body.code):
        return JSONResponse({"ok": False, "message": "Wrong or expired code."}, status_code=401)
    return _set_session(JSONResponse({"ok": True}), request, uid)


@app.get("/auth/link", include_in_schema=False)
def auth_link(t: str, request: Request):
    uid = auth.codes.redeem_link(t)
    if not uid:
        return HTMLResponse("<p style='font-family:sans-serif'>This sign-in link is invalid or expired. "
                            "Send <b>/web</b> to the bot for a new one.</p>", status_code=401)
    return _set_session(RedirectResponse("/", status_code=303), request, uid)


@app.post("/auth/logout", include_in_schema=False)
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/v1/me", tags=["settings"])
def me(request: Request) -> dict:
    uid = auth.session_user(request)
    return {"via": "session" if uid else "bearer" if auth.bearer_ok(request) else "local",
            "user": f"#{sorted(auth.allowed_ids()).index(uid) + 1}" if uid else None, "can_logout": bool(uid)}


# ---------------------------------------------------------------- API v1
@app.get("/api/v1/watches", tags=["watches"])
def list_watches() -> list[dict]:
    return db.list_watches()


@app.post("/api/v1/watches", tags=["watches"], status_code=201)
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


@app.get("/api/v1/watches/{wid}", tags=["watches"])
def get_watch(wid: int) -> dict:
    w = db.get_watch(wid)
    if not w:
        raise HTTPException(404, "watch not found")
    return w


@app.patch("/api/v1/watches/{wid}", tags=["watches"])
def patch_watch(wid: int, body: WatchPatch) -> dict:
    if not db.get_watch(wid):
        raise HTTPException(404, "watch not found")
    fields = body.model_dump(exclude_none=True)
    if "interval_sec" in fields or fields.get("active"):
        fields["next_check"] = None  # reschedule from last check + new interval
    db.update_watch(wid, **fields)
    w = db.get_watch(wid)
    notify.broadcast("watch", {"watch": w})
    return w or {}


@app.delete("/api/v1/watches/{wid}", tags=["watches"], status_code=204)
def delete_watch(wid: int) -> None:
    db.delete_watch(wid)
    notify.broadcast("deleted", {"watch_id": wid})


@app.post("/api/v1/watches/{wid}/check", tags=["watches"])
async def check_now(wid: int) -> dict:
    w = db.get_watch(wid)
    if not w:
        raise HTTPException(404, "watch not found")
    return await monitor.run_check(w)


@app.post("/api/v1/watches/{wid}/reset", tags=["watches"])
def reset_watch(wid: int) -> dict:
    """Forget what was already alerted so the next check alerts again."""
    db.update_watch(wid, state={}, status="pending")
    return db.get_watch(wid) or {}


@app.get("/api/v1/dataset", tags=["results"])
def dataset() -> list[dict]:
    """Flat list of every currently matching show across all watches (one item per show)."""
    items = []
    for w in db.list_watches():
        for s in (w["last_result"] or {}).get("shows", []):
            items.append({"watch_id": w["id"], "movie": w["title"], "event_code": w["event_code"],
                          "city": w["city"], "checked_at": w["last_checked"], **s})
    return items


@app.post("/api/v1/run-sync", tags=["results"])
async def run_sync(body: RunIn) -> dict:
    """One-shot check without creating a watch: resolve the movie, fetch, filter, return results."""
    ref = await resolve_movie(body.movie, body.city)
    s = db.get_settings()
    r = await bms_mod.check(monitor.client, event_code=ref.event_code, city=ref.city, slug=ref.slug,
                            theatre=body.theatre, dates=body.dates, periods=body.periods, region=body.region,
                            max_dates=int(s["max_dates"] or 10), browser_fallback=s["browser_fallback"] == "1",
                            headless=s["headless"] == "1")
    out = {k: v for k, v in r.__dict__.items() if k != "state"}
    out["movie"] = {"title": r.movie_name or ref.title, "event_code": ref.event_code, "city": ref.city, "url": ref.url}
    return out


@app.get("/api/v1/movies/search", tags=["movies"])
async def search_movies(q: str, city: str = "chennai") -> list[dict]:
    s = db.get_settings()
    refs = await monitor.client.search_movies(city.lower(), q, headless=s["headless"] == "1")
    return [{**r.__dict__, "url": r.url} for r in refs]


@app.get("/api/v1/events", tags=["activity"])
def events(limit: int = 100, watch_id: int | None = None) -> list[dict]:
    return db.list_events(min(limit, 500), watch_id)


@app.get("/api/v1/stats", tags=["activity"])
def stats() -> dict:
    return db.stats()


@app.get("/api/v1/settings", tags=["settings"])
def get_settings() -> dict:
    return public_settings()


@app.put("/api/v1/settings", tags=["settings"])
async def put_settings(body: SettingsIn) -> dict:
    vals: dict[str, str] = {}
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "telegram_bot_token" and ("…" in v or v == "set"):
            continue  # masked value echoed back unchanged
        vals[k] = ("1" if v else "0") if isinstance(v, bool) else str(v).strip()
    db.save_settings(vals)
    return public_settings()


@app.post("/api/v1/notify/test", tags=["settings"])
async def notify_test() -> dict:
    return await notify.send_all("🔔 BMS Monitor test", "Notifications are working.",
                                 "Notifications are working.", "https://in.bookmyshow.com")


@app.post("/api/v1/telegram/detect-chat", tags=["settings"])
async def telegram_detect() -> list[dict]:
    tok = db.get_settings().get("telegram_bot_token", "")
    if not tok:
        raise HTTPException(400, "Save the bot token first")
    try:
        return await notify.telegram_detect_chat(tok)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Telegram error: {e}") from e


@app.get("/api/v1/stream", tags=["activity"])
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
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
    # explicit PORT (e.g. the systemd service): use exactly that port, fail loudly if taken
    port = wanted if os.getenv("PORT") else _free_port(host, wanted)
    if port != wanted:
        print(f"Port {wanted} is in use; using {port} instead.")
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"\n  BMS Monitor dashboard → http://{shown}:{port}\n")
    SERVER_FILE.write_text(json.dumps({"url": f"http://{shown}:{port}"}))  # lets bms-mcp find us
    # short graceful shutdown: open dashboard tabs hold a live event stream that would otherwise block stop
    uvicorn.run("app.main:app", host=host, port=port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    run()
