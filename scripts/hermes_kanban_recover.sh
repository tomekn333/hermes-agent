#!/usr/bin/env bash
# Bezpieczne odzyskiwanie bazy Hermes Kanban przez kontrolowany .dump/.import.
# Domyślnie zapisuje wynik do osobnego pliku. --in-place wymaga zatrzymania gatewaya,
# braku otwartych deskryptorów oraz tworzy komplet kopii forensic przed podmianą.
set -euo pipefail

PATH=/usr/local/bin:/usr/bin:/bin
SOURCE=""
OUTPUT=""
IN_PLACE=0
GATEWAY_WAS_ACTIVE=0
LOCK=/tmp/hermes-kanban-recover.lock

usage() {
  echo "Użycie: $0 --source /ścieżka/kanban.db [--output /ścieżka/recovered.db | --in-place]" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --source) SOURCE=${2:-}; shift 2 ;;
    --output) OUTPUT=${2:-}; shift 2 ;;
    --in-place) IN_PLACE=1; shift ;;
    -h|--help) usage ;;
    *) echo "Nieznany argument: $1" >&2; usage ;;
  esac
done

[[ -n "$SOURCE" && -f "$SOURCE" ]] || usage
SOURCE=$(readlink -f "$SOURCE")
if ((IN_PLACE)); then
  [[ -z "$OUTPUT" ]] || { echo "--output i --in-place wzajemnie się wykluczają" >&2; exit 2; }
  [[ "$(basename "$SOURCE")" == "kanban.db" ]] || { echo "--in-place dozwolone tylko dla pliku kanban.db" >&2; exit 2; }
else
  [[ -n "$OUTPUT" ]] || OUTPUT="${SOURCE}.recovered"
  OUTPUT=$(readlink -m "$OUTPUT")
  [[ "$OUTPUT" != "$SOURCE" ]] || { echo "Output nie może nadpisać source bez --in-place" >&2; exit 2; }
fi

exec 9>"$LOCK"
flock -n 9 || { echo "Inny recovery już działa" >&2; exit 3; }

cleanup() {
  local rc=$?
  if ((GATEWAY_WAS_ACTIVE)); then
    systemctl --user start hermes-gateway >/dev/null 2>&1 || true
  fi
  [[ -z "${TMPDIR_RECOVERY:-}" ]] || rm -rf "$TMPDIR_RECOVERY"
  exit "$rc"
}
trap cleanup EXIT INT TERM

stamp=$(date +%Y%m%d_%H%M%S)
if ((IN_PLACE)); then
  if systemctl --user is-active --quiet hermes-gateway; then
    GATEWAY_WAS_ACTIVE=1
    systemctl --user stop hermes-gateway
  fi
  # Nie wolno podmieniać inode'u, jeśli jakikolwiek proces nadal trzyma DB/WAL/SHM.
  open_files=$(lsof "$SOURCE" "${SOURCE}-wal" "${SOURCE}-shm" 2>/dev/null || true)
  if [[ -n "$open_files" ]]; then
    echo "ABORT: otwarte deskryptory DB/WAL/SHM po zatrzymaniu gatewaya:" >&2
    echo "$open_files" >&2
    exit 4
  fi
fi

TMPDIR_RECOVERY=$(mktemp -d /tmp/hermes-kanban-recover.XXXXXX)
stage="$TMPDIR_RECOVERY/source.db"
cp --reflink=auto --preserve=mode,timestamps "$SOURCE" "$stage"
for suffix in -wal -shm; do
  [[ -f "${SOURCE}${suffix}" ]] && cp --reflink=auto --preserve=mode,timestamps "${SOURCE}${suffix}" "${stage}${suffix}"
done

# Kopie forensic są obowiązkowe także w trybie output, aby zachować dokładne wejście.
for suffix in "" -wal -shm; do
  [[ -f "${SOURCE}${suffix}" ]] && cp --reflink=auto --preserve=mode,timestamps \
    "${SOURCE}${suffix}" "${SOURCE}.forensic.${stamp}${suffix}.bak"
done

sql="$TMPDIR_RECOVERY/dump.sql"
dump_err="$TMPDIR_RECOVERY/dump.err"
# sqlite3 może zwrócić rc=0, lecz zakończyć dump przez ROLLBACK po wykryciu uszkodzenia.
sqlite3 "$stage" '.dump' >"$sql" 2>"$dump_err" || true
[[ -s "$sql" ]] || { echo "Recovery nie wygenerował SQL" >&2; exit 5; }

dump_warnings=$(grep -c 'CORRUPTION ERROR' "$sql" || true)
python3 - "$sql" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text(errors="strict")
rollback = "ROLLBACK; -- due to errors\n"
if s.endswith(rollback):
    s = s[:-len(rollback)] + "COMMIT;\n"
elif not s.rstrip().endswith("COMMIT;"):
    raise SystemExit("dump nie kończy się ani kontrolowanym ROLLBACK, ani COMMIT")
if "CREATE TABLE tasks" not in s:
    raise SystemExit("dump nie zawiera wymaganej tabeli tasks")
p.write_text(s)
PY

rebuilt="$TMPDIR_RECOVERY/rebuilt.db"
if ! sqlite3 "$rebuilt" <"$sql" 2>"$TMPDIR_RECOVERY/import.err"; then
  echo "Import odzyskanego SQL nie powiódł się" >&2
  cat "$TMPDIR_RECOVERY/import.err" >&2
  exit 6
fi
[[ "$(sqlite3 "$rebuilt" 'PRAGMA quick_check;')" == "ok" ]] || { echo "quick_check odzyskanej DB nie przeszedł" >&2; exit 7; }
[[ "$(sqlite3 "$rebuilt" 'PRAGMA integrity_check;')" == "ok" ]] || { echo "integrity_check odzyskanej DB nie przeszedł" >&2; exit 7; }
foreign_errors=$(sqlite3 "$rebuilt" 'PRAGMA foreign_key_check;' | wc -l)
((foreign_errors == 0)) || { echo "foreign_key_check: $foreign_errors błędów" >&2; exit 7; }

for table in tasks task_links task_comments task_events task_runs kanban_notify_subs; do
  count=$(sqlite3 "$rebuilt" "SELECT count(*) FROM $table;")
  printf '%s=%s\n' "$table" "$count"
done
printf 'corruption_markers=%s\n' "$dump_warnings"
[[ ! -s "$dump_err" ]] || { echo "Ostrzeżenia sqlite3 .dump:" >&2; cat "$dump_err" >&2; }

if ((IN_PLACE)); then
  target_tmp="${SOURCE}.recovered.${stamp}.tmp"
  cp --preserve=mode "$rebuilt" "$target_tmp"
  mv -f "$target_tmp" "$SOURCE"
  rm -f "${SOURCE}-wal" "${SOURCE}-shm"
  OUTPUT="$SOURCE"
else
  mkdir -p "$(dirname "$OUTPUT")"
  cp --preserve=mode "$rebuilt" "$OUTPUT"
fi
chmod 600 "$OUTPUT"
echo "RECOVERY_OK output=$OUTPUT forensic=${SOURCE}.forensic.${stamp}.bak"
