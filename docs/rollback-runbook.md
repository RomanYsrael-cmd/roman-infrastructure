# Rollback runbook

The controller preserves the active release as the previous release in app-scoped root-owned state. HTTP rollback checks the retained container and configured health endpoint, changes only the app's managed route include, validates Nginx, and records the new active/previous pointer. Coordinated stacks validate all retained components before switching their route together. If route validation fails, the controller restores the prior route and state.

The controller's rollback operation requires the same approved application ID, main-branch workflow run, pinned reusable workflow, current main commit, and server lock as a deploy request. It does not accept an image or arbitrary target release from the caller; it selects only the retained previous release from root-owned server state.

The caller example includes a separate ROLLBACK SMOKE choice. It uses the same pinned reusable workflow, manual main-branch dispatch, SSH identity, workflow-origin check, and server lock. It selects only the controller's retained previous release.

Application rollback never reverses database migrations. A database-backed application can return to a retained application image only if that app's recorded schema is compatible with the live, observed schema. New registered databases require a supported backup and migration adapter before production rollout.

## RomanLMS production schema migration recovery

The migration controller derives source and target versions from the live Flyway history and the exact release artifact. It records the planned files and checksums, plan fingerprint, backup identity, execution result and postflight schema in the deployment audit. Compatibility is checked for the actual observed schema, not a fixed V132/V133 pair.

If the controller cannot establish whether Flyway finished, leave the affected writer stopped and keep the existing route and last verified release state in place. Do not retry, run `flyway repair`, or edit `flyway_schema_history` while the outcome is uncertain. Use the audited backup identity and the selected adapter's verification to establish the recovery point, restore to an isolated recovery database, and validate its schema and data before any live database restoration or route change. Any recovery that would replace or restore a live database must follow the database incident procedure; application rollback alone is not database recovery.

## RomanLMS private staging rollback

RomanLMS stores a coordinated frontend/backend release in root-controlled state. The supported action is **ROLLBACK PRIVATE ROMANLMS** through the owner-only manual workflow on current `main`. The controller selects only the retained previous pair after checking its component identities, image metadata, schema version, storage mounts, capacity, and health. It validates and reloads the private Nginx include once for the pair, checks both routed components, updates the release pointer, and writes an audit/Prometheus result.

After the workflow succeeds, verify the private root and backend health endpoint return HTTP 200, both application components are healthy, the active release points to the intended prior SHA and digests, and the deployment audit records `operation=rollback-stack` with `outcome=success`. Confirm the Telegram rollback/recovery notification.

If rollback readiness fails, the controller restores the original route/state and removes only the failed rollback candidate. Do not manually delete retained containers or alter the route include. Preserve the currently working pair and use the owner-authorized workflow to retry only after diagnosing the recorded failure.

Rollback is application-only. It does not restore, reverse, or migrate PostgreSQL. Phase 6 runs Flyway validation/info without applying migrations, and staging writes are confined to `romanlms_phase6_test`; the preserved recovery baseline is `romanlms_phase6`.

### Latest verified rollback evidence

On 2026-11-08, RomanLMS private rollback runs [42460564776](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460564776) and [42460785540](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460785540) both completed successfully. The second operation reactivated the corrected blue build with source SHA `df7c3bf12d6bd09fd5ed08626bacfad308bb4e21`; its active image digests are listed in the [Phase 6 evidence report](evidence/phase6-romanlms-production-integration.md). Server audit records show `rollback-stack` outcome `success` for both runs. The final private route and component health checks returned HTTP 200. No migration or production database action was part of either rollback.

Telegram displayed the successful private deployment and the expected unhealthy backend/frontend candidate firing notices. The latest rollback-specific Telegram notices were not visible in the inspected chat view, so treat the rollback runs as functionally verified while individual rollback-alert delivery remains unverified.
