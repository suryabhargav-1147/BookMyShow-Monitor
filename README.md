# BookMyShow Monitor

Watches BookMyShow and alerts you on **Telegram**, **ntfy** (phone push) and a **live dashboard** when:

- tickets open for a movie in your city, or at **one specific theatre**
- a new date opens, or a new show is added
- a sold-out show gets seats back

It also exposes everything as a REST API (OpenAPI), a one-shot `run-sync` endpoint and an MCP server, so you can use it from scripts, cron, n8n or Home Assistant, or have Claude manage it.

## Quick start

```bash
uv sync
uv run playwright install chromium   # only used as a fallback when Cloudflare blocks plain requests
uv run bms-monitor                   # http://127.0.0.1:8642
```

1. **Integrations** tab: paste a Telegram bot token from @BotFather, message your bot, then click **Detect chat ID** and **Send test**.
2. **Input** tab: paste the movie URL, e.g. `https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706`, or type a name and click **Find**.
   - Theatre (optional): `PVR Phoenix Velachery`. Every word must appear in the venue name, and commas separate alternatives.
   - Optional filters: dates (`20261218,20261219`) and time of day (morning, afternoon, evening, night).
3. Click **Save & start monitoring**. Monitors are checked every 1–30 min (your choice).

Not sure of the exact theatre name? Create the monitor without one, open **Details** once the city opens, and click a venue to use it as the filter.

## Chat with the Telegram bot

While `bms-monitor` runs, your bot also takes commands. Only the user IDs in your settings can use it.

- Send a **movie name** or a **BookMyShow link**. It asks for the theatre (or "any theatre"), starts watching (checks every 2 min), and checks once right away.
- `/list` shows your watches with 🔄 Check, ⏸ Pause and 🗑 Delete buttons.
- `/check` checks everything now.
- `/city mumbai` changes the default city.

Run it as a service so it's always on (Linux):

```bash
systemctl --user enable --now bms-monitor   # unit file: ~/.config/systemd/user/bms-monitor.service
journalctl --user -u bms-monitor -f        # logs
```

## Run free on GitHub Actions (no PC needed)

`.github/workflows/bms-monitor.yml` runs `bms-check` every 5 minutes. Each run:
1. checks BookMyShow once
2. alerts on Telegram / ntfy if something changed
3. commits `state/bms_state.json`, but only when something changed

Each run's summary page shows a table of the matching shows.

Configure it in the repo under **Settings → Secrets and variables → Actions**.

| Kind | Name | Example |
|---|---|---|
| Variable | `BMS_MOVIE` | `https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706` |
| Variable | `BMS_CITY` | `chennai` |
| Variable | `BMS_THEATRE` | `PVR Grand Mall Velachery` (empty = any theatre) |
| Variable | `BMS_DATES` / `BMS_TIME` | `20261218` / `evening,night` (optional) |
| Secret | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | from @BotFather / the dashboard's *Detect chat ID* |
| Secret | `NTFY_TOPIC` | subscribe to it in the ntfy app |

Manage it with `gh`:

```bash
gh workflow run bms-monitor.yml -f notify_test=true   # run now + send a test alert
gh run list --workflow bms-monitor.yml --limit 5      # recent runs
gh run view --log                                     # log of the latest run
gh variable set BMS_THEATRE --body "PVR Grand Mall Velachery"
gh workflow disable bms-monitor.yml                   # pause (enable to resume)
```

Things to know:
- Scheduled runs often start 5–15 min late.
- Plain HTTP only: there's no browser fallback in CI. If Cloudflare blocks GitHub's IPs, runs report `blocked` and you get one alert about it.
- Public repos get unlimited free minutes. A private repo at every 5 min would use about 8,600 min/month, so use `*/30` there.

## How it works

It calls the same JSON endpoint the BookMyShow buytickets page uses (`/api/movies-data/v4/showtimes-by-event/primary-dynamic`; approach from [aviiciii/bms-ticket-notifier](https://github.com/aviiciii/bms-ticket-notifier)). From that it reads:

- date chips (open or not open yet)
- venues and showtimes
- seat classes with prices and availability

It filters by your theatre, dates and times, compares with the previous check, and alerts on anything new. If Cloudflare challenges plain HTTP, it opens the page in a real Chromium window (persistent profile in `data/browser-profile`) and reads the same data. If even that is challenged, untick **Headless** in Integrations and solve the check once in the window that opens.

Built-in cities: chennai, mumbai, delhi-ncr, bengaluru, hyderabad, kolkata, pune, kochi. For other cities, set **Region code** on the Input tab.

## API (use it anywhere)

Interactive docs are at `/docs`.

```bash
# one-shot check (no monitor saved)
curl -X POST localhost:8642/api/v1/run-sync -H 'Content-Type: application/json' \
  -d '{"movie":"ET00439706","city":"chennai","theatre":"PVR Phoenix Velachery"}'

# start monitoring
curl -X POST localhost:8642/api/v1/watches -H 'Content-Type: application/json' \
  -d '{"movie":"https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706","theatre":"PVR Phoenix Velachery","interval_sec":180}'

curl localhost:8642/api/v1/dataset      # every matching show
curl localhost:8642/api/v1/stream       # live events (SSE)
```

To reach it from other machines, run with `HOST=0.0.0.0 BMS_API_TOKEN=<secret>`. Remote clients must then send `Authorization: Bearer <secret>`; localhost is always trusted.

### MCP (Claude and other agents)

```bash
claude mcp add bms-monitor -- uv --directory /path/to/BookMyShow-Monitor run bms-mcp
```

Tools: `list_watches`, `add_watch`, `check_now`, `set_active`, `remove_watch`, `check_showtimes`, `search_movies`, `recent_activity`. The monitor server must be running. Set `BMS_MONITOR_URL` and `BMS_API_TOKEN` if it runs elsewhere.

### Docker

```bash
docker build -t bms-monitor .
docker run -d -p 8642:8642 -v bms-data:/data -e BMS_API_TOKEN=secret bms-monitor
```

## Configuration

Settings live in the dashboard. Every setting can also be forced with an env var, `BMS_<SETTING>`:

- `BMS_TELEGRAM_BOT_TOKEN`
- `BMS_TELEGRAM_CHAT_ID`
- `BMS_NTFY_TOPIC`
- `BMS_HEADLESS=0|1`
- `BMS_MAX_DATES`

Other env vars:

- `HOST`, `PORT`: listen address (default `127.0.0.1:8642`)
- `BMS_DATA_DIR`: where data is stored (default `./data`)
- `BMS_API_TOKEN`: bearer token required from remote clients

## Development

```bash
uv run pytest        # offline tests with fixture payloads
```

Be gentle: the minimum interval is 60 s, and checks are spread out. This is for personal use. Book on BookMyShow yourself.
