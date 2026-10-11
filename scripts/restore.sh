#!/bin/sh
# Ripristino distruttivo, esplicito e verificato della fondazione GBrain Fase 1.
set -eu

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
COMPOSE_FILE="$PROJECT_ROOT/compose.yaml"
BACKUP_DIR=""
CONFIRMATION=""
STOP_TIMEOUT="${GBRAIN_RESTORE_STOP_TIMEOUT:-45}"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-gbrain}"
SOURCE_PROJECT="${GBRAIN_RESTORE_SOURCE_PROJECT:-$COMPOSE_PROJECT}"
EXPECTED_CONFIRMATION="RESTORE-GBRAIN-ERASE-TARGET"

usage() {
    cat <<EOF
Uso: scripts/restore.sh --backup DIRECTORY --confirm $EXPECTED_CONFIRMATION

Opzioni:
  --backup DIRECTORY       Directory di backup completa prodotta da backup.sh.
  --confirm FRASE          Conferma distruttiva esatta.
  --help                   Mostra questo messaggio.

Il comando rifiuta uno stack avviato: usare uno stack isolato, non esposto alla
rete di produzione, creato con volumi nuovi e poi arrestato con Compose.
EOF
}

fail() {
    printf 'ERRORE: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --backup)
            [ "$#" -ge 2 ] || fail "valore mancante per --backup"
            BACKUP_DIR=$2
            shift 2
            ;;
        --confirm)
            [ "$#" -ge 2 ] || fail "valore mancante per --confirm"
            CONFIRMATION=$2
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
    ''|*[!0-9]*) fail "GBRAIN_RESTORE_STOP_TIMEOUT deve essere un intero positivo" ;;
esac
[ "$STOP_TIMEOUT" -gt 0 ] || fail "GBRAIN_RESTORE_STOP_TIMEOUT deve essere maggiore di zero"
case "$COMPOSE_PROJECT" in
    ''|*[!a-zA-Z0-9_.-]*) fail "COMPOSE_PROJECT_NAME contiene caratteri non sicuri" ;;
esac
case "$SOURCE_PROJECT" in
    ''|*[!a-zA-Z0-9_.-]*) fail "GBRAIN_RESTORE_SOURCE_PROJECT contiene caratteri non sicuri" ;;
esac
[ "$COMPOSE_PROJECT" != "$SOURCE_PROJECT" ] \
    || fail "il progetto target deve essere diverso dal progetto sorgente del backup"
[ "$CONFIRMATION" = "$EXPECTED_CONFIRMATION" ] || fail "conferma mancante o errata"
[ -n "$BACKUP_DIR" ] || fail "specificare --backup"
case "$BACKUP_DIR" in
    /*) ;;
    *) fail "la directory di backup deve essere un percorso assoluto" ;;
esac
[ -d "$BACKUP_DIR" ] || fail "directory di backup inesistente: $BACKUP_DIR"
[ ! -L "$BACKUP_DIR" ] || fail "la directory di backup non può essere un link simbolico"
[ -f "$COMPOSE_FILE" ] || fail "compose.yaml non trovato"

for command_name in docker sha256sum awk grep tr; do
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

manifest_value() {
    key=$1
    value=$(awk -F '\t' -v wanted="$key" '$1 == wanted { count++; result=$2 } END { if (count != 1) exit 1; print result }' "$MANIFEST") \
        || fail "chiave manifest assente o duplicata: $key"
    [ -n "$value" ] || fail "valore manifest vuoto: $key"
    printf '%s' "$value"
}

safe_member() {
    value=$1
    label=$2
    case "$value" in
        ''|*/*|*\\*|.|..) fail "$label non è un nome file sicuro" ;;
    esac
    printf '%s' "$value"
}

container_id() {
    compose ps --all --quiet "$1"
}

is_running() {
    compose ps --status running --services | grep -qx "$1"
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

compose config --quiet

MANIFEST="$BACKUP_DIR/manifest.tsv"
CHECKSUMS="$BACKUP_DIR/checksums.sha256"
[ -f "$MANIFEST" ] || fail "manifest.tsv assente"
[ ! -L "$MANIFEST" ] || fail "manifest.tsv non può essere un link simbolico"
[ -f "$CHECKSUMS" ] || fail "checksums.sha256 assente"
[ ! -L "$CHECKSUMS" ] || fail "checksums.sha256 non può essere un link simbolico"

EXPECTED_FILES='configuration.tar
manifest.tsv
neo4j.dump
postgresql.dump'
seen_configuration=0
seen_manifest=0
seen_neo4j=0
seen_postgresql=0
checksum_count=0
while IFS= read -r checksum_line; do
    [ -n "$checksum_line" ] || continue
    checksum_digest=${checksum_line%%  *}
    checksum_file=${checksum_line#*  }
    [ "$checksum_file" != "$checksum_line" ] || fail "riga checksum malformata"
    case "$checksum_digest" in
        *[!0-9a-f]*|'') fail "digest checksum non valido" ;;
    esac
    [ "${#checksum_digest}" -eq 64 ] || fail "digest checksum di lunghezza non valida"
    printf '%s\n' "$EXPECTED_FILES" | grep -Fxq "$checksum_file" \
        || fail "nome file non consentito in checksums.sha256: $checksum_file"
    case "$checksum_file" in
        configuration.tar)
            [ "$seen_configuration" -eq 0 ] || fail "checksum duplicato: $checksum_file"
            seen_configuration=1
            ;;
        manifest.tsv)
            [ "$seen_manifest" -eq 0 ] || fail "checksum duplicato: $checksum_file"
            seen_manifest=1
            ;;
        neo4j.dump)
            [ "$seen_neo4j" -eq 0 ] || fail "checksum duplicato: $checksum_file"
            seen_neo4j=1
            ;;
        postgresql.dump)
            [ "$seen_postgresql" -eq 0 ] || fail "checksum duplicato: $checksum_file"
            seen_postgresql=1
            ;;
    esac
    checksum_count=$((checksum_count + 1))
done < "$CHECKSUMS"

[ "$checksum_count" -eq 4 ] \
    && [ "$seen_configuration" -eq 1 ] \
    && [ "$seen_manifest" -eq 1 ] \
    && [ "$seen_neo4j" -eq 1 ] \
    && [ "$seen_postgresql" -eq 1 ] \
    || fail "checksums.sha256 deve contenere una volta ciascuno i quattro file previsti"
(
    cd "$BACKUP_DIR"
    sha256sum --check --strict checksums.sha256
) || fail "verifica checksum fallita"

FORMAT_VERSION=$(manifest_value format_version)
[ "$FORMAT_VERSION" = "1" ] || fail "versione manifest non supportata: $FORMAT_VERSION"
[ "$(manifest_value compose_project)" = "$SOURCE_PROJECT" ] \
    || fail "progetto manifest inatteso rispetto a GBRAIN_RESTORE_SOURCE_PROJECT"
[ "$(manifest_value secrets_included)" = "false" ] || fail "manifest non conforme: i secret non devono essere inclusi"
[ "$(manifest_value postgres_method)" = "pg_dump-custom-consistent" ] || fail "metodo PostgreSQL non supportato"
[ "$(manifest_value neo4j_method)" = "neo4j-admin-database-dump-offline" ] || fail "metodo Neo4j non supportato"
[ "$(manifest_value configuration_archive)" = "configuration.tar" ] || fail "nome archivio configurazione inatteso"
[ "$(manifest_value postgres_dump)" = "postgresql.dump" ] || fail "nome dump PostgreSQL inatteso"
[ "$(manifest_value neo4j_dump)" = "neo4j.dump" ] || fail "nome dump Neo4j inatteso"

POSTGRES_DUMP=$(safe_member "$(manifest_value postgres_dump)" postgres_dump)
NEO4J_DUMP=$(safe_member "$(manifest_value neo4j_dump)" neo4j_dump)
CONFIGURATION_ARCHIVE=$(safe_member "$(manifest_value configuration_archive)" configuration_archive)
POSTGRES_DB=$(manifest_value postgres_database)
POSTGRES_USER=$(manifest_value postgres_user)
POSTGRES_IMAGE_EXPECTED=$(manifest_value postgres_image)
POSTGRES_VERSION_EXPECTED=$(manifest_value postgres_server_version)
NEO4J_IMAGE_EXPECTED=$(manifest_value neo4j_image)
NEO4J_VERSION_EXPECTED=$(manifest_value neo4j_version)
NEO4J_DATABASE=$(manifest_value neo4j_database)
ALEMBIC_REVISION_EXPECTED=$(manifest_value alembic_revision)

[ -f "$BACKUP_DIR/$POSTGRES_DUMP" ] || fail "dump PostgreSQL assente"
[ ! -L "$BACKUP_DIR/$POSTGRES_DUMP" ] || fail "dump PostgreSQL non può essere un link simbolico"
[ -s "$BACKUP_DIR/$POSTGRES_DUMP" ] || fail "dump PostgreSQL vuoto"
[ -f "$BACKUP_DIR/$NEO4J_DUMP" ] || fail "dump Neo4j assente"
[ ! -L "$BACKUP_DIR/$NEO4J_DUMP" ] || fail "dump Neo4j non può essere un link simbolico"
[ -s "$BACKUP_DIR/$NEO4J_DUMP" ] || fail "dump Neo4j vuoto"
[ -f "$BACKUP_DIR/$CONFIGURATION_ARCHIVE" ] || fail "archivio configurazione assente"
[ ! -L "$BACKUP_DIR/$CONFIGURATION_ARCHIVE" ] || fail "archivio configurazione non può essere un link simbolico"
[ -s "$BACKUP_DIR/$CONFIGURATION_ARCHIVE" ] || fail "archivio configurazione vuoto"

case "$POSTGRES_DB" in
    ''|*[!A-Za-z0-9_.-]*) fail "nome database PostgreSQL non sicuro nel manifest" ;;
esac
case "$POSTGRES_USER" in
    ''|*[!A-Za-z0-9_.-]*) fail "utente PostgreSQL non sicuro nel manifest" ;;
esac
[ "$NEO4J_DATABASE" = "neo4j" ] || fail "solo il database Neo4j Community predefinito è supportato"

for service_name in app worker postgres neo4j; do
    if is_running "$service_name"; then
        fail "servizio $service_name attivo: arrestare l'intero stack isolato prima del restore"
    fi
done

for service_name in app worker postgres neo4j; do
    [ -n "$(container_id "$service_name")" ] \
        || fail "container $service_name assente: inizializzare prima lo stack isolato con volumi nuovi"
done

POSTGRES_CONTAINER=$(container_id postgres)
NEO4J_CONTAINER=$(container_id neo4j)
APP_CONTAINER=$(container_id app)
CURRENT_POSTGRES_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$POSTGRES_CONTAINER")
CURRENT_NEO4J_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$NEO4J_CONTAINER")
CURRENT_APP_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$APP_CONTAINER")
[ "$CURRENT_POSTGRES_IMAGE" = "$POSTGRES_IMAGE_EXPECTED" ] \
    || fail "immagine PostgreSQL diversa dal backup: $CURRENT_POSTGRES_IMAGE"
[ "$CURRENT_NEO4J_IMAGE" = "$NEO4J_IMAGE_EXPECTED" ] \
    || fail "immagine Neo4j diversa dal backup: $CURRENT_NEO4J_IMAGE"
[ "$CURRENT_APP_IMAGE" = "$(manifest_value application_image)" ] \
    || fail "immagine applicativa diversa dal backup: $CURRENT_APP_IMAGE"

LOCK_DIR="$PROJECT_ROOT/.gbrain-restore.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "restore già in corso o lock residuo presente: $LOCK_DIR"
fi
NEO_TEMP="/data/.gbrain-restore-$$"
NEO_DUMP_INTERNAL="$NEO_TEMP/$NEO4J_DATABASE.dump"
POSTGRES_STARTED=0
NEO4J_STARTED=0
NEO_TEMP_CREATED=0
COMPLETED=0

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$COMPLETED" -ne 1 ]; then
        if [ "$POSTGRES_STARTED" -eq 1 ]; then
            compose stop -t "$STOP_TIMEOUT" postgres >/dev/null 2>&1 || true
        fi
        if [ "$NEO4J_STARTED" -eq 1 ]; then
            compose stop -t "$STOP_TIMEOUT" neo4j >/dev/null 2>&1 || true
        fi
        if [ "$NEO_TEMP_CREATED" -eq 1 ]; then
            compose run --rm --no-deps -T --entrypoint /bin/sh neo4j \
                -ec 'case "$1" in /data/.gbrain-restore-*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
                sh "$NEO_TEMP" >/dev/null 2>&1 || true
        fi
    fi
    rmdir "$LOCK_DIR" 2>/dev/null || true
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

printf '%s\n' "Avvio isolato di PostgreSQL per il ripristino..."
compose start postgres
POSTGRES_STARTED=1
wait_healthy postgres 120 || fail "PostgreSQL non è healthy"
CURRENT_POSTGRES_VERSION=$(compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --command 'SHOW server_version;' | tr -d '\r\n')
[ "$CURRENT_POSTGRES_VERSION" = "$POSTGRES_VERSION_EXPECTED" ] \
    || fail "versione PostgreSQL diversa dal backup: $CURRENT_POSTGRES_VERSION"
compose exec -T postgres pg_restore --list < "$BACKUP_DIR/$POSTGRES_DUMP" >/dev/null \
    || fail "catalogo dump PostgreSQL non leggibile"

ACTIVE_CONNECTIONS=$(compose exec -T postgres psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    --command "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND application_name <> 'pg_isready';" \
    | tr -d '[:space:]')
[ "$ACTIVE_CONNECTIONS" = "0" ] || fail "connessioni PostgreSQL inattese: $ACTIVE_CONNECTIONS"

printf '%s\n' "Ripristino PostgreSQL con pulizia esplicita incorporata nel dump..."
compose exec -T postgres pg_restore \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --no-owner \
    --no-privileges \
    --exit-on-error \
    < "$BACKUP_DIR/$POSTGRES_DUMP"
RESTORED_REVISION=$(compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --command 'SELECT version_num FROM alembic_version;' | tr -d '\r\n')
[ "$RESTORED_REVISION" = "$ALEMBIC_REVISION_EXPECTED" ] \
    || fail "revisione Alembic ripristinata inattesa: $RESTORED_REVISION"
compose stop -t "$STOP_TIMEOUT" postgres
POSTGRES_STARTED=0

printf '%s\n' "Ripristino offline Neo4j Community..."
CURRENT_NEO4J_VERSION=$(compose run --rm --no-deps -T --entrypoint neo4j-admin neo4j --version | tr -d '\r\n')
[ "$CURRENT_NEO4J_VERSION" = "$NEO4J_VERSION_EXPECTED" ] \
    || fail "versione Neo4j diversa dal backup: $CURRENT_NEO4J_VERSION"
compose run --rm --no-deps -T --entrypoint /bin/sh neo4j \
    -ec 'umask 077; mkdir "$1"; cat > "$2"; test -s "$2"; chown -R neo4j:neo4j "$1"' \
    sh "$NEO_TEMP" "$NEO_DUMP_INTERNAL" < "$BACKUP_DIR/$NEO4J_DUMP"
NEO_TEMP_CREATED=1
compose run --rm --no-deps -T --entrypoint neo4j-admin neo4j \
    database load "$NEO4J_DATABASE" "--from-path=$NEO_TEMP" --overwrite-destination=true
compose run --rm --no-deps -T --entrypoint /bin/sh neo4j \
    -ec 'case "$1" in /data/.gbrain-restore-*) rm -rf -- "$1" ;; *) exit 64 ;; esac' \
    sh "$NEO_TEMP"
NEO_TEMP_CREATED=0

printf '%s\n' "Avvio temporaneo dei datastore e applicazione migrazioni..."
compose start postgres neo4j
POSTGRES_STARTED=1
NEO4J_STARTED=1
wait_healthy postgres 120 || fail "PostgreSQL non è healthy dopo il restore"
wait_healthy neo4j 180 || fail "Neo4j non è healthy dopo il restore"
compose run --rm --no-deps -T app migrate
FINAL_REVISION=$(compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --command 'SELECT version_num FROM alembic_version;' | tr -d '\r\n')
[ -n "$FINAL_REVISION" ] || fail "revisione Alembic finale assente"

compose stop -t "$STOP_TIMEOUT" neo4j postgres
NEO4J_STARTED=0
POSTGRES_STARTED=0
COMPLETED=1
rmdir "$LOCK_DIR"
trap - EXIT HUP INT TERM
printf '%s\n' "Restore completato con datastore nuovamente fermi."
printf 'Revisione Alembic: %s -> %s\n' "$ALEMBIC_REVISION_EXPECTED" "$FINAL_REVISION"
printf '%s\n' "Non avviare app/worker né esporre la rete prima delle verifiche isolate del runbook."
