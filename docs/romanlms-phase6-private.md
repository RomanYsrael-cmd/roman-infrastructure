# RomanLMS Phase 6 private deployment contract

This contract is for the isolated Phase 6 test environment. It does not authorize production database replacement, migration, or public traffic changes. The Phase 5 smoke application keeps its separate policy and listener.

## Verified active release (2026-11-08)

The private route is operating with source SHA df7c3bf12d6bd09fd5ed08626bacfad308bb4e21 on the blue slot and schema V132. The active backend digest is sha256:f4ca922870570787dc52cd19f0cce17e671515dafbfb70ddf8b5bada8e00d53f; the active frontend digest is sha256:16bcb1e2f349c19c2d1ad21fb7f7268366da10c22ea715b9327d70d96424e785. The frontend root, backend health, and backend Prometheus endpoints returned HTTP 200 through the private listener; the collector reported romanlms_phase6_metrics_up=1. The exact-commit CI, successful deployment, unhealthy candidate tests, and successful rollback run IDs are recorded in docs/evidence/phase6-romanlms-production-integration.md.

The live synthetic acceptance used three synthetic accounts and only the disposable database and isolated object storage. It verified group membership, persisted group messages, student SSE delivery, synthetic PDF preview, group archival, and revocation of the three test sessions. It did not use student records or the preserved recovery baseline.

## Isolation and state

- Host Nginx listens on `127.0.0.1:18081`. One root-controlled include contains both frontend and backend locations, so one validated Nginx reload changes the pair. The include starts closed with HTTP 503.
- Containers use the internal Docker network `romanlms-phase6-staging`, subnet `172.30.66.0/24`, with fixed private addresses and no published ports. The Linux host reaches these addresses directly. The frontend, backend, database, and object service do not expose host ports.
- The PostgreSQL copy and object data use dedicated directories on `/srv/databases` and `/srv/data`. Application and deployment state use `/srv/apps`. Mount checks precede setup, deployment, and startup. The boot service checks all mounts before starting staging containers; their Docker restart policy is `no`.
- Staging uses a separate database role, generated credentials, a separate S3-compatible service, and a separate bucket. Application schedulers run against the isolated staging copy, while external mail, FCM push, and automatic Flyway migration remain disabled. Application tests that write data use disposable staging data; the original recovery baseline is not an application test database.
- The restore marker records the tested source hash and V132 schema. It is proof of this staging restore, not evidence of a fresh production backup.

## Release and rollback

The owner manually dispatches the RomanLMS private workflow on current `main`. The reusable workflow and server check the caller, actor, branch, exact commit, pinned workflow, and latest required CI result. Every component image uses an allowlisted GHCR namespace and immutable digest. The server checks image labels, the isolated network, storage, available memory, dependency health, root-owned environment files, and the allowlisted database URL. Flyway validation and the exact schema/history checks run without applying a migration.

Both candidates must pass direct health checks before the route file changes. The controller then validates and reloads Nginx and checks both routed endpoints. A failure restores the prior route and removes candidate containers. The prior release pair is retained for rollback; stopped retained containers can be restarted after capacity and identity checks. Rollback changes application containers and routing only. It does not reverse or restore a database migration.

The server allowlist must contain the exact reusable-workflow SHA used by the caller. A new workflow revision requires a reviewed update to both the caller pin and the server allowlist. `main` remains unprotected by owner decision.

## Metrics access policy

The application metrics endpoint remains protected by the backend security matcher. Loopback is trusted by default. When host Nginx reaches the backend over the isolated Docker bridge, configure `ROMANLMS_METRICS_TRUSTED_PROXY_ADDRESSES` with only the exact IPv4 gateway observed for `romanlms-phase6-staging` (currently `172.30.66.1`). Keep the value in the root-owned mode `0600` runtime environment file. Do not trust a whole subnet, forwarded client-supplied address, or wildcard. The Nginx application listener remains bound to `127.0.0.1:18081`; no backend port is published on the host. After deploying a build with the matcher support, verify `/lms/actuator/prometheus` returns HTTP 200 through that private route and Prometheus reports `romanlms_phase6_metrics_up=1`.

## Staging restore procedure

1. Verify the source backup checksum, source date, readability, and available later backups. The reviewed snapshot is `2026-11-06_162051`; its RomanLMS dump SHA-256 is `b9a1d013332c7692d11b84c2503e51195b8e6ec36e71a5fa223640fa9a617890`. It may omit later production writes.
2. Install the reviewed playbook with `install_romanlms_phase6=true`, using the existing restricted deploy public key and administrative access. The default playbook path keeps Phase 6 opt-in.
3. Place a root-owned mode `0600` copy of the reviewed dump at `/run/roman-platform/romanlms-phase6-source.dump`. `/run` must be `tmpfs`. Preserve the original backup.
4. Run the installed root-only `prepare-romanlms-phase6` bootstrap. It refuses existing staging destinations, checks the fixed checksum, mounts, memory, disk, Docker storage root, and subnet conflicts, creates staging credentials, restores only a new staging database, and enables the mount-checked startup unit.
5. Check the staging restore marker and service health without printing credentials or records. Validate the exact Flyway version and history using the pinned migrator. Investigate any partial bootstrap before retrying; the script does not erase an existing staging destination.

The reviewed restore uses `--no-owner --no-privileges`, then grants a staging application role access to the restored schema. It does not establish that production owners, roles, grants, or external file objects have been recovered. Do not use the staging marker as authorization to modify production.

## Capacity and operations

The host has approximately 7.1 GiB usable RAM and 12 logical CPU threads. The initial staging limits are 1 GiB for PostgreSQL, 512 MiB for object storage, 1.5 GiB per backend with a 640 MiB JVM heap, and 256 MiB per frontend. Deployment requires at least 3 GiB available before a candidate starts. These are upper limits, not measured peak requirements. Measure live host available memory, swap, existing services, and both backend instances before accepting overlap. If the gate fails, retain the current release and use a separately reviewed maintenance procedure; this controller does not yet implement a maintenance strategy.

The existing monitoring schedule collects bounded application health, HTTP aggregates, JVM memory, container CPU/memory, and dependency status into the existing Node Exporter textfile directory. It discards request URI labels. The Phase 6 dashboard and alert rules use the existing Prometheus and Telegram route. Backup and disk panels reflect existing host metrics, not a verified production recovery chain.

## Production cutover boundary

Before proposing cutover, identify the production source of truth and any writes after the snapshot; take and verify a fresh consistent backup; inventory roles, grants, extensions, and object storage; validate migration checksums and compatibility; define exact immutable application digests and secrets; review the public Nginx and Cloudflare changes; establish a maintenance window and database recovery plan; and perform private acceptance, rollback, monitoring, and notification checks. Public DNS, public routes, production database replacement, and destructive production migrations require separate owner approval.
