#!/usr/bin/env bash
set -Eeuo pipefail
set +x

: "${POSTGRES_CONTAINER:?GitHub Actions PostgreSQL service container is required}"
: "${MIGRATION_DB_PASSWORD:?Disposable migration-test password is required}"

if [[ ! "$MIGRATION_DB_PASSWORD" =~ ^[A-Za-z0-9_-]{12,80}$ ]]; then
  echo "The disposable migration-test password is malformed." >&2
  exit 64
fi

network_name="roman-flyway-plan-${GITHUB_RUN_ID:-local}-$$"
if [[ ! "$network_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "The disposable PostgreSQL integration network name is invalid." >&2
  exit 1
fi
docker network create "$network_name" >/dev/null
cleanup_network() {
  docker network disconnect "$network_name" "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
}
trap cleanup_network EXIT
docker network connect --alias postgres "$network_name" "$POSTGRES_CONTAINER"
POSTGRES_NETWORK="$network_name"

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

flyway_history() {
  docker exec -e PGPASSWORD="$MIGRATION_DB_PASSWORD" "$POSTGRES_CONTAINER" \
    psql -h 127.0.0.1 -p 5432 -U roman_migration_runner -d roman_migration_it -Atqc \
    "SELECT COALESCE(json_agg(json_build_object('version',version,'description',description,'type',type,'script',script,'checksum',checksum,'success',success) ORDER BY installed_rank),'[]'::json)::text FROM roman_lms.flyway_schema_history"
}

run_flyway "$baseline_image" -target=133 migrate
initial_info="$(run_flyway "$single_image" info -outputType=json)"
printf '%s' "$initial_info" | python3 -c '
import json, sys
report = json.load(sys.stdin)
fields = ("version", "state", "category", "type", "checksum", "filepath", "script", "description")
print("Flyway plan diagnostic:", json.dumps([{key: row.get(key) for key in fields} for row in report.get("migrations", [])], sort_keys=True))
' >&2
single_plan_target="$(printf '%s' "$initial_info" | env \
FLYWAY_HISTORY_JSON="$(flyway_history)" \
MIGRATION_SQL_DIR="$context_root/single/sql" \
PLAN_FILE="$context_root/single-plan.json" \
PYTHONPATH=deploy/controller python3 -c '
import json, os, sys
from pathlib import Path
from forward_migration_plan import bind_flyway_checksums, migration_artifact_checksums, validate_forward_plan
checksums = migration_artifact_checksums(Path(os.environ["MIGRATION_SQL_DIR"]))
bound = bind_flyway_checksums(sys.stdin.read(), os.environ["FLYWAY_HISTORY_JSON"], checksums)
plan = validate_forward_plan(bound, max_pending=1, expected_flyway_version="11.14.1", expected_schema_name="roman_lms")
assert len(plan.pending) == 1, plan.pending
assert plan.source_version == "133", plan.source_version
Path(os.environ["PLAN_FILE"]).write_text(json.dumps({"source_version": plan.source_version, "target_version": plan.target_version, "pending": plan.pending, "fingerprint": plan.fingerprint}), encoding="utf-8")
print(plan.target_version)
')"
[[ "$single_plan_target" == "134" ]]
run_flyway "$single_image" -target="$single_plan_target" migrate

multi_info="$(run_flyway "$multi_image" info -outputType=json)"
batch_target="$(printf '%s' "$multi_info" | env \
FLYWAY_HISTORY_JSON="$(flyway_history)" \
MIGRATION_SQL_DIR="$context_root/multi/sql" \
PLAN_FILE="$context_root/multi-plan.json" \
PYTHONPATH=deploy/controller python3 -c '
import json, os, sys
from pathlib import Path
from forward_migration_plan import bind_flyway_checksums, migration_artifact_checksums, validate_forward_plan
checksums = migration_artifact_checksums(Path(os.environ["MIGRATION_SQL_DIR"]))
bound = bind_flyway_checksums(sys.stdin.read(), os.environ["FLYWAY_HISTORY_JSON"], checksums)
plan = validate_forward_plan(bound, max_pending=3, expected_flyway_version="11.14.1", expected_schema_name="roman_lms")
assert len(plan.pending) == 3, plan.pending
assert plan.source_version == "134", plan.source_version
Path(os.environ["PLAN_FILE"]).write_text(json.dumps({"source_version": plan.source_version, "target_version": plan.target_version, "pending": plan.pending, "fingerprint": plan.fingerprint}), encoding="utf-8")
print(plan.target_version)
')"
[[ "$batch_target" == "137" ]]
run_flyway "$multi_image" -target="$batch_target" migrate

clean_info="$(run_flyway "$multi_image" info -outputType=json)"
printf '%s' "$clean_info" | env \
FLYWAY_HISTORY_JSON="$(flyway_history)" \
MIGRATION_SQL_DIR="$context_root/multi/sql" \
PLAN_FILE="$context_root/multi-plan.json" \
PYTHONPATH=deploy/controller python3 -c '
import json, os, sys
from pathlib import Path
from forward_migration_plan import ForwardMigrationPlan, bind_flyway_checksums, migration_artifact_checksums, validate_migration_result
checksums = migration_artifact_checksums(Path(os.environ["MIGRATION_SQL_DIR"]))
bound = bind_flyway_checksums(sys.stdin.read(), os.environ["FLYWAY_HISTORY_JSON"], checksums)
plan_data = json.loads(Path(os.environ["PLAN_FILE"]).read_text(encoding="utf-8"))
planned = ForwardMigrationPlan(plan_data["source_version"], plan_data["target_version"], tuple(tuple(row) for row in plan_data["pending"]), plan_data["fingerprint"])
plan = validate_migration_result(bound, planned, expected_flyway_version="11.14.1", expected_schema_name="roman_lms")
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

docker exec "$POSTGRES_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE roman_control_prod"
docker exec "$POSTGRES_CONTAINER" psql -U postgres -d roman_control_prod -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE roman_control_owner NOLOGIN" \
  -c "CREATE ROLE roman_control_migrator LOGIN" \
  -c "CREATE ROLE roman_control_runtime LOGIN" \
  -c "CREATE ROLE roman_control_backup LOGIN" \
  -c "CREATE ROLE roman_control_observer LOGIN" \
  -c "CREATE SCHEMA roman_control AUTHORIZATION roman_control_owner" \
  -c "GRANT roman_control_owner TO roman_control_migrator" \
  -c "GRANT pg_read_all_data TO roman_control_backup" \
  -c "GRANT pg_read_all_stats TO roman_control_observer" \
  -c "GRANT USAGE ON SCHEMA roman_control TO roman_control_runtime, roman_control_backup, roman_control_observer"
docker exec -i "$POSTGRES_CONTAINER" psql -U postgres -d roman_control_prod -v ON_ERROR_STOP=1 -f - \
  < deploy/scripts/romanlms-control-plane/database-roles.sql
docker exec "$POSTGRES_CONTAINER" psql -U postgres -d roman_control_prod -v ON_ERROR_STOP=1 \
  -c "SET ROLE roman_control_owner; CREATE TABLE roman_control.role_policy_smoke (id integer); CREATE SEQUENCE roman_control.role_policy_smoke_seq; CREATE FUNCTION roman_control.role_policy_smoke_function() RETURNS integer LANGUAGE SQL AS 'SELECT 1'; RESET ROLE"
docker exec "$POSTGRES_CONTAINER" psql -U postgres -d roman_control_prod -v ON_ERROR_STOP=1 \
  -c "DO \$check\$ BEGIN
    IF pg_has_role('roman_control_backup','pg_read_all_data','MEMBER') THEN
      RAISE EXCEPTION 'Backup role still has cluster-wide data access';
    END IF;
    IF pg_has_role('roman_control_migrator','roman_control_owner','USAGE')
       OR NOT pg_has_role('roman_control_migrator','roman_control_owner','SET') THEN
      RAISE EXCEPTION 'Migration login role membership is not least-privilege';
    END IF;
    IF NOT has_database_privilege('roman_control_owner','roman_control_prod','CREATE')
       OR has_database_privilege('roman_control_migrator','roman_control_prod','CREATE')
       OR has_database_privilege('roman_control_runtime','roman_control_prod','CREATE')
       OR has_database_privilege('roman_control_backup','roman_control_prod','CREATE') THEN
      RAISE EXCEPTION 'Database CREATE privilege is not restricted to the non-login migration owner';
    END IF;
    IF NOT has_table_privilege('roman_control_runtime','roman_control.role_policy_smoke','SELECT')
       OR NOT has_table_privilege('roman_control_runtime','roman_control.role_policy_smoke','INSERT')
       OR has_table_privilege('roman_control_runtime','roman_control.role_policy_smoke','TRUNCATE')
       OR NOT has_sequence_privilege('roman_control_runtime','roman_control.role_policy_smoke_seq','USAGE')
       OR NOT has_table_privilege('roman_control_backup','roman_control.role_policy_smoke','SELECT')
       OR has_table_privilege('roman_control_backup','roman_control.role_policy_smoke','INSERT')
       OR NOT has_sequence_privilege('roman_control_backup','roman_control.role_policy_smoke_seq','SELECT')
       OR NOT has_table_privilege('roman_control_migrator','roman_control.role_policy_smoke','SELECT')
       OR has_table_privilege('roman_control_migrator','roman_control.role_policy_smoke','INSERT')
       OR NOT has_function_privilege('roman_control_runtime','roman_control.role_policy_smoke_function()','EXECUTE')
       OR has_function_privilege('roman_control_backup','roman_control.role_policy_smoke_function()','EXECUTE')
       OR has_function_privilege('roman_control_observer','roman_control.role_policy_smoke_function()','EXECUTE') THEN
      RAISE EXCEPTION 'Runtime, backup, or migrator privileges do not match the application policy';
    END IF;
    IF has_schema_privilege('roman_control_runtime','roman_control','CREATE')
       OR has_schema_privilege('roman_control_backup','roman_control','CREATE')
       OR has_schema_privilege('roman_control_observer','roman_control','USAGE')
       OR NOT pg_has_role('roman_control_observer','pg_read_all_stats','MEMBER') THEN
      RAISE EXCEPTION 'Schema or observer privileges do not match the application policy';
    END IF;
    IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='roman_control.role_policy_smoke'::regclass) <> 'roman_control_owner' THEN
      RAISE EXCEPTION 'New database objects are not owned by the migration role';
    END IF;
  END \$check\$;"
docker exec "$POSTGRES_CONTAINER" pg_dump -U roman_control_backup -d roman_control_prod --schema-only >/dev/null
echo "Control Plane database role integration passed: migration ownership and scoped runtime, backup, and observer permissions."
