# Platform status — 2026-11-21

## Universal platform work

The universal deployment and migration changes are under review in draft [Infrastructure PR #26](https://github.com/RomanYsrael-cmd/roman-infrastructure/pull/26) and coordinated RomanLMS work. Infrastructure PR head `a24422c12baa790cf31f77b14b2c9f697adf78db` passed its required CI on 2026-11-21, including disposable PostgreSQL tests for one and multiple pending migrations. The RomanLMS integration is committed locally at `17555ba6` and its complete local deployment suite passed; it still needs the latest-main sync, caller workflow pin updates, and hosted CI. Neither the universal controller nor the RomanLMS migrator update has been installed on production.

See the [platform overview](universal-deployment-platform.md), [onboarding guide](application-onboarding.md), [migration lifecycle](database-migration-lifecycle.md), and [current runbook](universal-deployment-runbook.md). The dated [RomanLMS deployment notes](deployment-runbook.md) and [November 9 hardening report](production-hardening-2026-11-09.md) are historical records.

## Production observation

Read-only checks over `roman-server` on 2026-11-20 observed:

- RomanLMS production active in the green slot at source commit `faab6a34ccf625ff9f519ad5479b1212d1ceed27`, schema version 133. Backend image digest: `sha256:d1b684e908109711a6b02149446c0218dc533728b0993d58cdc7eb24c472efb7`; frontend: `sha256:485a15da6bb1560fc6389420bb6468ab97dfe00ba839f2d1cd339183e2a3f7d9`.
- The active backend and frontend containers were healthy; `http://127.0.0.1:8081/lms/actuator/health` returned HTTP 200. Docker, Nginx, PostgreSQL 18, and Cloudflare Tunnel services were active. The existing smoke and Phase 6 containers and Prometheus/Grafana services were present.
- The installed controller file SHA-256 was `3b5e55865c01aba2ea56dd5c453d68838d94f60be02466d728fd0db7d4d0c441`. The active production release state records reusable-workflow revision `fcebbbaa87a2823d66a0b01503f07a6254eeb352`.
- pgBackRest reported stanza `roman-platform` status `ok`, with a full backup at `2026-11-19 15:53 UTC` and a differential backup at `2026-11-19 18:30 UTC`. The full and differential sets were visible in repository 1. A fresh cutover backup is still required immediately before production installation.
- Available storage was 95 GiB on `/`, 171 GiB on `/srv/apps`, and 196 GiB on `/srv/databases`. The host reported 4.9 GiB available memory, 12 CPUs, and 45 GiB swap.
- The Prometheus API responded to a read-only `up` query. This observation did not inspect Telegram delivery or certify application load capacity.

No production data, volumes, routes, or services were changed during these checks. The live server is still on its previously installed controller; it has not yet accepted the universal registry or dynamic multi-migration path.

## Current rollout boundary

The checked-in application registry currently contains the smoke app and RomanLMS policies. The example profiles are not live registrations. Candidate repositories remain unregistered until their build, data, ports, domains, secrets, resource budgets, backup, and rollback plans are reviewed. Install the matching infrastructure and RomanLMS revisions together, then complete the non-destructive production acceptance in the universal runbook before describing the universal platform as production-ready.
