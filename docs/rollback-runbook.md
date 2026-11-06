# Rollback runbook

The v1 controller preserves the currently active container as the previous release. Rollback checks that the previous container still exists and answers the configured health endpoint, changes only the private route include, validates Nginx, and then records the new active/previous pointer. If route validation fails, it restores the prior Nginx include.

The controller's rollback operation requires the same approved application ID, main-branch workflow run, pinned reusable workflow, current main commit, and server lock as a deploy request. It does not accept an image or arbitrary target release from the caller; it selects only the retained previous release from root-owned server state.

The caller example includes a separate ROLLBACK SMOKE choice. It uses the same pinned reusable workflow, manual main-branch dispatch, SSH identity, workflow-origin check, and server lock. It selects only the controller's retained previous release.

Application rollback never reverses database migrations. The smoke service has no database. Any future database-backed application requires a backup and forward-compatible migration policy before production rollout.
