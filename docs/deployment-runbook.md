# Deployment runbook

## Current state

The controller and workflow files are staged in this repository. The production host has not been changed. Do not dispatch the example workflow until the Ansible bootstrap has been applied by an authenticated administrator, the dedicated public key is installed, the two RomanLMS secrets exist, and the Docker Linux engine is online on the laptop runner.

## Server bootstrap after admin access is restored

1. Verify an independent administrative SSH session to 100.67.102.11. The current laptop identity is rejected, so this step has not passed yet.
2. Create a dedicated Ed25519 deployment key pair. Keep its private half out of Git and pass only its public half to the Ansible playbook.
3. Confirm /srv/apps is mounted, Docker's data root is physically below /srv/apps, Docker and Nginx are healthy, and port 18080 is unused.
4. Run deploy/ansible/install.yml with become from a trusted admin environment. Review the diff first. The playbook adds one account, one sudo rule, a forced authorized key, and an isolated Nginx listener on loopback. It does not change the existing public route, Cloudflare, Tailscale, firewall, PostgreSQL, or monitoring setup.
5. Verify Nginx syntax and the initial local 503 route; verify the admin SSH session again.
6. After the reusable workflow commit is pinned in the caller example, populate allowed_platform_shas in the server policy with that exact commit, then install the same policy.
7. Add the private key and a host-key-verified known_hosts entry to RomanLMS repository secrets. Do not obtain the server key by blindly trusting an unverified public network scan.
8. Copy examples/romanlms/roman-platform-smoke.yml to RomanLMS at .github/workflows/roman-platform-smoke.yml. Keep the full commit SHA pin. No branch protection is required.

## Manual smoke deployment

1. Ensure the existing ROMAN-LAPTOP runner is online and Docker Desktop is running its Linux engine.
2. Open RomanLMS Actions, choose Roman platform smoke deployment, select main, and choose DEPLOY SMOKE.
3. The job validates the runner, Tailscale peer, private SSH port, image health, digest, OCI labels, and server response. A missing secret or failed gate stops before a server change.
4. After success, verify the controller's active state and audit record on /srv/apps/roman-platform, check the Prometheus textfile metric, and request the private local route on roman-server at 127.0.0.1:18080/health.
5. Confirm the route is still inaccessible from the public internet and that no Cloudflare route was added.

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
