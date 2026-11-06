# Roman Infrastructure

This repository holds the shared deployment controller, the allowlist for approved applications, a harmless HTTP smoke service, and reusable workflow material for RomanLMS.

The intended release route is:

1. The infrastructure repository runs source-policy tests and builds the smoke container on GitHub's hosted Linux runner.
2. The repository owner manually starts the RomanLMS smoke workflow from main and selects the explicit deployment or rollback confirmation.
3. The existing Windows laptop runner checks the smoke image, publishes it to the RomanLMS GHCR namespace using the workflow token, and connects over the laptop's existing Tailscale connection.
4. A forced-command SSH key submits one JSON request to the root-owned deployment controller.
5. The controller verifies the workflow run, both main-branch revisions, the pinned reusable workflow, the allowlisted image digest, mounts, capacity, services, and health before changing its private Nginx route.

The workflow material is staged as an example for RomanLMS. The server installation, dedicated deployment key, private image pull, and production route have not been applied or verified because the current laptop SSH identity cannot authenticate to roman-server.

## Cost target and branch policy

The setup uses the public infrastructure repository, public-repository GitHub Actions runs, the existing self-hosted laptop runner, GHCR, and the existing Tailscale connection. It creates no paid compute service, hosted runner, second runner, external monitoring account, or public ingress route.

The main branch is intentionally left unprotected. No branch protection rule or ruleset is required. The deploy example is manually dispatched and restricted to the repository owner account, with an explicit confirmation choice; server-side actor and release allowlists and forced-command SSH provide additional checks. Since main is unprotected, a person with write access can still push directly to it.

The free-cost assumption depends on GitHub's current public-repository and container-registry policies and a personal, non-commercial Tailscale use case. See the cost notes in docs/cost-model.md.

## Contents

- deploy/controller: restricted Python deployment controller
- deploy/ansible: opt-in server bootstrap playbook; not yet run
- deploy/tests: validation, workflow-origin, storage, capacity, lock, and route rollback tests
- platform/applications.json: root-owned server allowlist source
- app/smoke: stateless health-check service used for deployment validation
- .github/workflows/ci.yml: public-repository test and local container smoke checks
- .github/workflows/romanlms-smoke-deploy.yml: reusable workflow for a manual RomanLMS deployment
- examples/romanlms/roman-platform-smoke.yml: caller workflow example, pinned after the reusable workflow commit

See docs/status.md for the verified state and remaining server-side steps.
