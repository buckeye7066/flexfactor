#!/data/data/com.termux/files/usr/bin/bash
# ---------------------------------------------------------------------------
# engine.sh — FlexFactor on the phone: watch runs, and start real ones.
#
#   flexfactor-engine start|stop|restart|status|logs [n]
#   flexfactor-engine run <program> [extra flexfactor args...]
#   flexfactor-engine prodready <program> [...]
#
# `start` runs the read-only web dashboard. `run` is the part that makes this
# phone independent rather than merely informed: it executes a real audit here,
# against a checkout here, with no laptop in the path.
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DIR="${FLEXFACTOR_APP_DIR:-$HOME/phone-console/flexfactor}"
RUN_DIR="$HOME/.phone-console"
PID_FILE="$RUN_DIR/flexfactor-web.pid"
LOG_FILE="$RUN_DIR/flexfactor-web.log"
AUDIT_LOG="$RUN_DIR/flexfactor-audit.log"
PORT="${FLEXFACTOR_WEB_PORT:-8765}"
APP_PKG="com.firer.console.flexfactor"

mkdir -p "$RUN_DIR"
[ -f "$HOME/.flexfactor-phone.env" ] && . "$HOME/.flexfactor-phone.env"

alive() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

url() { python "$APP_DIR/flexfactor_web.py" --host 127.0.0.1 --port "$PORT" --print-url; }

# Ask the SERVER, not the pid file. A live pid that never bound the port is a
# hang, and calling that "running" is the failure this exists to catch.
http_ok() { curl -fsS --max-time 4 -o /dev/null "$(url)"; }

# Hand the freshly-minted token to the app, so the owner never types it. The
# dashboard mints its own token into ~/.flexfactor/web-token.txt, which an APK
# cannot read (different UID), and no storage permission is involved either way.
#
# A BROADCAST, not `am start`. Measured on an S25 Ultra (Android 16): `am start`
# from Termux prints "Starting: Intent {...}", exits 0, and the activity never
# resumes -- Android's background-activity-start restriction drops it silently.
# A handover that reports success while doing nothing is worse than none, so
# this uses `-W` and reads the receiver's own result code instead of assuming.
tell_app() {
  command -v am >/dev/null || return 0
  out="$(am broadcast -n "$APP_PKG/com.firer.console.ConfigReceiver" \
          --es local "$(url)" 2>&1)" || true
  case "$out" in
    *"result=1"*)
      echo "app configured: $APP_PKG now points at this phone" ;;
    *"result=2"*)
      echo "app REFUSED the address as non-loopback -- that is a bug, report it" >&2 ;;
    *"result=3"*)
      echo "app rejected the handover (no address sent) -- that is a bug" >&2 ;;
    *"without waiting"*)
      # Measured on an S25 Ultra (Android 16): from Termux's UID `am` prints
      # "Broadcast sent without waiting for result" and returns nothing, while
      # the same command from `adb shell` prints result=1. So the delivery is
      # real but UNCONFIRMABLE from here. Say exactly that -- claiming success
      # on an unread result is the failure this whole handover was rewritten to
      # avoid, and printing the URL costs one line.
      echo "handover sent to $APP_PKG (Android does not report the result to Termux)."
      echo "  If the app still shows the laptop: gear icon -> \"This phone's engine\" ="
      echo "  $(url)" ;;
    *)
      echo "could not configure $APP_PKG. Is it installed, and is it v2.0.0 or newer?" >&2
      echo "  set it by hand: gear icon -> \"This phone's engine\" = $(url)" >&2 ;;
  esac
}

cmd_start() {
  [ -d "$APP_DIR" ] || { echo "not installed: $APP_DIR — run setup.sh first" >&2; exit 1; }
  if alive && http_ok; then
    echo "already running (pid $(cat "$PID_FILE")) on 127.0.0.1:$PORT"
    tell_app; return 0
  fi
  command -v termux-wake-lock >/dev/null && termux-wake-lock || true
  cd "$APP_DIR"
  echo "--- $(date -Iseconds) starting ---" >> "$LOG_FILE"
  nohup python flexfactor_web.py --host 127.0.0.1 --port "$PORT" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  for _ in $(seq 1 20); do
    sleep 1
    if http_ok; then echo "dashboard up: $(url)"; tell_app; return 0; fi
    alive || { echo "process exited during startup:" >&2; tail -n 20 "$LOG_FILE" >&2; exit 1; }
  done
  echo "started but never answered on :$PORT within 20s — treating as FAILED" >&2
  tail -n 20 "$LOG_FILE" >&2
  exit 1
}

cmd_stop() {
  [ -f "$PID_FILE" ] || { echo "not running"; return 0; }
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "stopped"
}

cmd_status() {
  if alive; then echo "web pid: $(cat "$PID_FILE") (alive)"; else echo "web pid: none"; fi
  if http_ok; then echo "web http: OK  $(url)"; else echo "web http: NOT ANSWERING on :$PORT"; fi
  if [ -f "$HOME/.flexfactor/status.json" ]; then
    echo "last status.json: $(date -r "$HOME/.flexfactor/status.json" -Iseconds 2>/dev/null || echo '?')"
  else
    echo "last status.json: none (no audit has run on this phone yet)"
  fi
  if [ -f "$RUN_DIR/audit.pid" ] && kill -0 "$(cat "$RUN_DIR/audit.pid")" 2>/dev/null; then
    echo "audit: RUNNING (pid $(cat "$RUN_DIR/audit.pid")) — tail $AUDIT_LOG"
  else
    echo "audit: not running"
  fi
}

# --- the actual work ------------------------------------------------------
# --no-dashboard is required: the Tk dashboard cannot exist here. flexfactor
# already fails soft if the spawn fails, but a soft failure printed on every
# run is noise that hides real ones.
cmd_run() {
  local mode="$1"; shift
  [ $# -ge 1 ] || { echo "usage: flexfactor-engine $mode <program> [args...]" >&2; exit 2; }
  [ -d "$APP_DIR" ] || { echo "not installed: $APP_DIR" >&2; exit 1; }
  if [ -f "$RUN_DIR/audit.pid" ] && kill -0 "$(cat "$RUN_DIR/audit.pid")" 2>/dev/null; then
    echo "an audit is already running (pid $(cat "$RUN_DIR/audit.pid")); stop it first" >&2
    exit 1
  fi
  command -v termux-wake-lock >/dev/null && termux-wake-lock || true
  cd "$APP_DIR"
  echo "--- $(date -Iseconds) $mode $* ---" >> "$AUDIT_LOG"
  nohup python flexfactor.py "$mode" --program "$1" --no-dashboard "${@:2}" \
      >> "$AUDIT_LOG" 2>&1 &
  echo $! > "$RUN_DIR/audit.pid"
  echo "$mode started on this phone (pid $(cat "$RUN_DIR/audit.pid"))"
  echo "watch it: the FlexFactor app, or  tail -f $AUDIT_LOG"
}

case "${1:-status}" in
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_stop; cmd_start ;;
  status)    cmd_status ;;
  logs)      tail -n "${2:-80}" "$LOG_FILE" ;;
  audit-log) tail -n "${2:-80}" "$AUDIT_LOG" ;;
  run|audit) shift; cmd_run audit "$@" ;;
  prodready) shift; cmd_run prodready "$@" ;;
  *) echo "usage: flexfactor-engine start|stop|restart|status|logs|audit-log|run <program>|prodready <program>" >&2; exit 2 ;;
esac
