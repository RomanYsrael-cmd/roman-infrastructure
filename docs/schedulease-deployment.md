# SchedulEase deployment

SchedulEase packages the web assets with the Spring Boot server in one image. The
`schedulease-staging` and `schedulease-production` registrations use one backend
component each. They have separate database instances, bridge networks, secret
references, backups, audit logs and release state. Production is registered for
`schedulease.romanlms.com`. Staging uses the private loopback listener on port
18093. Production uses loopback port 18092 as the Cloudflare origin.

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

The stage is an independent test environment. RomanLMS returned HTTP 200
during staging verification.

## Fresh production database on 2026-11-22

The production cluster was initialized with an empty `schedulease_prod`
database on the internal `roman-platform-schedulease-production` bridge at
`172.30.73.10:5432`. The November 6 archive was kept as a historical recovery
point. The fresh cluster received no historical users, institution records,
departments, teachers, or schedules.

The PostgreSQL 18 service starts after its Docker and database mounts. The
production runtime, migration login, non-login migration owner, read-only
metadata login, backup login, and writer observer each have a separate purpose.
The runtime can work with application tables and cannot modify Flyway history
or create schema objects. Root-owned credentials are kept under
`/etc/roman-platform/schedulease-production`. The production GitHub runner uses
a separate restricted SSH key and the already verified server host pin.

The [first managed production deployment](https://github.com/RomanYsrael-cmd/schedulease-server/actions/runs/45001227949)
used source `4e7e202f7c23de9fdb0b0c0a11d4a27d61ae30dc`, reusable workflow
`27c001d5eadf531645c9869a977905d89ba49bb3`, and [exact-source CI](https://github.com/RomanYsrael-cmd/schedulease-server/actions/runs/45000257674).
The installed controller was built from `9fa1aa78d43b328fa285618e3b1963a0b6342d6c`.
The installed registry has SHA-256 `ad4902f0837f8633f933907fec7c81df35be9adc110f5dc190c31b4d7084a1e1`.

| Image | GHCR digest |
| --- | --- |
| Backend | `sha256:60e504e389180729863234c0ff59c070f31041c5a8a062eeae226ffe45664935` |
| Migrator | `sha256:3f1528dc9118910f65e7cd4ddfe437ac761faa0b632b800345af4e8f5507c3ec` |

The controller verified an empty-schema backup before applying V1–V12. It
verified every Flyway checksum and switched to the private blue slot only after
readiness passed. The temporarily enabled `allow_initial_schema` setting was
then restored to `false`. The [read-only production verify run](https://github.com/RomanYsrael-cmd/schedulease-server/actions/runs/45001708022)
passed with the strict policy active. Flyway had zero pending and zero failed
migrations.

| Flyway version | Checksum |
| --- | ---: |
| V1 | 1186112418 |
| V2 | 608468509 |
| V3 | -636919216 |
| V4 | 100423734 |
| V5 | 688628073 |
| V6 | -1704580588 |
| V7 | -69753045 |
| V8 | 1744874997 |
| V9 | 2139377222 |
| V10 | 1202240979 |
| V11 | -303109904 |
| V12 | 620844546 |

### First administrator and public handoff

Use the app-specific [owner initializer](../deploy/scripts/schedulease-production/initialize-schedulease-owner)
over the existing private SSH connection. Supply the real institution and
department. Enter the owner's username, email, and password at the prompts.
The password is hashed in memory and is never returned or written to a file.
A transaction creates the first platform administrator and the required
institution and department. A second initialization fails once the account
exists.

```powershell
ssh -t roman-server 'sudo -n /opt/roman-platform/bin/initialize-schedulease-owner --school "Actual institution" --department "Actual department"'
```

Use `--verify` to verify an existing administrator privately and
`--verify-public` to verify HTTPS after the handoff. The command tests login,
authorization, CSRF, secure session cookies, the authenticated SSE heartbeat,
and logout. Its root-only receipt contains the source revision and check
results. The exact-source integration suite covers authenticated scheduling,
solver, and department isolation using disposable fixtures.

Cloudflared shares the host network namespace and reached the private
listener at `127.0.0.1:18092`. Its applied route for
`schedulease.romanlms.com` already pointed to `http://127.0.0.1:18092` when
production work started. The Nginx production site gates Cloudflare requests
until the private owner check and readiness acceptance pass. The operator
then restores the original app-specific site from the root-owned
`nginx-site-before-private-acceptance.conf` copy, tests Nginx, and reloads
without restarting it. Other Cloudflare hostnames and Nginx sites retain their
existing configuration.

### DBeaver access

Use the dedicated `schedulease_prod_dbeaver` login for administrative data
editing. Its permissions include application table reads and writes and
read-only Flyway history. It does not own the schema or have schema creation
rights.

Open a separate SSH forward from PowerShell. The RomanLMS forward uses a
different port and database.

```powershell
ssh -N -L 127.0.0.1:15433:172.30.73.10:5432 roman-server
```

Set DBeaver to PostgreSQL at `127.0.0.1:15433` with database
`schedulease_prod` and the dedicated login. Enter its password privately.
Disconnect DBeaver before a schema migration. The controller checks for
sessions under this login before applying migrations.

### Baseline, ongoing backups, and monitoring

The preserved baseline is
`/srv/apps/schedulease-production/backups/baseline/fresh-schema-v12.dump`
with SHA-256 `1ea0a0a18b85ae6e2398fc4294f7567f0ce5e186ac397c91ba208eb4a7470a98`.
It was taken with zero business rows and successfully restored into an
isolated PostgreSQL 18 instance. The restore had the same 12 Flyway checksums
and no business records. The accompanying JSON contains the archive checksum,
source revision, migration image, and Flyway history.

The app's systemd timer takes a verified dump daily at 19:00 UTC. The registered
retention keeps seven recent verified backups and the separate fresh baseline.
To validate recovery, check the manifest digest and restore into a new isolated
database with `pg_restore --exit-on-error --no-owner --no-privileges`.
Restore the dedicated grants and validate the matched application and schema
before connecting an active service.

Prometheus scrapes the app's readiness, resource usage, backup result,
container restarts, and deployment outcomes. Grafana routes the SchedulEase
deployment, readiness, and backup alerts through the existing Telegram policy.
The audit JSONL and release state are app-specific and live under
`/srv/apps/schedulease-production`. Automatic releases remain disabled during
initial acceptance.

A disposable SchedulEase candidate with an unavailable database failed the
controller readiness probe and was removed. The live staging release and
route remained healthy and retained the same contents throughout the test.
