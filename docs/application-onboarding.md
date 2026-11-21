# Application onboarding

Add each server application to `platform/applications.json` with a unique app ID. The registry is the deployment controller's root-owned policy. It is also the single source for the reusable workflow, image namespace, resource budget, network, route include, secret references, database adapter, backup method, health checks, and rollback strategy.

## 1. Assess the repository

Record the repository's default and release branch, runtime, build tool, test command, required image or artifact outputs, ports and protocols, health behavior, database and migration framework, persistent data, external integrations, domain/path routes, and safe rollback behavior. Decide if the repository should build a server image or publish a build-only artifact. Libraries, desktop applications, and Android projects must use `build-only` and have no host deployment entry.

For stateful workloads, determine what a consistent backup requires and test restoring it in isolation. For database migrations, test old and new app revisions against the expanded schema. Do not treat a filename or a developer assertion as proof of backward compatibility.

## 2. Choose a profile

Use a profile whose runtime adapter matches the process:

- `static-web` or `backend-api` for one HTTP image behind an app-specific Nginx include.
- `full-stack` or `multi-container` for a coordinated named service set with a private app network and one route switch.
- `worker` for a continuous non-HTTP process with a Docker health check.
- `scheduled-job` for one bounded invocation driven by a UTC systemd calendar.
- `stateful-service` for a persistent TCP service that needs a volume backup policy.
- `game-server` for persistent game state and explicit TCP/UDP host bindings.
- `build-only` for libraries, Android/desktop builds, and other artifacts that are not hosted on this server.

The installed database adapter supports PostgreSQL/Flyway only. Do not register MySQL, Liquibase, MongoDB, or another database as if it were supported.

## Candidate repository assessment

These repositories were inspected as possible integrations. The review does not authorize replacing their current deployments.

| Repository | Observed stack and operations need | Initial fit |
| --- | --- | --- |
| `roman-lms` | Java/Spring backend and Maven, TypeScript frontend, Kotlin/Gradle Android client, PostgreSQL; production and private staging already have separate policies | Existing full-stack registration; retain the dedicated SHORT/LONG CI and Flyway controls |
| `schedulease-server` | Java 17 Spring Boot and packaged frontend, PostgreSQL/Flyway, server-sent events and OptaPlanner | Dedicated staging and production full-stack registrations. See the [SchedulEase runbook](schedulease-deployment.md) for the cutover gates. |
| `roman-search` | Python API with SearXNG and PostgreSQL/pgvector retrieval in Docker Compose; persistent indexes and external search/provider integrations | Multi-container candidate; define index backup/rebuild and provider-secret references first |
| `battle-of-bluffs` | Java 17/Spring backend, React/TypeScript frontend, PostgreSQL; STOMP live updates and LiveKit client; persistent matches but an in-memory single-instance ranked queue | Full-stack candidate with single-instance rollout; determine media server/UDP requirements before assigning public routes |
| `mailforge` | Stalwart mail storage, Postfix SMTP edge, HAProxy TCP proxy and WireGuard; public SMTP and domain/DKIM delivery concerns | Stateful network service; needs verified mail ports, public IP reputation, queues, certificates, and mail-store recovery before registration |
| `minecraft-server` | Bedrock server properties and world assets are present; the repository also contains JavaScript and `mcfunction` assets | Game-server candidate; confirm the actual Bedrock image/build source, UDP listener, world persistence, and restore process before deployment |

Do not infer deployment readiness from language counts or a repository's presence. Re-check the current production state, runtime configuration, data ownership, and backup recovery for each project when it is actually onboarded.

## 3. Add the registration

Copy a matching example from [application-profiles.json](../examples/application-profiles.json) into `platform/applications.json`, then replace every `sample-*` value. A deployed application registration includes:

- Repository, production branch, CI workflow file, caller workflow path, owner actor, and the pinned reusable workflow path/SHA.
- Explicit confirmation phrases for each enabled operation, plus a separate automatic-release phrase if automatic release is enabled.
- A root-owned Docker build specification whose components match the profile. The caller cannot change its build paths, arguments, base runtime, or image namespace in a release request.
- Profile strategy, CPU/memory/process limits, ports, health path/text, bounded readiness timeout, domain/path routes, and route include file. HTTP/stack routes can declare a root-owned loopback Nginx site; Ansible generates it from the registry and refuses to overwrite an unmanaged site file. DNS and Cloudflare changes remain separate owner-reviewed configuration.
- Private per-app Docker network/subnet where needed, a per-app `/srv/apps/<app-id>` storage root for persistent services, and explicit TCP/UDP binds for a game server.
- App-scoped root-owned secret references under `/etc/roman-platform/<app-id>/`. Put only filenames and non-secret names in the registry; never add secret values.
- Database engine, migration adapter, backup adapter, dedicated migration login and execution role, pending batch limit, and writer usernames when database migrations are enabled.
- A profile-compatible rollout and application rollback strategy. Database rollback remains forward-only.

The `allowed_platform_shas` list must contain the exact 40-character commit SHA referenced by the caller's reusable workflow. Add that SHA to the root registry in a later infrastructure commit after the reusable workflow commit exists. In the caller, set both `uses:` and `platform_ref` to that same allowed workflow SHA. At job start, the reusable workflow resolves root `main` once, records that immutable registry commit, and uses the same registry snapshot for authorization and image builds. This avoids a self-referential registry pin while keeping the workflow code pinned.

For `build-only`, declare one supported preset (`gradle-android`, `gradle-jvm`, `maven-jvm`, `npm`, or `python`), the project directory and safe artifact globs. The preset runs its fixed tests/build steps; arbitrary shell command strings are rejected. Include the CI workflow, authorized actor, reusable `build-application.yml` path, pinned platform SHA, and `build-artifact` confirmation. No deployment secret or server configuration is accepted for this profile.

## 4. Add a caller workflow

For a container deployment, call `RomanYsrael-cmd/roman-infrastructure/.github/workflows/deploy-application.yml@<workflow-SHA>` and set `platform_ref` to the same workflow SHA. Set `app_id`, `operation`, and `confirmation` from the registered policy. Pass the server SSH key and known-hosts values only as the two repository secrets required by the reusable workflow. Keep `contents`, `actions`, `pull-requests`, `packages`, and `id-token` permissions at the minimum needed by the selected job. Do not expose server secrets to build jobs or pull-request code.

For a build-only release, call `build-application.yml` from an owner-controlled caller workflow on the configured branch. For automatic releases, invoke it only from a successful exact-source CI `workflow_run`, pass the triggering run ID/attempt and set the root registry's automatic-release policy. The reusable workflow verifies the current run, source branch and SHA, exact CI success, actor, and pinned workflow before it executes repository build code. The build job receives no server secrets or GitHub API token.

## 5. Validate and stage

Run the complete Infrastructure suite and the candidate project's required CI. Infrastructure CI validates both the installed registry and all profile examples, then tests workflow authorization, collision/resource checks, and adapter behavior. Confirm the registry's ports, routes, subnets, storage, memory and CPU limits against the live server inventory. Install no new public route or DNS record until the candidate has passed an isolated deployment and health check.

Create each secret file on the server through the approved administrative process, owned by root with mode `0600`, and verify the exact database URL, login, app network, storage and required mounts. Never upload secrets through the app registry or deployment request.

After review, merge the registry and caller changes, install the corresponding root-owned registry and controller revision, and verify the installed SHA before dispatching the first deployment. The main branch stays unprotected; CI authorization and the server-side allowlist remain the release controls.

## Example first release

1. Merge the project CI and caller workflow after tests pass.
2. Add its app record and reusable-workflow commit SHA to the root registry.
3. Confirm the server's controller and registry revisions are compatible with that workflow.
4. Dispatch the owner-authorized deployment with the exact registered phrase.
5. Check candidate health, route state, audit event, per-app metrics, and any required backup.
6. Use the registered rollback operation only if the retained application's schema/data compatibility checks pass.

See [the universal deployment runbook](universal-deployment-runbook.md) for health, monitoring, failure and rollback steps.
