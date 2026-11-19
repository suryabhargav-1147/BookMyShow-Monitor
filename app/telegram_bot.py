"""Chat with the Telegram bot to manage watches.

Long-polls getUpdates (no public URL/webhook needed). Only chats listed in the
`telegram_chat_id` setting may use it; anyone else is told their ID so the owner
can add them.

  /add            guided: movie → theatre → watching
  /list           watches with Check / Pause / Delete buttons
  /check          check every active watch now
  /city <name>    default city for new watches
  /help
  <any text>      treated as a movie name or BookMyShow link (starts /add)
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import httpx

from . import auth, db, monitor, parse

log = logging.getLogger("telegram-bot")
ICON = {"open": "🟢", "not_open": "⚪", "blocked": "🟠", "error": "🔴", "pending": "⏳"}
STATUS = {"open": "Open", "not_open": "Not open yet", "blocked": "Blocked", "error": "Error", "pending": "Not checked yet"}
HELP = (
    "🎟️ <b>BookMyShow Monitor</b>\n\n"
    "Send a <b>movie name</b> or a <b>BookMyShow link</b> to start watching it, "
    "or use:\n"
    "/add – watch a movie (I'll ask for the theatre)\n"
    "/list – your watches (check, pause, delete)\n"
    "/check – check everything now\n"
    "/city <i>name</i> – default city (now: <b>{city}</b>)\n"
    "/web – one-tap sign-in link for the dashboard\n"
    "/help – this message\n\n"
    "I message you when tickets open at your theatre, a new date or show appears, "
    "or a sold-out show gets seats back."
)
Markup = list[list[dict[str, str]]]


class Bot:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=45)
        self.offset = 0
        self.sessions: dict[str, dict[str, Any]] = {}  # chat_id -> add-flow state
        self.status = "stopped"

    # ------------------------------------------------------------ telegram api
    def _token(self) -> str:
        return db.get_settings().get("telegram_bot_token", "").strip()

    def _allowed(self) -> set[str]:
        raw = db.get_settings().get("telegram_chat_id", "")
        return {c.strip() for c in raw.replace(";", ",").split(",") if c.strip()}

    async def api(self, method: str, **params: Any) -> dict:
        r = await self.http.post(f"https://api.telegram.org/bot{self._token()}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(r.status_code, data.get("description", ""))
        return data.get("result")

    async def send(self, chat: str, text: str, buttons: Markup | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                  "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        await self.api("sendMessage", **params)

    # ------------------------------------------------------------ loop
    async def run(self) -> None:
        while True:
            if not self._token():
                self.status = "no token"
                await asyncio.sleep(15)
                continue
            try:
                if self.status != "running":
                    me = await self.api("getMe")
                    await self.api("setMyCommands", commands=[
                        {"command": "add", "description": "Watch a movie at a theatre"},
                        {"command": "list", "description": "Your watches"},
                        {"command": "check", "description": "Check everything now"},
                        {"command": "city", "description": "Set default city"},
                        {"command": "web", "description": "Sign-in link for the dashboard"},
                        {"command": "help", "description": "How to use"}])
                    self.status = "running"
                    log.info("Telegram bot @%s listening", me.get("username"))
                updates = await self.api("getUpdates", offset=self.offset, timeout=30,
                                         allowed_updates=["message", "callback_query"])
                for u in updates:
                    self.offset = u["update_id"] + 1
                    try:
                        await self.handle(u)
                    except Exception:  # noqa: BLE001
                        log.exception("handling update failed")
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                # 409: another program is reading this bot's updates (or a webhook is set)
                self.status = f"error: {e.description}"
                log.warning("Telegram: %s", e.description)
                await asyncio.sleep(30 if e.code == 409 else 10)
            except Exception as e:  # noqa: BLE001
                self.status = f"error: {type(e).__name__}"
                log.warning("Telegram poll failed: %s", e)
                await asyncio.sleep(10)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------ routing
    async def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = str(cq["message"]["chat"]["id"])
            await self.api("answerCallbackQuery", callback_query_id=cq["id"])
            if chat in self._allowed():
                await self.on_button(chat, cq.get("data", ""))
            return
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            return
        if chat not in self._allowed():
            await self.send(chat, "🔒 This bot is private.\nYour chat ID is <code>" + html.escape(chat) +
                            "</code>. Ask the owner to add it to <i>telegram_user_ids</i>.")
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        city = self._city()
        if cmd in ("/start", "/help"):
            await self.send(chat, HELP.format(city=html.escape(city)))
        elif cmd == "/add":
            self.sessions[chat] = {"step": "movie"}
            if arg.strip():
                await self.on_movie(chat, arg.strip())
            else:
                await self.send(chat, f"🎬 Which movie? Send its name or BookMyShow link.\n(city: <b>{html.escape(city)}</b>, change with /city)")
        elif cmd == "/list":
            await self.on_list(chat)
        elif cmd == "/check":
            await self.on_check_all(chat)
        elif cmd == "/city":
            if arg.strip():
                db.save_settings({"default_city": arg.strip().lower().replace(" ", "-")})
                await self.send(chat, f"📍 Default city set to <b>{html.escape(self._city())}</b>.")
            else:
                await self.send(chat, f"📍 Default city is <b>{html.escape(city)}</b>. Change it: <code>/city mumbai</code>")
        elif cmd == "/web":
            base = auth.public_url()
            if not base:
                await self.send(chat, "The dashboard has no public address (BMS_PUBLIC_URL isn't set). "
                                      "Open it on the machine running the monitor.")
            else:
                link = f"{base}/auth/link?t={auth.codes.link_token(chat)}"
                await self.send(chat, f'🔐 <a href="{html.escape(link)}">Open BMS Monitor dashboard</a>\n'
                                      "One-time link, valid 10 minutes. Don't forward it.")
        elif cmd == "/cancel":
            self.sessions.pop(chat, None)
            await self.send(chat, "Cancelled.")
        elif cmd.startswith("/"):
            await self.send(chat, "I don't know that command. /help")
        else:
            sess = self.sessions.get(chat)
            if sess and sess.get("step") == "theatre":
                await self.on_theatre(chat, text)
            else:
                self.sessions[chat] = {"step": "movie"}
                await self.on_movie(chat, text)

    def _city(self) -> str:
        return db.get_settings().get("default_city") or "chennai"

    # ------------------------------------------------------------ add flow
    async def on_movie(self, chat: str, text: str) -> None:
        city = self._city()
        ref = parse.parse_movie_input(text, city)
        if ref:
            await self.ask_theatre(chat, ref)
            return
        await self.send(chat, f"🔎 Searching BookMyShow {html.escape(city)} for “{html.escape(text)}”…")
        s = db.get_settings()
        try:
            found = await monitor.client.search_movies(city, text, headless=s.get("headless", "1") == "1")
        except Exception as e:  # noqa: BLE001
            await self.send(chat, f"Search failed ({html.escape(type(e).__name__)}). Paste the BookMyShow link instead.")
            return
        if not found:
            await self.send(chat, "No match. Paste the movie's BookMyShow link (open the movie on in.bookmyshow.com and share/copy the URL).")
            return
        self.sessions[chat] = {"step": "pick", "cands": found[:6]}
        buttons = [[{"text": f"{m.title} · {m.event_code}", "callback_data": f"pick:{i}"}] for i, m in enumerate(found[:6])]
        await self.send(chat, "Which one?", buttons)

    async def ask_theatre(self, chat: str, ref: parse.MovieRef) -> None:
        self.sessions[chat] = {"step": "theatre", "ref": ref}
        await self.send(chat, f"🎬 <b>{html.escape(ref.title)}</b> ({html.escape(ref.city)})\n\n"
                              "🏛️ Which theatre? Send words from its name, e.g. <code>PVR Grand Mall Velachery</code>.\n"
                              "Several: separate with commas. Or tap below.",
                        [[{"text": "Any theatre in " + ref.city.title(), "callback_data": "th:any"}],
                         [{"text": "Cancel", "callback_data": "cancel"}]])

    async def on_theatre(self, chat: str, theatre: str) -> None:
        sess = self.sessions.pop(chat, None) or {}
        ref: parse.MovieRef | None = sess.get("ref")
        if not ref:
            await self.send(chat, "Let's start over: send a movie name. /add")
            return
        wid = db.create_watch(title=ref.title, city=ref.city, slug=ref.slug, event_code=ref.event_code,
                              theatre=theatre, interval_sec=120)
        db.log_event(wid, "info", f"Added from Telegram: {ref.title} ({ref.event_code}) @ {theatre or 'any theatre'}")
        await self.send(chat, f"✅ Watching <b>{html.escape(ref.title)}</b> @ <b>{html.escape(theatre or 'any theatre')}</b>, "
                              "every 2 min. Checking now…")
        w = await monitor.run_check(db.get_watch(wid))
        await self.send(chat, self.describe(w), self.watch_buttons(w))

    # ------------------------------------------------------------ list / actions
    def describe(self, w: dict) -> str:
        r = w.get("last_result") or {}
        lines = [f"{ICON.get(w['status'], '')} <b>{html.escape(w['title'])}</b> #{w['id']}",
                 f"🏛️ {html.escape(w['theatre'] or 'any theatre')} · {html.escape(w['city'])}"
                 + ("" if w["active"] else " · ⏸ paused"),
                 f"{STATUS.get(w['status'], w['status'])}: {html.escape(w['status_detail'])}"]
        if w["status"] == "open" and r.get("booking_url"):
            lines.append(f'<a href="{html.escape(r["booking_url"])}">Book on BookMyShow →</a>')
        return "\n".join(lines)

    def watch_buttons(self, w: dict) -> Markup:
        return [[{"text": "🔄 Check", "callback_data": f"chk:{w['id']}"},
                 {"text": "⏸ Pause" if w["active"] else "▶️ Resume", "callback_data": f"tog:{w['id']}"},
                 {"text": "🗑 Delete", "callback_data": f"del:{w['id']}"}]]

    async def on_list(self, chat: str) -> None:
        ws = db.list_watches()
        if not ws:
            await self.send(chat, "No watches yet. Send a movie name to add one.")
            return
        for w in ws[:15]:
            await self.send(chat, self.describe(w), self.watch_buttons(w))

    async def on_check_all(self, chat: str) -> None:
        ws = [w for w in db.list_watches() if w["active"]]
        if not ws:
            await self.send(chat, "Nothing active to check.")
            return
        await self.send(chat, f"🔄 Checking {len(ws)} watch(es)…")
        for w in ws:
            w2 = await monitor.run_check(w)
            await self.send(chat, self.describe(w2), self.watch_buttons(w2))

    async def on_button(self, chat: str, data: str) -> None:
        kind, _, val = data.partition(":")
        if kind == "pick":
            sess = self.sessions.get(chat) or {}
            cands = sess.get("cands") or []
            if val.isdigit() and int(val) < len(cands):
                await self.ask_theatre(chat, cands[int(val)])
            else:
                await self.send(chat, "That list expired; send the movie name again.")
        elif kind == "th":
            await self.on_theatre(chat, "")
        elif kind == "cancel":
            self.sessions.pop(chat, None)
            await self.send(chat, "Cancelled.")
        elif kind in ("chk", "tog", "del") and val.isdigit():
            w = db.get_watch(int(val))
            if not w:
                await self.send(chat, "That watch no longer exists.")
                return
            if kind == "chk":
                w = await monitor.run_check(w)
                await self.send(chat, self.describe(w), self.watch_buttons(w))
            elif kind == "tog":
                db.update_watch(w["id"], active=not w["active"])
                w = db.get_watch(w["id"])
                await self.send(chat, self.describe(w), self.watch_buttons(w))
            else:
                db.delete_watch(w["id"])
                await self.send(chat, f"🗑 Deleted <b>{html.escape(w['title'])}</b> @ {html.escape(w['theatre'] or 'any theatre')}.")


class TelegramError(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(description)
        self.code, self.description = code, description


bot = Bot()
