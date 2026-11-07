# RomanLMS Phase 6 handoff

RomanLMS production restoration and cutover remain out of scope for this deployment phase.

The current private RomanLMS repository runs RomanLMS CI on main using the existing self-hosted Windows runner, a same-repository owner-authored pull request gate, read-only contents permissions, pinned language setup actions, and deployment contract tests. The deployment caller is pinned to the server-approved SHA `2981677b61d3b80e0d87f7767f401f1509a53c82`. Latest completed run [42317669874](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42317669874) failed because backend PostgreSQL/Testcontainers and Android checks failed; deployment contract and workflow security checks passed. Run [42324069539](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42324069539) is checking the caller pin update. Keep the deploy gate closed until the checks pass.

Before a RomanLMS production release:

- Keep the existing laptop runner scoped to RomanLMS; do not move or replace it.
- Keep the Docker Desktop Linux engine available and verify its storage reserve.
- Resolve the current CI failures in backend PostgreSQL/Testcontainers and Android checks before deploying.
- Restore and validate production database backups separately; this controller does not touch the RomanLMS database.
- Add the RomanLMS-specific server allowlist, resource policy, health/readiness tests, and migration/backup gates.
- Verify the deployment SSH account and GHCR package permissions without exposing secrets.
- Decide whether the current 7.1 GiB server can safely run two Java releases concurrently. Otherwise use a documented maintenance-window recreate strategy.
- Preserve RomanLMS's existing migration, backup, smoke-check, release-health, Android build, retention, and rollback protections.
- Add and test a RomanLMS workflow that cannot execute deployment secrets on pull requests.
- Complete an isolated successful deployment, failure test, rollback test, persistence check, and monitoring scrape before public traffic or database restoration.

Do not restore the production database or route RomanLMS traffic as part of the smoke application test.
