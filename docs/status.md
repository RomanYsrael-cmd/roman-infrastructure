# Phase 5 implementation status

This repository now contains an implemented, testable controller and a RomanLMS caller example. The code has not yet been installed on roman-server, and no production deployment or rollback has been claimed.

## Verified from live GitHub and laptop state

- RomanYsrael-cmd/roman-infrastructure exists, is public, and was empty when audited.
- Its default branch is main and has no branch protection or rulesets. GitHub's branch-protection API reports "Branch not protected" and the rulesets API returns an empty list.
- Actions are enabled. Commit 0c69351942293967dc04b15af096113b48ecacfe passed [run 42312367154](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/42312367154), including 21 Linux controller tests, Ansible syntax validation, and a loopback smoke container.
- The RomanLMS runner is named ROMAN-LAPTOP, runner ID 30, and is registered to RomanLMS with self-hosted, X64, Windows, and roman-laptop labels. Its actual local installation is D:\GitHubActions\romanlms-runner. It was started with its existing run.cmd and GitHub reports it online and idle; no workflow was run on it during this task.
- D:\GitHubActions\biligo-runner is a separate registration for the Biligo repository, runner ID 22, with the biligo-laptop label. Its startup script was briefly launched while identifying the correct install, it remained idle, and that process was stopped without changing registration or configuration.
- Docker CLI and Buildx are installed on the laptop. After the user started Docker, the Linux engine became reachable and the smoke image built and passed a loopback /health check locally.
- The laptop is connected to Tailscale and sees roman-server at 100.67.102.11. TCP port 22 is reachable. The corrected admin password authenticated as romanysrael; no password is stored in this repository or its settings.
- Read-only server checks confirmed /srv/apps, /srv/databases, and /srv/data are separate mounted filesystems; /srv/apps has 212 GB free. Docker root is /srv/apps/docker. Docker, Nginx, PostgreSQL 18, and Cloudflare Tunnel are active; Nginx syntax is valid and port 18080 is unused.
- Node Exporter runs in the existing monitoring stack with --collector.textfile.directory=/textfile and a read-only bind mount from /srv/apps/monitoring/textfile_collector. That directory is root:root mode 0755. Host firewall policy includes a rule that drops TCP/22 unless traffic arrives via tailscale0; UFW itself is inactive.
- A temporary textfile metric was observed through both Node Exporter and Prometheus with `job=node`, `instance=127.0.0.1:9100`, and `host=roman-server`, then removed. No monitoring files or services were changed by that check.
- The existing RomanLMS repository is private and has one CI workflow. Its latest recorded run failed in required-check aggregation after Docker/Testcontainers and Android checks were skipped; the workflow has no deployment workflow.

## Implemented in Git

- A root-owned-policy Python controller with exact request fields, GitHub workflow-run validation, branch-head validation, immutable GHCR digest and OCI-label checks, mount/Docker-root/disk/CPU/memory/service/backup gates, per-app file lock, blue/green health gate, atomic Nginx include, rollback logic, audit history, and Prometheus textfile output.
- A stateless smoke HTTP app and container image definition.
- A public-repository CI workflow with unit tests and a loopback-only container health test.
- A RomanLMS-only reusable workflow and manual caller example, pinned to a full infrastructure commit SHA after the first commit.
- An opt-in Ansible bootstrap that adds only a dedicated forced-command deployment identity, exact sudo permission, root-owned controller policy, and a loopback-only Nginx listener.
- Deployment, security, cost, rollback, onboarding, and Phase 6 handoff notes.

## Not complete

- The laptop runner is now online, but this task did not execute a RomanLMS Actions workflow on it.
- The server is accessible for read-only audit, but the controller, dedicated deployment user/key, sudoers entry, and Nginx snippet are not installed. No server files or services were changed.
- No private deployment key or verified server host-key secret is configured.
- No workflow has published a RomanLMS package or deployed an image to the server.
- No live controller metric, deployment audit, rollout, rollback, persistence, or server-side failure-gate behavior has been verified. The existing metrics collector path is verified, but no metric has yet been emitted.
- An idempotent Grafana 13.3 alert-rule provisioner is implemented. It checks for the existing Prometheus data source, alerts folder, warning-to-Telegram policy, and Telegram contact point, then adds only deployment success/failure rules. It has not been run against Grafana; existing infrastructure alerting remains unchanged.
- RomanLMS's existing workflow is audited but has not been modified.

Phase 5 remains NOT COMPLETE until the live server setup and end-to-end tests pass.
