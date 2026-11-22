# SchedulEase deployment

SchedulEase packages the web assets with the Spring Boot server in one image. The
`schedulease-staging` and `schedulease-production` registrations use one backend
component each. They have separate database instances, bridge networks, secret
references, backups, audit logs and release state. Production is registered for
`schedulease.romanlms.com`. Staging uses the private loopback listener on port
18093. Production uses loopback port 18092 until the Cloudflare ingress points
there.

## Authorization and rollout

The caller workflow runs only on owner-authorized dispatch or the explicitly
enabled canonical-main CI handoff. Keep automatic releases disabled during
onboarding. The controller independently verifies the repository, CI, immutable
image digests and OIDC provenance. The backend runs with Flyway disabled. The
separate migrator provides the standard `info`, `validate`, `history`, and
`migrate` commands, with a target authorized at release time.

The rollout uses the controller's blue/green state and single-backend maintenance
policy. Nginx tests a loopback readiness URL and keeps buffering disabled for
server-sent events. Application rollback uses the previous approved image and
checks schema compatibility. Database restoration is a separate operator action.

## Recovery and cutover

The November 6 pre-reset backup of `schedulease_prod` matched its recorded
SHA-256 and restored into disposable PostgreSQL 18.6. The restore had 51 public
tables and 12 successful Flyway history rows. All 12 checksums match the
repository. The restore demonstrates that recovery point. Confirm the final
write boundary before using it for production recovery.

Test the immutable candidate in staging. Verify readiness, assets, login, a
scheduling operation and the authenticated event stream. Review the backup and
restore evidence, capacity and audit record. Install the production loopback
route, validate the candidate without public traffic, then update only the
SchedulEase Cloudflare hostname after the data and routing handoff is approved.

## Private staging preparation on 2026-11-22

The installed registry revision is `5486f52958437018dde42640468b136661aff47a`
with SHA-256 `85e96bb07b3b8493b20ffd188730a62ffe3fc5e70e619f58ea8b51402b2dd5e0`.
The private PostgreSQL 18.6 staging container has a dedicated persistent mount,
no published ports and separate role credentials. The platform's storage,
capacity and private network preflight passed on `roman-server`. The restored
Flyway history is V12/checksum `620844546`, with no failed rows. The runtime
role cannot update Flyway history.

The app-specific backup role produced a custom-format staging dump
`stage-baseline-20261121T202627Z.dump`. Its archive listing and SHA-256
`7e1115568c904edc10bb66a5737c8992768cab3cf612d62d44ab44fa5a443526`
were verified. The original November 6 archive was restored separately
before the staging import. The public hostname returned HTTP 502 before this
onboarding, and the private staging route returns 503 until an approved
image is running.

SchedulEase [main CI](https://github.com/RomanYsrael-cmd/schedulease-server/actions/runs/44946053293)
passed Java, PostgreSQL, browser and image checks. The [first staging release
request](https://github.com/RomanYsrael-cmd/schedulease-server/actions/runs/44946321235)
passed exact-source authorization and published the following immutable images.

| Component | GHCR digest |
| --- | --- |
| Backend and web assets | `sha256:11096f604c1f9408cbdfd0680a9ce522a5fa43cab0c06e46b7a76df6004e2219` |
| Migrator | `sha256:18a0615d5d075f550762cf9d54c5fc85e01b28ea572eac5bc85db1805adac` |

The submit job queued without an eligible runner and the run was canceled.
The reusable workflow requires an approved Windows/X64 runner with the
`roman-laptop` label and private access to the server. SchedulEase does not
yet have a runner or its environment-specific forced-command credential. Production dispatch and automatic releases are
disabled until staging and cutover acceptance.
