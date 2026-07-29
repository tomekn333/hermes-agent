#!/usr/bin/env bash
# Cykliczny, tylko-do-odczytu PRAGMA quick_check dla wszystkich baz Kanbanu.
# Alert Slack jest wysyłany tylko przy przejściu OK->FAIL; powrót do OK także raportujemy.
set -uo pipefail
PATH=/home/tomek/.hermes/hermes-agent/venv/bin:/usr/local/bin:/usr/bin:/bin
HOME=/home/tomek
LOG="$HOME/.hermes/logs/kanban_quick_check.log"
STATE_DIR="$HOME/.hermes/logs/kanban_quick_check_state"
SLACK_TARGET="slack:U0B5A8YPL9W"
DRY_RUN=0
DBS=()

while (($#)); do
  case "$1" in
    --db) DBS+=("${2:-}"); shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) echo "Użycie: $0 [--db /path/kanban.db] [--dry-run]"; exit 0 ;;
    *) echo "Nieznany argument: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
exec 9>/tmp/hermes-kanban-quick-check.lock
flock -n 9 || exit 0

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }
alert() {
  local msg=$1
  if ((DRY_RUN)); then
    log "DRY_RUN alert: $msg"
    printf '%s\n' "$msg"
    return 0
  fi
  if ! timeout 90 hermes send --to "$SLACK_TARGET" "$msg" >>"$LOG" 2>&1; then
    log "ERROR: wysłanie alertu Slack nie powiodło się"
    return 1
  fi
}

if ((${#DBS[@]} == 0)); then
  shopt -s nullglob
  DBS=("$HOME"/.hermes/kanban/boards/*/kanban.db)
fi

failures=0
for db in "${DBS[@]}"; do
  [[ -f "$db" ]] || continue
  key=$(printf '%s' "$db" | sha256sum | cut -d' ' -f1)
  state_file="$STATE_DIR/$key"
  if [[ -f "$state_file" ]]; then
    previous=$(<"$state_file")
  else
    previous=unknown
  fi
  board=$(basename "$(dirname "$db")")

  output=$(timeout 60 sqlite3 -readonly "$db" 'PRAGMA quick_check;' 2>&1)
  rc=$?
  if [[ $rc -eq 0 && "$output" == "ok" ]]; then
    current=ok
    if [[ "$previous" == fail ]]; then
      alert "Kanban SQLite RECOVERED: board=$board, quick_check=ok, db=$db" || true
    fi
  else
    current=fail
    failures=$((failures + 1))
    compact=$(printf '%s' "$output" | tr '\n' ' ' | cut -c1-500)
    log "FAIL board=$board rc=$rc detail=$compact"
    if [[ "$previous" != fail ]]; then
      alert "Kanban SQLite CORRUPTION: board=$board, rc=$rc, db=$db, quick_check: $compact. Recovery: /home/tomek/scripts/hermes_kanban_recover.sh" || true
    fi
  fi
  printf '%s\n' "$current" >"$state_file"
done

if ((failures == 0)); then
  log "OK: sprawdzono ${#DBS[@]} baz"
  exit 0
fi
exit 1
