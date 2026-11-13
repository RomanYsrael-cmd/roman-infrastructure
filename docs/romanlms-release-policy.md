# RomanLMS production release authorization

RomanLMS production accepts two automatic release paths. Both must pass the exact-source CI job, GitHub workflow and actor checks, root-owned release switches, image provenance, database and backup safeguards, blue/green readiness, health checks, audit logging, and rollback policy. The restricted deployment controller remains the final authority.

## Feature branch and pull request

1. Run LONG CI on the feature pull request.
2. Merge the owner-authored same-repository pull request to canonical `main` only after LONG succeeds.
3. Run SHORT CI on the resulting exact main commit.
4. Release only when GitHub confirms the same tested tree was merged and the exact-main SHORT required job succeeds.

## Direct push to main

An owner-authorized direct push to canonical `main` starts SHORT CI automatically. LONG CI does not start for direct pushes. This route intentionally has reduced test coverage and must be used only with awareness of that tradeoff.

The controller requires GitHub Actions evidence for the exact push event, trusted actor, canonical repository, `main` branch, current main SHA, approved CI workflow and run attempt, and the successful required job. It independently checks the GitHub commit identity and pull-request association. If the commit is associated with a merged pull request, it must satisfy the full PR/LONG policy; missing or invalid PR evidence cannot fall back to direct-push authorization. Commit messages are not authorization evidence.

## Fail-closed conditions

Pending or failed SHORT CI, wrong actors, repositories, branches, workflow identities or attempts, stale SHAs, mismatched commit metadata, incomplete PR association, and non-production changes all block automatic release. The repository freeze and automatic-release switch plus the controller's root-owned enable switch remain required. A workflow or controller change is not active on the server until its immutable revision is reviewed, allowlisted, installed and verified.

Focused controller regressions cover both allowed paths, invalid or missing LONG evidence on PR merges, unauthorized actors, failed and pending SHORT runs, stale source commits, incorrect repositories/branches/workflows/attempts, and the production-change safety gate. Full RomanLMS LONG CI remains the required PR check for normal feature work; a direct-main release reports its intentionally reduced coverage in the release summary.
