# Platform status — 2026-11-21

## Universal platform work

Roman Infrastructure PR #26 merged to `main` as `61d2305c0fe2458ee939303f509ee2c8d986fc0e`. Post-merge CI run [44854410980](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/44854410980) passed, including disposable PostgreSQL migration integration. The local Infrastructure suite passed 212 tests with 7 skips.

RomanLMS OIDC policy PR [#111](https://github.com/RomanYsrael-cmd/roman-lms/pull/111) merged to `main` as `353e6b098f7090bcdb0fb12f71e6c55a557ecbf8` after hosted LONG CI run [44855204692](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44855204692) passed. The coordinated migration integration PR [#112](https://github.com/RomanYsrael-cmd/roman-lms/pull/112) merged as `2bcf284e508f84b4f11339ae081e1243bac9603e` after required LONG CI run [44862173399](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44862173399) passed. Post-merge main SHORT CI run [44868541377](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44868541377) is running. The local deployment suite passed 304 tests with 35 skips, workflow-policy tests passed 36, the workflow security validator and shell syntax checks passed, and documentation checks scanned 396 Markdown files with 3 verifier tests passing. Neither the universal controller nor the matching RomanLMS migrator has been verified on production.

See the [platform overview](universal-deployment-platform.md), [onboarding guide](application-onboarding.md), [migration lifecycle](database-migration-lifecycle.md), and [current runbook](universal-deployment-runbook.md). The dated [RomanLMS deployment notes](deployment-runbook.md) and [November 9 hardening report](production-hardening-2026-11-09.md) are historical records.

## Production observation

Read-only checks over `roman-server` on 2026-11-20 observed:

- RomanLMS production active in the green slot at source commit `faab6a34ccf625ff9f519ad5479b1212d1ceed27`, schema version 133. Backend image digest: `sha256:d1b684e908109711a6b02149446c0218dc533728b0993d58cdc7eb24c472efb7`; frontend: `sha256:485a15da6bb1560fc6389420bb6468ab97dfe00ba839f2d1cd339183e2a3f7d9`.
- The active backend and frontend containers were healthy; `http://127.0.0.1:8081/lms/actuator/health` returned HTTP 200. Docker, Nginx, PostgreSQL 18, and Cloudflare Tunnel services were active. The existing smoke and Phase 6 containers and Prometheus/Grafana services were present.
- The installed controller file SHA-256 was `3b5e55865c01aba2ea56dd5c453d68838d94f60be02466d728fd0db7d4d0c441`. The active production release state records reusable-workflow revision `fcebbbaa87a2823d66a0b01503f07a6254eeb352`.
- pgBackRest reported stanza `roman-platform` status `ok`, with a full backup at `2026-11-19 15:53 UTC` and a differential backup at `2026-11-19 18:30 UTC`. The full and differential sets were visible in repository 1. A fresh cutover backup is still required immediately before production installation.
- Available storage was 95 GiB on `/`, 171 GiB on `/srv/apps`, and 196 GiB on `/srv/databases`. The host reported 4.9 GiB available memory, 12 CPUs, and 45 GiB swap.
- The Prometheus API responded to a read-only `up` query. This observation did not inspect Telegram delivery or certify application load capacity.

Later reachability checks on 2026-11-21 found the `roman-server` Tailscale peer offline; a fresh SSH probe also timed out. Automated requests to the public homepage and health path received Cloudflare HTTP 530 and then 403 error 1010 responses. Those edge responses did not verify origin health. No production data, volumes, routes, or services were changed during this work. Current host health and backup freshness are unverified.

## Current rollout boundary

The checked-in registry contains three RomanLMS-related registrations: production, private staging, and an isolated smoke application. The example profiles are not live registrations. Candidate repositories remain unregistered until their build, data, ports, domains, secrets, resource budgets, backup, and rollback plans are reviewed. Install the matching infrastructure and RomanLMS revisions together, then complete the non-destructive production acceptance in the universal runbook before describing the universal platform as production-ready. Keep canonical `main` unprotected and retain the existing direct-main SHORT and merged-PR LONG release authorization paths.
