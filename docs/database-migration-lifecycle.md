# Database migration lifecycle

This guide describes the implemented PostgreSQL/Flyway migration adapter. The controller and root application record choose the adapter, target version, migration identity, batch limit, credentials, backup provider, and database writers. A migration file version or checksum is never added to controller source as an approval exception.

## Release contents

The application release contains the migration directory and the migrator image build specification. RomanLMS release metadata v2 identifies the Flyway CLI and schema but has no fixed source version, target version, filename, or checksum. The source image and migrator image are built from the same verified source commit and released by immutable digest.

Only versioned SQL migrations run automatically. The validator confirms canonical numeric versions, strictly increasing unique order, expected SQL path and names, valid Flyway checksums, no modified successful migrations, and no failed/unknown history. Unsupported repeatable, missing, out-of-order, injected, or malformed migration states stop the release.

## No pending migrations

The controller runs Flyway information and validation against the exact release migrator. It records the observed schema version and an empty pending plan. It does not stop database writers, create a full backup, or run migration credentials. The application release then follows its normal health-checked rollout.

## One or more pending migrations

1. The controller validates the applied schema and all pending rows and SQL files in the immutable release image. `target_version` is derived from the last pending migration. The validated plan is fingerprinted with source/target versions, files, checksums, Flyway identity, source commit, database/schema identity, and migrator digest.
2. It loads the root-owned dedicated migration environment file and verifies its database URL and migration login. Runtime credentials are not used for schema changes.
3. It creates a new consistent backup through the registered adapter. For RomanLMS, `pgBackRest` creates a full backup and the controller checks the new backup-set identity and verifies that set. A `verified-dump` adapter records the dump identity and SHA-256.
4. It quiesces the registered writers and confirms their database sessions have ended. It then re-reads Flyway info and validates the same plan fingerprint. A changed plan aborts before migration.
5. It executes only Flyway `migrate` against the validated target in a read-only, resource-limited container using the dedicated migration login and approved execution role. `clean`, `repair`, baseline-on-migrate, and arbitrary Flyway commands are disabled.
6. It re-reads the live schema and history and verifies the exact target and checksums, no pending changes, and no failed migration state. The candidate app must also pass health checks before the route/state transition completes.

The per-application `max_pending` is a bounded operational batch size (1–100). It permits multiple sequential migrations in one release; it does not encode a schema version or one-file approval.

## Failure and recovery

- If backup creation or verification fails, no migration runs and the release does not start a candidate.
- If writer quiescence fails, the plan changes, or pre-migration revalidation fails, the release aborts before executing SQL.
- If Flyway fails but the controller confirms the exact unchanged source plan, the previous app may return only if its observed schema matches the retained release record.
- If Flyway reports the target fully applied and the history checksums are verified, the new candidate can continue to health checks. Application rollback remains blocked until the retained version is explicitly compatible with that observed schema.
- If Flyway is still running or the database outcome is unknown, the writer stays stopped and traffic does not move to an unverified app. Do not retry, run `repair`, or edit `flyway_schema_history`. Preserve the backup metadata and follow the database incident/restore process.
- If candidate health fails after migration, the controller may restore the retained application only when the actual schema and compatibility policy permit it. It never rolls back SQL automatically.

The controller audits the source/target, exact plan and fingerprint, migration image, backup adapter and identity, writer quiescence, execution outcome, checksums, release source, and postflight schema. Audit output does not include database passwords.

## RomanLMS specifics

RomanLMS uses the `romanlms-production` registry entry, PostgreSQL schema `roman_lms`, Flyway CLI pinned to the release metadata, an app-scoped production database environment file, a separate app-scoped migration environment file, dedicated role `romanlms_app`, and verified pgBackRest. Its old installed release policy is not replaced until the matching infrastructure and RomanLMS revisions are merged and installed together. The prior V132/V133 exception is removed from the release path; future normal migrations require no infrastructure source change.

Private Phase 6 remains a separate test database and uses validation/info without applying production migrations.
