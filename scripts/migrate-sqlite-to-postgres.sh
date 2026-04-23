#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P)

COMPOSE_FILES=()
SQLITE_DB="${SQLITE_DB_PATH:-${OPEN_WEBUI_SQLITE_DB_PATH:-/data/container/openwebui_data/webui.db}}"
OPENWEBUI_SERVICE="${OPENWEBUI_SERVICE:-}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DATA_DIR="${OPEN_WEBUI_POSTGRES_DATA_DIR:-}"
MIGRATION_IMAGE="${MIGRATION_IMAGE:-python:3.12-slim-bookworm}"
MIGRATION_PACKAGE="${MIGRATION_PACKAGE:-open-webui-sqlite-migration==0.1.22}"
BACKUP_DIR=""
YES=false
BUILD=false
DRY_RUN_ONLY=false
SKIP_DRY_RUN=false

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [options]

Options:
  -f, --compose-file FILE       Compose file to use. Repeat for base + override.
  --sqlite-db PATH              Source Open WebUI SQLite DB. Default: $SQLITE_DB
  --openwebui-service NAME      Open WebUI service name. Default: auto-detected.
  --postgres-service NAME       PostgreSQL service name. Default: $POSTGRES_SERVICE
  --postgres-data-dir PATH      Host dir for PostgreSQL bind volume.
  --backup-dir PATH             Backup output directory.
  --build                       Build the Open WebUI image before startup.
  --dry-run-only                Prepare PostgreSQL and run validation preview only.
  --skip-dry-run                Skip the preview step before the real migration.
  -y, --yes                     Do not ask before truncating/importing PostgreSQL tables.
  -h, --help                    Show this help.

Required environment:
  OPEN_WEBUI_POSTGRES_PASSWORD  PostgreSQL password used by Compose and migration.

Optional environment:
  OPEN_WEBUI_POSTGRES_DB        Default: openwebui
  OPEN_WEBUI_POSTGRES_USER      Default: openwebui
  MIGRATE_DATABASE_URL          Explicit PostgreSQL URL for the migration tool.
  OPEN_WEBUI_BUILD_CONTEXT      Default: this repository root.
  OPENWEBUI_DATA_DIR            Default: directory containing --sqlite-db.
  MIGRATION_DOCKER_NETWORK      Override detected Docker network for the migration container.
EOF
}

log() {
  printf '[open-webui-migrate] %s\n' "$*"
}

fail() {
  printf '[open-webui-migrate] ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--compose-file)
      [[ $# -ge 2 ]] || fail "$1 requires a file"
      COMPOSE_FILES+=("$2")
      shift 2
      ;;
    --sqlite-db)
      [[ $# -ge 2 ]] || fail "$1 requires a path"
      SQLITE_DB="$2"
      shift 2
      ;;
    --openwebui-service)
      [[ $# -ge 2 ]] || fail "$1 requires a service name"
      OPENWEBUI_SERVICE="$2"
      shift 2
      ;;
    --postgres-service)
      [[ $# -ge 2 ]] || fail "$1 requires a service name"
      POSTGRES_SERVICE="$2"
      shift 2
      ;;
    --postgres-data-dir)
      [[ $# -ge 2 ]] || fail "$1 requires a path"
      POSTGRES_DATA_DIR="$2"
      shift 2
      ;;
    --backup-dir)
      [[ $# -ge 2 ]] || fail "$1 requires a path"
      BACKUP_DIR="$2"
      shift 2
      ;;
    --build)
      BUILD=true
      shift
      ;;
    --dry-run-only)
      DRY_RUN_ONLY=true
      shift
      ;;
    --skip-dry-run)
      SKIP_DRY_RUN=true
      shift
      ;;
    -y|--yes)
      YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  fail "docker is required"
fi

abs_existing_file() {
  local path="$1"
  local dir
  local base

  [[ -f "$path" ]] || fail "File not found: $path"
  dir=$(cd "$(dirname "$path")" && pwd -P)
  base=$(basename "$path")
  printf '%s/%s\n' "$dir" "$base"
}

SQLITE_DB_ABS=$(abs_existing_file "$SQLITE_DB")
SQLITE_DIR=$(dirname "$SQLITE_DB_ABS")
STAMP=$(date +%Y%m%d-%H%M%S)

if [[ ${#COMPOSE_FILES[@]} -eq 0 ]]; then
  if [[ -f /data/container/docker-compose.yml && -f "$REPO_ROOT/docker-compose.prod.postgres.yaml" ]]; then
    COMPOSE_FILES=(/data/container/docker-compose.yml "$REPO_ROOT/docker-compose.prod.postgres.yaml")
    : "${OPENWEBUI_SERVICE:=openwebui}"
  elif [[ -f "$REPO_ROOT/docker-compose.yaml" && -f "$REPO_ROOT/docker-compose.postgres.yaml" ]]; then
    COMPOSE_FILES=("$REPO_ROOT/docker-compose.yaml" "$REPO_ROOT/docker-compose.postgres.yaml")
    : "${OPENWEBUI_SERVICE:=open-webui}"
  else
    fail "No Compose files found. Pass -f base.yml -f docker-compose.prod.postgres.yaml."
  fi
fi

: "${OPENWEBUI_SERVICE:=openwebui}"

USES_PROD_POSTGRES_OVERRIDE=false
for compose_file in "${COMPOSE_FILES[@]}"; do
  if [[ "$(basename "$compose_file")" == "docker-compose.prod.postgres.yaml" ]]; then
    USES_PROD_POSTGRES_OVERRIDE=true
  fi
done

if [[ -z "$POSTGRES_DATA_DIR" && "$USES_PROD_POSTGRES_OVERRIDE" == true ]]; then
  POSTGRES_DATA_DIR="$(dirname "$SQLITE_DIR")/openwebui_postgres"
fi

export OPEN_WEBUI_POSTGRES_DB="${OPEN_WEBUI_POSTGRES_DB:-openwebui}"
export OPEN_WEBUI_POSTGRES_USER="${OPEN_WEBUI_POSTGRES_USER:-openwebui}"
export OPEN_WEBUI_POSTGRES_PASSWORD="${OPEN_WEBUI_POSTGRES_PASSWORD:-}"
export OPEN_WEBUI_POSTGRES_DATA_DIR="$POSTGRES_DATA_DIR"
export OPEN_WEBUI_BUILD_CONTEXT="${OPEN_WEBUI_BUILD_CONTEXT:-$REPO_ROOT}"
export OPENWEBUI_DATA_DIR="${OPENWEBUI_DATA_DIR:-$SQLITE_DIR}"

if [[ -z "$OPEN_WEBUI_POSTGRES_PASSWORD" ]]; then
  fail "Set OPEN_WEBUI_POSTGRES_PASSWORD before running this script"
fi

if [[ -z "${MIGRATE_DATABASE_URL:-}" ]]; then
  if [[ "$OPEN_WEBUI_POSTGRES_PASSWORD" =~ [^A-Za-z0-9._~-] ]]; then
    fail "Password contains URL-special characters. Set MIGRATE_DATABASE_URL explicitly."
  fi
  MIGRATE_DATABASE_URL="postgresql://${OPEN_WEBUI_POSTGRES_USER}:${OPEN_WEBUI_POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${OPEN_WEBUI_POSTGRES_DB}"
fi

COMPOSE_ARGS=()
for compose_file in "${COMPOSE_FILES[@]}"; do
  [[ -f "$compose_file" ]] || fail "Compose file not found: $compose_file"
  COMPOSE_ARGS+=(-f "$compose_file")
done

compose() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

require_service() {
  local service="$1"
  compose config --services | grep -Fxq "$service" || fail "Compose service not found: $service"
}

ensure_postgres_data_dir() {
  if [[ -n "$POSTGRES_DATA_DIR" && "$POSTGRES_DATA_DIR" = /* ]]; then
    mkdir -p "$POSTGRES_DATA_DIR" || fail "Cannot create PostgreSQL data dir: $POSTGRES_DATA_DIR"
  fi
}

check_sqlite_integrity() {
  local result

  log "Checking SQLite integrity"
  result=$(
    docker run --rm \
      -v "$SQLITE_DB_ABS:/data/webui.db:ro" \
      "$MIGRATION_IMAGE" \
      python -c 'import sqlite3, sys; c=sqlite3.connect("/data/webui.db"); r=c.execute("pragma integrity_check").fetchone()[0]; c.close(); print(r); sys.exit(0 if r == "ok" else 1)'
  ) || fail "SQLite integrity check failed"

  [[ "$result" == "ok" ]] || fail "SQLite integrity check returned: $result"
}

backup_sqlite() {
  local dest="$BACKUP_DIR/webui.sqlite.before-postgres-$STAMP.db"

  log "Backing up SQLite to $dest"
  cp -p "$SQLITE_DB_ABS" "$dest"
  for suffix in -wal -shm; do
    if [[ -f "$SQLITE_DB_ABS$suffix" ]]; then
      cp -p "$SQLITE_DB_ABS$suffix" "$dest$suffix"
    fi
  done
}

backup_postgres() {
  local dest="$BACKUP_DIR/postgres.before-sqlite-import-$STAMP.dump"

  log "Backing up PostgreSQL to $dest"
  compose exec -T "$POSTGRES_SERVICE" sh -lc \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "$dest"
}

wait_for_postgres() {
  log "Waiting for PostgreSQL"
  for _ in $(seq 1 60); do
    if compose exec -T "$POSTGRES_SERVICE" sh -lc \
      'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; then
      return 0
    fi
    sleep 2
  done
  fail "PostgreSQL did not become ready"
}

wait_for_openwebui_schema() {
  local exists

  log "Waiting for Open WebUI PostgreSQL schema"
  for _ in $(seq 1 90); do
    exists=$(
      compose exec -T "$POSTGRES_SERVICE" sh -lc \
        "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"select count(*) from information_schema.tables where table_schema = 'public' and table_name = 'alembic_version';\"" \
        2>/dev/null | tr -d '[:space:]'
    ) || true
    if [[ "$exists" == "1" ]]; then
      return 0
    fi
    sleep 2
  done

  fail "Open WebUI did not create the PostgreSQL schema in time"
}

detect_migration_network() {
  local cid
  local network

  if [[ -n "${MIGRATION_DOCKER_NETWORK:-}" ]]; then
    printf '%s\n' "$MIGRATION_DOCKER_NETWORK"
    return 0
  fi

  cid=$(compose ps -q "$POSTGRES_SERVICE")
  [[ -n "$cid" ]] || fail "Cannot find PostgreSQL container id"

  network=$(
    docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$cid" \
      | head -n 1
  )
  [[ -n "$network" ]] || fail "Cannot detect Docker network for PostgreSQL"
  printf '%s\n' "$network"
}

run_migrator() {
  docker run --rm \
    --network "$MIGRATION_NETWORK" \
    -e SQLITE_DB_PATH=/data/webui.db \
    -e MIGRATE_DATABASE_URL="$MIGRATE_DATABASE_URL" \
    -e MIGRATION_PACKAGE="$MIGRATION_PACKAGE" \
    -v "$SQLITE_DB_ABS:/data/webui.db:ro" \
    "$MIGRATION_IMAGE" \
    sh -lc 'pip install --no-cache-dir "$MIGRATION_PACKAGE" && exec open-webui-migrate-sqlite "$@"' \
    open-webui-migrate-sqlite "$@"
}

confirm_real_migration() {
  local answer

  if [[ "$YES" == true || "$DRY_RUN_ONLY" == true ]]; then
    return 0
  fi

  cat <<EOF

This will truncate the Open WebUI PostgreSQL tables and import SQLite data.
Backups have been written to:
  $BACKUP_DIR

Type "migrate" to continue:
EOF
  read -r answer
  [[ "$answer" == "migrate" ]] || fail "Aborted"
}

if [[ -z "$BACKUP_DIR" ]]; then
  BACKUP_DIR="$SQLITE_DIR/migration-backups-$STAMP"
fi
mkdir -p "$BACKUP_DIR"

log "Compose files: ${COMPOSE_FILES[*]}"
log "SQLite DB: $SQLITE_DB_ABS"
log "Backup dir: $BACKUP_DIR"
log "Open WebUI service: $OPENWEBUI_SERVICE"
log "PostgreSQL service: $POSTGRES_SERVICE"

require_service "$OPENWEBUI_SERVICE"
require_service "$POSTGRES_SERVICE"
ensure_postgres_data_dir

log "Stopping Open WebUI before backups"
compose stop "$OPENWEBUI_SERVICE" >/dev/null || true

check_sqlite_integrity
backup_sqlite

log "Starting PostgreSQL"
compose up -d "$POSTGRES_SERVICE"
wait_for_postgres

log "Starting Open WebUI once so PostgreSQL migrations create the schema"
if [[ "$BUILD" == true ]]; then
  compose up -d --build "$OPENWEBUI_SERVICE"
else
  compose up -d "$OPENWEBUI_SERVICE"
fi
wait_for_openwebui_schema

log "Stopping Open WebUI before data import"
compose stop "$OPENWEBUI_SERVICE" >/dev/null

backup_postgres
MIGRATION_NETWORK=$(detect_migration_network)
log "Migration Docker network: $MIGRATION_NETWORK"

log "SQLite row counts"
run_migrator --sqlite-counts | tee "$BACKUP_DIR/sqlite-counts.before.log"

log "PostgreSQL row counts before import"
run_migrator --postgres-counts | tee "$BACKUP_DIR/postgres-counts.before.log"

if [[ "$SKIP_DRY_RUN" != true ]]; then
  log "Running dry-run"
  run_migrator --dry-run | tee "$BACKUP_DIR/dry-run.log"
fi

if [[ "$DRY_RUN_ONLY" == true ]]; then
  log "Dry-run complete. Real migration not executed."
  exit 0
fi

confirm_real_migration

log "Running migration"
run_migrator | tee "$BACKUP_DIR/migration.log"

log "Validating migration"
run_migrator --validate | tee "$BACKUP_DIR/validation.log"
if grep -q "Mismatches found" "$BACKUP_DIR/validation.log"; then
  fail "Validation reported mismatches. See $BACKUP_DIR/validation.log"
fi

log "Starting Open WebUI"
if [[ "$BUILD" == true ]]; then
  compose up -d --build "$OPENWEBUI_SERVICE"
else
  compose up -d "$OPENWEBUI_SERVICE"
fi

if compose config --services | grep -Fxq caddy; then
  log "Starting Caddy"
  compose up -d caddy
fi

log "PostgreSQL row counts after import"
run_migrator --postgres-counts | tee "$BACKUP_DIR/postgres-counts.after.log"

log "Done. Keep the SQLite backup until you have checked the UI manually."
