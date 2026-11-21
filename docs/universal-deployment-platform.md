# Universal deployment platform

Status: **implementation is under review in Roman Infrastructure PR #26 and the coordinated RomanLMS branch.** The local code now contains the version-agnostic migration path, registered application profiles, reusable deployment workflow, OIDC-bound image provenance, and a build-only artifact workflow. These changes are not production-installed until the PRs pass CI and the matching controller and RomanLMS migrator revisions are installed together.

The main branch remains unprotected. Existing direct pushes to `main`, RomanLMS SHORT CI authorization, and RomanLMS pull-request LONG CI authorization remain supported.

## Architecture

`platform/applications.json` is the root-owned registry used by CI, the release workflow, and the server controller. Each registered application has a unique ID and GitHub repository, production branch, workflow identity, approved reusable-workflow revision, CI policy, confirmation phrases, resource budget, network and port policy, health checks, route include, secret references, persistence policy, and rollout strategy. The registry validator rejects malformed registrations, duplicate host ports, overlapping route domains or paths, overlapping Docker subnets, shared application networks, unsafe mounts, resource overcommit, and unsupported adapters.

The controller accepts a bounded JSON request through the forced-command SSH interface. It does not accept caller-provided shell commands, Compose files, arbitrary Docker flags, or host paths. Root-owned registry policy selects the deployment adapter. Deployment histories, release state, metrics, and storage are scoped by application ID.

The reusable container workflow checks the exact caller repository, branch, source commit, CI run, actor, current workflow run, and full reusable-workflow SHA. It checks out the helper code at that authorized workflow SHA and the application registry from a separately pinned registry snapshot. This separates the workflow version from the registry version and avoids a self-referential commit pin. GitHub Actions builds immutable GHCR images from the registered build specification. The server verifies image digests, labels, package/source identity, build specification hash, and a signed GitHub OIDC token whose audience binds the requested images and source commit. Package-write permission is limited to the build job; the server handoff receives package-read and short-lived GitHub API credentials only.

Build-only projects use `build-application.yml`. That workflow has no SSH key, production token, package-write permission, or server job. It runs one of the root-registered Gradle Android, Gradle JVM, Maven wrapper, npm lockfile, or Python package presets, runs that preset's tests, and uploads a source-bound archive with a SHA-256 manifest.

## Supported profiles

| Profile | Current controller behavior | Rollout and persistence |
| --- | --- | --- |
| `static-web` | Single HTTP container behind an app-specific Nginx include | Blue/green, readiness-checked route switch |
| `backend-api` | Single HTTP API with bounded health checks and resource limits | Blue/green, rolling, or recreate according to registered policy |
| `full-stack` | Named components, isolated network, per-component readiness, coordinated route file | Blue/green or maintenance; components switch together |
| `multi-container` | Named service set with explicit ports, health paths, and resource budget | Blue/green, rolling, recreate, or maintenance |
| `worker` | Private, non-root container with Docker health; duplicate processing must be declared safe for rolling | Rolling or recreate |
| `scheduled-job` | Non-root image invoked by a root-managed systemd timer using a validated UTC calendar | Scheduled image replacement; each run is bounded and isolated |
| `stateful-service` | Non-root container, app-owned persistent directory, isolated network and verified volume archive | Recreate or maintenance, with application-specific backup/restore policy |
| `game-server` | Stateful service with explicit TCP/UDP bindings, persistent data, health check, and resource caps | Recreate or maintenance; never assumes HTTP routing |
| `build-only` | Build and test artifact; no server deployment operation is authorized | Immutable Actions artifact plus checksums; signing remains a project-specific release step |

The installed database adapter currently supports PostgreSQL with Flyway and either verified `pgBackRest` or verified dump backups. Other engines and migration tools are not enabled yet; adding one requires an adapter implementation and integration tests, not a per-version controller exception.

Examples for all profiles are in [application-profiles.json](../examples/application-profiles.json). The checked-in applications registry contains the current smoke and RomanLMS registrations; candidate repositories are not automatically deployed when added to GitHub.

## Migration lifecycle

For a database-backed release, the controller validates the exact immutable migrator image, obtains Flyway `info` and `validate` output, and constructs a plan from the observed schema version, applied history, pending migration filenames, and checksums. It rejects failed or unknown history, modified applied migrations, duplicate or out-of-order versions, unrelated pending changes, and a plan whose database/schema identity does not match the registered application. The target is the last version in that release's validated pending list; no V133 target is encoded in controller logic.

The plan fingerprint binds the source version, target version, migration names/checksums, Flyway version, database and schema identity, image digest, and source commit. A no-migration plan skips backup, writer quiescence, and migration execution. When migrations are pending, the registered backup adapter must produce and verify a fresh recoverable backup before the controller quiesces configured writers, re-reads the plan, and compares the fingerprint. The controller then invokes the migrator image using dedicated migration credentials and the registered execution role. Postflight must show the exact target and planned checksums before a candidate application may become active.

Migration execution is forward-only. The controller never runs `repair`, changes Flyway history, or blindly rolls back SQL. A known unchanged source plan can restore the old app; a verified new target can continue only when the candidate passes health. An unknown migration outcome leaves the affected writer stopped and traffic on the last verified route until an operator follows the backup recovery procedure. App rollback is allowed only when the retained app's recorded schema matches the live observed schema.

`max_pending` is an application policy between 1 and 100 (RomanLMS currently sets 32). It bounds an operational batch; it does not require one migration per release. Clean application releases and databases with no pending migrations do not incur a full database backup.

See [the migration lifecycle and recovery guide](database-migration-lifecycle.md) for the preflight, backup, audit, failure and restoration details.

## CI and release authorization

`deploy-application.yml` is the common container deployment entry point. Each caller pins the reusable workflow commit in `uses:` and passes that same SHA as `platform_ref`; the root-owned `allowed_platform_shas` list must include it. At authorization start, the workflow resolves root `main` once and records that exact registry commit. Authorization and image building both use that recorded registry snapshot. A registration selects automatic release behavior explicitly; otherwise the owner dispatches a workflow with a root-registered confirmation phrase. Pull requests do not receive deployment secrets or a server release path.

RomanLMS keeps the existing release gate: direct owner pushes to `main` use exact-commit SHORT CI, while a merged pull request requires the successful LONG CI evidence for the merged tree. Automatic production release also requires the root and GitHub freeze switches, exact successful SHORT run, owner identity, and a positive production-change classification. Manual verify, deploy, rollback, and runtime refresh remain explicit owner-dispatched operations. No branch protection is added.

New application builds execute on GitHub-hosted runners. Only the final restricted SSH handoff runs on the registered Windows laptop runner. The server remains the authority for application allowlisting, the current main commit, workflow SHA, image provenance, capacity, health, migration state, audit logging, and route switching.

## Onboarding

Use the steps in [Application onboarding](application-onboarding.md). A new project is a registry entry plus its own CI caller, secret files, ports, storage, routes, resource limits, and rollout decision. Normal app releases and new sequential SQL migrations do not require controller-source edits. A new runtime type, database engine, migration executor, or backup adapter does.

The registry is intentionally an allowlist. Candidate repositories such as `schedulease-server`, `roman-search`, `battle-of-bluffs`, `mailforge`, and `minecraft-server` require individual stack, data, network, and production-use assessment before registration. Their existence is not approval to replace an existing deployment.

## Monitoring and operations

The controller writes app-ID-labelled deployment metrics to the existing Node Exporter textfile collector, and writes bounded deployment events to each app's root-owned audit file. The existing Prometheus, Grafana, and Telegram alert path remains in use. Per-app health and release state are visible in the dashboard and in the `roman_platform_*` metrics. The monitoring guide is in [the deployment runbook](universal-deployment-runbook.md).

The repository still requires a staged rollout: validate source and CI, confirm a recoverable backup, verify that the controller and caller use compatible revisions, install the root-owned registry/controller, exercise the private smoke and RomanLMS gates, then perform only a non-destructive production release. The production controller and RomanLMS image must never be upgraded independently across an incompatible request or migrator contract.
