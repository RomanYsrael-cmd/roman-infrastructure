# Phase 6 RomanLMS private integration evidence

Status: **private staging integration complete and verified; production cutover not performed.** This report records the private application deployment only. It does not authorize production database, public route, DNS, or Cloudflare changes.

Checkpoint date: 2026-11-08.

## Revisions and CI

- Infrastructure main: `65aa5ad2cd6f1c3dcb72cf2bab6b883a62f66f1a` (`Keep private staging application schedulers enabled`). Required infrastructure CI [run 42459234210](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/42459234210) passed. The preceding monitoring matcher fix is in infrastructure revision `8b50e65ef9e1efa3f01a249d609e7ea3236351a1`.
- RomanLMS main: `df7c3bf12d6bd09fd5ed08626bacfad308bb4e21` (`Fix private chat member listing query`). Exact-commit CI [run 42452119200](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42452119200) passed on the existing `ROMAN-LAPTOP` runner. Backend compilation, unit, architecture, and PostgreSQL Testcontainers integration checks passed. Path-filtered unrelated frontend/Android checks were skipped in this run.
- The infrastructure repository worktree contains six documentation-only changes for this evidence refresh. RomanLMS has no tracked changes from this continuation. Its seven untracked Phase 5 logs remain untouched and were not staged, committed, or included here; one has credential-like fixture content and must remain private.

## Active private release and workflow evidence

- Listener: Nginx is bound to `127.0.0.1:18081`; no public bind or application host port is configured.
- Active release: blue, RomanLMS source SHA `df7c3bf12d6bd09fd5ed08626bacfad308bb4e21`, schema V132.
- Deployment controller SHA: `60c74abfec435dd3eb20ea71b9246d7de476615e`.
- Backend image: `ghcr.io/romanysrael-cmd/romanlms-phase6-backend@sha256:f4ca922870570787dc52cd19f0cce17e671515dafbfb70ddf8b5bada8e00d53f`.
- Frontend image: `ghcr.io/romanysrael-cmd/romanlms-phase6-frontend@sha256:16bcb1e2f349c19c2d1ad21fb7f7268366da10c22ea715b9327d70d96424e785`.

| Scenario | GitHub run | Result |
| --- | --- | --- |
| Exact RomanLMS main CI | [42452119200](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42452119200) | Passed for the deployed source SHA. |
| Private database validation and deployment | [42459327787](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42459327787) | Passed. Immutable frontend/backend digests above were deployed to blue; server audit records schema V132 validation and `deploy-stack` success. |
| Deliberately unhealthy backend candidate | [42459983275](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42459983275) | Passed as a negative test. Server rejected the candidate; active release and route were retained. |
| Deliberately unhealthy frontend candidate | [42460323434](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460323434) | Passed as a negative test. Server rejected the candidate; active release and route were retained. |
| Coordinated rollback | [42460564776](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460564776) | Passed. Server audit records `rollback-stack` success. |
| Reactivate corrected blue release | [42460785540](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460785540) | Passed. Corrected blue source SHA is active after the rollback exercise. |

Server audit records include start and result entries for the deploy, each rejected candidate, and both rollbacks. The final active route points to the corrected blue frontend/backend pair. The application workflows are manual dispatches gated to the owner actor, current main, exact commit, successful required CI, pinned reusable workflow, and server-side repository/image policy. Pushes to either repository's main branch run CI only; no push-triggered deployment, migration, or traffic switch was present.

## Live application acceptance

- The frontend root, `/lms/actuator/health`, and `/lms/actuator/prometheus` returned HTTP 200 through the loopback route. The collector reported `romanlms_phase6_metrics_up 1`; backend/frontend/database/object dependency health metrics reported up.
- A live synthetic acceptance on the deployed source used three synthetic accounts and the disposable staging database/storage. It verified three group members, persisted group messaging, delivery of the synthetic message through authenticated student SSE, preview of the exact synthetic PDF bytes, group archival, and revocation of the three synthetic sessions. The temporary acceptance helper emitted only Boolean/count summaries and did not export student records.
- Earlier private acceptance on the preceding app revision exercised login/session handling, role and tenant access, academic workflows, assignment/submission/grading, quizzes, attendance, direct chat, SSE, and synthetic object-storage upload/download. Those checks cover unchanged application paths; the latest exact-source focused live retest covered the group-member query and SSE fix.
- The checked-in infrastructure suite ran locally after the scheduler setting change: 75 passed, 6 skipped for Windows/root-only cases. Infrastructure CI run 42459234210 passed, including Ansible syntax and controller/negative-gate coverage.

## Telegram and monitoring

The server's monitoring collector and Healthchecks heartbeat timers are active. Grafana health/metrics collection is healthy. In the inspected Telegram alert chat, the private deployment success notice and firing notices for both expected unhealthy candidate runs were visible. Earlier controlled tests confirmed Grafana firing/recovery and Healthchecks outage/recovery delivery. The latest rollback-specific Telegram messages were not visible in the inspected chat view; their individual delivery is therefore unverified and should be confirmed during the next owner-observed rollback exercise.

## Database and isolation

- The November 6 source dump remains at `/run/roman-platform/romanlms-phase6-source.dump`; its SHA-256 remains `b9a1d013332c7692d11b84c2503e51195b8e6ec36e71a5fa223640fa9a617890`.
- The preserved `romanlms_phase6` recovery baseline and disposable `romanlms_phase6_test` database both report schema V132 and 135 successful Flyway history records. Deployment preflight ran read-only migration validation/info. No migration was applied.
- Application writes were confined to `romanlms_phase6_test` and isolated object storage. The original recovery baseline was not used for application acceptance. Synthetic test sessions were revoked; no student data was exported into CI artifacts, logs, screenshots, or this report.
- Outbound mail and FCM are disabled. Application schedulers are enabled only against private staging so persisted-event polling and chat SSE work. The isolated environment prevents the tested mail/push side effects; no public webhook or production messaging integration was enabled.
- Production PostgreSQL and application traffic were not changed. Existing administrative SSH/Tailscale access and existing infrastructure services were preserved. No Cloudflare or DNS changes were made. No `pg_restore` or Flyway migration process remained active at the final checks.

The restored dump may omit production writes after November 6, and the original object-storage contents were not recovered. Production roles, grants, extensions, and current object storage still require a fresh review and verified backup before any separate cutover.

## Resource observations

The host has approximately 7.1 GiB usable RAM. The final point-in-time observation showed about 4.0 GiB available and 204 MiB of 4 GiB swap used. Observed memory use was approximately 668 MiB for the active backend, 570 MiB for the retained backend, 12 MiB and 11 MiB for the frontend containers, 167 MiB for PostgreSQL, 119 MiB for object storage, 336 MiB for Grafana, and 72 MiB for Prometheus. Configured caps are 1 GiB for PostgreSQL, 512 MiB for object storage, 1.5 GiB for each backend with a 640 MiB JVM heap, and 256 MiB for each frontend.

The controller enforces a 3 GiB available-memory gate before candidate startup. These observations do not certify peak capacity or sustained load. If the memory gate fails, retain the current release and schedule an approved maintenance-window deployment instead of forcing blue/green overlap.

## Remaining unverified items and cutover prerequisites

- Account activation and password reset through email were not tested because outbound email is disabled.
- Live Android device/emulator behavior was not tested. The exact CI run path-filtered Android/frontend work; emulator acceptance remains pending.
- Chat uses authenticated SSE. No WebSocket endpoint was identified or verified.
- Peak-load behavior and production recovery timing are not certified.
- Telegram delivery of the newest rollback-specific notices is not individually verified, as stated above.
- The November 6 restore may be stale and does not include original object-storage contents. Before any separately authorized production cutover, identify later writes and the actual source of truth; take and independently verify a fresh consistent database backup; validate roles, grants, extensions, migration compatibility and object storage; freeze approved image digests and secrets; prepare the maintenance and route plan; and obtain separate approval for production database or public traffic changes.

No production cutover was performed during this phase.
