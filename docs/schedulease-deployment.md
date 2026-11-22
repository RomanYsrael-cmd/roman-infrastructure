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

The staging database was prepared under registry revision `5486f52958437018dde42640468b136661aff47a`
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
before the staging import.

## Managed staging acceptance on 2026-11-22

The [managed staging deployment](https://github.com/RomanYsrael-cmd/schedulease-server/actions/runs/44996167325)
activated the blue slot for source `3e1aa935f943cf555738b88a5788f01a7b009374`.
It used reusable workflow `27c001d5eadf531645c9869a977905d89ba49bb3` and
installed registry revision `994501dde27c0fd644c06d74e19c2c581ff71baa`
with SHA-256 `f87c2a204794cda9c8cdcaa71cc09c7258ce79dc94a8f78aa5add34e47ad79e1`.
The [exact-source CI run](https://github.com/RomanYsrael-cmd/schedulease-server/actions/runs/44996006121)
passed its Java, PostgreSQL, frontend, Playwright and image checks.

| Component | GHCR digest |
| --- | --- |
| Backend and web assets | `sha256:0b76b1f53e7cff25f55f13ed5b468f1d39ba92c3ff4e1767b26c2777cf9c5f46` |
| Migrator | `sha256:bc96cf1641bdf4f2ba88885d3444560439342b30320dd5088eacde3259164404` |

The authorized Windows service runner submitted through the restricted SSH
account. The forced command rejects remote shell commands and delegates release
authorization to the controller. GitHub Actions uses a dedicated staging
credential in `SCHEDULEASE_STAGING_DEPLOY_KEY` and a verified server host pin in
`ROMAN_PLATFORM_SSH_KNOWN_HOSTS`. The workflow protects its temporary credential
directory using the Windows process identity and removes it after submission.

The staging loopback listener returned HTTP 200 for readiness, the login page,
its JavaScript and its stylesheet. The deployment audit and textfile metrics
recorded success. Both Flyway plans reported V12 with zero pending migrations and
the same release binding. A direct database check confirmed 12 successful history
rows, zero failures, and checksum `620844546` for V12. The backend runs as
`10001:10001` on the isolated bridge with resource limits and no published ports.
This first deployment has no previous managed staging slot.

The owner can request another staging deployment with the registered confirmation.

```powershell
gh workflow run platform-deploy.yml --repo RomanYsrael-cmd/schedulease-server --ref main -f environment=staging -f operation=deploy-stack -f 'confirmation=DEPLOY SCHEDULEASE STAGING'
```

Interactive staging checks for login, scheduling and the event stream remain
part of cutover acceptance. Production dispatch and automatic releases remain
disabled. RomanLMS returned HTTP 200 during staging verification.
