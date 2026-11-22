#!/usr/bin/env sh
# AI DEV DIRECTOR companion launcher for macOS / Linux (unverified; the Windows launcher is start-companion.ps1).
# Starts the 8081 console in the background and records its PID in data/companion.pid.
set -e
cd "$(dirname "$0")"
mkdir -p data
if [ -f data/companion.pid ] && kill -0 "$(cat data/companion.pid)" 2>/dev/null; then
  echo "AI DEV companion already running (PID $(cat data/companion.pid))"
  echo "Login link (once per browser): $("${PYTHON:-python3}" companion_server.py --login-url)"
  exit 0
fi
PY="${PYTHON:-python3}"
LINK="$("$PY" companion_server.py --login-url)"   # creates data/solomon-bridge.token before the server reads it
nohup "$PY" companion_server.py >data/companion.log 2>data/companion.err &
echo $! >data/companion.pid
echo "AI DEV companion started (PID $!)"
echo "Login link (once per browser): $LINK"
