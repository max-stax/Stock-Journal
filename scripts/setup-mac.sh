#!/bin/bash
# Stock Journal: set up (or update) the data job on this Mac.
#   - weekdays 5:30 PM: full refresh of SEC financials
#   - every 5 minutes: fetch SEC financials for newly added stocks (does nothing if none)
# Paste into Terminal:   bash <(curl -fsSL https://raw.githubusercontent.com/max-stax/Stock-Journal/main/scripts/setup-mac.sh)
# Add --new-token to replace a saved token.
set -e
JOB="$HOME/Stock-Journal-data-job"
AGENTS="$HOME/Library/LaunchAgents"
REPO="github.com/max-stax/Stock-Journal.git"

echo "Stock Journal: setting up the data job on this Mac."
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Apple's command line tools (git + Python) are needed. A window will open to install them."
  xcode-select --install || true
  echo "When that install finishes, paste the same command again."
  exit 1
fi

check_token() {
  /usr/bin/python3 - "$1" <<'PY'
import json, sys, urllib.request
req = urllib.request.Request("https://api.github.com/repos/max-stax/Stock-Journal",
                             headers={"Authorization": "Bearer " + sys.argv[1], "User-Agent": "stock-journal-setup"})
try:
    d = json.load(urllib.request.urlopen(req, timeout=20))
    print("ok" if d.get("permissions", {}).get("push") else "readonly")
except Exception:
    print("bad")
PY
}

TOKEN=""
if [ "${1:-}" != "--new-token" ] && [ -d "$JOB/repo/.git" ]; then
  SAVED="$(git -C "$JOB/repo" remote get-url origin 2>/dev/null | sed -n 's#https://x-access-token:\([^@]*\)@.*#\1#p')"
  if [ -n "$SAVED" ] && [ "$(check_token "$SAVED")" = "ok" ]; then
    TOKEN="$SAVED"; echo "Using your saved GitHub token (ending in …${TOKEN: -4})."
  fi
fi
if [ -z "$TOKEN" ]; then
  echo
  echo "1) Your GitHub token (fine-grained, Contents read/write on Stock-Journal)."
  echo "   Typing is hidden: paste with Cmd+V, then press Return."
  while true; do
    read -r -s -p "   Token: " TOKEN; echo
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
    [ -n "$TOKEN" ] || { echo "   Nothing was entered. Try again."; continue; }
    echo "   Got a token ending in …${TOKEN: -4} (${#TOKEN} characters). Checking it with GitHub..."
    case "$(check_token "$TOKEN")" in
      ok) echo "   Token works and can save to Stock-Journal."; break;;
      readonly) echo "   That token can't write to Stock-Journal. On GitHub, add the repo with Contents: Read and write.";;
      *) echo "   GitHub rejected that token (mistyped, expired, or deleted). Paste it again or make a new one.";;
    esac
  done
fi

if [ -f "$JOB/config" ]; then
  echo "Using your saved SEC contact email."
else
  DEFAULT_EMAIL="$(git config --global user.email 2>/dev/null || true)"
  echo
  echo "2) SEC asks every data request to include a contact email. It's stored only on this Mac."
  read -r -p "   Email [${DEFAULT_EMAIL}]: " EMAIL; EMAIL="${EMAIL:-$DEFAULT_EMAIL}"
  [ -n "$EMAIL" ] || { echo "An email is required by SEC."; exit 1; }
  mkdir -p "$JOB"
  ( umask 077; printf 'SEC_USER_AGENT="Stock Journal personal research %s"\n' "$EMAIL" > "$JOB/config" )
fi

mkdir -p "$JOB"
if [ -d "$JOB/repo/.git" ]; then
  git -C "$JOB/repo" remote set-url origin "https://x-access-token:${TOKEN}@${REPO}"
  git -C "$JOB/repo" fetch -q origin main && git -C "$JOB/repo" reset -q --hard origin/main
else
  git clone -q --depth 50 "https://x-access-token:${TOKEN}@${REPO}" "$JOB/repo"
fi
chmod -R go-rwx "$JOB"

# run.sh is a thin wrapper so the Mac always runs the latest job script from the repo
cat > "$JOB/run.sh" <<'SH'
#!/bin/bash
cd "$HOME/Stock-Journal-data-job/repo" 2>/dev/null && git fetch -q origin main 2>/dev/null && git reset -q --hard origin/main
exec /bin/bash "$HOME/Stock-Journal-data-job/repo/scripts/mac-run.sh" "$@"
SH
chmod 700 "$JOB/run.sh"

mkdir -p "$AGENTS"
DAYS=""
for d in 1 2 3 4 5; do
  DAYS="$DAYS<dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>17</integer><key>Minute</key><integer>30</integer></dict>"
done
cat > "$AGENTS/com.maxstax.stockjournal.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.maxstax.stockjournal</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$JOB/run.sh</string></array>
  <key>StartCalendarInterval</key><array>$DAYS</array>
  <key>StandardOutPath</key><string>$JOB/launchd.log</string>
  <key>StandardErrorPath</key><string>$JOB/launchd.log</string>
</dict></plist>
PL
cat > "$AGENTS/com.maxstax.stockjournal.newstocks.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.maxstax.stockjournal.newstocks</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$JOB/run.sh</string><string>--need-sec</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$JOB/launchd.log</string>
  <key>StandardErrorPath</key><string>$JOB/launchd.log</string>
</dict></plist>
PL
for L in com.maxstax.stockjournal com.maxstax.stockjournal.newstocks; do
  launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$L.plist"
done

echo
echo "Done."
echo " - Weekdays 5:30 PM: full SEC refresh (or when the Mac wakes, if it was asleep)."
echo " - Every 5 minutes: SEC financials for any newly added stock."
echo "Log: $JOB/last-run.log"
