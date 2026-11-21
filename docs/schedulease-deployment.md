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
