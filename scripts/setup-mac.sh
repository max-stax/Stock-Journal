#!/bin/bash
# One-time setup: runs the Stock Journal data job on this Mac every weekday at 5:30 PM.
# Paste into Terminal:   bash <(curl -fsSL https://raw.githubusercontent.com/max-stax/Stock-Journal/main/scripts/setup-mac.sh)
set -e
JOB="$HOME/Stock-Journal-data-job"
PLIST="$HOME/Library/LaunchAgents/com.maxstax.stockjournal.plist"
REPO="github.com/max-stax/Stock-Journal.git"

echo "Stock Journal: setting up the daily data job on this Mac."
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Apple's command line tools (git + Python) are needed. A window will open to install them."
  xcode-select --install || true
  echo "When that install finishes, paste the same command again."
  exit 1
fi

echo
echo "1) Your GitHub token (the fine-grained one with Contents read/write on Stock-Journal)."
echo "   Typing is hidden: paste with Cmd+V, then press Return."
while true; do
  read -r -s -p "   Token: " TOKEN; echo
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
  [ -n "$TOKEN" ] || { echo "   Nothing was entered. Try again."; continue; }
  echo "   Got a token ending in …${TOKEN: -4} (${#TOKEN} characters). Checking it with GitHub..."
  PERM="$(curl -s -H "Authorization: Bearer $TOKEN" https://api.github.com/repos/max-stax/Stock-Journal \
    | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("permissions",{}).get("push", "bad") if "id" in d else "bad")' 2>/dev/null)"
  if [ "$PERM" = "True" ]; then echo "   Token works and can save to Stock-Journal."; break; fi
  if [ "$PERM" = "False" ]; then
    echo "   GitHub accepted the token, but it can't write to Stock-Journal."
    echo "   Edit the token on GitHub: add the Stock-Journal repo and set Contents to 'Read and write'."
  else
    echo "   GitHub rejected that token. It may be mistyped, expired, or deleted. Paste it again"
    echo "   (or generate a new one: GitHub > Settings > Developer settings > Fine-grained tokens)."
  fi
done
DEFAULT_EMAIL="$(git config --global user.email 2>/dev/null || true)"
echo
echo "2) SEC asks every data request to include a contact email. It's stored only on this Mac."
read -r -p "   Email [${DEFAULT_EMAIL}]: " EMAIL; EMAIL="${EMAIL:-$DEFAULT_EMAIL}"
[ -n "$EMAIL" ] || { echo "An email is required by SEC."; exit 1; }

mkdir -p "$JOB"
umask 077
printf 'SEC_USER_AGENT="Stock Journal personal research %s"\n' "$EMAIL" > "$JOB/config"
if [ -d "$JOB/repo/.git" ]; then
  git -C "$JOB/repo" remote set-url origin "https://x-access-token:${TOKEN}@${REPO}"
else
  git clone -q --depth 50 "https://x-access-token:${TOKEN}@${REPO}" "$JOB/repo"
fi
chmod -R go-rwx "$JOB"
cp "$JOB/repo/scripts/mac-run.sh" "$JOB/run.sh" && chmod 700 "$JOB/run.sh"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.maxstax.stockjournal</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$JOB/run.sh</string></array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>17</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>17</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>17</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>17</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>17</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$JOB/launchd.log</string>
  <key>StandardErrorPath</key><string>$JOB/launchd.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/com.maxstax.stockjournal" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "3) Test run now (about a minute)..."
bash "$JOB/run.sh"
tail -n 25 "$JOB/last-run.log"
echo
echo "Done. It runs every weekday at 5:30 PM (or as soon as the Mac wakes, if it was asleep)."
echo "Log: $JOB/last-run.log    To remove: launchctl bootout gui/\$(id -u)/com.maxstax.stockjournal"
