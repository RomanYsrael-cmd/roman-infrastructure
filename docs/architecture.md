# Deployment architecture

## Release path

The central repository provides controller code and shared policy. RomanLMS keeps its language-specific builds and tests. Its existing self-hosted runner remains registered only to RomanLMS and is selected by the label set self-hosted, Windows, X64, roman-laptop.

An operator manually starts the smoke workflow from RomanLMS main and selects DEPLOY SMOKE, ROLLBACK SMOKE, or TEST UNHEALTHY SMOKE. The workflow calls this repository's reusable workflow at a full commit SHA. The job refuses any other repository, branch, actor, event, or mismatched confirmation. It uses the existing laptop runner, verifies Docker Desktop's Linux engine and Tailscale, builds and health-checks the test image, publishes the image under the RomanLMS GHCR namespace, then submits its immutable digest to roman-server over Tailscale SSH. The fixed unhealthy fixture returns HTTP 503; its job passes only when the server rejects it at candidate readiness.

The server accepts only a dedicated public key with a forced command, no PTY, and no forwarding. A root-owned launcher rejects any SSH remote command before invoking the controller through one exact sudo rule. The controller validates the workflow run via the GitHub API, checks the main commit and pinned reusable workflow, and accepts only the application and GHCR namespace in its root-owned policy.

## Server release behavior

The smoke application has two loopback-only container slots. The candidate starts on the inactive slot and must answer its health URL before Nginx changes. Nginx listens for this route only on 127.0.0.1:18080; it does not add a Cloudflare route, public DNS record, or production hostname. If Nginx validation or the routed health check fails, the previous include file is restored and the candidate is removed. The controller retains the previous healthy container for a later rollback.

The controller rejects deployment if /srv/apps is not a mount, Docker stores data outside /srv/apps, free disk or memory is below policy, required services are down, a required backup marker is stale, or another deployment holds the application lock. These checks fail closed. The smoke application has no database or persistent application data.

The controller writes an audit JSONL record and active/previous revision state on the applications SSD. Its deployment metric is written to the existing `/srv/apps/monitoring/textfile_collector` directory. A read-only server audit confirmed that Node Exporter mounts this root-owned directory read-only as its textfile collector path, and a temporary probe was scraped successfully by Prometheus then removed. The controller exports the latest outcome, operation duration, workflow run, requested commit, active application revision, and pinned platform revision. It writes failure metrics only after it holds the application lock, so a rejected concurrent request cannot overwrite an in-flight deployment's status.

The Ansible bootstrap adds two Grafana-managed rules through the Grafana 13.3 App Platform API: a failure alert that remains active until a newer operation supersedes it, and a short success notification that resolves after two minutes. Both use the existing Prometheus data source, `Roman Infrastructure Alerts` folder, warning-to-Telegram policy, and Telegram contact point. The provisioner verifies those existing resources first and does not modify contact points or notification policies. The initial bootstrap is installed; the pre-sudo remote-command gate is being tightened before the deployment key is accepted for smoke releases.

## Trust boundaries

- Public pull-request CI runs only on GitHub-hosted runners with a read-only token.
- RomanLMS keeps its existing runner scope. This repository does not request runner sharing or registration.
- The production caller is workflow_dispatch only from RomanLMS main, restricted to the repository owner account, and requires the explicit deployment or rollback confirmation choice.
- The deploy job receives only contents:read, actions:read, and packages:write in its short-lived GITHUB_TOKEN.
- The SSH private key and host key are workflow secrets; they never belong in this repository.
- The server receives the GitHub token only inside the encrypted SSH request and uses a temporary Docker configuration that is deleted after the pull.
- The request cannot name shell commands, file paths, compose files, ports, container arguments, or arbitrary images.
- The application and image are allowlisted on the server; remote inputs cannot select a different application policy.

## Main branch policy

The direct user instruction requires main to remain unprotected because of cost. The repository is public and has no branch ruleset. No branch protection is created. The separate manual deploy dispatch does not protect main from direct pushes and is not presented as equivalent to branch protection.
