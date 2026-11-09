#!/usr/bin/env bash
# Local Neo4j + PostgreSQL for the integration tests, as apptainer instances (no Docker needed).
#
#   export VISIONKB_WORK_DIR=/path/to/workdir            # runtime state goes to $VISIONKB_WORK_DIR/visionkb-api/
#   scripts/local_services.sh pull                       # docker://neo4j:2026.10.1-community, docker://postgres:17.11
#   scripts/local_services.sh fixture                    # tests/fixtures/make_fixture.py -> .../fixtures/
#   scripts/local_services.sh import [RELEASE_DIR]       # neo4j-admin database import full (fixture by default)
#   scripts/local_services.sh up                         # start both instances, run post_import.cypher
#   scripts/local_services.sh env                        # print the variables for pytest (VISIONKB_IT=1 ...)
#   scripts/local_services.sh down
#
# Ports bind to 127.0.0.1 only: NEO4J_BOLT_PORT (17687), NEO4J_HTTP_PORT (17474), PG_PORT (15433).
# Passwords are generated into $STATE/local/secrets (mode 600) and passed to the containers as files.
# Neo4j 2026.x enables Fleet Manager, fleet-discovery UDP broadcasts and usage reports by default;
# all three are switched off here.
set -euo pipefail

: "${VISIONKB_WORK_DIR:?set VISIONKB_WORK_DIR}"
STATE="$VISIONKB_WORK_DIR/visionkb-api"
IMAGES="${VISIONKB_IMAGES_DIR:-$STATE/images}"
LOCAL="$STATE/local"
SECRETS="$LOCAL/secrets"
NEO4J_IMAGE="${NEO4J_IMAGE:-neo4j:2026.10.1-community}"
PG_IMAGE="${PG_IMAGE:-postgres:17.11}"
NEO4J_SIF="$IMAGES/${NEO4J_IMAGE//:/_}.sif"
PG_SIF="$IMAGES/${PG_IMAGE//:/_}.sif"
NEO4J_BOLT_PORT="${NEO4J_BOLT_PORT:-17687}"
NEO4J_HTTP_PORT="${NEO4J_HTTP_PORT:-17474}"
PG_PORT="${PG_PORT:-15433}"
NEO4J_NAME="${NEO4J_NAME:-vkb-api-neo4j}"
PG_NAME="${PG_NAME:-vkb-api-pg}"
FIXTURE="$STATE/fixtures/visionkb-2026.11.0-fixture"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v apptainer >/dev/null 2>&1 && [[ -f /usr/share/Modules/init/bash ]]; then
  # shellcheck disable=SC1091
  source /usr/share/Modules/init/bash
  module load apptainer >/dev/null 2>&1 || true
fi
export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$STATE/apptainer-cache}"

secret() {  # secret <file>: create a random secret once
  mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
  [[ -s "$SECRETS/$1" ]] || { python3 -c 'import secrets; print(secrets.token_urlsafe(24))' >"$SECRETS/$1"; chmod 600 "$SECRETS/$1"; }
}

neo4j_binds() {
  local rel="$1"
  echo "$LOCAL/neo4j/data:/data,$LOCAL/neo4j/logs:/logs,$LOCAL/neo4j/conf:/var/lib/neo4j/conf,$LOCAL/neo4j/run:/var/lib/neo4j/run,$LOCAL/neo4j/plugins:/plugins,$rel/neo4j-import:/import:ro,$SECRETS/neo4j_auth:/run/secrets/neo4j_auth:ro"
}

cmd_pull() {
  mkdir -p "$IMAGES" "$APPTAINER_CACHEDIR"
  [[ -f "$NEO4J_SIF" ]] || apptainer pull "$NEO4J_SIF" "docker://$NEO4J_IMAGE"
  [[ -f "$PG_SIF" ]] || apptainer pull "$PG_SIF" "docker://$PG_IMAGE"
}

cmd_fixture() { "${PYTHON:-python3}" "$HERE/tests/fixtures/make_fixture.py" --out "$STATE/fixtures"; }

cmd_import() {
  local rel="${1:-$FIXTURE}"
  secret neo4j_password
  printf 'neo4j/%s\n' "$(cat "$SECRETS/neo4j_password")" >"$SECRETS/neo4j_auth"; chmod 600 "$SECRETS/neo4j_auth"
  mkdir -p "$LOCAL/neo4j"/{data,logs,conf,run,plugins}
  # The image's conf dir is read-only in a SIF: copy it out once and bind it writable.
  [[ -f "$LOCAL/neo4j/conf/neo4j.conf" ]] || apptainer exec "$NEO4J_SIF" sh -c "cp -r /var/lib/neo4j/conf/. '$LOCAL/neo4j/conf/'"
  echo "$rel" >"$LOCAL/neo4j/release_dir"
  apptainer exec --cleanenv --bind "$(neo4j_binds "$rel")" --pwd /import "$NEO4J_SIF" \
    neo4j-admin database import full neo4j --overwrite-destination=true @/import/import.args
}

cmd_up() {
  local rel; rel="$(cat "$LOCAL/neo4j/release_dir")"
  secret pg_password
  printf 'postgresql://visionkb:%s@127.0.0.1:%s/visionkb\n' "$(cat "$SECRETS/pg_password")" "$PG_PORT" >"$SECRETS/pg_dsn"
  chmod 600 "$SECRETS/pg_dsn"
  apptainer instance run --cleanenv --bind "$(neo4j_binds "$rel")" \
    --env NEO4J_AUTH_PATH=/run/secrets/neo4j_auth \
    --env "NEO4J_server_bolt_listen__address=127.0.0.1:$NEO4J_BOLT_PORT" \
    --env "NEO4J_server_bolt_advertised__address=127.0.0.1:$NEO4J_BOLT_PORT" \
    --env "NEO4J_server_http_listen__address=127.0.0.1:$NEO4J_HTTP_PORT" \
    --env "NEO4J_server_http_advertised__address=127.0.0.1:$NEO4J_HTTP_PORT" \
    --env NEO4J_server_memory_heap_max__size=1G --env NEO4J_server_memory_pagecache_size=512M \
    --env NEO4J_server_fleet__discovery_enabled=false --env NEO4J_dbms_fleet__manager_enabled=false \
    --env NEO4J_dbms_usage__report_enabled=false \
    "$NEO4J_SIF" "$NEO4J_NAME" >/dev/null
  mkdir -p "$LOCAL/postgres"/{data,run}; chmod 700 "$LOCAL/postgres/data"
  apptainer instance run --cleanenv \
    --bind "$LOCAL/postgres/data:/var/lib/postgresql/data,$LOCAL/postgres/run:/var/run/postgresql,$SECRETS/pg_password:/run/secrets/pg_password:ro" \
    --env POSTGRES_USER=visionkb --env POSTGRES_DB=visionkb --env POSTGRES_PASSWORD_FILE=/run/secrets/pg_password \
    "$PG_SIF" "$PG_NAME" postgres -p "$PG_PORT" -c listen_addresses=127.0.0.1 >/dev/null
  echo "waiting for Neo4j on 127.0.0.1:$NEO4J_BOLT_PORT ..."
  for _ in $(seq 1 60); do
    if apptainer exec "instance://$NEO4J_NAME" sh -c \
      "NEO4J_PASSWORD=\$(cut -d/ -f2- /run/secrets/neo4j_auth) cypher-shell -a bolt://127.0.0.1:$NEO4J_BOLT_PORT -u neo4j 'RETURN 1'" >/dev/null 2>&1; then
      break
    fi
    sleep 3
  done
  apptainer exec "instance://$NEO4J_NAME" sh -c \
    "NEO4J_PASSWORD=\$(cut -d/ -f2- /run/secrets/neo4j_auth) cypher-shell -a bolt://127.0.0.1:$NEO4J_BOLT_PORT -u neo4j -f /import/post_import.cypher"
  echo "waiting for PostgreSQL on 127.0.0.1:$PG_PORT ..."
  for _ in $(seq 1 40); do
    apptainer exec "instance://$PG_NAME" pg_isready -h 127.0.0.1 -p "$PG_PORT" -q && break
    sleep 2
  done
  apptainer instance list
}

cmd_env() {
  local rel; rel="$(cat "$LOCAL/neo4j/release_dir")"
  cat <<EOF
export VISIONKB_IT=1
export NEO4J_URI=bolt://127.0.0.1:$NEO4J_BOLT_PORT NEO4J_USER=neo4j NEO4J_PASSWORD_FILE=$SECRETS/neo4j_password
export VISIONKB_PG_DSN_FILE=$SECRETS/pg_dsn
export VISIONKB_TEST_RELEASE_DIR=$rel
EOF
}

cmd_down() {
  apptainer instance stop "$NEO4J_NAME" 2>/dev/null || true
  apptainer instance stop "$PG_NAME" 2>/dev/null || true
}

case "${1:-}" in
  pull | fixture | up | env | down) "cmd_$1" ;;
  import) shift; cmd_import "$@" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
