"""Fetching from BookMyShow.

Primary: BookMyShow's own JSON endpoint over plain HTTP (fast, light).
Fallback: when Cloudflare answers with a challenge (403 / HTML), open the
buytickets page in a real Playwright browser and read the same JSON from the
request the page itself makes. If even that is challenged, turn off "headless"
in Integrations, solve the check once in the window, and the cookies persist
in data/browser-profile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import parse
from .db import DATA_DIR
from .parse import BASE, MovieRef

log = logging.getLogger("bms")
API_URL = f"{BASE}/api/movies-data/v4/showtimes-by-event/primary-dynamic"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
PROFILE_DIR = DATA_DIR / "browser-profile"


class BlockedError(Exception):
    pass


@dataclass
class Fetched:
    payload: dict
    via: str  # http | browser


def _api_request(event_code: str, date_code: str, city: str, region_override: str = "") -> tuple[dict, dict]:
    rc, rslug, lat, lon, gh = parse.region_for(city, region_override)
    headers = {
        "User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-IN,en;q=0.9",
        "Referer": f"{BASE}/movies/{rslug}/buytickets/{event_code}/",
        "x-app-code": "WEB", "x-region-code": rc, "x-region-slug": rslug, "x-geohash": gh,
        "x-latitude": lat, "x-longitude": lon, "x-location-selection": "manual", "x-lsid": "",
    }
    params = {"eventCode": event_code, "dateCode": date_code or "", "isDesktop": "true", "regionCode": rc,
              "xLocationShared": "false", "memberId": "", "lsId": "", "subCode": "", "lat": lat, "lon": lon}
    return headers, params


class Bms:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=20, follow_redirects=True)
        self._pw = None
        self._ctx = None
        self._headless: bool | None = None
        self.browser_lock = asyncio.Lock()
        self.http_blocked = False  # sticky hint: last plain-HTTP call was challenged
        self._browser_uses = 0

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._close_browser()

    # ------------------------------------------------------------ HTTP path
    async def _fetch_http(self, event_code: str, date_code: str, city: str, region: str) -> dict:
        headers, params = _api_request(event_code, date_code, city, region)
        r = await self._http.get(API_URL, headers=headers, params=params)
        ctype = r.headers.get("content-type", "")
        if r.status_code in (403, 429, 503) or "json" not in ctype:
            raise BlockedError(f"HTTP {r.status_code} ({ctype.split(';')[0] or 'no content-type'})")
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------ browser path
    async def _ensure_browser(self, headless: bool):
        if self._ctx and self._headless == headless:
            return self._ctx
        await self._close_browser()
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=headless, locale="en-IN", timezone_id="Asia/Kolkata",
            viewport={"width": 1280, "height": 860})
        self._headless = headless
        return self._ctx

    async def _close_browser(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._ctx = self._pw = None

    async def _fetch_browser(self, event_code: str, date_code: str, city: str, slug: str,
                             region: str, headless: bool) -> dict:
        async with self.browser_lock:
            ctx = await self._ensure_browser(headless)
            page = await ctx.new_page()
            captured: dict[str, Any] = {}

            async def on_response(resp):
                if "showtimes-by-event" in resp.url and "json" in (resp.headers.get("content-type") or ""):
                    try:
                        captured["payload"] = await resp.json()
                    except Exception:  # noqa: BLE001
                        pass

            page.on("response", on_response)
            try:
                url = f"{BASE}/movies/{city}/{slug or 'movie'}/buytickets/{event_code}/{date_code}".rstrip("/")
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # wait for the page's own showtimes request; give a visible browser time for a human check
                limit = 20 if headless else 120
                for _ in range(limit):
                    if "payload" in captured:
                        break
                    await page.wait_for_timeout(1000)
                if "payload" not in captured:
                    # page didn't request it (e.g. redirected to the movie page): ask from the page's own origin
                    headers, params = _api_request(event_code, date_code, city, region)
                    headers.pop("User-Agent", None)
                    qs = str(httpx.QueryParams(params))
                    res = await page.evaluate(
                        """async ([u, h]) => { const r = await fetch(u, {headers: h, credentials: 'include'});
                           const t = await r.text(); return {status: r.status, text: t}; }""",
                        [f"{API_URL}?{qs}", headers])
                    if res["status"] != 200 or not res["text"].lstrip().startswith("{"):
                        title = await page.title()
                        raise BlockedError(f"browser fetch HTTP {res['status']} on '{title}'")
                    captured["payload"] = json.loads(res["text"])
                return captured["payload"]
            finally:
                await page.close()

    # ------------------------------------------------------------ public
    async def showtimes(self, event_code: str, date_code: str, city: str, *, slug: str = "", region: str = "",
                        browser_fallback: bool = True, headless: bool = True) -> Fetched:
        if not self.http_blocked:
            try:
                return Fetched(await self._fetch_http(event_code, date_code, city, region), "http")
            except BlockedError as e:
                log.warning("plain HTTP challenged (%s); using browser", e)
                if not browser_fallback:
                    raise
                self.http_blocked = True
        # retry plain HTTP every 10th call; Cloudflare decisions change over time
        self._browser_uses += 1
        if self._browser_uses % 10 == 0:
            self.http_blocked = False
        payload = await self._fetch_browser(event_code, date_code, city, slug, region, headless)
        return Fetched(payload, "browser")

    async def search_movies(self, city: str, query: str, *, headless: bool = True) -> list[MovieRef]:
        ref = parse.parse_movie_input(query, city)
        if ref:
            return [ref]
        html_all = ""
        for path in (f"/explore/movies-{city}", f"/explore/upcoming-movies-{city}"):
            try:
                r = await self._http.get(BASE + path, headers={"User-Agent": UA, "Accept": "text/html"})
                if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                    html_all += r.text
                    continue
            except httpx.HTTPError:
                pass
            async with self.browser_lock:  # fallback: browser
                ctx = await self._ensure_browser(headless)
                page = await ctx.new_page()
                try:
                    await page.goto(BASE + path, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(3000)
                    html_all += await page.content()
                finally:
                    await page.close()
        return parse.rank_movies(parse.movie_links_from_html(html_all), city, query)


@dataclass
class CheckResult:
    status: str  # open | not_open | blocked | error
    detail: str = ""
    via: str = ""
    movie_name: str = ""
    language: str = ""
    dates: list[dict] = field(default_factory=list)       # [{date_code, status}]
    shows: list[dict] = field(default_factory=list)       # filtered shows (Show.to_dict)
    all_venues: list[str] = field(default_factory=list)   # every venue seen (helps pick a theatre)
    formats: list[str] = field(default_factory=list)      # other ET codes in the format picker
    scanned: list[str] = field(default_factory=list)      # dates whose showtimes were actually fetched
    state: dict = field(default_factory=dict)
    booking_url: str = ""


async def check(bms: Bms, *, event_code: str, city: str, slug: str = "", theatre: str = "", dates: str = "",
                periods: str = "", region: str = "", max_dates: int = 10, browser_fallback: bool = True,
                headless: bool = True) -> CheckResult:
    """Fetch the default date, then every other bookable date (capped), filter, and build state."""
    booking_url = f"{BASE}/movies/{city}/{slug or 'movie'}/buytickets/{event_code}"
    try:
        first = await bms.showtimes(event_code, "", city, slug=slug, region=region,
                                    browser_fallback=browser_fallback, headless=headless)
    except BlockedError as e:
        return CheckResult("blocked", f"Cloudflare challenge: {e}. Turn off headless mode in Integrations "
                                      f"and solve it once in the browser window.", booking_url=booking_url)
    except Exception as e:  # noqa: BLE001
        return CheckResult("error", f"{type(e).__name__}: {e}"[:300], booking_url=booking_url)

    info = parse.parse_movie_info(first.payload)
    chips = parse.parse_dates(first.payload)
    shows = parse.parse_shows(first.payload)
    fetched_dates = {s.date_code for s in shows}
    scanned = set(fetched_dates)
    wanted = {d.strip() for d in dates.split(",") if d.strip()}
    todo = [c.date_code for c in chips if c.status == "BOOKABLE" and c.date_code not in fetched_dates
            and (not wanted or c.date_code in wanted)]
    for dc in todo[: max(0, max_dates - 1)]:
        try:
            more = await bms.showtimes(event_code, dc, city, slug=slug, region=region,
                                       browser_fallback=browser_fallback, headless=headless)
            shows += parse.parse_shows(more.payload)
            scanned.add(dc)
        except Exception as e:  # noqa: BLE001
            log.warning("date %s fetch failed: %s", dc, e)
        await asyncio.sleep(random.uniform(0.8, 2.5))

    filtered = parse.filter_shows(shows, theatre, periods, dates)
    bookable = [c for c in chips if c.status == "BOOKABLE"]
    res = CheckResult(
        status="open" if filtered else "not_open", via=first.via, movie_name=info["name"], language=info["language"],
        dates=[{"date_code": c.date_code, "status": c.status} for c in chips],
        shows=[s.to_dict() for s in filtered],
        all_venues=sorted({s.venue_name for s in shows}),
        formats=parse.parse_format_codes(first.payload, event_code),
        state=parse.build_state(filtered, chips), booking_url=booking_url, scanned=sorted(scanned),
    )
    venues = parse.group_by_venue(filtered)
    if filtered:
        res.detail = f"{len(filtered)} show(s) at {len(venues)} venue(s): " + ", ".join(list(venues)[:3])
    elif shows:
        res.detail = f"Booking open in {city.title()} ({len({s.venue_name for s in shows})} venues) but nothing matches your filters yet"
    elif bookable:
        res.detail = f"{len(bookable)} date(s) bookable but no showtimes returned"
    else:
        res.detail = "Not open for booking yet"
    return res


@dataclass
class Variant:
    event_code: str
    name: str
    language: str
    fmt: str
    slug: str

    @property
    def label(self) -> str:
        return " · ".join(filter(None, [self.language, self.fmt])) or self.event_code

    def ref(self, city: str) -> MovieRef:
        title = f"{self.name} ({self.label})" if self.label != self.event_code else self.name
        return MovieRef(title, city, self.slug, self.event_code)


async def variants(bms: Bms, refs: list[MovieRef], city: str, region: str = "", limit: int = 8) -> list[Variant]:
    """Every language/format version reachable from the search results: each found code, plus the
    sibling codes in its format picker. One light request per version (capped), spaced out."""
    queue = [(r.event_code, r.slug, r.title) for r in refs]
    seen: set[str] = set()
    out: list[Variant] = []
    while queue and len(seen) < limit:
        code, slug, title = queue.pop(0)
        if code in seen:
            continue
        seen.add(code)
        try:
            f = await bms.showtimes(code, "", city, slug=slug, region=region, browser_fallback=False)
        except Exception:  # noqa: BLE001
            out.append(Variant(code, title, parse.language_in(slug), "", slug))
            continue
        info = parse.parse_movie_info(f.payload)
        lang, fmt = parse.split_language(info["language"])
        out.append(Variant(code, info["name"] or title, lang or parse.language_in(slug), fmt, slug))
        for sib in parse.parse_format_codes(f.payload, code):
            if sib not in seen:
                queue.append((sib, slug, info["name"] or title))
        await asyncio.sleep(random.uniform(0.6, 1.5))
    return out


def pick_default(vs: list[Variant], query: str = "") -> Variant | None:
    """Language named in the query, else English, else the first; then the most standard format."""
    if not vs:
        return None
    want = parse.language_in(query) or "English"
    pool = [v for v in vs if v.language.lower() == want.lower()] or \
        [v for v in vs if v.language.lower() == "english"] or vs
    return sorted(pool, key=lambda v: parse.format_rank(v.fmt))[0]
