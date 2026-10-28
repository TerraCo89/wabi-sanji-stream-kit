#!/usr/bin/env bash
# Runs `wabisanji listen` and restarts it whenever memes.yaml changes, so memes
# added mid-stream (`wabisanji live fetch`) fire on their phrase straight away.
# Also restarts the listener if it exits. Used by `/aria watch` (run in the
# background, output to data/live/listen.log); kill it to stop both.
#
# usage: listen_autorestart.sh [poll_seconds=5]
cd "$(dirname "$0")/../../.." || exit 1

exe=apps/screenshot-parser/.venv/Scripts/wabisanji.exe
index=content/stream/memes.yaml
poll=${1:-5}

listeners() {
  powershell -NoProfile -Command "@(Get-CimInstance Win32_Process -Filter \"Name='wabisanji.exe'\" |
    Where-Object { \$_.CommandLine -match ' listen' }).Count" | tr -d '\r'
}

if [ "$(listeners)" != "0" ]; then
  echo "error: a wabisanji listen is already running; close it first"
  exit 1
fi

mtime() { stat -c %Y "$index" 2>/dev/null || echo 0; }

start() {
  PYTHONUNBUFFERED=1 "$exe" listen &
  pid=$!
  winpid=$(cat /proc/$pid/winpid 2>/dev/null)
  echo "[autorestart] $(date +%T) listen started"
}

stop() {
  # wabisanji.exe is a launcher with python children: kill the whole tree.
  [ -n "$winpid" ] && taskkill //T //F //PID "$winpid" >/dev/null 2>&1
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
}

trap 'stop; exit 0' INT TERM
last=$(mtime)
start
while true; do
  sleep "$poll"
  now=$(mtime)
  if [ "$now" != "$last" ]; then
    sleep 2  # let a fetch finish writing
    last=$(mtime)
    echo "[autorestart] $(date +%T) memes.yaml changed, restarting listen"
    stop
    start
  elif ! kill -0 "$pid" 2>/dev/null; then
    echo "[autorestart] $(date +%T) listen exited, restarting in 5 s"
    sleep 5
    start
  fi
done
