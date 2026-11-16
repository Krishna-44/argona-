#!/bin/bash
# Argona F&O launcher — starts the backend + frontend if they aren't already
# running, waits for them, and opens the dashboard in the default browser.
set -e

ROOT="/Users/krishnagoyal/ai-trading-os"
NODE_BIN="/Users/krishnagoyal/.nvm/versions/node/v20.19.0/bin"
export PATH="$NODE_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

API_PORT=4110
WEB_PORT=3110
# Point the Next proxy + websocket at the API explicitly.
export ARGONA_API="http://127.0.0.1:${API_PORT}"
export ARGONA_WS="ws://127.0.0.1:${API_PORT}"
LOGDIR="$ROOT/backend/logs"
mkdir -p "$LOGDIR"

listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# --- backend ---------------------------------------------------------------
if ! listening "$API_PORT"; then
  echo "starting Argona API on :$API_PORT…"
  cd "$ROOT/backend"
  nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --app-dir "$ROOT/backend" \
    >"$LOGDIR/api.log" 2>&1 &
fi

# --- frontend --------------------------------------------------------------
if ! listening "$WEB_PORT"; then
  echo "starting Argona web on :$WEB_PORT…"
  cd "$ROOT/web"
  nohup "$NODE_BIN/npm" run dev -- -p "$WEB_PORT" >"$LOGDIR/web.log" 2>&1 &
fi

# --- wait for the web server, then open ------------------------------------
for i in $(seq 1 40); do
  if listening "$WEB_PORT" && curl -s --max-time 2 "http://localhost:$WEB_PORT" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

open "http://localhost:$WEB_PORT"
echo "Argona F&O is up → http://localhost:$WEB_PORT"
