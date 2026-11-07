# Deployment runbook

## Current state

The controller, restricted deployment account, SSH key, loopback route, and Grafana alert rules are installed on `roman-server`. The deployment route is private and returns 503 until an application is deployed. Do not dispatch the smoke workflow until the root-owned pre-sudo SSH gate is installed and verified, RomanLMS CI is green, and its caller is pinned to an SHA approved by the server policy.

## Refresh the server bootstrap

1. Verify the independent administrative SSH session to `100.67.102.11` remains available.
2. Keep the existing dedicated Ed25519 deployment key private and continue using only the verified host-key secret. Never copy the private key into the repository.
3. Re-run `deploy/ansible/install.yml` from the trusted WSL environment with the public deployment key. This installs the pre-sudo forced-command gate and refreshes the root-owned controller policy.
4. Verify Nginx syntax, the local 503 route, the exact sudo rule, and the forced-command rejection tests. Verify the admin SSH session again.
5. Update the RomanLMS caller to the approved full SHA in `platform/applications.json`. The current server allowlist approves `2981677b61d3b80e0d87f7767f401f1509a53c82`; retain that pin unless the server policy is reviewed and updated with another immutable SHA.
6. Confirm all existing RomanLMS CI checks pass before dispatching. The latest run failed in the backend integration and Android checks.
7. The two RomanLMS repository secrets already exist by the required names. Verify their presence without printing values.

## Manual smoke deployment

1. Ensure the existing ROMAN-LAPTOP runner is online and Docker Desktop is running its Linux engine.
2. Confirm RomanLMS CI has passed for the current main head and the deployment caller pins the exact server-allowlisted workflow SHA.
3. Open RomanLMS Actions, choose Roman platform smoke deployment, select main, and choose DEPLOY SMOKE.
4. The job validates the runner, Tailscale peer, private SSH port, image health, digest, OCI labels, and server response. A missing secret or failed gate stops before a server change.
5. After success, verify the controller's active state and audit record on `/srv/apps/roman-platform`, check the Prometheus textfile metric, and request the private local route on `roman-server` at `127.0.0.1:18080/health`.
6. Confirm the route remains inaccessible from the public internet and no Cloudflare route was added.

## Inspect and troubleshoot

- Workflow: RomanLMS Actions run summary and step logs.
- Controller output: the restricted SSH command returns a JSON outcome without secret values.
- Server audit: /srv/apps/roman-platform/logs/deployments.jsonl.
- Active and previous releases: /srv/apps/roman-platform/state/platform-smoke.json.
- Controller invocation log: SSH and sudo journal entries on roman-server.
- Container state: inspect only the controller-labeled platform-smoke containers.
- Nginx: nginx -t, systemctl status nginx, and the managed file under /etc/roman-platform/nginx.
- Storage: findmnt /srv/apps and docker info --format '{{.DockerRootDir}}'.

Never run a deploy request by pasting a token into a shell command. Never disable a mount, stop PostgreSQL, change Cloudflare routing, or reboot the server as a deployment test.
