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
- The forced SSH key was tested. A root-owned pre-sudo gate now rejects remote commands in under a second before sudo; the normal JSON request channel remains available for policy validation.
- A fixed unhealthy smoke image mode is implemented for the explicit `TEST UNHEALTHY SMOKE` manual choice. It returns HTTP 503, and the reusable workflow treats only the server's readiness rejection as a passing failure test.

## Current release gates

- RomanLMS CI run [42317669874](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42317669874) failed its required-check aggregation. Backend PostgreSQL/Testcontainers and Android checks reported failures; deployment contract and security checks passed. Run [42324069539](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42324069539) is testing the caller pin update. Do not dispatch until current RomanLMS CI passes.
- The current RomanLMS caller is pinned to `2981677b61d3b80e0d87f7767f401f1509a53c82`, which the installed server policy allows. The new unhealthy-candidate workflow is pinned to `ed7f0067b23bb5b1e0126c8c783ee63066d6e025`; the matching policy and example caller update are prepared locally, but the server policy must be refreshed before using that workflow. The live RomanLMS caller remains at the old allowed pin until the current CI gate is cleared.
- The Ansible refresh installed the launcher and controller. A forced `id` command returned exit 2 immediately, an unallowlisted app request returned the expected rejection, the private listener still returned 503, and the sudo rule parsed successfully. The independent administrative SSH session remains active.

## Not yet verified

- The GHCR smoke image has not been published and there is no server deployment digest.
- The private Nginx route has not yet served the smoke app.
- No live unhealthy-candidate rejection or successful deployment rollback has been exercised.
- Deployment metrics and Telegram notices have not been produced by a real deployment operation. Earlier synthetic Telegram delivery checks were confirmed by the user, but they are not evidence of deployment-alert delivery.

Phase 5 remains incomplete until the pinned RomanLMS workflow passes, the live smoke deployment and failure/rollback tests succeed, and deployment monitoring is verified.
