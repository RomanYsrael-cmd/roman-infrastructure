# RomanLMS Phase 6 handoff

RomanLMS production restoration and cutover remain out of scope for Phase 5.

The restricted deployment controller is installed on `roman-server` for the harmless `platform-smoke` app. It accepts only reviewed infrastructure workflow SHA `ed7f0067b23bb5b1e0126c8c783ee63066d6e025`. The RomanLMS main-branch caller is still pinned to `2981677b61d3b80e0d87f7767f401f1509a53c82`; the deploy gate must remain closed until the caller is updated to the reviewed SHA, includes its unhealthy-candidate test, and its required CI passes.

RomanLMS CI run [42324069539](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42324069539) failed the required-check aggregate: backend PostgreSQL/Testcontainers had 137 failures and 2 errors, and Android Gradle could not open its persistent wrapper-cache lock. The deployment contract, workflow security, frontend, backend unit, shell, dependency, Compose, and documentation checks passed. The CI failure details are recorded in [status.md](status.md). Do not dispatch a deployment while this gate is failing.

Before a RomanLMS production release:

- Keep the existing laptop runner scoped to RomanLMS; do not move or replace it.
- Keep Docker Desktop's Linux engine available and maintain its storage reserve.
- Resolve the backend integration failures and Android Gradle cache permission issue, then require the full CI suite to pass.
- Restore and validate production database backups separately; the Phase 5 smoke controller does not touch the RomanLMS database.
- Add RomanLMS-specific server allowlist, resource policy, health/readiness tests, and migration/backup gates.
- Verify deployment SSH scope and GHCR package permissions without exposing secrets.
- Decide whether the server can safely run two Java releases concurrently. Otherwise, document and approve a maintenance-window recreate strategy.
- Preserve RomanLMS's migration, backup, smoke-check, release-health, Android build, retention, and rollback protections.
- Add and test a RomanLMS workflow that cannot execute deployment secrets on pull requests.
- Complete an isolated successful deployment, failure test, rollback test, persistence check, and monitoring scrape before public traffic or database restoration.

Do not restore the production database or change public DNS, Cloudflare, or production routes as part of the smoke application test.
