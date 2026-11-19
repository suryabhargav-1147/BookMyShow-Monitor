#!/usr/bin/env bash
# The only part that needs root on the VPS: the nginx site. Run ON THE SERVER:
#
#   sudo bash ~/apps/bms-monitor/deploy/server-root.sh
#
# Safe to run again: it rewrites the site from deploy/nginx/bms.conf and reloads nginx.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo bash $0"; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"   # <app>/deploy
DOMAIN="bms.viswa2k.in"
SITE="/etc/nginx/sites-available/$DOMAIN.conf"

install -m 0644 "$HERE/nginx/bms.conf" "$SITE"
ln -sfn "$SITE" "/etc/nginx/sites-enabled/$DOMAIN.conf"
nginx -t
systemctl reload nginx

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Host: $DOMAIN" "$@"; }
echo "Check: page without sign-in → $(code http://127.0.0.1/) (expect 303, to the sign-in page)"
echo "Done. Open https://$DOMAIN"
