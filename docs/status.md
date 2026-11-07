# Phase 5 implementation status

Phase 5 is complete for the isolated `platform-smoke` application. The RomanLMS deployment caller passed its required CI, the reviewed image was published to GHCR and deployed through `ROMAN-LAPTOP`, the unhealthy candidate was rejected without switching traffic, and the retained release was restored successfully. Deployment failure, success, and recovery notices reached Telegram.

## Cost and scope

- No paid services, additional infrastructure, production runner, public route, Cloudflare DNS change, or RomanLMS production database restoration was added.
- `main` remains unprotected as requested. No branch protection was changed.
- The existing `ROMAN-LAPTOP` runner remains online and scoped to `RomanYsrael-cmd/roman-lms`.
- The only repository deployment secrets are `ROMAN_PLATFORM_SSH_PRIVATE_KEY` and `ROMAN_PLATFORM_SSH_KNOWN_HOSTS`. Their values were not retrieved or printed.
- The deployment host fingerprint was verified as `SHA256:Ou1kYNVRN74lXu5SSxa5mVop82fvxgK8P0zD+ymkIh4`.

## Reviewed workflow and server policy

- RomanLMS CI run [42347455950](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42347455950) passed for caller commit `cfa0d607ef9f35643149fb3a4b71b508a0446299` before any smoke deployment was dispatched.
- The caller pins the reusable workflow to `d95b276cf3d257a685fe7bb7464197c44f4b4ea8`. The server allowlist contains only that SHA.
- Infrastructure CI run [42353698237](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/42353698237) passed with the final allowlist. Run [42353359569](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/42353359569) passed the retained-container rollback fix.
- The server controller and allowlist were refreshed with the reviewed Ansible playbook; the latest play recap was `ok=26 changed=1 unreachable=0 failed=0`.
- The `roman-deploy` account remains restricted to the forced deployment command and the single reviewed sudo command. Independent administrative SSH/Tailscale access remained available.
- The isolated Nginx route listens only on `127.0.0.1:18080`; it returned HTTP 200 after deployment and rollback. No public route was configured.

## End-to-end evidence

| Scenario | GitHub run | Outcome and server evidence |
| --- | --- | --- |
| Expected unhealthy candidate | [42352201188](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42352201188) | Passed as a negative test. GHCR image `sha256:b33e39c91d61aa8e2fe4c1207970033fb24ddb957e66ef0bb34b386aa09af1a6` was rejected at the HTTP 503 readiness gate. Nginx continued serving the existing blue release; Prometheus recorded `outcome="failure"`. Telegram received the firing and recovery notices. |
| Healthy deployment | [42352509205](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42352509205) | Passed. GHCR image `sha256:1beef0981ff31e653bab4b69fc506403b6bf01687215a6e73a97ca81e357b77f` became active in the green slot. The private health route returned 200; Prometheus recorded the success and active release. Telegram received the firing success notice and its automatic resolution. |
| Rollback defect found | [42353133628](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42353133628) | The first rollback attempt exposed a name-versus-container-ID comparison bug. The controller rejected it before changing the route. The stored blue container remained healthy. The fix verifies the expected container name and its app/slot labels. |
| Successful rollback | [42353471254](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42353471254) | Passed after infrastructure CI and Ansible refresh. The server restored blue commit `48436c11fed3d451f4eeaf3f524df3572e130afc`; the route returned 200. The server audit and Prometheus show `operation="rollback", outcome="success"`. Telegram received the rollback success alert. |

Final server observations: the active release was blue at revision `48436c11fed3d451f4eeaf3f524df3572e130afc`, the most recent operation was successful rollback run `42353471254`, and the private health route returned HTTP 200.

## Phase 6 prerequisites

Phase 5 only deployed the harmless smoke application. Before restoring or routing production RomanLMS traffic:

- Restore and independently validate production database backups.
- Define a RomanLMS-specific application policy for repository, resources, readiness, migrations, backups, retention, and rollback.
- Confirm capacity for two actual RomanLMS releases on the server, or prepare an approved maintenance-window deployment strategy.
- Validate application persistence, database migration behavior, release health, backup recovery, and rollback without using production data in the smoke test.
- Review the production workflow and its secret boundaries before any DNS, Cloudflare, or production route change.

Production database restoration, public Cloudflare routing, and production traffic cutover remain out of scope.
