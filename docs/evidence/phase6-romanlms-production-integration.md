# Phase 6 RomanLMS integration evidence

Status: **in progress; private server deployment and public cutover are not complete.** Do not interpret this document as production-cutover approval.

## Baseline

- Phase 5 infrastructure baseline: `4e5baff5fb2e86c3769d7bd7bf445ce76fa7f13a`.
- Phase 5 RomanLMS baseline: `cfa0d607ef9f35643149fb3a4b71b508a0446299`.
- Phase 5 required CI: [RomanLMS run 42347455950](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42347455950).
- Phase 5 retained rollback: [smoke run 42353471254](https://github.com/RomanYsrael-cmd/roman-lms/actions/runs/42353471254). The smoke listener remains separate at `127.0.0.1:18080`.

## Recovery and local validation

- The November 6 `2026-11-06_162051` RomanLMS dump matched SHA-256 `b9a1d013332c7692d11b84c2503e51195b8e6ec36e71a5fa223640fa9a617890`. A later inventory snapshot did not include a newer RomanLMS dump. Later production writes may be absent.
- A local isolated PostgreSQL 18.6 restore had 121 tables and successful Flyway schema V132. Flyway validated 135 migration records. The exact V132 info report contained two reviewed retired repeatable records; the controller requires exact allowlisted exceptions for those records.
- A second disposable copy was used for application writes. Synthetic accounts passed login, privacy-policy and tenant acknowledgement, tenant isolation, administrator/student authorization, logout, and refresh revocation. A synthetic file passed application upload and download through isolated S3 storage; another synthetic tenant was denied access. The original recovery baseline was not used for application writes.
- The real frontend and backend images built locally. A private local proxy served the frontend and backend health endpoint and rejected unauthenticated access and invalid login. The object service rejected an unsigned S3 request and passed a synthetic authenticated upload/download/delete test.
- Two local backend instances became healthy. The observed idle memory was approximately 517 MiB and 393 MiB; the disposable PostgreSQL and object service used approximately 584 MiB and 76 MiB. These measurements are not a peak-load or server-capacity certification.
- A local Linux lab exercised real frontend/backend blue and green containers, rejected unhealthy candidates for each component, preserved the active route, stopped the retained pair, and restarted and rolled back to that pair. The lab bypassed GitHub authorization, registry publication, and the real migration gate; it does not establish server deployment success.
- The RomanLMS deployment contract suite completed 288 tests successfully, with seven platform-dependent skips. Focused scheduling and S3 configuration tests passed. Infrastructure controller tests and Linux Ansible syntax checks must be rerun against the final commit before release.

## Pending evidence

Record final source and infrastructure commits, required CI and deployment run URLs, GHCR digests, server restore and service health, resource measurements, private functional acceptance, security-gate rejection results, coordinated rollback, Prometheus/Grafana and Telegram delivery here after those actions actually complete. Do not include credentials, tokens, private keys, or student records.

## Risks and cutover gate

Original database roles and grants, current production writes, physical pgBackRest/WAL recovery, and original object storage have not been fully validated. Existing file references in the restored database are not recovered merely by creating an empty staging bucket. Full application acceptance and Android compatibility remain unverified. Public cutover, production database replacement, and destructive production migrations remain outside this authorization.
