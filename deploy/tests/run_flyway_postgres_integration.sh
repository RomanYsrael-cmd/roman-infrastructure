#!/usr/bin/env bash
set -Eeuo pipefail
set +x

: "${POSTGRES_CONTAINER:?GitHub Actions PostgreSQL service container is required}"
: "${MIGRATION_DB_PASSWORD:?Disposable migration-test password is required}"

if [[ ! "$MIGRATION_DB_PASSWORD" =~ ^[A-Za-z0-9_-]{12,80}$ ]]; then
  echo "The disposable migration-test password is malformed." >&2
  exit 64
fi

mapfile -t postgres_networks < <(
  docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$POSTGRES_CONTAINER"
)
if [[ ${#postgres_networks[@]} -ne 1 || ! "${postgres_networks[0]}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "The disposable PostgreSQL container must be attached to exactly one valid job network." >&2
  exit 1
fi
POSTGRES_NETWORK="${postgres_networks[0]}"

context_root="${RUNNER_TEMP:-/tmp}/flyway-plan-integration"
image_tag="${GITHUB_SHA:-local}"
dockerfile="$context_root/Dockerfile"
mkdir -p "$context_root"
cat > "$dockerfile" <<'EOF'
FROM flyway/flyway:11.14.1-alpine
COPY --chown=10001:10001 sql/ /flyway/sql/
EOF

build_release_image() {
  local release="$1"
  shift
  local context="$context_root/$release"
  local image="roman-infrastructure-flyway-it:$image_tag-$release"
  mkdir -p "$context/sql"
  cp "$@" "$context/sql/"
  docker build --pull --file "$dockerfile" --tag "$image" "$context" >&2
  printf '%s' "$image"
}

baseline_image="$(build_release_image baseline deploy/tests/fixtures/flyway-integration/V133__integration_baseline.sql)"
single_image="$(build_release_image single \
  deploy/tests/fixtures/flyway-integration/V133__integration_baseline.sql \
  deploy/tests/fixtures/flyway-integration/V134__integration_add_count.sql)"
multi_image="$(build_release_image multi deploy/tests/fixtures/flyway-integration/*.sql)"

ready=false
for attempt in {1..30}; do
  if docker exec "$POSTGRES_CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo "The isolated PostgreSQL integration service did not become ready." >&2
  exit 1
fi

docker exec "$POSTGRES_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE roman_migration_app NOLOGIN" \
  -c "CREATE ROLE roman_migration_runner LOGIN PASSWORD '${MIGRATION_DB_PASSWORD}'" \
  -c "GRANT roman_migration_app TO roman_migration_runner" \
  -c "CREATE DATABASE roman_migration_it OWNER roman_migration_app"
docker exec "$POSTGRES_CONTAINER" psql -U postgres -d roman_migration_it -v ON_ERROR_STOP=1 \
  -c "CREATE SCHEMA roman_lms AUTHORIZATION roman_migration_app"

run_flyway() {
  local image="$1"
  shift
  docker run --rm --network "$POSTGRES_NETWORK" \
    --env FLYWAY_URL=jdbc:postgresql://postgres:5432/roman_migration_it \
    --env FLYWAY_USER=roman_migration_runner \
    --env "FLYWAY_PASSWORD=$MIGRATION_DB_PASSWORD" \
    --env FLYWAY_SCHEMAS=roman_lms \
    --env FLYWAY_DEFAULT_SCHEMA=roman_lms \
    --env 'FLYWAY_INIT_SQL=SET ROLE roman_migration_app' \
    --env FLYWAY_LOCATIONS=filesystem:/flyway/sql \
    "$image" "$@"
}

run_flyway "$baseline_image" -target=133 migrate
initial_info="$(run_flyway "$single_image" info -outputType=json)"
single_plan_target="$(printf '%s' "$initial_info" | PYTHONPATH=deploy/controller python3 -c '
import json, sys
from forward_migration_plan import validate_forward_plan
plan = validate_forward_plan(sys.stdin.read(), max_pending=1, expected_flyway_version="11.14.1", expected_schema_name="roman_lms")
assert len(plan.pending) == 1, plan.pending
assert plan.source_version == "133", plan.source_version
print(plan.target_version)
')"
[[ "$single_plan_target" == "134" ]]
run_flyway "$single_image" -target="$single_plan_target" migrate

multi_info="$(run_flyway "$multi_image" info -outputType=json)"
batch_target="$(printf '%s' "$multi_info" | PYTHONPATH=deploy/controller python3 -c '
import sys
from forward_migration_plan import validate_forward_plan
plan = validate_forward_plan(sys.stdin.read(), max_pending=3, expected_flyway_version="11.14.1", expected_schema_name="roman_lms")
assert len(plan.pending) == 3, plan.pending
assert plan.source_version == "134", plan.source_version
print(plan.target_version)
')"
[[ "$batch_target" == "137" ]]
run_flyway "$multi_image" -target="$batch_target" migrate

clean_info="$(run_flyway "$multi_image" info -outputType=json)"
printf '%s' "$clean_info" | PYTHONPATH=deploy/controller python3 -c '
import sys
from forward_migration_plan import validate_forward_plan
plan = validate_forward_plan(sys.stdin.read(), expected_flyway_version="11.14.1", expected_schema_name="roman_lms")
assert not plan.pending, plan.pending
assert plan.source_version == "137", plan.source_version
'

actual="$(docker exec -e PGPASSWORD="$MIGRATION_DB_PASSWORD" "$POSTGRES_CONTAINER" \
  psql -h 127.0.0.1 -p 5432 -U roman_migration_runner -d roman_migration_it -Atqc \
  "SELECT release_name || '|' || migration_count FROM roman_lms.migration_integration WHERE id=1")"
if [[ "$actual" != "multi-release|4" ]]; then
  echo "The isolated database did not contain the expected migrated state." >&2
  exit 1
fi

echo "Flyway PostgreSQL integration passed: one pending migration, a three-migration release batch, and a clean schema at V137."
