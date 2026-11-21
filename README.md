# Roman Infrastructure

Roman Infrastructure is a restricted deployment platform for independently registered applications. The server controller reads a root-owned app registry and supports HTTP applications, coordinated multi-container releases, workers, scheduled jobs, stateful TCP services, game servers with TCP/UDP ports, and build-only projects that publish artifacts without host access.

## Current implementation

Roman Infrastructure PR #26 merged to `main` on 2026-11-21 as `61d2305c0fe2458ee939303f509ee2c8d986fc0e`. Post-merge CI run [44854410980](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/44854410980) passed, including the disposable PostgreSQL migration integration checks. The merged implementation includes:

- A strict root-owned application registry with port, route, subnet, network and resource conflict checks.
- App-specific build definitions, immutable image digests, GitHub OIDC provenance and authorized reusable workflows.
- Profile-specific deployment, health, persistent-volume, backup, route-switch and rollback adapters.
- A version-agnostic PostgreSQL/Flyway migration planner and executor. It supports multiple ordered migrations, skips backups when there are none, verifies a fresh backup when needed, and does not roll SQL back.
- A separate build-only workflow for Android/JVM, Maven, npm and Python artifacts; it has no server secrets or production job.
- Per-app deployment audit and metrics through the existing Prometheus, Grafana and Telegram monitoring path.

The RomanLMS OIDC policy prerequisite merged as [PR #111](https://github.com/RomanYsrael-cmd/roman-lms/pull/111); the matching migrator and caller-workflow integration is under review in [PR #112](https://github.com/RomanYsrael-cmd/roman-lms/pull/112), with hosted LONG CI pending. The infrastructure implementation has not been verified on production; the last successful host check was 2026-11-20. A fresh SSH probe on 2026-11-21 timed out while Tailscale reported `roman-server` offline. The production freeze remains closed. Keep the controller, registry, caller workflow and migrator revisions coordinated; see [current platform status](docs/status.md) before any rollout.

See [the platform overview](docs/universal-deployment-platform.md), [onboarding guide](docs/application-onboarding.md), [migration lifecycle](docs/database-migration-lifecycle.md), and [operations runbook](docs/universal-deployment-runbook.md).

## Repository map

- `deploy/controller/`: restricted deployment controller, application registry, migration planner and OIDC verifier.
- `deploy/scripts/`: registered container builds, build-only artifact creation and release authorization.
- `deploy/ansible/`: opt-in installation and configuration of the root-owned controller, routes and monitoring.
- `platform/applications.json`: root-owned application registration source.
- `examples/application-profiles.json`: validated manifests for each supported workload profile.
- `.github/workflows/deploy-application.yml`: reusable registered container deployment workflow.
- `.github/workflows/build-application.yml`: reusable build-only artifact workflow.
- `deploy/tests/`: controller, migration, registry, authorization, resource, route and service-profile regressions.

RomanLMS keeps its existing direct-owner-main SHORT CI and PR/LONG CI authorization paths. Main remains unprotected. Registration and deployment remain explicit; GitHub repositories are never deployed automatically merely because they exist.
