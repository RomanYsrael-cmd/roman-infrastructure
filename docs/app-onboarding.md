# Application onboarding contract

Each application has one server-side allowlist entry. The caller workflow is application-specific and stays in that application's repository. Shared controller and reusable workflow updates are pinned by full commit SHA.

An application entry defines:

- Application ID, exact source repository, production branch, caller repository and caller workflow path
- Approved reusable workflow path and a list of approved workflow commit SHAs
- GHCR package namespace and deployment environment
- Deployment strategy, resource limits, expected health response, and readiness timeout
- Required mounts, Docker storage root, free-space and memory thresholds
- Root-owned state, audit, lock, metrics, and Nginx include paths
- Optional backup marker and maximum age

The request format contains only the operation, application ID, source repository and branch, full source commit SHA, caller commit SHA, pinned reusable workflow SHA, environment, immutable image digest, run ID/attempt/actor, and short-lived GitHub tokens. Any additional field is rejected.

The v1 controller is intentionally scoped to the stateless platform-smoke application. It does not accept arbitrary Compose files, migration commands, volume mounts, ports, commands, or app secrets. Database-backed services, RomanLMS production deployment, migration gates, recreate strategy, and application-specific persistent volumes require a separate reviewed policy and must not be enabled by editing a workflow input.

For an application with persistent storage, mount and bind paths must be explicitly added to the root-owned policy, tested for correct filesystem identity, and backed by service-level mount dependencies before onboarding.
