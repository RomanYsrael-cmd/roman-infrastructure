# Deployment runbook

## Current smoke state

- The controller, restricted `roman-deploy` account, Ed25519 key, private Nginx route, and deployment Grafana alerts are installed on `roman-server`.
- The active harmless release is in the blue slot. `http://127.0.0.1:18080/health` returns HTTP 200. The green release is retained as the previous release.
- The RomanLMS caller requires the owner actor, `main`, `workflow_dispatch`, the expected caller workflow path, and the exact pinned reusable workflow SHA `d95b276cf3d257a685fe7bb7464197c44f4b4ea8`.
- The server allowlist contains only that reusable workflow SHA. The SSH key and verified host key are stored in the RomanLMS repository secrets; never print or paste their values.
- The route listens only on loopback. Do not add a public DNS, Cloudflare, or production route for the smoke app.

## Run the smoke workflow

1. Confirm the existing `ROMAN-LAPTOP` runner is online and Docker Desktop's Linux engine is available.
2. Confirm required RomanLMS CI is green on the current `main` commit.
3. In RomanLMS Actions, run **Roman platform smoke deployment** on `main` and choose `DEPLOY SMOKE`. This publishes the harmless candidate to GHCR, obtains its immutable digest, and sends it through restricted Tailscale SSH.
4. Verify the private health endpoint returns HTTP 200 and Prometheus reports the active slot and successful deployment run.
5. Choose `TEST UNHEALTHY SMOKE`. The workflow succeeds only when the server rejects the candidate at its HTTP 503 readiness gate. Confirm the existing route remains healthy and the failure alert reaches Telegram.
6. Choose `DEPLOY SMOKE` again to make a new healthy slot active while retaining the former slot.
7. Choose `ROLLBACK SMOKE`. Confirm the previous release is active, the private health endpoint remains HTTP 200, the server audit says `operation=rollback` and `outcome=success`, and Telegram receives the rollback success alert.

The expected-unhealthy workflow run is a passing test even though the controller records a failed deployment attempt. Its `Roman Platform Deployment Failed` Telegram alert should resolve after a healthy operation. The success alert is temporary and resolves automatically after two minutes.

## Verify server evidence

- Private route: `curl -fsS http://127.0.0.1:18080/health` from `roman-server`.
- Prometheus scrape: `curl -fsS http://127.0.0.1:9100/metrics`.
- Prometheus API: query `roman_platform_active_release_info` and `roman_platform_last_deployment_info` at `http://127.0.0.1:9090`.
- Active and retained release state: `/srv/apps/roman-platform/state/platform-smoke.json` (admin access only).
- Deployment audit: `/srv/apps/roman-platform/logs/deployments.jsonl` (admin access only).
- Running test containers: inspect only containers labeled `roman.platform.app=platform-smoke`.

Keep logs limited to deployment metadata. Never print SSH credentials, GHCR tokens, repository secrets, or private keys.

## Phase 6 boundary

This controller policy is for the harmless smoke app. Do not use it to restore the RomanLMS production database or route production traffic. Phase 6 needs its own application policy for resource limits, readiness, migrations, backups, retention, production database recovery, and rollback. See [phase6-handoff.md](phase6-handoff.md).
