#!/bin/bash
# Stock Journal data job, run on Max's Mac by launchd each weekday after the close.
# SEC and Yahoo block GitHub's shared servers but not home connections.
JOB="$HOME/Stock-Journal-data-job"
LOG="$JOB/last-run.log"
[ -f "$LOG" ] && tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
{
  echo "=== $(date)"
  # shellcheck disable=SC1091
  source "$JOB/config"
  export SEC_USER_AGENT
  cd "$JOB/repo" || { echo "repo missing"; exit 1; }
  for attempt in 1 2 3; do
    git fetch -q origin main && git reset -q --hard origin/main || { echo "git fetch failed (offline?)"; exit 1; }
    /usr/bin/python3 scripts/fetch_data.py || { echo "fetch_data.py failed"; exit 1; }
    git add data/market.json
    if git diff --cached --quiet; then echo "no changes"; exit 0; fi
    git -c user.name="stock-journal-mac" -c user.email="actions@users.noreply.github.com" \
      commit -q -m "Update market data from Mac $(date +%F)"
    if git push -q origin main; then echo "pushed"; exit 0; fi
    echo "push rejected (GitHub job pushed at the same time), retrying"
    sleep 20
  done
} >> "$LOG" 2>&1
