# RomanLMS production release authorization

RomanLMS production accepts two automatic release paths. Both must pass the exact-source CI job, GitHub workflow and actor checks, root-owned release switches, image provenance, database and backup safeguards, blue/green readiness, health checks, audit logging, and rollback policy. The restricted deployment controller remains the final authority.

## Feature branch and pull request

1. Run LONG CI on the feature pull request.
2. Merge the owner-authored same-repository pull request to canonical `main` only after LONG succeeds.
3. Run SHORT CI on the resulting exact main commit.
4. Release only when GitHub confirms the same tested tree was merged and the exact-main SHORT required job succeeds.

## Direct push to main

An owner-authorized direct push to canonical `main` starts SHORT CI automatically. LONG CI does not start for direct pushes. This route intentionally has reduced test coverage and must be used only with awareness of that tradeoff.

The controller requires GitHub Actions evidence for the exact push event, trusted actor, canonical repository, `main` branch, current main SHA, approved CI workflow and run attempt, and the successful required job. It independently checks the GitHub commit identity and pull-request association. If the commit is associated with a merged pull request, it must satisfy the full PR/LONG policy; missing or invalid PR evidence cannot fall back to direct-push authorization. Commit messages are not authorization evidence.

## Fail-closed conditions

Pending or failed SHORT CI, wrong actors, repositories, branches, workflow identities or attempts, stale SHAs, mismatched commit metadata, incomplete PR association, and non-production changes all block automatic release. The repository freeze and automatic-release switch plus the controller's root-owned enable switch remain required. A workflow or controller change is not active on the server until its immutable revision is reviewed, allowlisted, installed and verified.

Focused controller regressions cover both allowed paths, invalid or missing LONG evidence on PR merges, unauthorized actors, failed and pending SHORT runs, stale source commits, incorrect repositories/branches/workflows/attempts, and the production-change safety gate. Full RomanLMS LONG CI remains the required PR check for normal feature work; a direct-main release reports its intentionally reduced coverage in the release summary.

## Controlled production schema migration

The production LAMR release requires the specifically approved additive transition from schema V132 to V133. An earlier controller target check rejected Flyway's calculated `Above Target` state for V133; the controller now accepts only the exact approved V132-to-V133 pending plan. The subsequent production attempt reached Flyway, where the runtime login could not alter an owner-controlled table: `romanlms_runtime` did not own `roman_lms.tenant_institution_settings`, whose owner is `romanlms_app`. PostgreSQL rejected the first `ALTER TABLE`. The transaction rolled back, leaving schema V132, no V133 or failed history record, and no partial LAMR schema. The healthy retained backend remains active until a corrected migration release completes.

The root-owned production manifest approves only `V133__tesda_lamr_report_settings.sql`, its exact description and checksum, and a fresh full backup from the configured `roman-platform` pgBackRest stanza. The normal application database environment remains the runtime login. For a pending approved production migration, the controller separately loads `/etc/roman-platform/romanlms-production/migration.env`, a root-owned private file with exactly `DB_URL`, `DB_USERNAME`, and `DB_PASSWORD`. It rejects the file unless its database URL exactly matches the runtime URL and its username matches the configured dedicated migration login. The immutable Flyway image connects with that account and executes `SET ROLE romanlms_app` for the migration session; the login is not a superuser and does not inherit the owner role. This lets Flyway apply schema changes as the existing object owner without elevating the application runtime account.

Before applying the migration, the controller must see schema V132 and exactly one pending V133 migration, verify the protected migration credentials, create a fresh full backup, verify its new backup-set label and run pgBackRest `verify` against that set, stop the active backend, confirm that no sessions from the application role remain, and re-read the same Flyway plan. The migration runs from the immutable approved migrator image in a read-only, resource-limited container. The controller then validates schema V133 before starting the candidate application.

The migration is additive and the retained application release is explicitly approved as compatible with schema V133. If Flyway confirms either the unchanged V132 pending plan or the fully applied V133 target after a normal migration error, the retained backend can be restored. If the migration container or database outcome cannot be verified, the backend remains stopped for safe recovery and the public route is not switched. The controller does not run `repair`, edit `flyway_schema_history`, or allow other pending migrations. This production-only release policy does not change private Phase 6: its application containers keep Flyway disabled and its preflight remains read-only.
