# RomanLMS Phase 6 handoff

Phase 5 completed an isolated smoke deployment only. The restricted controller is installed on `roman-server`; the active smoke release is the retained blue release, and `127.0.0.1:18080/health` returns HTTP 200. No RomanLMS production database or public traffic was changed.

The RomanLMS caller is at `cfa0d607ef9f35643149fb3a4b71b508a0446299`, with required CI run [42347455950](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42347455950) green. It calls reusable workflow SHA `d95b276cf3d257a685fe7bb7464197c44f4b4ea8`; the server allowlist is restricted to that SHA. The successful smoke deploy, unhealthy rejection, and rollback are recorded in [status.md](status.md).

Before a RomanLMS production release:

- Restore and validate production database backups separately from the smoke app.
- Define a RomanLMS-specific server allowlist, resource limits, health/readiness checks, migration policy, backup gate, retention policy, and rollback strategy.
- Confirm the server can run two actual RomanLMS releases at once. If not, document an approved maintenance-window recreate strategy.
- Validate persistence, migrations, backups, and backup recovery with non-production data.
- Preserve the laptop runner boundary, full reusable-workflow pin, actor/repository/ref checks, deployment secret restrictions, and server-side verification.
- Verify the release and rollback workflow against the RomanLMS application before changing production traffic.
- Treat database restoration, public Cloudflare routes, DNS, and production cutover as separately authorized Phase 6 work.

Keep `main` unprotected as requested for the zero-cost setup. Continue using the existing `ROMAN-LAPTOP` runner; do not add a production runner.
