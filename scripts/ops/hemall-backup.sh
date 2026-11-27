#!/usr/bin/env bash
# Hemall production backup: PostgreSQL custom dump + Redis snapshot.
# Intended to run on the Docker host as the soffy user. It only reads from the
# running containers and never restarts or replaces them.

set -euo pipefail

BACKUP_ROOT="${HEMALL_BACKUP_ROOT:-/data/soffy/backups/hemall}"
DB_CONTAINER="${HEMALL_DB_CONTAINER:-hemal-db-1}"
REDIS_CONTAINER="${HEMALL_REDIS_CONTAINER:-hemal-redis-1}"
MIN_FREE_GB="${HEMALL_BACKUP_MIN_FREE_GB:-10}"
DAILY_KEEP="${HEMALL_BACKUP_DAILY_KEEP:-7}"
WEEKLY_KEEP="${HEMALL_BACKUP_WEEKLY_KEEP:-4}"

export TZ="${TZ:-Asia/Shanghai}"
umask 077

mkdir -p "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly"
chmod 700 "$BACKUP_ROOT" "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly"

write_backup_metrics() {
  local status="$1"
  local timestamp
  timestamp="$(date +%s)"
  {
    printf '# HELP hemal_backup_last_run_failed 1 when the latest backup failed.\n'
    printf '# TYPE hemal_backup_last_run_failed gauge\n'
    printf 'hemal_backup_last_run_failed %s\n' "$([[ "$status" == "ok" ]] && echo 0 || echo 1)"
    printf '# HELP hemal_backup_last_success_timestamp_seconds Unix time of latest successful backup.\n'
    printf '# TYPE hemal_backup_last_success_timestamp_seconds gauge\n'
    if [[ "$status" == "ok" ]]; then
      printf 'hemal_backup_last_success_timestamp_seconds %s\n' "$timestamp"
    else
      local previous="0"
      [[ -f "$BACKUP_ROOT/last_success_timestamp" ]] \
        && previous="$(cat "$BACKUP_ROOT/last_success_timestamp")"
      printf 'hemal_backup_last_success_timestamp_seconds %s\n' "${previous:-0}"
    fi
  } > "$BACKUP_ROOT/hemall_backup.prom.tmp"
  chmod 644 "$BACKUP_ROOT/hemall_backup.prom.tmp"
  mv -- "$BACKUP_ROOT/hemall_backup.prom.tmp" "$BACKUP_ROOT/hemall_backup.prom"
  if [[ "$status" == "ok" ]]; then
    printf '%s\n' "$timestamp" > "$BACKUP_ROOT/last_success_timestamp"
    chmod 600 "$BACKUP_ROOT/last_success_timestamp"
  fi
}

tmp=""
cleanup_backup() {
  local rc=$?
  if [[ -n "$tmp" && -d "$tmp" ]]; then
    rm -rf -- "$tmp"
  fi
  if (( rc != 0 )); then
    write_backup_metrics "failed" || true
  fi
  exit "$rc"
}
trap cleanup_backup EXIT

exec 9>"$BACKUP_ROOT/backup.lock"
if ! flock -n 9; then
  echo "[$(date -Is)] backup already running; exit" >&2
  exit 0
fi

log() {
  echo "[$(date -Is)] $*"
}

free_gb() {
  df -Pk "$BACKUP_ROOT" | awk 'NR == 2 { print int($4 / 1024 / 1024) }'
}

prune_dirs() {
  local dir="$1"
  local keep="$2"
  find "$dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
    | sort -r \
    | tail -n +"$((keep + 1))" \
    | while IFS= read -r old; do
        log "prune $dir/$old"
        rm -rf -- "$dir/$old"
      done
}

if (( $(free_gb) < MIN_FREE_GB )); then
  log "ERROR free space below ${MIN_FREE_GB}G; refusing backup"
  exit 1
fi

stamp="$(date '+%Y%m%d-%H%M%S')"
day="$(date '+%Y%m%d')"
week="$(date '+%G-W%V')"
dest="$BACKUP_ROOT/daily/$stamp"
tmp="$dest.partial"
mkdir -p "$tmp"

log "backup start db=$DB_CONTAINER redis=$REDIS_CONTAINER dest=$dest"

docker exec "$DB_CONTAINER" sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$tmp/hemall_prod.dump"

docker exec "$REDIS_CONTAINER" redis-cli BGSAVE >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$REDIS_CONTAINER" redis-cli INFO persistence \
    | tr -d '\r' | grep -q '^rdb_bgsave_in_progress:0$'; then
    break
  fi
  sleep 1
done
if ! docker exec "$REDIS_CONTAINER" redis-cli INFO persistence \
  | tr -d '\r' | grep -q '^rdb_last_bgsave_status:ok$'; then
  log "ERROR Redis BGSAVE did not finish successfully"
  exit 1
fi
docker cp "$REDIS_CONTAINER:/data/dump.rdb" "$tmp/redis-dump.rdb" >/dev/null

sha256sum "$tmp/hemall_prod.dump" "$tmp/redis-dump.rdb" > "$tmp/SHA256SUMS"
docker exec -i "$DB_CONTAINER" pg_restore -l < "$tmp/hemall_prod.dump" >/dev/null
chmod 600 "$tmp/hemall_prod.dump" "$tmp/redis-dump.rdb" "$tmp/SHA256SUMS"
mv -- "$tmp" "$dest"
tmp=""

# Sunday backups are copied into a four-week set; hard links keep disk use low.
if [[ "$(date '+%u')" == "7" ]]; then
  weekly="$BACKUP_ROOT/weekly/$week"
  if [[ ! -d "$weekly" ]]; then
    cp -al -- "$dest" "$weekly"
    log "weekly snapshot $weekly"
  fi
fi

prune_dirs "$BACKUP_ROOT/daily" "$DAILY_KEEP"
prune_dirs "$BACKUP_ROOT/weekly" "$WEEKLY_KEEP"

write_backup_metrics "ok"
log "backup ok size=$(du -sh "$dest" | awk '{print $1}') sha256=$(cut -d' ' -f1 "$dest/SHA256SUMS" | paste -sd, -)"
