# RomanLMS Phase 6 handoff

RomanLMS production restoration and cutover remain out of scope for this deployment phase.

The current private RomanLMS repository has one active workflow, RomanLMS CI, on main. It uses the existing self-hosted Windows runner, a same-repository owner-authored pull request gate, read-only contents permissions, pinned language setup actions, and deployment contract tests. It has no deployment workflow. The latest recorded run failed in its required-check aggregation; see the Actions run and logs before relying on it as a release signal.

Before a RomanLMS production release:

- Re-enable and verify the existing laptop runner; do not move or replace it.
- Restore the Docker Desktop Linux engine and verify its storage reserve.
- Confirm the current CI aggregate failure and any skipped Docker/Testcontainers checks.
- Restore and validate production database backups separately; this controller does not touch the RomanLMS database.
- Add the RomanLMS-specific server allowlist, resource policy, health/readiness tests, and migration/backup gates.
- Verify the deployment SSH account and GHCR package permissions without exposing secrets.
- Decide whether the current 7.1 GiB server can safely run two Java releases concurrently. Otherwise use a documented maintenance-window recreate strategy.
- Preserve RomanLMS's existing migration, backup, smoke-check, release-health, Android build, retention, and rollback protections.
- Add and test a RomanLMS workflow that cannot execute deployment secrets on pull requests.
- Complete an isolated successful deployment, failure test, rollback test, persistence check, and monitoring scrape before public traffic or database restoration.

Do not restore the production database or route RomanLMS traffic as part of the smoke application test.
