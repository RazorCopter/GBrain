#!/bin/sh
# Collaudo distruttivo soltanto per stack sintetici con prefisso gbrain_phase1_.
set -eu

umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
RUN_ID="${1:-$(date -u '+%Y%m%dT%H%M%SZ')}"
SOURCE_PROJECT="gbrain_phase1_src_$RUN_ID"
RESTORE_PROJECT="gbrain_phase1_restore_$RUN_ID"
WEB_PORT="${GBRAIN_PHASE1_WEB_PORT:-18080}"
RESTORE_WEB_PORT="${GBRAIN_PHASE1_RESTORE_WEB_PORT:-18081}"
RUNTIME_ROOT="$PROJECT_ROOT/runtime/phase1-$RUN_ID"
REPORT="$PROJECT_ROOT/phase1-runtime-$RUN_ID.log"
SOURCE_ENV="$RUNTIME_ROOT/source.env"
RESTORE_ENV="$RUNTIME_ROOT/restore.env"
COOKIE_JAR="$RUNTIME_ROOT/cookies.txt"
PASSWORD='Synthetic-Owner-Password-2026!'
POSTGRES_PASSWORD="synthetic-postgres-$RUN_ID"
AUTH_SECRET="synthetic-auth-secret-$RUN_ID-0123456789abcdef"
NEO4J_PASSWORD="synthetic-neo4j-$RUN_ID"
BACKUP_ROOT="$RUNTIME_ROOT/source/backup-output"
CLEANUP_DONE=0

fail() { printf 'ERRORE: %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*" | tee -a "$REPORT"; }
require() { command -v "$1" >/dev/null 2>&1 || fail "comando richiesto assente: $1"; }

case "$RUN_ID" in ''|*[!a-zA-Z0-9_.-]*) fail "RUN_ID non sicuro" ;; esac
case "$SOURCE_PROJECT:$RESTORE_PROJECT" in gbrain_phase1_*:gbrain_phase1_*) ;; *) fail "prefisso progetto non sicuro" ;; esac
[ "$SOURCE_PROJECT" != "$RESTORE_PROJECT" ] || fail "progetti source e restore devono essere distinti"
for command_name in docker curl grep sed tr date tee mkdir rm sleep find head tail sha256sum; do require "$command_name"; done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 assente"
docker info --format '{{.OSType}}' | grep -qx linux || fail "serve un daemon Docker Linux operativo"

mkdir -p "$RUNTIME_ROOT/source/data" "$RUNTIME_ROOT/source/cache" "$RUNTIME_ROOT/source/import" \
    "$RUNTIME_ROOT/source/backup" "$BACKUP_ROOT" "$RUNTIME_ROOT/restore/data" \
    "$RUNTIME_ROOT/restore/cache" "$RUNTIME_ROOT/restore/import" "$RUNTIME_ROOT/restore/backup"

write_env() {
    env_file=$1 project=$2 port=$3 root=$4
    cat > "$env_file" <<EOF
COMPOSE_PROJECT_NAME=$project
COMPOSE_PROFILES=compact
GBRAIN_IMAGE_TAG=phase1-$RUN_ID
GBRAIN_ENVIRONMENT=test
GBRAIN_LOG_LEVEL=INFO
GBRAIN_PUBLIC_URL=http://127.0.0.1:$port
GBRAIN_DEMO_MODE=true
GBRAIN_SESSION_SECURE_COOKIE=false
WEB_BIND_ADDRESS=127.0.0.1
WEB_PORT=$port
DATA_DIR=$root/data
CACHE_DIR=$root/cache
IMPORT_DIR=$root/import
BACKUP_DIR=$root/backup
POSTGRES_DB=gbrain_phase1
POSTGRES_USER=gbrain_test
GBRAIN_POSTGRES_PASSWORD=$POSTGRES_PASSWORD
GBRAIN_AUTH_SECRET=$AUTH_SECRET
GBRAIN_NEO4J_PASSWORD=$NEO4J_PASSWORD
EOF
}
write_env "$SOURCE_ENV" "$SOURCE_PROJECT" "$WEB_PORT" "$RUNTIME_ROOT/source"
write_env "$RESTORE_ENV" "$RESTORE_PROJECT" "$RESTORE_WEB_PORT" "$RUNTIME_ROOT/restore"

compose_source() { docker compose --env-file "$SOURCE_ENV" --project-name "$SOURCE_PROJECT" --profile compact "$@"; }
compose_restore() { docker compose --env-file "$RESTORE_ENV" --project-name "$RESTORE_PROJECT" --profile compact "$@"; }

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    case "$SOURCE_PROJECT" in gbrain_phase1_*) compose_source down --volumes --remove-orphans >/dev/null 2>&1 || true ;; esac
    case "$RESTORE_PROJECT" in gbrain_phase1_*) compose_restore down --volumes --remove-orphans >/dev/null 2>&1 || true ;; esac
    case "$RUNTIME_ROOT" in "$PROJECT_ROOT"/runtime/phase1-*) rm -rf -- "$RUNTIME_ROOT" ;; esac
    CLEANUP_DONE=1
    printf 'cleanup=%s exit=%s\n' "$CLEANUP_DONE" "$status" >> "$REPORT"
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

log "run_id=$RUN_ID source=$SOURCE_PROJECT restore=$RESTORE_PROJECT"
log "runtime=$(docker version --format 'client={{.Client.Version}} server={{.Server.Version}} os={{.Server.Os}} arch={{.Server.Arch}}')"
log "compose=$(docker compose version --short)"
log "docker_info=$(docker info --format 'cpus={{.NCPU}} memory={{.MemTotal}} cgroup={{.CgroupVersion}} driver={{.Driver}}')"
log "disk_before=$(docker system df --format '{{json .}}' | tr '\n' ' ')"

compose_source config --quiet
compose_source build app
compose_source up -d postgres neo4j
compose_source run --rm app migrate
compose_source exec -T postgres psql -U gbrain_test -d gbrain_phase1 -Atc \
    "SELECT to_regclass('public.procrastinate_jobs');" | grep -qx procrastinate_jobs \
    || fail "schema Procrastinate assente dopo la migrazione"
compose_source up -d app worker
compose_source ps >> "$REPORT"

BASE="http://127.0.0.1:$WEB_PORT"
until curl --fail --silent "$BASE/health/ready" >/dev/null; do sleep 2; done
[ "$(curl --silent --output /dev/null --write-out '%{http_code}' "$BASE/api/profile")" = 401 ] \
    || fail "API privata non ha rifiutato la richiesta anonima"
BOOTSTRAP=$(curl --fail --silent --cookie-jar "$COOKIE_JAR" -H 'Content-Type: application/json' \
    -d "{\"username\":\"owner.test\",\"display_name\":\"Owner Sintetico\",\"password\":\"$PASSWORD\"}" \
    "$BASE/api/auth/bootstrap")
printf '%s' "$BOOTSTRAP" | grep -q '"authenticated":true' || fail "bootstrap non autenticato"
CSRF=$(printf '%s' "$BOOTSTRAP" | sed -n 's/.*"csrf_token":"\([^"]*\)".*/\1/p')
[ -n "$CSRF" ] || fail "token CSRF assente"
[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --cookie "$COOKIE_JAR" -X POST "$BASE/api/auth/logout")" = 403 ] \
    || fail "mutazione senza header CSRF non rifiutata"

compose_source exec -T app python -m gbrain.cli demo-fixtures
compose_source exec -T app python -m gbrain.cli demo-fixtures
DEMO_STATUS=$(curl --fail --silent --cookie "$COOKIE_JAR" "$BASE/api/demo/status")
printf '%s' "$DEMO_STATUS" | grep -q '"seeded":true' || fail "fixture demo non pronte"

JOB_ID=$(compose_source exec -T app procrastinate -a gbrain.worker.procrastinate_app defer gbrain.heartbeat '{}' | tr -d '\r' | grep -Eo '[0-9]+' | tail -n 1)
[ -n "$JOB_ID" ] || fail "ID job Procrastinate non rilevato"
elapsed=0
while [ "$elapsed" -lt 60 ]; do
    JOB_STATUS=$(compose_source exec -T postgres psql -U gbrain_test -d gbrain_phase1 -Atc "SELECT status FROM procrastinate_jobs WHERE id=$JOB_ID;" | tr -d '\r')
    [ "$JOB_STATUS" = "succeeded" ] && break
    sleep 2; elapsed=$((elapsed + 2))
done
[ "$JOB_STATUS" = "succeeded" ] || fail "job Procrastinate non completato: $JOB_STATUS"

compose_source exec -T postgres psql -U gbrain_test -d gbrain_phase1 -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO exclusions (id, source_kind, external_id_hash, reason_code, created_at)
VALUES ('11111111-1111-1111-1111-111111111111', 'demo_fixture', repeat('a', 64), 'synthetic_test', now());
INSERT INTO deletion_records (id, source_kind, external_id_hash, reason_code, requested_at, projection_cleanup_pending)
VALUES ('22222222-2222-2222-2222-222222222222', 'demo_fixture', repeat('b', 64), 'synthetic_test', now(), true);
INSERT INTO outbox_events (id, stable_key, aggregate_type, aggregate_id, event_type, payload, status, retry_count, max_retries, available_at, created_at)
VALUES ('33333333-3333-3333-3333-333333333333', 'phase1-test-record', 'test', '44444444-4444-4444-4444-444444444444', 'test.record', '{"synthetic":true}', 'pending', 0, 5, now(), now());
SQL

compose_source restart app worker postgres
until curl --fail --silent "$BASE/health/ready" >/dev/null; do sleep 2; done
LOGIN=$(curl --fail --silent --cookie-jar "$COOKIE_JAR" -H 'Content-Type: application/json' \
    -d "{\"username\":\"owner.test\",\"password\":\"$PASSWORD\"}" "$BASE/api/auth/login")
printf '%s' "$LOGIN" | grep -q '"authenticated":true' || fail "login dopo riavvio fallito"
compose_source exec -T postgres psql -U gbrain_test -d gbrain_phase1 -Atc \
    "SELECT (SELECT count(*) FROM owner_accounts),(SELECT count(*) FROM auth_sessions),(SELECT count(*) FROM import_jobs WHERE checkpoint->'progress'->>'complete'='true');" >> "$REPORT"

compose_source exec -T app python - <<'PY'
import asyncio
from neo4j import AsyncGraphDatabase
from gbrain.config import get_settings
async def main():
    settings = get_settings()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_secret),
    )
    try:
        async with driver.session(database=settings.neo4j_database) as session:
            await session.run("MERGE (n:Phase1Synthetic {id: 'backup-marker'}) SET n.synthetic = true")
    finally:
        await driver.close()
asyncio.run(main())
PY

log "stats_sample_begin"
docker stats --no-stream $(compose_source ps -q) >> "$REPORT"
for cid in $(compose_source ps -q); do docker inspect --format '{{.Name}} mem={{.HostConfig.Memory}} nano_cpus={{.HostConfig.NanoCpus}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid"; done >> "$REPORT"
log "stats_sample_end"

set +e
(
    set -a
    . "$SOURCE_ENV"
    set +a
    GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE=true sh "$PROJECT_ROOT/scripts/backup.sh" \
        --destination "$BACKUP_ROOT"
) >> "$REPORT" 2>&1
FAULT_STATUS=$?
set -e
[ "$FAULT_STATUS" -ne 0 ] || fail "hook di guasto non ha causato il fallimento atteso"
compose_source ps --status running --services | grep -qx app || fail "app non ripristinata dopo guasto backup"
compose_source ps --status running --services | grep -qx worker || fail "worker non ripristinato dopo guasto backup"

(
    set -a
    . "$SOURCE_ENV"
    set +a
    sh "$PROJECT_ROOT/scripts/backup.sh" --destination "$BACKUP_ROOT"
) | tee -a "$REPORT"
BACKUP_DIR=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'gbrain-*' | head -n 1)
[ -n "$BACKUP_DIR" ] || fail "backup finale non trovato"
( cd "$BACKUP_DIR" && sha256sum --check --strict checksums.sha256 ) | tee -a "$REPORT"

compose_restore up --no-start app worker postgres neo4j
(
    set -a
    . "$RESTORE_ENV"
    set +a
    GBRAIN_RESTORE_SOURCE_PROJECT="$SOURCE_PROJECT" sh "$PROJECT_ROOT/scripts/restore.sh" \
        --backup "$BACKUP_DIR" --confirm RESTORE-GBRAIN-ERASE-TARGET
) | tee -a "$REPORT"
compose_restore start postgres neo4j
compose_restore up -d app worker
RESTORE_BASE="http://127.0.0.1:$RESTORE_WEB_PORT"
until curl --fail --silent "$RESTORE_BASE/health/ready" >/dev/null; do sleep 2; done
RESTORE_LOGIN=$(curl --fail --silent -H 'Content-Type: application/json' \
    -d "{\"username\":\"owner.test\",\"password\":\"$PASSWORD\"}" "$RESTORE_BASE/api/auth/login")
printf '%s' "$RESTORE_LOGIN" | grep -q '"authenticated":true' || fail "login dopo restore fallito"
COUNTS=$(compose_restore exec -T postgres psql -U gbrain_test -d gbrain_phase1 -Atc \
    "SELECT (SELECT count(*) FROM owner_accounts),(SELECT count(*) FROM import_jobs WHERE checkpoint->'progress'->>'complete'='true'),(SELECT count(*) FROM exclusions WHERE external_id_hash=repeat('a', 64)),(SELECT count(*) FROM deletion_records WHERE external_id_hash=repeat('b', 64)),(SELECT count(*) FROM outbox_events WHERE stable_key='phase1-test-record');" | tr -d '\r')
[ "$COUNTS" = "1|1|1|1|1" ] || fail "conteggi PostgreSQL ripristinati inattesi: $COUNTS"
NEO_COUNT=$(compose_restore exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (n:Phase1Synthetic {id: 'backup-marker', synthetic: true}) RETURN count(n);" | tail -n 1 | tr -d '\r')
[ "$NEO_COUNT" = "1" ] || fail "marker Neo4j non ripristinato: $NEO_COUNT"

log "disk_after=$(docker system df --format '{{json .}}' | tr '\n' ' ')"
compose_source logs --no-color app worker postgres neo4j > "$RUNTIME_ROOT/logs.txt"
if grep -F "$PASSWORD" "$RUNTIME_ROOT/logs.txt" >/dev/null || grep -F "$AUTH_SECRET" "$RUNTIME_ROOT/logs.txt" >/dev/null; then
    fail "segreto sintetico presente nei log"
fi
log "RESULT=SUPERATO"
