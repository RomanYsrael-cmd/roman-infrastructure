# RomanLMS Phase 6 handoff

## Current state

Private RomanLMS integration is complete and verified on roman-server. The frontend and backend are active in the blue slot behind the loopback-only 127.0.0.1:18081 listener. Active source SHA is df7c3bf12d6bd09fd5ed08626bacfad308bb4e21, controller SHA is 60c74abfec435dd3eb20ea71b9246d7de476615e, and database schema is V132. The exact immutable frontend and backend image digests, CI runs, deployment checks, and synthetic acceptance results are in the [evidence report](evidence/phase6-romanlms-production-integration.md).

This is a private staging deployment, not a production cutover. Do not change the production database, public DNS, Cloudflare, or public Nginx route without separate authorization. None of those were changed during Phase 6.

## Phase 5 baseline

Phase 5 remains active and isolated as the harmless platform-smoke application on 127.0.0.1:18080. Its restricted controller, roman-deploy account, SSH key, server allowlist, monitoring, and smoke rollback remain in place. The existing ROMAN-LAPTOP runner is still used; no production runner or paid service was added. main remains unprotected by the owner's decision.

## Operating gates

- Use only the owner-dispatched RomanLMS private staging workflow from current main. The actor, repository, branch, exact commit, pinned reusable workflow, and latest required CI result must match.
- Pushes to either repository's main branch run CI only. The private deployment workflow is manually dispatched and does not deploy from a push.
- Images are addressed by immutable GHCR digests and checked server-side. The controller verifies repository and run authorization, image metadata, the root-owned environment file, storage mounts, internal network, application/database configuration, migration history, capacity, both component health checks, and the loopback route.
- Staging uses romanlms_phase6_test plus isolated object storage. The baseline romanlms_phase6 is preserved. Flyway validate/info is read-only; automatic migration is disabled. Mail and FCM are disabled. Schedulers are enabled against the isolated staging environment so chat SSE continues to operate.
- Supported manual actions are DEPLOY PRIVATE ROMANLMS, TEST PRIVATE BACKEND FAILURE, TEST PRIVATE FRONTEND FAILURE, VERIFY PRIVATE ROMANLMS, and ROLLBACK PRIVATE ROMANLMS. These actions must not be used to change public routing or production data.
- The controller requires at least 3 GiB available before candidate startup. If that gate fails, retain the active release; use a separately approved maintenance-window procedure rather than forcing overlap.

## Verified Phase 6 results

- Exact RomanLMS main CI and infrastructure CI passed.
- The private deploy passed with separate frontend/backend containers, immutable image digests, healthy database and object storage dependencies, and frontend, backend health, and Prometheus routes returning HTTP 200.
- Unhealthy backend and frontend candidates were rejected without changing the active release.
- Coordinated rollback succeeded twice; the corrected blue df7c3bf release is active now.
- Synthetic live acceptance verified group membership, persisted group messaging, student SSE delivery, synthetic PDF preview, group archival, and synthetic session revocation on the disposable database and storage.
- Grafana and Healthchecks alert paths have confirmed firing/recovery delivery. Telegram showed the private deploy success and both candidate-rejection firing notices. The newest rollback-specific notices were not visible in the inspected chat view and remain individually unverified.

## Unverified features and production cutover gates

Account activation/reset email remains unverified because outbound mail is disabled. Live Android device/emulator behavior and peak-load behavior are unverified. Chat delivery uses authenticated SSE; no WebSocket endpoint has been identified or tested. Current resource observations are not a capacity certification. The November 6 database dump may omit later production writes, and the original object-storage contents were not restored.

Before any separately authorized production cutover: identify the production source of truth and writes after the snapshot; take and verify a fresh consistent backup; validate production roles, grants, and extensions; recover and validate original object storage; review migration checksums and forward/rollback compatibility; freeze the approved image digests and secrets; prepare a public Nginx/Cloudflare plan, maintenance window, and database recovery procedure; complete any remaining alert-delivery verification; then obtain separate approval for production data or traffic changes.
