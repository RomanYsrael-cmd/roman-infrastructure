# Security model

## Application registration and trust boundaries

`platform/applications.json` is the root-owned registry. A registration binds an application ID to its repository, release branch, caller workflow, reusable infrastructure workflow, authorized actors, required CI evidence, build definition, image namespace, deployment profile, resource limits, service ports, routes, health checks, secret references, storage and recovery policy. The controller validates the complete registry before an operation. Repository presence alone never grants deployment access.

The registry and controller are installed root-owned on `roman-server`. The deployment account has no interactive shell or Docker-group access. Its SSH key invokes only the restricted `roman-deploy-ssh` forced command; sudo permits only the root-owned controller entry point. The request parser allows only operation-specific fields and rejects arbitrary commands, environment values, mounts, images, and paths.

## Build and release credentials

Build jobs run on GitHub-hosted runners against an exact source commit and a pinned infrastructure revision. The root registry fixes approved build presets, contexts, Dockerfiles, package managers, and build arguments. Container builds publish immutable image digests. Deployment authorization checks the caller repository and workflow, event, branch, actor, exact source revision, required CI result, reusable workflow path and pinned infrastructure SHA. The server rechecks current branch and workflow evidence before switching traffic.

After the build job publishes immutable images, the pinned reusable workflow's restricted submit job requests a short-lived OIDC token. Its audience binds the token to the canonical image/build/source digest, and the GitHub claims identify the authorized caller run and reusable workflow SHA. The server verifies the issuer, audience, claims and registered build specification before accepting the image. The token is not written to deployment history. GHCR credentials are limited to the registered package permissions and are redacted from logs.

Build-only projects use `build-application.yml`. That workflow receives no server SSH secret, production credential, OIDC permission, or package-write permission. It builds only a registered build-only application and uploads a source-bound artifact with a manifest and digest. It cannot call the server deployment interface.

The self-hosted Windows runner is used only for the restricted SSH submission stage when a deployment profile requires the existing private network path. SSH private-key and known-host material are repository secrets, are written only to the D-based temporary directory for the job, and are deleted at job completion. Host-key checking remains strict. Never print, commit, or copy secret values into a release manifest or audit record.

## Runtime isolation and secrets

Each application has an isolated network, storage root, state and audit files, resource limits, health policy, and root-managed environment/secret references. The registry rejects overlapping ports, listener ports, domains, routes, subnets, networks, or storage paths. Service and database credentials are app-scoped. Secret files are root-owned with restrictive permissions and may be mounted read-only only at registry-approved container paths.

Candidate containers run without public port publishing unless a profile explicitly declares a safe listener, with no Linux capabilities, read-only root filesystems where supported, no-new-privileges, bounded memory/CPU/PIDs, and only declared persistent mounts. Nginx routes are generated from registered configuration and atomically activated after health checks. Existing Cloudflare and public routing configuration is preserved unless a separately reviewed registered route requires an additive change.

## Database and recovery controls

Database changes require a verified immutable release artifact and a dynamically derived migration plan bound to its digest. The controller validates applied history, pending migration ordering and checksums, database identity, and the plan again immediately before execution. It takes no full backup when there are no pending migrations. When migrations are pending, the registered database backup adapter must produce and verify a recoverable backup before execution. Migration credentials are separate from normal runtime credentials. Failures are forward-only and fail closed: traffic stays on a release compatible with the observed schema, and SQL is never blindly rolled back.

The audit log records application/release identity, workflow evidence, image digest, migration plan fingerprint, backup identity, execution result, health checks and route outcome. Secrets and bearer tokens are excluded and redacted. Recovery procedures use a compatible application release or a deliberate database restore plan; application rollback never implies database rollback.

## Main and RomanLMS authorization

GitHub `main` remains unprotected. The system continues to support the registered direct-main and pull-request CI authorization policies; RomanLMS retains its SHORT and LONG evidence paths. Main branch write access remains trusted to change source. Deployment authorization independently verifies the triggering run and exact commit, so a stale run or an unrelated workflow cannot authorize a release. No branch protection or organization-wide runner access is required.

Existing RomanLMS registrations retain their explicit release workflows, actors, database, backup, and production strategy. A new repository receives no production access until an owner adds and reviews a complete registry entry and its caller workflow. Never grant arbitrary repository workflows broad production secrets or unrestricted host commands.
