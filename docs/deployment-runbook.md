# Deployment runbook

## Current smoke state

- The controller, restricted `roman-deploy` account, Ed25519 key, private Nginx route, and deployment Grafana alerts are installed on `roman-server`.
- The active harmless release is in the blue slot. `http://127.0.0.1:18080/health` returns HTTP 200. The green release is retained as the previous release.
- The RomanLMS caller requires the owner actor, `main`, `workflow_dispatch`, the expected caller workflow path, and the exact pinned reusable workflow SHA `d95b276cf3d257a685fe7bb7464197c44f4b4ea8`.
- The server allowlist contains only that reusable workflow SHA. The SSH key and verified host key are stored in the RomanLMS repository secrets; never print or paste their values.
- The route listens only on loopback. Do not add a public DNS, Cloudflare, or production route for the smoke app.

## Run the smoke workflow

1. Confirm the existing `ROMAN-LAPTOP` runner is online and Docker Desktop's Linux engine is available.
2. Confirm required RomanLMS CI is green on the current `main` commit.
3. In RomanLMS Actions, run **Roman platform smoke deployment** on `main` and choose `DEPLOY SMOKE`. This publishes the harmless candidate to GHCR, obtains its immutable digest, and sends it through restricted Tailscale SSH.
4. Verify the private health endpoint returns HTTP 200 and Prometheus reports the active slot and successful deployment run.
5. Choose `TEST UNHEALTHY SMOKE`. The workflow succeeds only when the server rejects the candidate at its HTTP 503 readiness gate. Confirm the existing route remains healthy and the failure alert reaches Telegram.
6. Choose `DEPLOY SMOKE` again to make a new healthy slot active while retaining the former slot.
7. Choose `ROLLBACK SMOKE`. Confirm the previous release is active, the private health endpoint remains HTTP 200, the server audit says `operation=rollback` and `outcome=success`, and Telegram receives the rollback success alert.

The expected-unhealthy workflow run is a passing test even though the controller records a failed deployment attempt. Its `Roman Platform Deployment Failed` Telegram alert should resolve after a healthy operation. The success alert is temporary and resolves automatically after two minutes.

## Verify server evidence

- Private route: `curl -fsS http://127.0.0.1:18080/health` from `roman-server`.
- Prometheus scrape: `curl -fsS http://127.0.0.1:9100/metrics`.
- Prometheus API: query `roman_platform_active_release_info` and `roman_platform_last_deployment_info` at `http://127.0.0.1:9090`.
- Active and retained release state: `/srv/apps/roman-platform/state/platform-smoke.json` (admin access only).
- Deployment audit: `/srv/apps/roman-platform/logs/deployments.jsonl` (admin access only).
- Running test containers: inspect only containers labeled `roman.platform.app=platform-smoke`.

Keep logs limited to deployment metadata. Never print SSH credentials, GHCR tokens, repository secrets, or private keys.

## RomanLMS private staging

RomanLMS uses a separate application policy, loopback listener `127.0.0.1:18081`, internal Docker network, isolated PostgreSQL and object storage, and paired frontend/backend release. The application containers have no published host ports. Keep the smoke application and its `127.0.0.1:18080` listener independent.

### Latest verified release

The active release is blue at source SHA `df7c3bf12d6bd09fd5ed08626bacfad308bb4e21`, schema V132. The backend digest is `sha256:f4ca922870570787dc52cd19f0cce17e671515dafbfb70ddf8b5bada8e00d53f`; the frontend digest is `sha256:16bcb1e2f349c19c2d1ad21fb7f7268366da10c22ea715b9327d70d96424e785`. Exact-commit CI [42452119200](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42452119200), deployment [42459327787](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42459327787), backend/frontend rejection tests [42459983275](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42459983275) and [42460323434](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460323434), and successful coordinated rollbacks [42460564776](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460564776) and [42460785540](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460785540) are complete. The final route serves the corrected blue pair. The private frontend, backend health, and Prometheus endpoints returned HTTP 200; Prometheus reports `romanlms_phase6_metrics_up=1`.

Telegram showed the private deployment success and firing notices for both expected unhealthy-candidate runs. The newest rollback-specific Telegram notices were not visible in the inspected chat view; verify them on the next owner-observed rollback exercise if individual rollback-notice evidence is required.

The only supported release path is the owner-dispatched **RomanLMS private staging deployment** workflow on current `main`. The caller workflow, reusable workflow SHA, server authorization allowlist, actor, repository, branch, exact commit, and latest required CI result must all match. Pushes to `main` run CI only. Never use a direct SSH command as a release mechanism.

For each operation:

1. Confirm the current RomanLMS `main` commit and its required CI result are green on `ROMAN-LAPTOP`.
2. Confirm the requested operation is one of `DEPLOY PRIVATE ROMANLMS`, `TEST PRIVATE BACKEND FAILURE`, `TEST PRIVATE FRONTEND FAILURE`, `VERIFY PRIVATE ROMANLMS`, or `ROLLBACK PRIVATE ROMANLMS`.
3. Dispatch the workflow from `main` with the exact confirmation choice. The build publishes frontend and backend images with immutable GHCR digests; the server verifies metadata and repository policy before starting containers.
4. For deploy, confirm database connectivity, exact schema/history validation, storage mounts, network policy and memory preflight. Flyway application is disabled; preflight must not apply a migration.
5. Verify the private frontend root and `/lms/actuator/health`; verify both components are healthy, the expected release identity is active, and the deployment audit and Prometheus metrics record the operation.
6. For expected-unhealthy tests, treat a passing workflow as proof only that the named candidate was rejected and the active route remained healthy. Confirm the relevant failure/recovery notification reaches Telegram.
7. After rollback, verify both frontend and backend route to the retained release, readiness returns healthy, the active/previous pointer is coherent, and the audit records a successful rollback. Database state is never rolled back by this action.

The monitoring collector must report `romanlms_phase6_metrics_up=1` after the metrics matcher build is deployed. If it remains zero, diagnose the exact Nginx-to-backend source address and compare it with the single IP literal in the root-owned runtime environment file. Never broaden the trusted address range to clear an alert.

The server should have at least 3 GiB available before a candidate starts. Current limits are PostgreSQL 1 GiB, object storage 512 MiB, backend 1.5 GiB per instance with JVM `-Xmx640m`, and frontend 256 MiB per instance. These are caps, not peak guarantees. If the gate fails, keep the current release active and use only a separately reviewed maintenance-window strategy; do not force an overlap.

## Production cutover boundary

The private Phase 6 release is not a production cutover. It does not authorize restoring or migrating the live RomanLMS production database, changing public Nginx routes, DNS, or Cloudflare. The November 6 staging restore may omit later writes and does not recover original object storage. See [romanlms-phase6-private.md](romanlms-phase6-private.md) and [phase6-handoff.md](phase6-handoff.md) for the remaining production prerequisites.
