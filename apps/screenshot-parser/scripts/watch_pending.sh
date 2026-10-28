#!/usr/bin/env bash
# Emits one line each time new screenshots show up in `wabisanji pending`.
# Used by `/translate watch` (Claude Code Monitor): every stdout line is an event.
# Waits for a burst of F8 presses to settle so they arrive as one batch.
#
# usage: watch_pending.sh [poll_seconds=2] [settle_seconds=3]
set -o pipefail
cd "$(dirname "$0")/.." || exit 1

exe=.venv/Scripts/wabisanji.exe
poll=${1:-2}
settle=${2:-3}

list() { "$exe" pending 2>&1 | tr -d '\r' | sort; }

seen=$(list) || { echo "ERROR: wabisanji pending failed: $seen"; exit 1; }
echo "WATCHING ($(grep -c . <<<"$seen") already pending)"

while true; do
  sleep "$poll"
  cur=$(list) || { echo "ERROR: wabisanji pending failed: $cur"; continue; }
  if [ -z "$(comm -13 <(echo "$seen") <(echo "$cur"))" ]; then
    seen=$cur  # shrinks as shots get ingested
    continue
  fi
  while sleep "$settle"; do
    nxt=$(list) || break
    [ "$nxt" = "$cur" ] && break
    cur=$nxt
  done
  echo "NEW_SCREENSHOTS $(comm -13 <(echo "$seen") <(echo "$cur") | grep -c .)"
  seen=$cur
done
