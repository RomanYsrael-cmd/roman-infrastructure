# Database migration lifecycle

This guide describes the implemented PostgreSQL/Flyway migration adapter. The controller and root application record choose the adapter, target version, migration identity, batch limit, credentials, backup provider, and database writers. A migration file version or checksum is never added to controller source as an approval exception.

## Release contents

The application release contains the migration directory and the migrator image build specification. RomanLMS release metadata v2 identifies the Flyway CLI and schema but has no fixed source version, target version, filename, or checksum. The source image and migrator image are built from the same verified source commit and released by immutable digest. Flyway 11's `info -outputType=json` may omit SQL checksums, so the controller joins its migration identities to the dedicated database history query and computes pending SQL checksums from `/flyway/sql` copied out of the exact verified image. The image is created with a read-only filesystem and no network and is never started for inspection. The database-plan fingerprint covers applied history and pending migration checksums; a second release-binding fingerprint binds it to the application, source commit, platform workflow commit, migrator digest, database identity, schema, migration login, execution role, and exact workflow run/attempt. Execution refuses a plan whose release binding does not match the supplied migration request.

Only versioned SQL migrations run automatically. The validator confirms canonical numeric versions, strictly increasing unique order, expected SQL path and names, valid Flyway checksums, no modified successful migrations, and no failed/unknown history. Flyway-compatible descriptions may contain uppercase letters, numbers, spaces, and underscores; the original filename and Flyway description remain part of the checked identity. A per-script `.sql.conf` file is accepted only when it is named for an existing SQL migration, and is treated as Flyway configuration rather than a second migration or SQL checksum. Its contents remain bound to the immutable, provenance-verified migrator image used for inspection and execution. Unsupported extensions, orphaned sidecars, repeatable migrations, missing files, out-of-order migrations, injected paths, and malformed migration states stop the release.

## No pending migrations

The controller runs Flyway information and validation against the exact release migrator. It records the observed schema version and an empty pending plan. It does not stop database writers, create a full backup, or run migration credentials. After the candidate passes health checks, it re-reads the schema history and requires the same plan fingerprint before switching traffic.

## One or more pending migrations

1. The controller validates the applied schema and all pending rows and SQL files in the immutable release image. `target_version` is derived from the last pending migration. It records both the migration-history fingerprint and the release-binding fingerprint described above.
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

## Audited forward-compatibility reconciliation

A migration can finish while candidate activation fails, leaving the live database ahead of the schema version recorded on the still-running release. The controller does not edit that historical release record to make the versions appear equal. A mismatch is denied unless the root-owned application registry contains one exact `schema_compatibility_evidence` record for the recorded version, observed version, target version, target source commit, and pinned deployment workflow.

Each record binds the reviewed migration effect to the applied filename, Flyway checksum, and SHA-256 of its SQL bytes in the provenance-verified migrator image. It also lists every allowed pending migration with its exact filename, Flyway checksum, and content hash, and binds the retained source commit, workflow commit, migrator image digest, and every retained component image digest. The registry validator rejects incomplete, ambiguous, mutable, or unsupported evidence. The installed policy currently accepts only a specifically reviewed `grant-only` effect; it does not infer compatibility from an additive label or a version number.

Before allowing the forward release to continue, the controller validates all Flyway history and checksums against the exact immutable migrator image; matches the full pending plan and target release; checks the active and previous release records; verifies the active retained containers, immutable image digests, provenance labels, application health, and route; and emits a `schema-reconciliation` audit record with the evidence ID, migration plan and release fingerprints, retained and target release identities, workflow actor/run, and timestamp. A previous-slot record, when present, must match the reviewed release and exact slot-specific container names. Its containers may already have been removed by prior cleanup; they are inactive rollback metadata, not a prerequisite for validating the running release. Compatibility is rechecked after the verified backup and writer quiescence. The release-state file is snapshotted and rechecked around those steps so a concurrent state change aborts before migration and is preserved.

Passing this check only allows the normal forward-deployment path to proceed. A fresh verified backup, writer quiescence, unchanged migration plan, approved Flyway execution, postflight validation, candidate health, and the existing traffic-switch safeguards remain mandatory. The reconciliation path changes neither the database nor the retained release metadata. It cannot be used for a different target release or a later schema version. Missing evidence, an unhealthy retained release, any checksum or image mismatch, an unexpected migration, or a concurrent state change fails closed. If candidate activation fails after the database has moved forward, the old release is not restarted against an unapproved schema; keep traffic and state unchanged and use the forward-fix or approved database-recovery process.

To handle another incident, verify the actual successful Flyway history and migration source, prove the retained release is healthy against that database, and review the exact effect and release identities before adding a narrowly scoped root-owned record. Never change `schema_version`, run Flyway `repair`, or use a client-supplied compatibility override as a recovery shortcut.

## RomanLMS specifics

RomanLMS uses the `romanlms-production` registry entry, PostgreSQL schema `roman_lms`, Flyway CLI pinned to the release metadata, an app-scoped production database environment file, a separate app-scoped migration environment file, dedicated role `romanlms_app`, and verified pgBackRest. The shared migration adapter is installed and passed the read-only RomanLMS production acceptance. The prior V132/V133 exception is removed from the release path; future normal migrations require no infrastructure source change.

Private Phase 6 remains a separate test database and uses validation/info without applying production migrations.
