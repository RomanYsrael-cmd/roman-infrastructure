# Platform status — 2026-11-22

## Universal platform work

Roman Infrastructure PR [#26](https://github.com/RomanYsrael-cmd/roman-infrastructure/pull/26) merged as `61d2305c0fe2458ee939303f509ee2c8d986fc0e`. Post-merge CI run [44854410980](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/44854410980) passed, including disposable PostgreSQL coverage for clean, single-migration, and multi-migration states. The root suite passed 212 tests with 7 skips.

RomanLMS OIDC policy PR [#111](https://github.com/RomanYsrael-cmd/roman-lms/pull/111) merged as `353e6b098f7090bcdb0fb12f71e6c55a557ecbf8`. Migration integration PR [#112](https://github.com/RomanYsrael-cmd/roman-lms/pull/112) merged as `2bcf284e508f84b4f11339ae081e1243bac9603e`; LONG CI [44862173399](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44862173399) and main SHORT CI [44868541377](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44868541377) passed. The deployment suite passed 304 tests with 35 skips, workflow policy tests passed 36, and workflow security, shell syntax, and documentation checks passed.

Infrastructure PR [#30](https://github.com/RomanYsrael-cmd/roman-infrastructure/pull/30) merged as `c5bfd9086a5da9f6f4e138ca85495a9dba22145a`. It fixes the standalone release-authorizer import path and allowlists controller pin `e386312fdccd56670c029a6ff3066e6d03c932dd` for production, Phase 6, and smoke. CI [44875926891](https://github.com/RomanYsrael-cmd/roman-infrastructure/actions/runs/44875926891) passed 166 tests with 1 skip. RomanLMS pin-update PR [#113](https://github.com/RomanYsrael-cmd/roman-lms/pull/113) merged as `fad2cdf3e7b5313f8729da2c07fdb62e9b5da495`; its LONG CI [44876358122](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44876358122) and main SHORT CI [44883373643](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44883373643) passed.

See the [platform overview](universal-deployment-platform.md), [onboarding guide](application-onboarding.md), [migration lifecycle](database-migration-lifecycle.md), and [production runbook](universal-deployment-runbook.md). The dated [RomanLMS deployment notes](deployment-runbook.md) and [November 9 hardening report](production-hardening-2026-11-09.md) are historical records.

## Initial production installation and acceptance on 2026-11-21

Roman Infrastructure is installed on `roman-server`. The restricted controller SHA-256 is `68fa2a8bf8f52de166207caa21023844a7e995eafa0b69630c8a670925530e34`; the schema-v2 registry SHA-256 is `7ba5a267861457df81cd3e830f34f18310b176c34dd1938d49e99959b4d7bb66`. The registry contains `platform-smoke`, `romanlms-phase6`, and `romanlms-production`. All three allow platform pin `e386312fdccd56670c029a6ff3066e6d03c932dd`. A root-owned pre-update registry backup is retained at `/etc/roman-platform/applications.json.149606.2026-11-21@06:46:13~`.

Before production acceptance, full pgBackRest backup `20261121-052234F` completed and verified all 3,077 files and WAL. An isolated restore recovered and reported schema 133/checksum 850684505; the restored clone was stopped and its scratch directory removed. No production database data was changed by the restore test.

The private static smoke application deployed successfully in workflow [44884687248](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44884687248), activating its isolated green slot and leaving the previous slot available. RomanLMS production deployment [44886291102](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44886291102) then built and deployed immutable images from main commit `fad2cdf3e7b5313f8729da2c07fdb62e9b5da495` under platform pin `e386312fdccd56670c029a6ff3066e6d03c932dd`. Its validated migration plan observed schema 133→133 with no pending migrations, so it took no migration backup and performed no migration. The blue release is active; the prior green release remains retained.

The post-deployment read-only production verification [44887103083](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44887103083) passed. Server audit records `validate-database` and `verify-stack` success for that run. Production Flyway history remains at version 133/checksum 850684505 with zero failed rows; the backend readiness endpoint reports `UP`, both active containers are healthy, and `nginx -t` passes. The new generic migrator image is now the active release migrator. The automatic release repository switch is `true` and the production freeze is `open` after acceptance.

The first read-only verify attempt was rejected by the previously active migrator image because its old entrypoint required a fixed target variable. The request was stopped before a database or traffic mutation. The successful no-pending-migration deployment above installed the compatible migrator; the subsequent read-only verification passed.

The server has 12 CPUs and about 7.1 GiB RAM. The observed free space was 171 GiB on `/srv/apps`, 196 GiB on `/srv/databases`, and 907 GiB on `/srv/data`. Docker, Nginx, PostgreSQL 18, Cloudflare Tunnel, Prometheus, and Grafana were healthy; the existing Telegram alert route remains configured. These observations do not certify peak-load capacity.

## RomanLMS retirement acceptance on 2026-11-22

RomanLMS removed the old fixed-target deployment paths, their systemd units and
archived production workflow. The trusted-policy transition and script removal
merged in [RomanLMS PR #115](https://github.com/RomanYsrael-cmd/roman-lms/pull/115)
and [PR #116](https://github.com/RomanYsrael-cmd/roman-lms/pull/116).
The cleanup's [LONG CI](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44933306069)
and [main SHORT CI](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44942279301)
passed. Infrastructure [PR #32](https://github.com/RomanYsrael-cmd/roman-infrastructure/pull/32)
authorized the new CI workflow identity. The installed controller code stayed
at SHA-256 `68fa2a8bf8f52de166207caa21023844a7e995eafa0b69630c8a670925530e34`.
The [read-only production verification](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/44943993988)
passed against the new pin. RomanLMS remained on its existing healthy release
with Flyway V133 and zero failed migrations. The release freeze is open, with
automatic releases enabled.

## Current rollout boundary

The installed registry has five registrations. Its active RomanLMS and smoke entries are joined by `schedulease-staging` and `schedulease-production`. The first managed `schedulease-staging` release is active and healthy at Flyway V12. The [SchedulEase runbook](schedulease-deployment.md) records the deployment evidence and remaining cutover checks. Other candidate repositories remain unregistered until their operating and recovery needs are reviewed. `main` remains unprotected; direct-main SHORT and merged-PR LONG authorization paths remain enabled. Other database engines and migration tools are not enabled: PostgreSQL/Flyway is the implemented migration adapter, with pgBackRest and verified dump backup options.

Examples for all profiles are in [application-profiles.json](../examples/application-profiles.json). Registering a repository does not authorize it for deployment until the root-owned application policy and its release workflow are configured and reviewed.
