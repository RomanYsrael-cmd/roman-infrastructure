# RomanLMS production release authorization

RomanLMS uses the shared `deploy-application.yml` release workflow and the root-owned `romanlms-production` registry policy. The server controller remains the final authority for actor, repository, workflow, source SHA, exact CI evidence, immutable images, OIDC provenance, database state, backup, capacity, health, audit, and route change.

## Pull request release path

1. The feature pull request must pass RomanLMS LONG CI for the tested tree.
2. Only the owner-authored same-repository change is eligible for merge to canonical `main`.
3. The resulting exact `main` commit must pass RomanLMS SHORT CI.
4. Automatic production release is limited to an exact successful owner-triggered SHORT `workflow_run` and a positive production-change classification. The repository freeze variable, automatic-release variable, and root-owned server switch must all allow it.

## Direct push to main

An owner-authorized direct push to canonical `main` retains its existing SHORT-only path. This route has intentionally reduced coverage compared with a merged pull request. The server independently checks the GitHub commit identity, exact run, required job, current branch commit and any pull-request association. A commit associated with a merged pull request must satisfy the LONG policy; incomplete PR evidence cannot fall back to direct-push authorization. Commit messages are never authorization evidence.

## Manual operations

The owner may dispatch the registered production workflow from current `main` with an exact root-registered phrase for deploy, verify, rollback or runtime refresh. A manual request cannot add automatic trigger metadata. The caller pins the generic infrastructure workflow SHA in `uses:` and passes the same SHA as `platform_ref`. The reusable workflow resolves root `main` once at authorization start and records the exact application-registry commit used for both authorization and builds. The server allowlist must contain the reusable workflow SHA.

## Generic migration policy

The production controller derives the live Flyway schema version and pending migration set from the immutable release migrator. It validates canonical version order, unique identities, checksums, applied history, database/schema identity and exact release provenance. The target is derived from the pending release migrations; no V132-to-V133 source/target pair, migration filename, checksum or one-pending-file rule is embedded in the controller.

If no migration is pending, the release skips full backup, database writer quiescence and migration execution. If one or more migrations are pending, the controller uses the app's dedicated migration environment and execution role, creates and verifies a fresh pgBackRest backup, quiesces the registered writers, revalidates the same plan fingerprint, runs only Flyway migrate, and verifies the resulting history and target before activating the candidate. The registered batch limit is a cap on one release's pending set, currently 32; it does not require a single migration per release.

The migration adapter is forward-only. It does not run `repair`, alter Flyway history, or reverse SQL. A known unchanged source plan may restore the retained application. A fully applied target may continue to candidate health checks. An unknown outcome keeps the affected writer stopped and does not switch traffic to an unverified application. Application rollback is allowed only when the previous release is compatible with the live, observed schema.

See [Database migration lifecycle](database-migration-lifecycle.md) for backup verification, audit fields, failures and recovery steps. The earlier failed V133 migration attempt is historical context only; it is not an active per-version approval or the current migration contract. Always inspect live schema and backup health before a production operation.

## CI and controller regressions

The RomanLMS CI gate tests exact SHORT/LONG trees, direct push, merged PR behavior, missing/failed jobs, stale commits, wrong actors/repositories/branches/workflows/attempts and non-production changes. The migration regressions cover no pending changes, single and multiple pending migrations, duplicate/order/checksum failures, unknown history, plan revalidation, backup errors, timeout/unknown outcome, and app rollback compatibility. These tests do not replace a restored staging database or the live read-only production preflight.
