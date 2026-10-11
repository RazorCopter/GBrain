#!/bin/sh
# Backup coordinato della fondazione GBrain Fase 1.
set -eu

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
COMPOSE_FILE="$PROJECT_ROOT/compose.yaml"
DESTINATION="${GBRAIN_BACKUP_DESTINATION:-}"
STOP_TIMEOUT="${GBRAIN_BACKUP_STOP_TIMEOUT:-45}"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-gbrain}"
# Hook esclusivamente di collaudo, disabilitato per default. Consente di
# verificare il recovery del trap subito dopo la quiescenza, prima dei dump.
TEST_FAIL_AFTER_QUIESCE="${GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE:-false}"

usage() {
    cat <<'EOF'
Uso: scripts/backup.sh --destination DIRECTORY

Opzioni:
  --destination DIRECTORY  Directory host esistente, non simbolica e scrivibile.
  --help                   Mostra questo messaggio.

La directory può essere impostata anche con GBRAIN_BACKUP_DESTINATION.
Lo script non include .env né i valori dei secret nel backup.
EOF
}

fail() {
    printf 'ERRORE: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --destination)
            [ "$#" -ge 2 ] || fail "valore mancante per --destination"
            DESTINATION=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "argomento sconosciuto: $1"
            ;;
    esac
done

case "$STOP_TIMEOUT" in
    ''|*[!0-9]*) fail "GBRAIN_BACKUP_STOP_TIMEOUT deve essere un intero positivo" ;;
esac
[ "$STOP_TIMEOUT" -gt 0 ] || fail "GBRAIN_BACKUP_STOP_TIMEOUT deve essere maggiore di zero"
case "$COMPOSE_PROJECT" in
    ''|*[!a-zA-Z0-9_.-]*) fail "COMPOSE_PROJECT_NAME contiene caratteri non sicuri" ;;
esac
case "$TEST_FAIL_AFTER_QUIESCE" in
    true|false) ;;
    *) fail "GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE deve essere true o false" ;;
esac
[ -n "$DESTINATION" ] || fail "specificare --destination o GBRAIN_BACKUP_DESTINATION"
case "$DESTINATION" in
    /*) ;;
    *) fail "la directory di destinazione deve essere un percorso assoluto" ;;
esac
[ -d "$DESTINATION" ] || fail "la directory di destinazione non esiste: $DESTINATION"
[ ! -L "$DESTINATION" ] || fail "la directory di destinazione non può essere un link simbolico"
[ -w "$DESTINATION" ] || fail "la directory di destinazione non è scrivibile"
[ -f "$COMPOSE_FILE" ] || fail "compose.yaml non trovato"

for command_name in docker sha256sum tar grep tr date; do
    command -v "$command_name" >/dev/null 2>&1 || fail "comando richiesto non disponibile: $command_name"
done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 non è disponibile"

compose() {
    docker compose \
        --project-directory "$PROJECT_ROOT" \
        --file "$COMPOSE_FILE" \
        --profile compact \
        "$@"
}

compose config --quiet

is_running() {
    compose ps --status running --services | grep -qx "$1"
}

container_id() {
    compose ps --all --quiet "$1"
}

wait_healthy() {
    service_name=$1
    timeout_seconds=$2
    elapsed=0
    while [ "$elapsed" -lt "$timeout_seconds" ]; do
        service_container=$(container_id "$service_name")
        if [ -n "$service_container" ]; then
            health_status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service_container" 2>/dev/null || true)
            [ "$health_status" = "healthy" ] && return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

single_line() {
    value=$1
    label=$2
    case "$value" in
        *"
"*|*"	"*) fail "$label contiene caratteri non ammessi nel manifest" ;;
    esac
    [ -n "$value" ] || fail "$label è vuoto"
    printf '%s' "$value"
}

manifest_line() {
    key=$1
    value=$2
    single_line "$value" "$key" >/dev/null
    printf '%s\t%s\n' "$key" "$value" >> "$MANIFEST"
}

LOCK_DIR="$DESTINATION/.gbrain-backup.lock"
if ! mkdir -m 700 "$LOCK_DIR" 2>/dev/null; then
    fail "backup già in corso o lock residuo presente: $LOCK_DIR"
fi

TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
BACKUP_ID="gbrain-$TIMESTAMP-$$"
STAGING="$DESTINATION/.gbrain-backup-tmp-$TIMESTAMP-$$"
FINAL="$DESTINATION/$BACKUP_ID"
NEO_TEMP="/data/.gbrain-backup-$TIMESTAMP-$$"
NEO_DUMP_INTERNAL="$NEO_TEMP/neo4j.dump"
MANIFEST="$STAGING/manifest.tsv"
APP_WAS_RUNNING=0
WORKER_WAS_RUNNING=0
NEO_WAS_STOPPED=0
NEO_TEMP_CREATED=0
QUIESCED=0
COMPLETED=0

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM

    if [ "$COMPLETED" -ne 1 ]; then
        if [ "$NEO_WAS_STOPPED" -eq 1 ]; then
            if [ "$NEO_TEMP_CREATED" -eq 1 ]; then
                compose run --rm --no-deps -T --entrypoint /bin/sh neo4j \
                    -ec 'case "$1" in /data/.gbrain-backup-*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
                    sh "$NEO_TEMP" >/dev/null 2>&1 || true
            fi
            compose start neo4j >/dev/null 2>&1 || true
            wait_healthy neo4j 180 >/dev/null 2>&1 || true
        fi
        if [ "$QUIESCED" -eq 1 ]; then
            if [ "$APP_WAS_RUNNING" -eq 1 ]; then
                compose start app >/dev/null 2>&1 || true
                wait_healthy app 120 >/dev/null 2>&1 || true
            fi
            if [ "$WORKER_WAS_RUNNING" -eq 1 ]; then
                compose start worker >/dev/null 2>&1 || true
            fi
        fi
        if [ -n "$STAGING" ] && [ -d "$STAGING" ]; then
            case "$STAGING" in
                "$DESTINATION"/.gbrain-backup-tmp-*) rm -rf -- "$STAGING" ;;
            esac
        fi
    fi

    rmdir "$LOCK_DIR" 2>/dev/null || true
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

[ ! -e "$STAGING" ] || fail "directory temporanea già esistente: $STAGING"
[ ! -e "$FINAL" ] || fail "destinazione finale già esistente: $FINAL"
mkdir -m 700 "$STAGING"

POSTGRES_CONTAINER=$(container_id postgres)
NEO4J_CONTAINER=$(container_id neo4j)
APP_CONTAINER=$(container_id app)
WORKER_CONTAINER=$(container_id worker)
[ -n "$POSTGRES_CONTAINER" ] || fail "container PostgreSQL del progetto non trovato"
[ -n "$NEO4J_CONTAINER" ] || fail "container Neo4j del progetto non trovato"
[ -n "$APP_CONTAINER" ] || fail "container app del progetto non trovato"
[ -n "$WORKER_CONTAINER" ] || fail "container worker del progetto non trovato"
is_running postgres || fail "PostgreSQL deve essere in esecuzione"
is_running neo4j || fail "Neo4j deve essere in esecuzione"
wait_healthy postgres 30 || fail "PostgreSQL non è healthy"
wait_healthy neo4j 30 || fail "Neo4j non è healthy"

if is_running app; then APP_WAS_RUNNING=1; fi
if is_running worker; then WORKER_WAS_RUNNING=1; fi
if [ "$WORKER_WAS_RUNNING" -eq 1 ] && [ "$APP_WAS_RUNNING" -ne 1 ]; then
    fail "stato inatteso: worker attivo con app ferma"
fi

POSTGRES_DB=$(compose exec -T postgres /bin/sh -ec 'printf %s "$POSTGRES_DB"')
POSTGRES_USER=$(compose exec -T postgres /bin/sh -ec 'printf %s "$POSTGRES_USER"')
POSTGRES_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$POSTGRES_CONTAINER")
NEO4J_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$NEO4J_CONTAINER")
APPLICATION_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$APP_CONTAINER")
POSTGRES_VERSION=$(compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --command 'SHOW server_version;' | tr -d '\r\n')
ALEMBIC_REVISION=$(compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --command 'SELECT version_num FROM alembic_version;' | tr -d '\r\n')
NEO4J_VERSION=$(compose exec -T neo4j neo4j-admin --version | tr -d '\r\n')
NEO4J_DATABASE=neo4j

single_line "$POSTGRES_DB" POSTGRES_DB >/dev/null
single_line "$POSTGRES_USER" POSTGRES_USER >/dev/null
single_line "$POSTGRES_IMAGE" POSTGRES_IMAGE >/dev/null
single_line "$NEO4J_IMAGE" NEO4J_IMAGE >/dev/null
single_line "$APPLICATION_IMAGE" APPLICATION_IMAGE >/dev/null
single_line "$POSTGRES_VERSION" POSTGRES_VERSION >/dev/null
single_line "$ALEMBIC_REVISION" ALEMBIC_REVISION >/dev/null
single_line "$NEO4J_VERSION" NEO4J_VERSION >/dev/null

if command -v git >/dev/null 2>&1 && git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_COMMIT=$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || printf 'non-disponibile')
    if git -C "$PROJECT_ROOT" diff --quiet --ignore-submodules -- 2>/dev/null \
        && git -C "$PROJECT_ROOT" diff --cached --quiet --ignore-submodules -- 2>/dev/null; then
        GIT_DIRTY=false
    else
        GIT_DIRTY=true
    fi
else
    GIT_COMMIT=non-disponibile
    GIT_DIRTY=non-disponibile
fi

tar -C "$PROJECT_ROOT" -cf "$STAGING/configuration.tar" \
    compose.yaml example.env Dockerfile pyproject.toml uv.lock alembic.ini \
    frontend/package.json frontend/package-lock.json backend/migrations

printf '%s\n' "Quiescenza controllata di app e worker..."
compose stop -t "$STOP_TIMEOUT" app worker
QUIESCED=1

if [ "$TEST_FAIL_AFTER_QUIESCE" = "true" ]; then
    printf '%s\n' "Hook di collaudo: guasto controllato dopo la quiescenza." >&2
    fail "guasto controllato richiesto da GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE"
fi

printf '%s\n' "Creazione dump consistente PostgreSQL..."
compose exec -T postgres pg_dump \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --format=custom \
    --no-owner \
    --no-privileges \
    --clean \
    --if-exists \
    > "$STAGING/postgresql.dump"
[ -s "$STAGING/postgresql.dump" ] || fail "il dump PostgreSQL è vuoto"
compose exec -T postgres pg_restore --list < "$STAGING/postgresql.dump" >/dev/null

printf '%s\n' "Arresto controllato Neo4j per dump Community offline..."
compose stop -t "$STOP_TIMEOUT" neo4j
NEO_WAS_STOPPED=1

compose run --rm --no-deps -T --entrypoint /bin/sh neo4j \
    -ec 'umask 077; mkdir "$1"; chown neo4j:neo4j "$1"' sh "$NEO_TEMP"
NEO_TEMP_CREATED=1
compose run --rm --no-deps -T --entrypoint neo4j-admin neo4j \
    database dump "$NEO4J_DATABASE" "--to-path=$NEO_TEMP"
compose run --rm --no-deps -T --entrypoint /bin/sh neo4j \
    -ec 'test -s "$1"; cat "$1"' sh "$NEO_DUMP_INTERNAL" \
    > "$STAGING/neo4j.dump"
[ -s "$STAGING/neo4j.dump" ] || fail "il dump Neo4j è vuoto"
compose run --rm --no-deps -T --entrypoint /bin/sh neo4j \
    -ec 'case "$1" in /data/.gbrain-backup-*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
    sh "$NEO_TEMP"
NEO_TEMP_CREATED=0

compose start neo4j
wait_healthy neo4j 180 || fail "Neo4j non è tornato healthy dopo il dump"
NEO_WAS_STOPPED=0
if [ "$APP_WAS_RUNNING" -eq 1 ]; then
    compose start app
    wait_healthy app 120 || fail "app non è tornata healthy dopo il backup"
fi
if [ "$WORKER_WAS_RUNNING" -eq 1 ]; then
    compose start worker
fi
QUIESCED=0

: > "$MANIFEST"
manifest_line format_version 1
manifest_line backup_id "$BACKUP_ID"
manifest_line created_utc "$TIMESTAMP"
manifest_line compose_project "$COMPOSE_PROJECT"
manifest_line git_commit "$GIT_COMMIT"
manifest_line git_dirty "$GIT_DIRTY"
manifest_line application_image "$APPLICATION_IMAGE"
manifest_line postgres_image "$POSTGRES_IMAGE"
manifest_line postgres_server_version "$POSTGRES_VERSION"
manifest_line postgres_database "$POSTGRES_DB"
manifest_line postgres_user "$POSTGRES_USER"
manifest_line alembic_revision "$ALEMBIC_REVISION"
manifest_line postgres_dump postgresql.dump
manifest_line postgres_method pg_dump-custom-consistent
manifest_line neo4j_image "$NEO4J_IMAGE"
manifest_line neo4j_version "$NEO4J_VERSION"
manifest_line neo4j_database "$NEO4J_DATABASE"
manifest_line neo4j_dump neo4j.dump
manifest_line neo4j_method neo4j-admin-database-dump-offline
manifest_line configuration_archive configuration.tar
manifest_line secrets_included false

(
    cd "$STAGING"
    sha256sum configuration.tar manifest.tsv neo4j.dump postgresql.dump > checksums.sha256
    sha256sum --check checksums.sha256 >/dev/null
)

mv "$STAGING" "$FINAL"
STAGING=""
COMPLETED=1
rmdir "$LOCK_DIR"
trap - EXIT HUP INT TERM
printf 'Backup completato: %s\n' "$FINAL"
printf '%s\n' "Secret esclusi; conservare separatamente la configurazione protetta per il disaster recovery."
