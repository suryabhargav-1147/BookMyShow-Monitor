"""Pure parsing + matching helpers (no network) so they are unit-testable.

Response shape follows BookMyShow's web endpoint
`/api/movies-data/v4/showtimes-by-event/primary-dynamic` (same one used by
github.com/aviiciii/bms-ticket-notifier).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

BASE = "https://in.bookmyshow.com"

AVAIL = {"0": "SOLD OUT", "1": "ALMOST FULL", "2": "FILLING FAST", "3": "AVAILABLE"}
DATE_STYLE = {"date-selected": "BOOKABLE", "date-default": "BOOKABLE", "date-disabled": "NOT_OPEN"}
TIME_PERIODS = {"morning": (600, 1200), "afternoon": (1200, 1600), "evening": (1600, 1900), "night": (1900, 2400)}

# city slug -> (regionCode, regionSlug, lat, lon, geohash)
REGIONS = {
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
    # added later; region codes not yet confirmed against a live check
    "madurai": ("MADU", "madurai", "9.925", "78.120", ""),
    "coimbatore": ("COIM", "coimbatore", "11.017", "76.956", ""),
}


def geohash(lat: float, lon: float, precision: int = 3) -> str:
    """Standard geohash (BookMyShow sends a 3-character one in x-geohash)."""
    chars, bits, out = "0123456789bcdefghjkmnpqrstuvwxyz", [16, 8, 4, 2, 1], []
    lat_r, lon_r, even, bit, ch = [-90.0, 90.0], [-180.0, 180.0], True, 0, 0
    while len(out) < precision:
        rng, val = (lon_r, lon) if even else (lat_r, lat)
        mid = (rng[0] + rng[1]) / 2
        if val > mid:
            ch |= bits[bit]
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(chars[ch])
            bit, ch = 0, 0
    return "".join(out)


for _k, _v in list(REGIONS.items()):
    if not _v[4]:
        REGIONS[_k] = (*_v[:4], geohash(float(_v[2]), float(_v[3])))

MOVIE_PATH = re.compile(r"/movies/(?:(?P<city>[a-z0-9-]+)/)?(?P<slug>[a-z0-9-]+)/(?:buytickets/)?(?P<code>ET\d{8})")
STOPWORDS = {"mall", "the", "cinema", "cinemas", "theatre", "theater", "multiplex", "screen", "screens",
             "and", "at", "in", "of"}


# ---------------------------------------------------------------- data classes
@dataclass
class MovieRef:
    title: str
    city: str
    slug: str
    event_code: str

    @property
    def url(self) -> str:
        return f"{BASE}/movies/{self.city}/{self.slug}/{self.event_code}"


@dataclass
class Category:
    name: str
    price: str
    status: str  # availStatus code "0".."3"

    @property
    def label(self) -> str:
        return AVAIL.get(self.status, "UNKNOWN")


@dataclass
class Show:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[Category] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return any(c.status != "0" for c in self.categories) if self.categories else True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["available"] = self.available
        for c in d["categories"]:
            c["label"] = AVAIL.get(c["status"], "UNKNOWN")
        return d


@dataclass
class DateChip:
    date_code: str
    status: str  # BOOKABLE | NOT_OPEN | UNKNOWN


# ---------------------------------------------------------------- inputs
def region_for(city: str, override: str = "") -> tuple[str, str, str, str, str]:
    key = (city or "").lower().strip()
    reg = REGIONS.get(key, (key.upper()[:6], key, "0", "0", ""))
    if override:
        reg = (override.upper(), *reg[1:])
    return reg


def title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-") if w)


def parse_movie_input(text: str, default_city: str) -> MovieRef | None:
    """A BookMyShow URL (movie or buytickets page) or a bare ET code. None for a free-text name."""
    text = (text or "").strip()
    if re.fullmatch(r"ET\d{8}", text, re.I):
        return MovieRef(text.upper(), default_city, "movie", text.upper())
    if "bookmyshow.com" in text or text.startswith("/movies/"):
        path = urlparse(text if "://" in text else "https://" + text.lstrip("/")).path
        m = MOVIE_PATH.search(path)
        if m:
            return MovieRef(title_from_slug(m.group("slug")), m.group("city") or default_city,
                            m.group("slug"), m.group("code"))
    return None


def movie_links_from_html(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r'href="([^"]*?/movies/[^"]*?ET\d{8}[^"]*)"', html):
        out.append({"href": m.group(1)})
    return out


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def tokens(s: str) -> list[str]:
    return [t for t in norm(s).split() if t and t not in STOPWORDS]


def rank_movies(links: list[dict], city: str, query: str, limit: int = 12) -> list[MovieRef]:
    q = tokens(query)
    best: dict[str, tuple[float, MovieRef]] = {}
    for link in links:
        m = MOVIE_PATH.search(urlparse(link.get("href", "")).path)
        if not m:
            continue
        slug, code = m.group("slug"), m.group("code")
        hay = tokens(slug.replace("-", " ") + " " + (link.get("text") or ""))
        score = (sum(1 for t in q if any(h.startswith(t) for h in hay)) / len(q)) if q else 0.0
        if q and score == 0:
            continue
        ref = MovieRef(title_from_slug(slug), city, slug, code)
        if code not in best or score > best[code][0]:
            best[code] = (score, ref)
    return [r for _, r in sorted(best.values(), key=lambda x: (-x[0], len(x[1].slug)))][:limit]


# ---------------------------------------------------------------- API response parsers
def _data(payload: dict) -> dict:
    return (payload or {}).get("data") or {}


def parse_movie_info(payload: dict) -> dict:
    info = {"name": "", "language": ""}
    d = _data(payload)
    for w in d.get("topStickyWidgets", []) or []:
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []) or []:
                for row in (item.get("leftText") or {}).get("data", []) or []:
                    for c in row.get("components", []) or []:
                        if "•" in (c.get("text") or ""):
                            info["language"] = c["text"].strip()
    bs = d.get("bottomSheetData") or {}
    for w in (bs.get("format-selector") or {}).get("widgets", []) or []:
        if w.get("type") == "vertical-text-list":
            for item in w.get("data", []) or []:
                if item.get("styleId") == "bottomsheet-subtitle" and item.get("text"):
                    info["name"] = item["text"]
    return info


def parse_dates(payload: dict) -> list[DateChip]:
    chips = []
    for w in _data(payload).get("topStickyWidgets", []) or []:
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []) or []:
            code = str(item.get("id", ""))
            if re.fullmatch(r"20\d{6}", code):
                chips.append(DateChip(code, DATE_STYLE.get(item.get("styleId", ""), "UNKNOWN")))
    return chips


def parse_shows(payload: dict) -> list[Show]:
    shows = []
    for w in _data(payload).get("showtimeWidgets", []) or []:
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []) or []:
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []) or []:
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData") or {}
                for st in card.get("showtimes", []) or []:
                    sa = st.get("additionalData") or {}
                    date_code = str(sa.get("showDateCode") or sa.get("dateCode") or "").strip()
                    if not date_code and re.match(r"^\d{8}", sa.get("cutOffDateTime", "") or ""):
                        date_code = sa["cutOffDateTime"][:8]
                    show = Show(
                        venue_code=addl.get("venueCode", ""), venue_name=addl.get("venueName", "Unknown"),
                        session_id=str(sa.get("sessionId", "")), date_code=date_code,
                        time=st.get("title", ""), time_code=str(sa.get("showTimeCode", "")),
                        screen_attr=st.get("screenAttr", "") or sa.get("attributes", "") or "",
                    )
                    for cat in sa.get("categories", []) or []:
                        show.categories.append(Category(cat.get("priceDesc", ""), str(cat.get("curPrice", "0")),
                                                        str(cat.get("availStatus", ""))))
                    shows.append(show)
    return shows


def parse_format_codes(payload: dict, own_code: str) -> list[str]:
    """Other event codes (IMAX/3D/language variants) mentioned in the format picker."""
    raw = json.dumps(_data(payload).get("bottomSheetData") or {})
    return sorted({c for c in re.findall(r"ET\d{8}", raw) if c != own_code})


# ---------------------------------------------------------------- filtering
def theatre_matches(name: str, theatre_filter: str) -> bool:
    """Comma-separated alternatives; inside one alternative every word must appear in the venue name.

    'PVR Phoenix mall Velachery' matches 'PVR: Phoenix MarketCity, Velachery'.
    'PVR, INOX' matches any PVR or INOX venue."""
    alts = [a for a in (theatre_filter or "").split(",") if a.strip()]
    if not alts:
        return True
    hay = tokens(name)
    for alt in alts:
        q = tokens(alt)
        if q and all(any(h.startswith(t) or (len(h) >= 4 and t.startswith(h)) for h in hay) for t in q):
            return True
    return False


def filter_shows(shows: list[Show], theatre: str = "", periods: str = "", dates: str = "") -> list[Show]:
    pset = [p.strip().lower() for p in (periods or "").split(",") if p.strip()]
    dset = {d.strip() for d in (dates or "").split(",") if d.strip()}
    out = []
    for s in shows:
        if not theatre_matches(s.venue_name, theatre):
            continue
        if dset and s.date_code and s.date_code not in dset:
            continue
        if pset:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = -1
            if not any(lo <= tc < hi for p in pset if p in TIME_PERIODS for lo, hi in [TIME_PERIODS[p]]):
                continue
        out.append(s)
    return out


# ---------------------------------------------------------------- change detection
def build_state(shows: list[Show], dates: list[DateChip]) -> dict:
    st: dict[str, dict] = {}
    for s in shows:
        cats = s.categories or [Category("", "", "3")]
        for c in cats:
            st[f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"] = {
                "venue": s.venue_name, "time": s.time, "date": s.date_code, "cat": c.name,
                "price": c.price, "status": c.status, "attr": s.screen_attr,
            }
    return {"shows": st, "dates": {d.date_code: d.status for d in dates}}


def detect_change_items(old: dict, new: dict) -> list[dict]:
    """Structured changes since the previous check. An empty `old` (first check) reports nothing;
    the monitor sends the first-opening alert itself.

    kinds: date (a date became bookable), new (a show appeared), back (sold out → seats again)."""
    if not old:
        return []
    items: list[dict] = []
    od, nd = old.get("dates", {}), new.get("dates", {})
    if od:
        for dc, status in sorted(nd.items()):
            if status == "BOOKABLE" and od.get(dc) in ("NOT_OPEN", None):
                items.append({"kind": "date", "date": dc})
    os_, ns = old.get("shows", {}), new.get("shows", {})
    seen_sessions = set()
    for key in sorted(set(ns) - set(os_)):
        sess = key.rsplit("|", 1)[0]
        if sess in seen_sessions or any(k.rsplit("|", 1)[0] == sess for k in os_):
            continue  # one item per show, and a new seat class on a known show isn't a new show
        seen_sessions.add(sess)
        v = ns[key]
        items.append({"kind": "new", "venue": v["venue"], "date": v["date"], "time": v["time"], "attr": v.get("attr", "")})
    for key, v in ns.items():
        o = os_.get(key)
        if o and o["status"] == "0" and v["status"] != "0":
            items.append({"kind": "back", "venue": v["venue"], "date": v["date"], "time": v["time"],
                          "cat": v["cat"], "status": v["status"]})
    return items


def detect_changes(old: dict, new: dict) -> list[str]:
    """Same as detect_change_items, as one readable line per change."""
    out = []
    for c in detect_change_items(old, new):
        if c["kind"] == "date":
            out.append(f"📅 Date opened: {fmt_date(c['date'])}")
        elif c["kind"] == "new":
            attr = f" [{c['attr']}]" if c.get("attr") else ""
            out.append(f"🆕 New show: {c['venue']}, {fmt_date(c['date'])} {c['time']}{attr}")
        else:
            out.append(f"🟢 Back in stock: {c['venue']}, {fmt_date(c['date'])} {c['time']}: "
                       f"{c['cat']} → {AVAIL.get(c['status'], '?')}")
    return out


def fmt_date(d: str) -> str:
    return f"{d[6:8]}/{d[4:6]}" if d and len(d) == 8 else (d or "")


def group_by_venue(shows: list[Show]) -> dict[str, list[Show]]:
    g: dict[str, list[Show]] = {}
    for s in shows:
        g.setdefault(s.venue_name, []).append(s)
    return g


# ---------------------------------------------------------------- languages / formats (each is its own ET code)
LANGUAGES = ["english", "tamil", "telugu", "hindi", "malayalam", "kannada", "bengali", "marathi", "punjabi", "gujarati"]
PREMIUM = ("imax", "4dx", "msinfinity", "ms-infinity", "infinity", "screenx", "dolby", "epiq", "icon", "playhouse")


def split_language(lang_str: str) -> tuple[str, str]:
    """'English  •  MS-Infinity Vsn 3d' → ('English', 'MS-Infinity Vsn 3d')."""
    parts = [p.strip() for p in re.split(r"[•·|]", lang_str or "") if p.strip()]
    return (parts[0] if parts else ""), ", ".join(parts[1:])


def language_in(text: str) -> str:
    """A language named in a slug or query ('...-3dtamil', 'doomsday tamil') → 'Tamil', else ''."""
    t = (text or "").lower()
    return next((lang.title() for lang in LANGUAGES if lang in t), "")


def format_rank(fmt: str) -> tuple[int, str]:
    """Standard formats first: 2D, then 3D, then premium screens."""
    f = (fmt or "").lower()
    premium = any(k in f.replace(" ", "") for k in PREMIUM)
    return (2 if premium else 1 if "3d" in f and "2d" not in f else 0), f


# ---------------------------------------------------------------- human date summary
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def date_ranges(codes: list[str]) -> str:
    """['20261217','20261218','20261219','20261223'] → '17–19 Dec, 23 Dec'."""
    from datetime import date, timedelta
    ds = sorted({date(int(c[:4]), int(c[4:6]), int(c[6:8])) for c in codes if len(c) == 8 and c.isdigit()})
    runs: list[list] = []
    for d in ds:
        if runs and d - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])
    out = []
    for r in runs:
        a, b = r[0], r[-1]
        if a == b:
            out.append(f"{a.day} {_MON[a.month - 1]}")
        elif a.month == b.month:
            out.append(f"{a.day}–{b.day} {_MON[a.month - 1]}")
        else:
            out.append(f"{a.day} {_MON[a.month - 1]} – {b.day} {_MON[b.month - 1]}")
    return ", ".join(out)


def date_summary(result: dict, theatre: str = "", dates_filter: str = "") -> list[str]:
    """What's open and what we're still waiting for, from a check result.

    → ['📅 Booking open: 17–20 Dec (at your theatre: 17, 18 Dec)', '👀 Watching for: 21 Dec onwards']"""
    chips = result.get("dates") or []
    open_dates = [c["date_code"] for c in chips if c.get("status") == "BOOKABLE"]
    soon = [c["date_code"] for c in chips if c.get("status") == "NOT_OPEN"]
    at_theatre = sorted({s["date_code"] for s in result.get("shows") or [] if s.get("date_code")})
    wanted = [d for d in (dates_filter or "").split(",") if d]
    scanned = result.get("scanned")
    unchecked = [d for d in open_dates if scanned is not None and d not in scanned
                 and (not wanted or d in wanted)]
    lines = []
    if open_dates:
        line = f"📅 Booking open: {date_ranges(open_dates)}"
        if theatre:
            line += f" (at your theatre: {date_ranges(at_theatre)})" if at_theatre else " (not at your theatre yet)"
        lines.append(line)
        if unchecked:
            lines.append(f"⚠️ Not checked this time: {date_ranges(unchecked)} (raise “dates to scan” in settings)")
    if wanted:
        waiting = [d for d in wanted if d not in at_theatre]
        lines.append(f"👀 Watching for: {date_ranges(waiting)}" if waiting else "✅ All your dates are open")
    elif open_dates:
        later = [d for d in soon if d > max(open_dates)]
        if theatre and not at_theatre:
            lines.append("👀 Watching for: your theatre to open any date")
        else:
            lines.append(f"👀 Watching for: {date_ranges(later[:1])} onwards (new dates)" if later
                         else "👀 Watching for: new dates and freed-up seats")
    else:
        lines.append("👀 Watching for: booking to open (any date)")
    return lines
