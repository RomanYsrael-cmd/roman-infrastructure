# Universal deployment platform — implementation contract

Status: **design and isolated migration-plan foundation only**. This is not a claim that production or any other repository has been migrated to the new controller. Existing production policies stay active until an explicitly tested cutover.

## Outcome

Roman Infrastructure will deploy independently registered applications from any repository that the owner explicitly authorizes. An app may have no database. For apps with databases, the deployment controller must no longer carry a hardcoded version pair, a single-file SQL checksum, or a one-pending-migration limit.

**This does not mean automatically deploying every repository, running arbitrary shell commands on the host, or granting every project's CI access to production credentials.** Each project must be registered once with an owner-approved, server-side policy.

## Deployment profiles

| Profile | Workload | Migration hook |
| --- | --- | --- |
| static-web | Immutable web image served by Nginx | None |
| http-service | API/server with health probes and blue/green | Optional |
| multi-service | Explicit named container set (e.g. UI/API/worker) | Optional |
| worker | Persistent background process; process/heartbeat health | Optional |
| scheduled-job | Bounded execution with isolated permissions | Optional |
| game-server | Long-lived network service with configured TCP/UDP ports and persistent data | Optional, normally none |
| stateful-service | A vetted persistent service with dedicated volume and backup policy | Service-specific |

A profile describes validated inputs and safe behavior; project repositories cannot supply unrestricted Docker options, privileged mode, host paths or shell commands through a release request.

## Project registry and onboarding

The approved registry is a declarative per-app server-side policy, not a copy of RomanLMS constants. Each entry declares:

- Unique app ID and GitHub owner/repository, allowed source branch(es), actors and workflow identity.
- Approved build recipe and immutable GHCR image namespaces, with the CI-success/commit provenance gate.
- Profile, components, dependency order, health/readiness behavior, resource limits and rollout strategy.
- Network, private listeners, domain/routing, TLS, Nginx/Cloudflare integration and port ownership.
- Required storage mounts, persistent volumes, restore expectations and backup type.
- Secrets by root-owned references only; no secrets in the registry, workflow inputs or image labels.
- Optional PostgreSQL/Flyway, MySQL/Liquibase, or explicitly reviewed migration adapter.
- Explicit rollback and backward-compatibility policy, including behavior after schema expansion.

Onboarding should be a CLI/CI-backed operation:
1. Analyze the repository and request profile, builds, ports, storage, database and domains.
2. Produce a generated manifest and CI/workflow templates for review.
3. Validate policy schemas; reject duplicate domains/ports, mount escapes, overcommit and unauthorized repositories.
4. Run isolated build/image verification and a staging deployment with smoke/negative tests.
5. Install reviewed root-owned manifest and immutable controller revision on the server, then deploy.
6. Record app ownership, versions, deployment events and exact backup/restore evidence.

Releases remain isolated per app. There must be no need to edit and redeploy controller source for ordinary application releases or forward migrations.

## Version-agnostic SQL migration design

The opt-in planner in deploy/controller/forward_migration_plan.py is a pure parser. It currently has no runtime authority and must not be treated as an operational migration switch. Controller integration is a separate change.

Migration execution contract:

1. Require an authorized source commit and successful exact-source CI, immutable migrator image digest, correct DB and schema identities and a dedicated migration account.
2. Run Flyway validation and info against that immutable image with **latest** target semantics (not a repository-global V133 target).
3. Determine current applied version and all pending SQL migrations. Require strictly increasing unique canonical versions, approved migration directory, valid checksums, no unknown/failed history, bounded pending count (e.g. 32).
4. Create a canonical, cryptographic fingerprint of source/target/pending file names and checksums. Bind it to the release attempt/image digest in deployment audit/state.
5. Check backward compatibility for the retained application with explicit release-scoped evidence. A user acknowledgement alone is not proof; integration tests should exercise old and new apps against the expanded schema.
6. If no pending migrations: **skip migration, backup and writer-quiescence steps**. Still validate DB state when DB is configured.
7. If pending: make a fresh consistent backup using a project-specific supported backup adapter, verify it, quiesce relevant writers, and **re-read and compare the exact same plan fingerprint**. Do not treat a changed plan as equivalent.
8. Run only the planned forward migrations with the dedicated database role; disable Flyway clean/repair/baseline. Validate actual postflight schema, checksum and history state before starting and routing the candidate.
9. If the result is unknown, do not restart applications into an unverified schema. If the candidate fails after verified migration, restart the old release only with verified compatibility evidence.
10. Never infer that rollback of SQL is safe; use forward repair or a separately reviewed point-in-time restore procedure.

Do not use migration filename heuristics to *prove* a DDL operation is additive. Explicit CI validation, compatibility testing, and policy gates are still required.

## Remaining code changes before production activation

1. Replace fixed schema_migration source/target/script/checksum in the live controller with an opt-in, release-scoped dynamic plan; remove the one-pending constraint. Maintain migration audit and planner fingerprint.
2. Update RomanLMS ops/deploy/migration/entrypoint.sh so Flyway target is derived from the verified release plan (and supports an initial info without a preselected version). Update its release metadata and tests. The current image hardcodes EXPECTED_TARGET_VERSION and is not yet compatible with this behavior.
3. Refactor _validate_database, _deploy_stack, backup audit, release state schema_version, _refresh_stack and rollback checks to use observed source/target versions. Preserve staged cutover and failure recovery.
4. Make database role, schema, Flyway container identity and backup settings per-application rather than romanlms-specific constants.
5. Implement typed project registry and profile-specific deploy/rollback logic. Keep deployment inputs structured and pinned; do **not** accept arbitrary Compose files from a workflow.
6. Add app-specific CI adapters (container build, static frontend, worker, game server), staging tests, negative authorization tests, backup restoration tests, and resource contention tests.
7. Perform read-only shadow validation against the live RomanLMS database and a restored staging copy. Only then approve installation and enablement; do not deploy framework changes automatically on merge.

## Acceptance tests

- Upgrade from V133 with 3 ordered pending SQL migrations in one release; postflight schema equals latest and all checksums match.
- A release with zero pending migrations succeeds without full backup or writer downtime.
- Invalid/out-of-order/repeated version, failed history, different database URL, mismatched image digest, modified file checksum, changed plan after backup, unsafe retained-app compatibility, and failed backup all fail closed.
- A failed/unknown migration cannot switch traffic or silently repair Flyway history.
- A static site, an HTTP API, a worker, and a multi-service application can be registered independently using the same controller, with per-project credentials and domain isolation.
- Repositories outside the explicit trusted registry cannot deploy, even if they can reference a reusable GitHub Action.
- Existing RomanLMS main direct-push, SHORT-CI and PR/LONG-CI behaviors remain unchanged; main remains intentionally unprotected.
- Active production routing, data, secrets and mount layout are preserved during migration to the new framework.

## Initial candidate repositories

Potential onboardees after individual assessment: RomanYsrael-cmd/roman-lms, schedulease-server, roman-search, battle-of-bluffs, mailforge, and other owner-controlled projects. A GitHub repository being present is **not** evidence that it is a deployable server workload.

## Rollout sequence

A. Land tested *pure planner* foundation (this branch).
B. Integrate generic migration policy and opt-in dry-run with RomanLMS, while leaving the V133 production controller installed.
C. Prove matching Flyway validation, backup, multi-migration and rollback behavior in restored staging.
D. Install the reviewed new controller and explicitly switch RomanLMS to the generic policy.
E. Introduce project profiles, registration CLI and onboard one simple stateless app before stateful services.
F. Expand to other project types only after their individual health, persistence, networking and recovery checks pass.
