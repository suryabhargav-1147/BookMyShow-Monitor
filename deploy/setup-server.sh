#!/usr/bin/env bash
# Deploy BMS Monitor to the VPS as https://bms.viswa2k.in. Run ON THE LAPTOP from the repo:
#
#   ./deploy/setup-server.sh              deploy (or update)
#   ./deploy/setup-server.sh --secrets    also (re)send Telegram settings from secrets.yml
#   ./deploy/setup-server.sh --migrate    also copy this laptop's watches (data/monitor.db) to the server
#
# What it does (no root needed; your user is in the docker group):
#   1. Copies the source to ~/apps/bms-monitor on the server (data/ and .env there are kept).
#   2. Writes the server .env (mode 600) from secrets.yml over ssh stdin: never on a command line or in git.
#   3. Builds and (re)starts the container with docker compose: 127.0.0.1:30030, restarts on boot.
#   4. Checks the app; if the nginx site is not installed yet, prints the ONE root command to run.
# Only one copy may run the Telegram bot: stop the laptop service first
#   (systemctl --user disable --now bms-monitor).
#
# Settings: SERVER=vps (an ~/.ssh/config host)   APP_DIR=apps/bms-monitor (under ~)
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER="${SERVER:-vps}"
APP_DIR="${APP_DIR:-apps/bms-monitor}"
DOMAIN="bms.viswa2k.in"
PORT=30030

send_secrets=0; migrate=0
for arg in "$@"; do
  case "$arg" in
    --secrets) send_secrets=1 ;;
    --migrate) migrate=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

say() { printf '\033[1m[setup-server]\033[0m %s\n' "$*"; }
ssh "$SERVER" true || { echo "Cannot ssh to $SERVER"; exit 1; }

# 1. Source: tracked + new (not ignored) files only, so data/, secrets.yml and .env never leave the laptop
say "Copying source to $SERVER:~/$APP_DIR"
ssh "$SERVER" "mkdir -p ~/$APP_DIR/data"
git ls-files -z --cached --others --exclude-standard | rsync -az --from0 --files-from=- ./ "$SERVER:$APP_DIR/"

# 2. Secrets → server .env
if [ "$send_secrets" = 1 ] || ! ssh "$SERVER" "grep -q '^BMS_TELEGRAM_BOT_TOKEN=' ~/$APP_DIR/.env 2>/dev/null"; then
  [ -f secrets.yml ] || { echo "secrets.yml missing: copy secrets.example.yml and fill it in"; exit 1; }
  uv run --no-dev python - <<'PY' | ssh "$SERVER" "umask 077 && cat > ~/$APP_DIR/.env"
import re, secrets
v = {}
for line in open("secrets.yml"):
    m = re.match(r'^\s*([a-z_]+)\s*:\s*(.*?)\s*$', line)
    if m and not line.lstrip().startswith("#"):
        v[m.group(1)] = m.group(2).split(" #")[0].strip().strip('"').strip("'")
ids = ",".join(i.strip() for i in v.get("telegram_user_ids", "").split(",") if i.strip())
assert v.get("telegram_bot_token") and ids, "secrets.yml needs telegram_bot_token and telegram_user_ids"
print(f"BMS_TELEGRAM_BOT_TOKEN={v['telegram_bot_token']}")
print(f"BMS_TELEGRAM_CHAT_ID={ids}")
if v.get("ntfy_topic"):
    print(f"BMS_NTFY_TOPIC={v['ntfy_topic']}")
print(f"BMS_SESSION_SECRET={secrets.token_hex(32)}")
print(f"BMS_API_TOKEN={secrets.token_urlsafe(24)}")
PY
  say "Server .env written (Telegram settings, session secret, API token)"
fi

# Optional: carry over this laptop's watches (only if the server has none yet)
if [ "$migrate" = 1 ]; then
  if ssh "$SERVER" "test -s ~/$APP_DIR/data/monitor.db"; then
    say "Server already has data/monitor.db: not overwriting"
  else
    uv run --no-dev python -c "import sqlite3; s=sqlite3.connect('data/monitor.db'); d=sqlite3.connect('/tmp/bms-migrate.db'); s.backup(d); d.execute(\"DELETE FROM settings WHERE key LIKE 'telegram%'\"); d.commit()"
    scp -q /tmp/bms-migrate.db "$SERVER:$APP_DIR/data/monitor.db" && rm -f /tmp/bms-migrate.db
    say "Watches copied to the server"
  fi
fi

# 3. Build and start
say "Building and starting the container"
ssh "$SERVER" "cd ~/$APP_DIR && docker compose up -d --build 2>&1 | tail -3 && docker image prune -f >/dev/null"

# 4. Checks
say "Checking"
ssh "$SERVER" bash -s <<EOF
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "\$@"; }
for _ in \$(seq 1 30); do [ "\$(code http://127.0.0.1:$PORT/healthz)" = 200 ] && break; sleep 2; done
echo "  app:                 \$(code http://127.0.0.1:$PORT/healthz) (expect 200)"
echo "  container:           \$(docker inspect -f '{{.State.Status}}, restart={{.HostConfig.RestartPolicy.Name}}' bms-monitor)"
echo "  sign-in required:    \$(code -H 'Host: $DOMAIN' -H 'X-Forwarded-For: 1.2.3.4' http://127.0.0.1:$PORT/api/v1/watches) (expect 401)"
docker logs --since 2m bms-monitor 2>&1 | grep -E "telegram-bot|ERROR" | tail -3 | sed 's/^/  log: /'
if [ -e /etc/nginx/sites-enabled/$DOMAIN.conf ]; then
  echo "  nginx site:          \$(code -H 'Host: $DOMAIN' http://127.0.0.1/) (expect 303)"
else
  echo
  echo "  The nginx site is not installed yet. On the server run (asks your sudo password):"
  echo "      ssh $SERVER"
  echo "      sudo bash ~/$APP_DIR/deploy/server-root.sh"
fi
EOF
say "Done: https://$DOMAIN"
