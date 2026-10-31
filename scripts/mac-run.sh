#!/bin/bash
# Stock Journal data job on Max's Mac (SEC isn't blocked here).
#   no args       full refresh of financials (weekdays 5:30 PM)
#   --need-sec    only stocks still missing SEC financials (every 5 minutes; usually does nothing)
JOB="$HOME/Stock-Journal-data-job"
LOG="$JOB/last-run.log"
[ -f "$LOG" ] && tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
QUIET=0; [ "${1:-}" = "--need-sec" ] && QUIET=1
OUT="$(
  # shellcheck disable=SC1091
  source "$JOB/config"; export SEC_USER_AGENT
  export SKIP_PRICES=1   # prices come from the GitHub job; price sites block this connection anyway
  cd "$JOB/repo" || { echo "repo missing"; exit 1; }
  for attempt in 1 2 3; do
    git fetch -q origin main && git reset -q --hard origin/main || { echo "git fetch failed (offline?)"; exit 1; }
    /usr/bin/python3 scripts/fetch_data.py "$@" || { echo "fetch_data.py failed"; exit 1; }
    git add data/market.json
    if git diff --cached --quiet; then echo "no changes"; exit 0; fi
    git -c user.name="stock-journal-mac" -c user.email="actions@users.noreply.github.com" \
      commit -q -m "Update market data from Mac $(date +%F)"
    PUSH="$(git push -q origin main 2>&1)" && { echo "pushed"; exit 0; }
    echo "$PUSH"
    case "$PUSH" in
      *uthentication*|*"Invalid username or token"*|*403*)
        echo "PUSH FAILED: GitHub rejected the token. Rerun the setup command."; exit 1;;
    esac
    echo "push rejected (another update saved at the same moment), retrying"
    sleep 20
  done
)"
# The 5-minute check only writes to the log when it actually did something
if [ "$QUIET" = 0 ] || ! printf '%s' "$OUT" | grep -q "^Nothing to do"; then
  { echo "=== $(date) $*"; printf '%s\n' "$OUT"; } >> "$LOG"
fi
