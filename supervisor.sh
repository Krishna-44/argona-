#!/usr/bin/env bash
# Argona supervisor — keeps the API and dashboard alive.
#
# Nothing watched these before: uvicorn was launched with a bare nohup and the
# Next dev server died on its own mid-session while the API stayed up. If
# either dies at Monday's open the desk stops trading and nothing notices.
#
# Health is checked by HTTP, not by "is a process named X alive" — a hung
# server that still holds the port is the failure mode that matters, and a
# pid check would call it healthy.
#
#   ./supervisor.sh            run in the foreground (ctrl-C to stop)
#   ./supervisor.sh --daemon   detach and keep running after the shell exits
#   ./supervisor.sh --stop     stop the supervisor (leaves the servers up)
#   ./supervisor.sh --status   one-shot health report

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"; mkdir -p "$LOG_DIR"
SUP_LOG="$LOG_DIR/supervisor.log"
API_LOG="$LOG_DIR/api.log"
WEB_LOG="$LOG_DIR/web.log"
PIDFILE="$LOG_DIR/supervisor.pid"

# launchd hands this script a minimal PATH that does not include nvm, so `npm`
# resolved to nothing and the web UI could never be restarted: web.log filled
# with "nohup: npm: No such file or directory" on every 20s tick while the
# supervisor reported WEB DOWN and looked like it was trying. Resolve node's
# bin directory explicitly. Highest version first (sort -rV), since the plain
# glob puts v16 ahead of v20 lexically and Next.js wants the newer one.
for _d in $(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -rV) \
          /opt/homebrew/bin /usr/local/bin; do
  if [ -x "$_d/npm" ]; then PATH="$_d:$PATH"; break; fi
done
export PATH
unset _d

API_PORT=4110
WEB_PORT=3110
INTERVAL=20          # seconds between checks
FAILS_BEFORE_RESTART=2   # tolerate one blip; restart on the second
MAX_LOG_BYTES=$((20 * 1024 * 1024))

# In daemon mode nohup already redirects stdout to $SUP_LOG, so piping through
# `tee -a` wrote every line twice. Write to stdout, and only append to the file
# as well when running interactively (where stdout is the terminal).
log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S')  $*"
  printf '%s\n' "$line"
  [ -t 1 ] && printf '%s\n' "$line" >> "$SUP_LOG"
  return 0
}

rotate() {  # keep the logs from eating the disk over a long run
  for f in "$SUP_LOG" "$API_LOG" "$WEB_LOG"; do
    [ -f "$f" ] || continue
    sz=$(wc -c < "$f" 2>/dev/null || echo 0)
    [ "$sz" -gt "$MAX_LOG_BYTES" ] && mv "$f" "$f.1" && : > "$f"
  done
}

api_up()  { curl -fsS --max-time 6 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; }
web_up()  { curl -fsS --max-time 8 -o /dev/null "http://127.0.0.1:$WEB_PORT/" 2>/dev/null; }

start_api() {
  log "starting API on $API_PORT"
  ( cd "$ROOT/backend" && nohup ./.venv/bin/python -m uvicorn app.main:app \
      --host 127.0.0.1 --port "$API_PORT" >> "$API_LOG" 2>&1 & )
}

start_web() {
  log "starting web on $WEB_PORT"
  ( cd "$ROOT/web" && nohup npm run dev -- --port "$WEB_PORT" >> "$WEB_LOG" 2>&1 & )
}

status() {
  api_up && echo "API  :$API_PORT  UP" || echo "API  :$API_PORT  DOWN"
  web_up && echo "WEB  :$WEB_PORT  UP" || echo "WEB  :$WEB_PORT  DOWN"
  # Authoritative source is the LOCK HOLDER, not a pgrep count.
  # start_api() forks a subshell, and a forked bash inherits the parent's
  # command line — so `pgrep -f "supervisor.sh --run"` transiently matches the
  # supervisor AND its own child, which looked like two supervisors fighting
  # and sent me chasing a bug that did not exist.
  local holder
  holder=$(cat "$LOG_DIR/supervisor.lock/pid" 2>/dev/null)
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    echo "SUPERVISOR  running (pid $holder)"
  else
    echo "SUPERVISOR  not running"
  fi
  if launchctl list 2>/dev/null | grep -q com.argona.supervisor; then
    echo "LAUNCHD     com.argona.supervisor loaded (auto-restarts, survives reboot)"
  fi
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --stop)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE" && echo "supervisor stopped"
    else
      echo "supervisor not running"
    fi
    exit 0 ;;
  --daemon)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "supervisor already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    # Detach into its OWN session, not just the background. `nohup &` plus
    # `disown` leaves the child in the caller's process group, so when the
    # launching shell is torn down the whole group is signalled and the
    # supervisor dies with it. macOS ships no setsid(1), so use Python's
    # os.setsid() to become a session leader first.
    /usr/bin/env python3 - "$BASH_SOURCE" "$SUP_LOG" <<'PYDETACH'
import os, sys
script, logfile = sys.argv[1], sys.argv[2]
if os.fork() != 0:
    sys.exit(0)            # parent returns to the shell immediately
os.setsid()                # new session: survives the caller's teardown
if os.fork() != 0:
    os._exit(0)            # never reacquire a controlling terminal
fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1); os.dup2(fd, 2)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
os.execv("/bin/bash", ["bash", script, "--run"])
PYDETACH
    sleep 2
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "supervisor started (pid $(cat "$PIDFILE"))"
    else
      echo "supervisor failed to start — see $SUP_LOG"; exit 1
    fi
    exit 0 ;;
esac

# Atomic single-instance lock. Reading a pidfile and then writing it is a
# TOCTOU race: two instances launched within the same moment both see no
# pidfile and both proceed, which is exactly what happened (two supervisors
# 23s apart, each fighting to restart the services). mkdir either succeeds or
# fails atomically, so only one can win.
LOCKDIR="$LOG_DIR/supervisor.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  holder=$(cat "$LOCKDIR/pid" 2>/dev/null)
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    log "another supervisor holds the lock (pid $holder) — exiting"
    exit 0
  fi
  # Stale lock from a kill -9: the trap never ran. Take it over.
  log "clearing stale lock from pid ${holder:-unknown}"
  rm -rf "$LOCKDIR"; mkdir "$LOCKDIR" 2>/dev/null || exit 0
fi
echo $$ > "$LOCKDIR/pid"

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; rm -rf "$LOCKDIR"; log "supervisor exiting"; exit 0' INT TERM

log "supervisor online — watching API:$API_PORT and WEB:$WEB_PORT every ${INTERVAL}s"
api_fails=0; web_fails=0
while true; do
  rotate
  if api_up; then
    [ "$api_fails" -gt 0 ] && log "API recovered"
    api_fails=0
  else
    api_fails=$((api_fails + 1))
    log "API health check failed ($api_fails/$FAILS_BEFORE_RESTART)"
    if [ "$api_fails" -ge "$FAILS_BEFORE_RESTART" ]; then
      pkill -f "uvicorn app.main" 2>/dev/null
      sleep 2; start_api; api_fails=0; sleep 10
    fi
  fi

  if web_up; then
    [ "$web_fails" -gt 0 ] && log "web recovered"
    web_fails=0
  else
    web_fails=$((web_fails + 1))
    log "web health check failed ($web_fails/$FAILS_BEFORE_RESTART)"
    if [ "$web_fails" -ge "$FAILS_BEFORE_RESTART" ]; then
      pkill -f "next dev" 2>/dev/null
      sleep 2; start_web; web_fails=0; sleep 15
    fi
  fi

  sleep "$INTERVAL"
done
