# RomanLMS production edge proxy

This Worker keeps romanlms.com as the browser origin while preserving the
existing apex MX and TXT records. Cloudflare routes the apex through a proxied
placeholder A record and invokes this Worker. The Worker sends requests to a
separate Cloudflare Tunnel hostname protected by a Service Auth policy.

## Required Cloudflare configuration

- Proxied A record @ -> 192.0.2.0 (Cloudflare proxy enabled; reserved
  placeholder address intercepted by Cloudflare).
- Published application romanlms-origin.romanlms.com -> http://127.0.0.1:8081
  through the existing healthy production tunnel.
- Cloudflare Access self-hosted application on romanlms-origin.romanlms.com/*
  with a Service Auth policy for one dedicated service token.
- Worker secrets ROMANLMS_ORIGIN_ACCESS_CLIENT_ID and
  ROMANLMS_ORIGIN_ACCESS_CLIENT_SECRET.
- Worker route romanlms.com/*, with workers.dev disabled and route failure
  set to fail closed.

Do not replace the root MX/TXT records with a CNAME. Do not change unrelated
DNS records, the staging hostname, or public tunnel routes.

The placeholder address is reserved for documentation and testing. The Worker
route must be active for production HTTP traffic to reach the application.
`workers_dev` and preview URLs are disabled so the proxy is not exposed on a
workers.dev URL.

## Free-plan limit

Cloudflare Workers Free is limited to 100,000 requests per UTC day and 10 ms CPU
time per invocation. If the daily limit is reached, the fail-closed route returns
Cloudflare's 1027 error until the limit resets. No paid Workers plan is enabled
by this configuration. Cloudflare Free zones also allow request bodies up to
100 MB; larger uploads return 413 even though the RomanLMS origin permits a
larger body.
