# Security model

## Runner isolation

The self-hosted runner remains scoped to RomanLMS. It is not added to roman-infrastructure or to an organization-wide runner group. The reusable workflow has a caller-repository check so other repositories cannot use its deployment job.

Untrusted external pull requests must not run on the laptop runner. The current RomanLMS workflow has an owner and same-repository pull-request gate before checkout. The new deployment example is manual-dispatch-only and rejects non-main refs before building or using secrets.

## Credentials

Never commit private keys, GitHub tokens, database passwords, package tokens, or app secrets. The deployment private key belongs in the RomanLMS repository secret ROMAN_PLATFORM_SSH_PRIVATE_KEY. The matching public key is installed by the server bootstrap playbook with a forced command and an exact source IP restriction for the verified RomanLMS runner (`100.104.209.82` at audit time). A separately verified known-hosts line belongs in ROMAN_PLATFORM_SSH_KNOWN_HOSTS.

The workflow token is limited to reading source and Actions run metadata plus writing the RomanLMS package. It is passed to the controller over SSH only for GitHub verification and GHCR pull, never placed in the release record or audit file.

The server deployment user is not in the docker group and has no interactive shell path through the authorized key. The key invokes only /opt/roman-platform/bin/roman-deploy --ssh. Sudoers grants exactly that command. The controller and policy remain root-owned.

## Image and rollout checks

The server requires a full commit SHA and a sha256 image digest under the exact configured GHCR namespace. It pulls by digest, checks OCI source and revision labels, and requires the commit to be the current main head at both preflight and route-switch time.

The controller checks the GitHub run ID, event type, branch, SHA, actor, caller workflow path, and pinned reusable workflow SHA. The run must still be in progress when the deployment request arrives.

Candidate containers have no published public interface, no Linux capabilities, a read-only root filesystem, bounded memory/CPU/PIDs, and no-new-privileges. Only the route include is atomically changed. The public gateway and Cloudflare Tunnel are not changed.

## Main branch and manual release authorization

Main remains unprotected as requested. The deploy workflow is manually dispatched and restricted to the repository owner account `RomanYsrael-cmd`, with an explicit confirmation choice. The controller enforces the same actor allowlist and checks the current main SHA, caller workflow, immutable reusable-workflow SHA, image provenance, and forced-command SSH request. Since main is unprotected, a person with write access can still push directly to it; repository writers remain trusted for source changes.

RomanLMS is private. This design does not rely on GitHub deployment-environment reviewers, which may be plan-dependent for private repositories. The manual workflow dispatch, repository-scoped secrets, workflow-origin validation, and server allowlist keep the setup on the free plan.
