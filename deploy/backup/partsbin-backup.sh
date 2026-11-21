#!/usr/bin/env bash
# Nightly backup of the Partsbin inventory (SQLite database + photos) to the NAS.
#
# Takes a consistent copy of the database inside the running container (SQLite backup API),
# streams it together with the photos as a tar.gz from docker LXC 107 to the PVE backup storage
# (NFS, /mnt/pve/backup) and prunes old archives. On failure a JSON message is POSTed to
# HA_WEBHOOK_URL (see /etc/default/partsbin-backup).
#
# Usage: partsbin-backup.sh [--no-notify]
# Restore: see "Backup" in the partsbin README.
set -euo pipefail

CT=107                       # docker LXC (VMID)
CONTAINER=partsbin           # container name in the Portainer stack
STORAGE_MOUNT=/mnt/pve/backup
DEST="$STORAGE_MOUNT/partsbin"
KEEP_DAYS=14                 # keep every archive this many days
KEEP_MONTHS=6                # plus the first archive of each of the last N months
HA_WEBHOOK_URL=""
[[ -r /etc/default/partsbin-backup ]] && . /etc/default/partsbin-backup
NOTIFY=1
[[ "${1:-}" == "--no-notify" ]] && NOTIFY=0

log() { echo "partsbin-backup: $*"; }

fail() {
  log "FAILED: $1"
  rm -f "${TMP:-}"
  if (( NOTIFY )) && [[ -n "$HA_WEBHOOK_URL" ]]; then
    python3 -c 'import json,sys; print(json.dumps({"status": "failed", "message": sys.argv[1], "host": sys.argv[2]}))' \
      "$1" "$(hostname)" \
      | curl -fsS -m 15 -X POST -H "Content-Type: application/json" --data-binary @- "$HA_WEBHOOK_URL" >/dev/null \
      || log "notification failed"
  fi
  exit 1
}
trap 'fail "command failed (line $LINENO): $BASH_COMMAND"' ERR

# The PVE storage is mounted on demand; activate it if needed.
mountpoint -q "$STORAGE_MOUNT" || pvesm status >/dev/null 2>&1 || true
mountpoint -q "$STORAGE_MOUNT" || fail "backup storage $STORAGE_MOUNT is not mounted"
pct status "$CT" | grep -q running || fail "LXC $CT is not running"
pct exec "$CT" -- docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
  || fail "container $CONTAINER is not running"

mkdir -p "$DEST"
STAMP=$(date +%Y-%m-%d)
FINAL="$DEST/partsbin-$STAMP.tar.gz"
TMP="$DEST/.partsbin-$STAMP.tar.gz.part"

# Consistent snapshot of the database next to the live one, checked before it is archived.
pct exec "$CT" -- docker exec "$CONTAINER" python -c '
import sqlite3
src = sqlite3.connect("/data/partsbin.db")
dst = sqlite3.connect("/data/.backup.db")
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
parts = dst.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
dst.close(); src.close()
assert ok == "ok", ok
print(f"database ok, {parts} parts")
'

pct exec "$CT" -- docker exec "$CONTAINER" \
  tar -C /data --transform 's|^\.backup\.db$|partsbin.db|' -czf - .backup.db images > "$TMP"
pct exec "$CT" -- docker exec "$CONTAINER" rm -f /data/.backup.db

gzip -t "$TMP"
# grep without -q reads the whole listing, so tar never gets SIGPIPE (pipefail).
tar -tzf "$TMP" | grep -x 'partsbin.db' >/dev/null || fail "archive does not contain partsbin.db"
mv -f "$TMP" "$FINAL"
log "wrote $FINAL ($(numfmt --to=iec "$(stat -c %s "$FINAL")"))"

# Retention: everything younger than KEEP_DAYS, plus the oldest archive of each recent month.
cutoff=$(date -d "-$KEEP_DAYS days" +%Y-%m-%d)
declare -A month_keep=()
for m in $(seq 0 $((KEEP_MONTHS - 1))); do
  month=$(date -d "$(date +%Y-%m-15) -$m months" +%Y-%m)
  first=$(ls "$DEST"/partsbin-"$month"-*.tar.gz 2>/dev/null | sort | head -1 || true)
  [[ -n "$first" ]] && month_keep["$first"]=1
done
for f in "$DEST"/partsbin-*.tar.gz; do
  d=$(basename "$f" .tar.gz); d=${d#partsbin-}
  if [[ "$d" < "$cutoff" && -z "${month_keep[$f]:-}" ]]; then
    rm -f "$f"; log "pruned $(basename "$f")"
  fi
done
