# Phase 5 implementation status

Phase 5 is in progress. The infrastructure controller and deployment alerts are installed, but no GHCR image has been published or deployed yet.

## Verified infrastructure state

- `RomanYsrael-cmd/roman-infrastructure` remains public, and `main` remains unprotected as requested.
- Infrastructure CI run [42322055727](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/42322055727) passed for commit `671e8ab` before the pre-sudo SSH gate change now being prepared.
- The existing `ROMAN-LAPTOP` runner remains scoped to private `RomanYsrael-cmd/roman-lms`; no runner was added to this repository.
- The runner has Docker Desktop's Linux engine and Tailscale available. The server is reachable over its verified Tailscale address.
- The dedicated deployment key was generated, the server's Ed25519 host key was verified against the known local trust record, and the two repository secrets are configured by name.
- The Ansible bootstrap installed the `roman-deploy` account, a root-owned controller and application policy, one exact sudo permission, and a loopback-only Nginx listener at `127.0.0.1:18080`. The initial route returns 503 until a smoke app is deployed.
- The bootstrap provisioned the success and failure deployment alert rules under the existing Grafana folder and Telegram notification route. Existing contact points and notification policies were left in place.
- The forced SSH key was tested. That test exposed a command-line rejection check placed after sudo, which could wait for request input when given an SSH remote command. A root-owned pre-sudo gate is now being added and must be installed and integration-tested before any deployment request is allowed.

## Current release gates

- RomanLMS CI run [42317669874](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42317669874) failed its required-check aggregation. The backend PostgreSQL/Testcontainers and Android checks reported failures; deployment contract and security checks passed. No smoke workflow may be dispatched until the repository's checks pass.
- RomanLMS's current deployment caller is pinned to `06ceaeb...`, but the server policy allowlist contains `2981677...`. Update the caller to the approved full SHA before dispatching.
- The latest Ansible change must be applied to install the new root-owned pre-sudo launcher. Then verify remote commands fail immediately, an unapproved app request is rejected, and the approved request interface remains available.

## Not yet verified

- The GHCR smoke image has not been published and there is no server deployment digest.
- The private Nginx route has not yet served the smoke app.
- No live unhealthy-candidate rejection or successful deployment rollback has been exercised.
- Deployment metrics and Telegram notices have not been produced by a real deployment operation. Earlier synthetic Telegram delivery checks were confirmed by the user, but they are not evidence of deployment-alert delivery.

Phase 5 remains incomplete until the pinned RomanLMS workflow passes, the live smoke deployment and failure/rollback tests succeed, and deployment monitoring is verified.
