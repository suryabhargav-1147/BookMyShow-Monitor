"""Read secrets.yml and store each value as a GitHub Actions secret (via the gh CLI).

Values are piped to `gh secret set` on stdin, so they never appear in shell history or process lists.
"""
import re
import subprocess
import sys
from pathlib import Path

MAP = {"telegram_bot_token": "TELEGRAM_BOT_TOKEN", "telegram_user_ids": "TELEGRAM_CHAT_ID", "ntfy_topic": "NTFY_TOPIC"}
path = Path(sys.argv[1] if len(sys.argv) > 1 else "secrets.yml")
if not path.exists():
    sys.exit(f"{path} not found: copy secrets.example.yml to secrets.yml and fill it in")

values = {}
for line in path.read_text().splitlines():
    m = re.match(r'^\s*([a-z_]+)\s*:\s*(.*?)\s*$', line)
    if m and not line.lstrip().startswith("#"):
        v = m.group(2).split(" #")[0].strip().strip('"').strip("'")
        values[m.group(1)] = v

token, ids = values.get("telegram_bot_token", ""), values.get("telegram_user_ids", "")
if token and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
    sys.exit("telegram_bot_token doesn't look like a BotFather token (digits:letters)")
if ids:
    ids = ",".join(i.strip() for i in ids.split(",") if i.strip())
    bad = [i for i in ids.split(",") if not re.fullmatch(r"-?\d{3,}", i)]
    if bad:
        sys.exit(f"telegram_user_ids must be numeric IDs, got: {len(bad)} invalid value(s)")
    values["telegram_user_ids"] = ids

done = []
for key, secret in MAP.items():
    v = values.get(key, "")
    if not v:
        continue
    subprocess.run(["gh", "secret", "set", secret], input=v.encode(), check=True)
    done.append(secret + (f" ({len(v.split(','))} id(s))" if key == "telegram_user_ids" else ""))
print("Set secrets:", ", ".join(done) if done else "nothing (all values empty)")
