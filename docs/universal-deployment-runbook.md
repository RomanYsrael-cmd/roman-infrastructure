# Universal deployment runbook

Use this runbook after an application and its reusable workflow are registered and the matching controller revision is installed. Production installation is a coordinated change: the registry, controller, caller workflow, migrator contract, and root allowlist must agree.

## Before a deployment

1. Check the server's installed controller SHA, root-owned `applications.json`, active release state, Docker and Nginx health, `/srv/apps` space, available RAM/CPU, existing routes and port bindings.
2. Check the app registration, full reusable workflow SHA, caller workflow path, exact current source SHA, successful required CI, actor, image source labels, image digests, and GitHub OIDC provenance.
3. For database-backed work, run the registered Flyway read-only preflight. Review the observed schema, pending file list, checksums, plan fingerprint, backup adapter, migration role and compatibility evidence before the operation can execute.
4. If migrations are pending, verify a fresh recoverable backup and writer quiescence before execution. With no pending migrations, confirm that the controller skips the backup and writer stop.
5. Do not proceed if the controller and migrator are on incompatible revisions or if the live capacity/port/routing preflight fails.

## Deploy and verify

Dispatch the registered application workflow on its release branch with the root-registered operation and exact confirmation phrase. Automatic releases are permitted only for apps whose root registry explicitly enables the exact workflow-run and operation; all other apps require an owner dispatch.

The controller locks only that app ID, starts a resource-limited candidate with its own configuration and secrets, waits for health, and performs the profile's route or service transition. Verify the workflow result, application health, active release state, deployment audit, `roman_platform_last_deployment_info`, `roman_platform_active_release_info`, CPU/memory/PID metrics, Prometheus scrape, Grafana alert state, and Telegram notice where configured.

For HTTP workloads, verify both the app route and the expected domain/path from an authorized network. For workers and scheduled jobs, check Docker health, last execution/exit status, timer state, and application-specific heartbeat. For stateful services and game servers, check the persistent volume mount, protocol-specific port, backup result, and an application-level client connection.

Build-only projects upload a checksummed artifact to the GitHub Actions run. Confirm the release manifest contains the expected application ID, source SHA, platform SHA, build system, artifact paths, sizes, and checksums before using a project-specific signing or store-publication workflow.

## Rollback

Use the app's explicit rollback action. The controller selects the retained release from root-owned state; the workflow cannot choose an arbitrary image or host path. The controller verifies the retained image, health, resource policy, route and—when a database is configured—the recorded schema against live history before switching traffic.

Application rollback never reverses SQL. If schema migration status is unknown, keep writers stopped and route unchanged, preserve the backup/audit metadata, and use the database recovery process. Stateful service restoration must use that application's verified volume backup adapter; do not restore a live volume from an unverified archive.

## Monitoring and troubleshooting

- `roman_platform_last_deployment_info`: last operation, result, run ID and request commit by app ID.
- `roman_platform_active_release_info`: active source revision and slot by app ID.
- App deployment audit: the registered `history_file`; inspect as an administrator and redact tokens or environment data before sharing.
- Metrics file: the registered Node Exporter textfile path; compare scrape state and latest outcome timestamps.
- Logs: inspect only containers carrying the app's `roman.platform.app` label and its app-scoped systemd units/timers.
- Database: use the migration plan, backup-set/dump identity, Flyway history, and postflight audit fields. Do not print private environment files or database command lines containing credentials.
- Nginx or health failure: the controller restores the previous route include when a checked route switch fails. Diagnose the candidate and use the registered workflow to retry; do not edit the active route file manually.
- Resource gate failure: reduce the candidate's registered budget or use a registered maintenance/recreate strategy after checking impact. Do not bypass host capacity or disk gates.

The existing Prometheus, Grafana and Telegram integrations remain the alert path. Every registered app exports independent deployment metrics; workload-specific application metrics remain the app owner's responsibility.

## Production acceptance

The initial production acceptance is a non-destructive `verify` or no-migration release for the exact registered app, with no schema change. Before controller installation, retain the current package/controller revision and a fresh database backup where the application uses a database. Verify the root-owned controller SHA and registry after installation. Roll back the infrastructure/controller only to a revision that accepts the current registry and release-state format. Never delete existing Docker volumes or release state as part of a controller upgrade.
