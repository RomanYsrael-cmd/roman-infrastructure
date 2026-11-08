# Roman Infrastructure V2 implementation status

## Phase 6: RomanLMS private integration (2026-11-08)

**Private staging integration is complete and verified. Production cutover has not been performed.** The application is running on roman-server behind Nginx bound only to 127.0.0.1:18081. No production database, public route, DNS, or Cloudflare configuration was changed.

### Active private release

- RomanLMS source: df7c3bf12d6bd09fd5ed08626bacfad308bb4e21.
- Deployment controller revision: 60c74abfec435dd3eb20ea71b9246d7de476615e.
- Active slot: blue; schema V132.
- Backend image: ghcr.io/romanysrael-cmd/romanlms-phase6-backend@sha256:f4ca922870570787dc52cd19f0cce17e671515dafbfb70ddf8b5bada8e00d53f.
- Frontend image: ghcr.io/romanysrael-cmd/romanlms-phase6-frontend@sha256:16bcb1e2f349c19c2d1ad21fb7f7268366da10c22ea715b9327d70d96424e785.
- Private frontend, backend health, and Prometheus routes returned HTTP 200; romanlms_phase6_metrics_up was 1.
- Staging uses romanlms_phase6_test and isolated object storage. Mail and FCM are disabled, automatic Flyway application is disabled, and application schedulers are enabled only against staging.

### CI and controlled deployment evidence

- Infrastructure main is 65aa5ad2cd6f1c3dcb72cf2bab6b883a62f66f1a. Infrastructure CI [run 42459234210](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/42459234210) passed.
- RomanLMS main is df7c3bf12d6bd09fd5ed08626bacfad308bb4e21. Exact-commit CI [run 42452119200](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42452119200) passed.
- Private deploy [run 42459327787](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42459327787) succeeded.
- Backend and frontend unhealthy candidate rejection [run 42459983275](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42459983275) and [run 42460323434](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460323434) succeeded as negative tests; the active release and route were retained.
- Coordinated rollback [run 42460564776](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460564776) and reactivation of the corrected blue release [run 42460785540](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42460785540) both succeeded. The final active release is the blue release listed above.
- A synthetic live acceptance run verified three synthetic group members, persisted group messaging, student SSE delivery, private PDF preview, group archival, and revocation of three synthetic sessions. It used only the disposable staging database and storage.

### Monitoring and alert delivery

The monitoring collector and Healthchecks heartbeat timers are active. The staging health and metrics endpoints are healthy. The Telegram alert chat showed the successful private deployment notification and firing notices for both expected unhealthy-candidate runs. The latest rollback-specific Telegram notices were not visible in the inspected chat view, so their individual delivery is not claimed as verified. The Grafana and Healthchecks firing/recovery paths had also been confirmed in earlier controlled tests.

### Safety, CI triggers, and preserved work

- Pushes to either repository's main branch trigger CI only. The RomanLMS private deployment remains owner-dispatched, actor/repository/main/CI gated, and pinned to the reviewed reusable workflow. No pending GitHub Actions jobs remain from the listed runs.
- Production PostgreSQL was not modified. The November 6 source dump hash remains b9a1d013332c7692d11b84c2503e51195b8e6ec36e71a5fa223640fa9a617890. Both the preserved romanlms_phase6 baseline and the disposable romanlms_phase6_test database report schema 132 and 135 successful Flyway history records. No Phase 6 migration was applied.
- Existing administrative SSH and Tailscale access were preserved. The private application listener has no public bind. No production route or Cloudflare/DNS changes were made.
- Seven untracked Phase 5 log files remain in the RomanLMS worktree and were not staged or included in evidence. One contains credential-like fixture material and must remain private; do not package or publish any of these logs.
- main remains unprotected as requested. No paid service or additional runner was added.

### Remaining production prerequisites

Private integration does not certify production cutover. The recovered November 6 database may omit later writes and original object-storage contents were not restored. Before a separately authorized cutover, identify the current production source of truth and later writes; take and verify a fresh consistent backup; validate production roles, grants, extensions, and object storage; review migration compatibility and recovery policy; freeze approved image digests and secrets; prepare a maintenance window and public routing plan; and obtain separate approval for any production database or public traffic change.

Account activation/reset email is unverified because outbound mail is disabled. Android device/emulator behavior and peak-load capacity remain unverified. Chat real-time delivery is authenticated SSE; no WebSocket endpoint was identified or tested. The server's 7.1 GiB usable RAM had about 4.0 GiB available in the final observation, but this is a point-in-time sample rather than a load certification. See the [Phase 6 evidence report](evidence/phase6-romanlms-production-integration.md) and [handoff](phase6-handoff.md).

## Phase 5 smoke deployment

Phase 5 remains functional and isolated on 127.0.0.1:18080. The restricted deployment controller, roman-deploy account, pinned caller, SSH host verification, monitoring, and rollback support remain in place. Smoke deployment and rollback were previously exercised through ROMAN-LAPTOP. RomanLMS uses its separate private staging policy and listener.
