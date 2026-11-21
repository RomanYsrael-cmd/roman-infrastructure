# Platform status — 2026-11-21

## Universal platform work

Roman Infrastructure PR [#26](https://github.com/RomanYsrael-cmd/roman-infrastructure/pull/26) merged as `61d2305c0fe2458ee939303f509ee2c8d986fc0e`. Post-merge CI run [44854410980](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/44854410980) passed, including disposable PostgreSQL coverage for clean, single-migration, and multi-migration states. The root suite passed 212 tests with 7 skips.

RomanLMS OIDC policy PR [#111](https://github.com/RomanYsrael-cmd/roman-lms/pull/111) merged as `353e6b098f7090bcdb0fb12f71e6c55a557ecbf8`. Migration integration PR [#112](https://github.com/RomanYsrael-cmd/roman-lms/pull/112) merged as `2bcf284e508f84b4f11339ae081e1243bac9603e`; LONG CI [44862173399](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44862173399) and main SHORT CI [44868541377](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44868541377) passed. The deployment suite passed 304 tests with 35 skips, workflow policy tests passed 36, and workflow security, shell syntax, and documentation checks passed.

Infrastructure PR [#30](https://github.com/RomanYsrael-cmd/roman-infrastructure/pull/30) merged as `c5bfd9086a5da9f6f4e138ca85495a9dba22145a`. It fixes the standalone release-authorizer import path and allowlists controller pin `e386312fdccd56670c029a6ff3066e6d03c932dd` for production, Phase 6, and smoke. CI [44875926891](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/44875926891) passed 166 tests with 1 skip. RomanLMS PR [#113](https://github.com/RomanYsrael-cmd/roman-lms/pull/113) updates the corresponding workflow pins; its required LONG CI and production acceptance are pending.

See the [platform overview](universal-deployment-platform.md), [onboarding guide](application-onboarding.md), [migration lifecycle](database-migration-lifecycle.md), and [production runbook](universal-deployment-runbook.md). The dated [RomanLMS deployment notes](deployment-runbook.md) and [November 9 hardening report](production-hardening-2026-11-09.md) are historical records.

## Production observation

Roman Infrastructure was installed on `roman-server` on 2026-11-21. The restricted controller and schema-v2 registry load successfully. The installed `roman_deploy.py` SHA-256 is `68fa2a8bf8f52de166207caa21023844a7e995eafa0b69630c8a670925530e34`; the registry SHA-256 is `7ba5a267861457df81cd3e830f34f18310b176c34dd1938d49e99959b4d7bb66`. The registry contains three applications: `platform-smoke`, `romanlms-phase6`, and `romanlms-production`. All three allow the verified infrastructure pin above. The prior root-owned registry backup is `/etc/roman-platform/applications.json.149606.2026-11-21@06:46:13~`.

The existing RomanLMS production release remains active in the green slot at source commit `faab6a34ccf625ff9f519ad5479b1212d1ceed27`. Its observed Flyway version/checksum is `133 / 850684505`, with zero failed history rows. The backend and frontend containers are healthy; `http://127.0.0.1:8081/lms/actuator/health` returned HTTP 200. Docker, Nginx, PostgreSQL 18, and Cloudflare Tunnel services are active. Nginx configuration validates, and the production route files are unchanged.

A fresh full pgBackRest backup, `20261121-052234F`, completed at 2026-11-21 05:22 UTC. Verification checked all 3,077 files and WAL without errors. An isolated restored clone recovered successfully and reported schema 133/checksum 850684505; the clone was stopped and its scratch directory removed. Production data and migration history were not changed.

Available space was 171 GiB on `/srv/apps`, 196 GiB on `/srv/databases`, and 907 GiB on `/srv/data`; the host had 12 CPUs and about 5.2 GiB available RAM out of 7.1 GiB. Prometheus and Grafana were healthy, and the existing Telegram route remained configured. These observations do not certify application peak-load capacity.

The controller registry update changed only the approved workflow SHA allowlists. No application images, containers, routes, secrets, schema, or traffic were changed. A GitHub-authorized read-only `verify-stack` acceptance run is still pending; automatic releases remain disabled and the production freeze is closed while the caller workflow pin is updated.

## Current rollout boundary

The checked-in registry contains three live RomanLMS-related registrations. Candidate repositories remain unregistered until their build, data, ports, domains, secrets, resource budgets, backup, and rollback plans are reviewed. `main` remains unprotected, and the direct-main SHORT and merged-PR LONG authorization paths are retained. See the production acceptance result above before making further rollout claims.
