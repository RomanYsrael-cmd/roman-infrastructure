# Phase 5 implementation status

Phase 5 infrastructure is installed and its local gates are ready. Phase 5 remains incomplete because the required RomanLMS CI run failed, so no smoke image has been published or deployed.

## Verified infrastructure state

- `RomanYsrael-cmd/roman-infrastructure` remains public, and `main` remains unprotected as requested.
- Infrastructure CI run [42326211900](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/42326211900) passed for commit `6bfcc4e0d59bac4f5c6aaa98f91400ef5d50e8a5`.
- The existing `ROMAN-LAPTOP` runner remains online and scoped to private `RomanYsrael-cmd/roman-lms`; no runner was added to this repository.
- A dedicated Ed25519 deployment key was generated. The server host key was verified as `SHA256:Ou1kYNVRN74lXu5SSxa5mVop82fvxgK8P0zD+ymkIh4`, and both repository secret names are present without retrieving their values.
- Ansible installed the restricted `roman-deploy` account, root-owned controller and SSH launcher, exact sudo rule, and a loopback-only Nginx listener at `127.0.0.1:18080`. The route returns 503 until a smoke release is deployed.
- The deployment user cannot run arbitrary SSH commands; the pre-sudo launcher rejects them. The allowlist contains only infrastructure workflow SHA `ed7f0067b23bb5b1e0126c8c783ee63066d6e025`.
- Grafana’s idempotent provisioner verified the deployment success and failure alert rules, the existing Telegram contact point and warning route, and the Prometheus data source (`changed=false`).
- The fixed unhealthy smoke mode and its `TEST UNHEALTHY SMOKE` workflow option are in the reviewed infrastructure commit.

## RomanLMS release gate

- Run [42324069539](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42324069539) completed with failure. Frontend, backend unit, deployment contract, workflow security, dependency, shell, Compose, and documentation checks passed.
- Backend PostgreSQL/Testcontainers reported 137 failures and 2 errors across 368 tests. The logs include a missing `notification_delivery_jobs` table and `MigrationVerificationIT` finding version 108 where version 132 is expected.
- Android Gradle failed immediately because the persistent Gradle wrapper cache lock file could not be opened (`Access is denied`).
- The live RomanLMS caller is still pinned to `2981677b61d3b80e0d87f7767f401f1509a53c82`; the server now allows only `ed7f0067b23bb5b1e0126c8c783ee63066d6e025`. The caller update and unhealthy-test job must be committed and pass CI before dispatch.
- Keep deployment dispatch closed until the updated RomanLMS caller passes all required checks. No GHCR image, active release, smoke-route response, deployment audit entry, or deployment alert has been produced yet.

## Phase 6 boundary

RomanLMS production database restoration and production traffic cutover remain out of scope. Before Phase 6, resolve the CI failures, validate production backups separately, define RomanLMS-specific allowlist and resource/readiness/migration gates, and confirm the server can safely run two Java releases or document an approved maintenance-window strategy.
