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
import re
from datetime import date
from typing import Any

import httpx

from . import auth, db, messages, monitor, parse
from .bms import Variant, pick_default, variants

log = logging.getLogger("telegram-bot")
HELP = (
    "🎟️ <b>BookMyShow Monitor</b>\n\n"
    "Send a <b>movie name</b> or a <b>BookMyShow link</b> to start watching it, "
    "or use:\n"
    "/add – watch a movie (I'll ask city, theatre, timing)\n"
    "One line: <code>/add doomsday | madurai | radials | 5</code>\n"
    "<i>(movie | city | theatre | minutes)</i>\n"
    "/list – your watches: ✏️ edit theatre, city, timing, movie, dates, show time\n"
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
            sess = self.sessions.get(chat) or {}
            step = sess.get("step")
            if step == "theatre":
                await self.on_theatre(chat, text)
            elif step == "city":
                await self.on_add_city(chat, text)
            elif step == "edit":
                await self.on_edit_text(chat, sess, text)
            else:
                self.sessions[chat] = {"step": "movie"}
                await self.on_movie(chat, text)

    def _city(self) -> str:
        return db.get_settings().get("default_city") or "chennai"

    # ------------------------------------------------------------ add flow
    async def on_movie(self, chat: str, text: str) -> None:
        if "|" in text:  # one line: movie | city | theatre | minutes
            await self.quick_add(chat, text)
            return
        city = self._city()
        ref = parse.parse_movie_input(text, city)
        if ref:
            await self.ask_theatre(chat, ref)
            return
        found = await self.search(chat, city, text)
        if not found:
            return
        vs = await self.find_variants(chat, found, city)
        if len(vs) <= 1:
            await self.ask_theatre(chat, (vs[0].ref(city) if vs else found[0]))
            return
        self.sessions[chat] = {"step": "lang", "variants": vs, "city": city, "query": text}
        await self.ask_language(chat)

    async def find_variants(self, chat: str, found: list[parse.MovieRef], city: str) -> list[Variant]:
        await self.send(chat, "🌐 Looking up languages and formats…")
        return await variants(monitor.client, found[:4], city)

    async def ask_language(self, chat: str) -> None:
        sess = self.sessions[chat]
        vs: list[Variant] = sess["variants"]
        langs = sorted({v.language or "Other" for v in vs}, key=lambda l: (l.lower() != "english", l))
        sess["langs"] = langs
        default = parse.language_in(sess.get("query", "")) or "English"
        if len(langs) == 1:
            await self.ask_format(chat, langs[0])
            return
        rows = [[{"text": l + (" ⭐ default" if l.lower() == default.lower() else ""), "callback_data": f"lang:{i}"}]
                for i, l in enumerate(langs)]
        await self.send(chat, f"🎬 <b>{html.escape(vs[0].name)}</b>\n🗣 Which language?", rows)

    async def ask_format(self, chat: str, lang: str) -> None:
        sess = self.sessions[chat]
        pool = sorted([v for v in sess["variants"] if (v.language or "Other") == lang],
                      key=lambda v: parse.format_rank(v.fmt))
        sess.update(step="fmt", pool=pool)
        if len(pool) == 1:
            await self.ask_theatre(chat, pool[0].ref(sess["city"]))
            return
        rows = [[{"text": (v.fmt or "Standard") + (" ⭐ default" if i == 0 else ""), "callback_data": f"fmt:{i}"}]
                for i, v in enumerate(pool)]
        await self.send(chat, f"🗣 {html.escape(lang)}\n🎞 Which format?", rows)

    async def search(self, chat: str, city: str, text: str) -> list[parse.MovieRef]:
        await self.send(chat, f"🔎 Searching BookMyShow {html.escape(city)} for “{html.escape(text)}”…")
        s = db.get_settings()
        try:
            found = await monitor.client.search_movies(city, text, headless=s.get("headless", "1") == "1")
        except Exception as e:  # noqa: BLE001
            await self.send(chat, f"Search failed ({html.escape(type(e).__name__)}). Paste the BookMyShow link instead.")
            return []
        if not found:
            await self.send(chat, "No match. Paste the movie's BookMyShow link (open the movie on in.bookmyshow.com and copy the URL).")
        return found

    async def quick_add(self, chat: str, text: str) -> None:
        parts = [p.strip() for p in text.split("|")]
        movie, city = parts[0], (parts[1] if len(parts) > 1 and parts[1] else self._city())
        theatre = parts[2] if len(parts) > 2 else ""
        city, region, warn = self.resolve_city(city)
        minutes = parts[3] if len(parts) > 3 else ""
        interval = int(float(minutes) * 60) if re.fullmatch(r"\d+(\.\d+)?", minutes or "") else 120
        if interval < monitor.MIN_INTERVAL:
            interval = monitor.MIN_INTERVAL
        ref = parse.parse_movie_input(movie, city)
        if not ref:
            ref = await self.default_version(chat, city, movie)
            if not ref:
                return
        ref.city = city
        if warn:
            await self.send(chat, warn)
        await self.create(chat, ref, theatre, interval, region)

    async def default_version(self, chat: str, city: str, query: str) -> parse.MovieRef | None:
        """Search by name and take English (or the language named in the query), standard format."""
        found = await self.search(chat, city, query)
        if not found:
            return None
        vs = await self.find_variants(chat, found, city)
        v = pick_default(vs, query)
        if not v:
            return found[0]
        others = len(vs) - 1
        await self.send(chat, f"🎞 Using <b>{html.escape(v.label)}</b>"
                              + (f" ({others} other version(s): use ✏️ Edit → 🎬 Movie, or add by name step by step)" if others else ""))
        return v.ref(city)

    async def ask_theatre(self, chat: str, ref: parse.MovieRef, region: str = "") -> None:
        self.sessions[chat] = {"step": "theatre", "ref": ref, "region": region}
        await self.send(chat, f"🎬 <b>{html.escape(ref.title)}</b>\n📍 City: <b>{html.escape(ref.city.title())}</b>\n\n"
                              "🏛️ Which theatre? Send words from its name, e.g. <code>PVR Grand Mall Velachery</code>.\n"
                              "Several: separate with commas (= any of them).",
                        [[{"text": "Any theatre in " + ref.city.title(), "callback_data": "th:any"}],
                         [{"text": "📍 Change city", "callback_data": "addcity"}, {"text": "Cancel", "callback_data": "cancel"}]])

    async def on_add_city(self, chat: str, text: str) -> None:
        sess = self.sessions.get(chat) or {}
        ref: parse.MovieRef | None = sess.get("ref")
        if not ref:
            await self.send(chat, "Let's start over: send a movie name.")
            return
        city, region, warn = self.resolve_city(text)
        ref.city = city
        if warn:
            await self.send(chat, warn)
        await self.ask_theatre(chat, ref, region)

    async def on_theatre(self, chat: str, theatre: str) -> None:
        sess = self.sessions.get(chat) or {}
        if not sess.get("ref"):
            await self.send(chat, "Let's start over: send a movie name. /add")
            return
        sess.update(step="interval", theatre=theatre)
        await self.send(chat, f"🏛️ {html.escape(theatre or 'Any theatre')}\n\n⏱ How often should I check?",
                        self.interval_buttons("ni"))

    def interval_buttons(self, prefix: str) -> Markup:
        mins = [1, 2, 3, 5, 10, 15, 30]
        row = lambda ms: [{"text": f"{m} min", "callback_data": f"{prefix}:{m * 60}"} for m in ms]  # noqa: E731
        return [row(mins[:4]), row(mins[4:])]

    async def create(self, chat: str, ref: parse.MovieRef, theatre: str, interval: int, region: str = "") -> None:
        self.sessions.pop(chat, None)
        wid = db.create_watch(title=ref.title, city=ref.city, slug=ref.slug, event_code=ref.event_code,
                              theatre=theatre, interval_sec=interval, region=region)
        db.log_event(wid, "info", f"Added from Telegram: {ref.title} ({ref.event_code}) in {ref.city} @ {theatre or 'any theatre'}")
        await self.send(chat, f"✅ Watching <b>{html.escape(ref.title)}</b> in <b>{html.escape(ref.city.title())}</b> @ "
                              f"<b>{html.escape(theatre or 'any theatre')}</b>, {self.every(interval)}. Checking now…")
        w = await monitor.run_check(db.get_watch(wid))
        await self.send(chat, self.describe(w), self.watch_buttons(w))

    @staticmethod
    def resolve_city(text: str) -> tuple[str, str, str]:
        """'madurai' → ('madurai', '', '') ; 'salem SALM' → ('salem', 'SALM', '') ; unknown → warning."""
        bits = text.strip().split()
        region = ""
        if len(bits) > 1 and re.fullmatch(r"[A-Za-z]{3,6}", bits[-1]) and bits[-1].isupper():
            region = bits.pop()
        city = "-".join(b.lower() for b in bits) or "chennai"
        city = {"bangalore": "bengaluru", "delhi": "delhi-ncr", "trichy": "tiruchirappalli"}.get(city, city)
        warn = ""
        if city not in parse.REGIONS and not region:
            warn = (f"⚠️ I don't know BookMyShow's region code for <b>{html.escape(city)}</b>, so I'll guess. "
                    "If checks never show dates, send the city again with its code, e.g. <code>salem SALM</code>.")
        return city, region, warn

    # ------------------------------------------------------------ watch cards
    @staticmethod
    def every(sec: int) -> str:
        jit = int(db.get_settings().get("jitter_sec") or 0)
        m = sec / 60
        return f"every {m:g} min" + (f" (±{jit}s)" if jit else "")

    def describe(self, w: dict) -> str:
        return messages.card(w, int(db.get_settings().get("jitter_sec") or 0))

    def watch_buttons(self, w: dict) -> Markup:
        url = (w.get("last_result") or {}).get("booking_url")
        book = [[{"text": "🎟 Book on BookMyShow", "url": url}]] if w["status"] == "open" and url else []
        return book + [[{"text": "🔄 Check", "callback_data": f"chk:{w['id']}"},
                 {"text": "✏️ Edit", "callback_data": f"edit:{w['id']}"}],
                [{"text": "⏸ Pause" if w["active"] else "▶️ Resume", "callback_data": f"tog:{w['id']}"},
                 {"text": "🗑 Delete", "callback_data": f"del:{w['id']}"}]]

    def edit_buttons(self, w: dict) -> Markup:
        i = w["id"]
        return [[{"text": "🏛 Theatre", "callback_data": f"e:{i}:th"}, {"text": "📍 City", "callback_data": f"e:{i}:ci"}],
                [{"text": "⏱ Timing", "callback_data": f"e:{i}:iv"}, {"text": "🎬 Movie", "callback_data": f"e:{i}:mv"}],
                [{"text": "📅 Dates", "callback_data": f"e:{i}:dt"}, {"text": "🕑 Show time", "callback_data": f"e:{i}:pt"}],
                [{"text": "✔️ Done", "callback_data": f"done:{i}"}]]

    def period_buttons(self, w: dict) -> Markup:
        on = set(filter(None, w["periods"].split(",")))
        names = {"morning": "Morning 6–12", "afternoon": "Afternoon 12–4", "evening": "Evening 4–7", "night": "Night 7–12"}
        rows = [[{"text": ("✅ " if p in on else "▫️ ") + label, "callback_data": f"pt:{w['id']}:{p}"}]
                for p, label in names.items()]
        return rows + [[{"text": "Any time", "callback_data": f"pt:{w['id']}:any"},
                        {"text": "✔️ Done", "callback_data": f"edit:{w['id']}"}]]

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

    # ------------------------------------------------------------ editing
    async def apply(self, chat: str, wid: int, recheck: bool, **fields: Any) -> None:
        """Save changes; filter changes reset alert memory so the next check reports afresh."""
        if recheck:
            fields.update(state={}, status="pending", status_detail="Changed, re-checking")
        fields["next_check"] = None
        db.update_watch(wid, **fields)
        w = db.get_watch(wid)
        if recheck and w["active"]:
            w = await monitor.run_check(w)
        await self.send(chat, "Saved.\n\n" + self.describe(w), self.watch_buttons(w))

    async def on_edit_text(self, chat: str, sess: dict, text: str) -> None:
        self.sessions.pop(chat, None)
        wid, field = sess["wid"], sess["field"]
        w = db.get_watch(wid)
        if not w:
            await self.send(chat, "That watch no longer exists.")
            return
        if field == "th":
            await self.apply(chat, wid, True, theatre="" if text.lower() in ("any", "all", "-") else text)
        elif field == "ci":
            city, region, warn = self.resolve_city(text)
            if warn:
                await self.send(chat, warn)
            await self.apply(chat, wid, True, city=city, region=region)
        elif field == "dt":
            dates = parse_dates_text(text)
            if dates is None:
                self.sessions[chat] = sess
                await self.send(chat, "Couldn't read those dates. Examples: <code>18 dec, 19 dec</code> · "
                                      "<code>18/12</code> · <code>20261218</code> · <code>any</code>")
                return
            await self.apply(chat, wid, True, dates=dates)
        elif field == "mv":
            ref = parse.parse_movie_input(text, w["city"]) or await self.default_version(chat, w["city"], text)
            if not ref:
                return
            await self.apply(chat, wid, True, title=ref.title, slug=ref.slug, event_code=ref.event_code)
        elif field == "iv":
            m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes)?\s*", text.lower())
            if not m:
                self.sessions[chat] = sess
                await self.send(chat, "Send minutes as a number, e.g. <code>5</code>.")
                return
            await self.apply(chat, wid, False, interval_sec=max(monitor.MIN_INTERVAL, int(float(m.group(1)) * 60)))

    async def ask_edit(self, chat: str, w: dict, field: str) -> None:
        i = w["id"]
        if field == "iv":
            await self.send(chat, f"⏱ Now {self.every(w['interval_sec'])}. Pick one, or send minutes (e.g. <code>4</code>):",
                            self.interval_buttons(f"iv:{i}"))
            self.sessions[chat] = {"step": "edit", "wid": i, "field": "iv"}
        elif field == "pt":
            await self.send(chat, "🕑 Which show times? Tap to toggle.", self.period_buttons(w))
        elif field == "th":
            venues = (w.get("last_result") or {}).get("all_venues") or []
            self.sessions[chat] = {"step": "edit", "wid": i, "field": "th", "venues": venues[:12]}
            rows = [[{"text": v[:60], "callback_data": f"tv:{i}:{k}"}] for k, v in enumerate(venues[:12])]
            rows.append([{"text": "Any theatre", "callback_data": f"tv:{i}:any"}])
            await self.send(chat, f"🏛 Now: <b>{html.escape(w['theatre'] or 'any theatre')}</b>\n"
                                  "Send words from the theatre name" + (", or pick one showing it now:" if venues else ":"), rows)
        else:
            prompts = {"ci": f"📍 Now <b>{html.escape(w['city'].title())}</b>. Send the city, e.g. <code>madurai</code>.",
                       "mv": "🎬 Send the movie name (add a language for a dub, e.g. <code>doomsday tamil</code>) or its BookMyShow link.",
                       "dt": "📅 Send dates (e.g. <code>18 dec, 19 dec</code>), or <code>any</code> for all dates."}
            self.sessions[chat] = {"step": "edit", "wid": i, "field": field}
            await self.send(chat, prompts[field])

    async def on_button(self, chat: str, data: str) -> None:
        parts = data.split(":")
        kind = parts[0]
        if kind == "pick":
            sess = self.sessions.get(chat) or {}
            cands = sess.get("cands") or []
            if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) < len(cands):
                await self.ask_theatre(chat, cands[int(parts[1])])
            else:
                await self.send(chat, "That list expired; send the movie name again.")
            return
        if kind in ("lang", "fmt"):
            sess = self.sessions.get(chat) or {}
            idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
            if kind == "lang" and sess.get("step") == "lang" and 0 <= idx < len(sess.get("langs", [])):
                await self.ask_format(chat, sess["langs"][idx])
            elif kind == "fmt" and sess.get("step") == "fmt" and 0 <= idx < len(sess.get("pool", [])):
                await self.ask_theatre(chat, sess["pool"][idx].ref(sess["city"]))
            else:
                await self.send(chat, "That choice expired; send the movie name again.")
            return
        if kind == "th":
            await self.on_theatre(chat, "")
            return
        if kind == "addcity":
            sess = self.sessions.get(chat) or {}
            if sess.get("ref"):
                sess["step"] = "city"
                await self.send(chat, "📍 Which city? e.g. <code>madurai</code>")
            return
        if kind == "ni":  # interval chosen while adding
            sess = self.sessions.get(chat) or {}
            if sess.get("ref") and parts[1].isdigit():
                await self.create(chat, sess["ref"], sess.get("theatre", ""), int(parts[1]), sess.get("region", ""))
            else:
                await self.send(chat, "That expired; send the movie again.")
            return
        if kind == "cancel":
            self.sessions.pop(chat, None)
            await self.send(chat, "Cancelled.")
            return
        if len(parts) < 2 or not parts[1].isdigit():
            return
        w = db.get_watch(int(parts[1]))
        if not w:
            await self.send(chat, "That watch no longer exists.")
            return
        wid = w["id"]
        if kind == "chk":
            w = await monitor.run_check(w)
            await self.send(chat, self.describe(w), self.watch_buttons(w))
        elif kind == "tog":
            db.update_watch(wid, active=not w["active"], next_check=None)
            w = db.get_watch(wid)
            await self.send(chat, self.describe(w), self.watch_buttons(w))
        elif kind == "del":
            await self.send(chat, f"Delete <b>{html.escape(w['title'])}</b> @ {html.escape(w['theatre'] or 'any theatre')}?",
                            [[{"text": "🗑 Yes, delete", "callback_data": f"delok:{wid}"},
                              {"text": "Keep", "callback_data": f"done:{wid}"}]])
        elif kind == "delok":
            db.delete_watch(wid)
            await self.send(chat, f"🗑 Deleted <b>{html.escape(w['title'])}</b> @ {html.escape(w['theatre'] or 'any theatre')}.")
        elif kind == "edit":
            self.sessions.pop(chat, None)
            name, version = messages.movie_and_version(w)
            await self.send(chat, f"✏️ <b>Edit #{wid}</b> · {html.escape(name)}"
                                  + (f"\n<i>{html.escape(version)}</i>" if version else "")
                                  + f"\n📍 {html.escape(w['theatre'] or 'Any theatre')} · {html.escape(w['city'].title())}"
                                  "\n\nWhat do you want to change?", self.edit_buttons(w))
        elif kind == "done":
            self.sessions.pop(chat, None)
            await self.send(chat, self.describe(w), self.watch_buttons(w))
        elif kind == "e" and len(parts) > 2:
            await self.ask_edit(chat, w, parts[2])
        elif kind == "iv" and len(parts) > 2 and parts[2].isdigit():
            self.sessions.pop(chat, None)
            await self.apply(chat, wid, False, interval_sec=int(parts[2]))
        elif kind == "pt" and len(parts) > 2:
            on = set(filter(None, w["periods"].split(",")))
            if parts[2] == "any":
                on = set()
            else:
                on ^= {parts[2]}
            order = ["morning", "afternoon", "evening", "night"]
            db.update_watch(wid, periods=",".join(p for p in order if p in on), state={}, next_check=None)
            await self.send(chat, "🕑 " + (", ".join(p for p in order if p in on) or "Any time"),
                            self.period_buttons(db.get_watch(wid)))
        elif kind == "tv" and len(parts) > 2:
            sess = self.sessions.pop(chat, None) or {}
            venues = sess.get("venues") or (w.get("last_result") or {}).get("all_venues") or []
            if parts[2] == "any":
                await self.apply(chat, wid, True, theatre="")
            elif parts[2].isdigit() and int(parts[2]) < len(venues):
                await self.apply(chat, wid, True, theatre=venues[int(parts[2])])


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_dates_text(text: str, today: date | None = None) -> str | None:
    """'18 dec, 19/12, 20261220' → '20261218,20261219,20261220'; 'any' → ''; unreadable → None."""
    today = today or date.today()
    t = text.strip().lower()
    if t in ("any", "all", "clear", "none", "-"):
        return ""
    out = []
    for part in re.split(r"[,;]+|\s+and\s+", t):
        part = part.strip()
        if not part:
            continue
        m8 = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", part)
        mnum = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", part)
        mtxt = re.fullmatch(r"(\d{1,2})\s*([a-z]{3})[a-z]*(?:\s+(\d{4}))?", part) or \
            re.fullmatch(r"([a-z]{3})[a-z]*\s*(\d{1,2})(?:\s+(\d{4}))?", part)
        try:
            if m8:
                d = date(int(m8[1]), int(m8[2]), int(m8[3]))
            elif mnum:
                y = int(mnum[3]) if mnum[3] else None
                y = (2000 + y if y and y < 100 else y)
                d = _with_year(int(mnum[1]), int(mnum[2]), y, today)
            elif mtxt:
                a, b = mtxt[1], mtxt[2]
                day, mon = (int(a), b) if a.isdigit() else (int(b), a)
                if mon[:3] not in MONTHS:
                    return None
                d = _with_year(day, MONTHS[mon[:3]], int(mtxt[3]) if mtxt[3] else None, today)
            else:
                return None
        except ValueError:
            return None
        out.append(d.strftime("%Y%m%d"))
    return ",".join(sorted(set(out))) if out else None


def _with_year(day: int, month: int, year: int | None, today: date) -> date:
    if year:
        return date(year, month, day)
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


class TelegramError(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(description)
        self.code, self.description = code, description


bot = Bot()
