"""Verify the two CI proofs required for an automatic RomanLMS production release."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_JOB_NAME = "CI / required"
CI_RUN_TITLE_PREFIX = "RomanLMS CI"
CANONICAL_REPOSITORY = "RomanYsrael-cmd/roman-lms"
AUTHORIZED_ACTOR = "RomanYsrael-cmd"


class CiGateError(ValueError):
    """A missing or unverifiable LONG/SHORT CI release proof."""


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise CiGateError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CiGateError(f"{label} timestamp is malformed") from error
    if parsed.tzinfo is None:
        raise CiGateError(f"{label} timestamp has no timezone")
    return parsed


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CiGateError(f"{label} is malformed")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CiGateError(f"{label} is malformed")
    return value


def _required_job(api_json: Callable[[str], Any], run_id: int, attempt: int) -> None:
    response = _object(
        api_json(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"),
        "CI job response",
    )
    jobs = response.get("jobs")
    if not isinstance(jobs, list) or len(jobs) > 100:
        raise CiGateError("CI job results are malformed or truncated")
    if isinstance(response.get("total_count"), int) and response["total_count"] != len(jobs):
        raise CiGateError("CI job results are incomplete")
    matches = [job for job in jobs if isinstance(job, dict) and job.get("name") == CI_JOB_NAME]
    if len(matches) != 1:
        raise CiGateError(f"CI run must contain exactly one {CI_JOB_NAME} job")
    job = matches[0]
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise CiGateError(f"{CI_JOB_NAME} did not complete successfully")


def _list_runs(api_json: Callable[[str], Any], path: str, label: str) -> list[dict[str, Any]]:
    response = _object(api_json(path), f"{label} run response")
    runs = response.get("workflow_runs")
    if not isinstance(runs, list) or len(runs) > 100:
        raise CiGateError(f"{label} run results are malformed or truncated")
    total = response.get("total_count")
    if isinstance(total, int) and total != len(runs):
        raise CiGateError(f"{label} run results are incomplete")
    if not all(isinstance(run, dict) for run in runs):
        raise CiGateError(f"{label} run results contain a malformed record")
    return runs


def _latest_run(runs: list[dict[str, Any]], label: str) -> dict[str, Any]:
    if not runs:
        raise CiGateError(f"No exact {label} CI run exists")
    for run in runs:
        _positive_int(run.get("id"), f"{label} run ID")
        _positive_int(run.get("run_attempt"), f"{label} run attempt")
    return max(runs, key=lambda item: (item["id"], item["run_attempt"]))


def _require_run_identity(
    run: dict[str, Any],
    *,
    workflow_id: int,
    source_sha: str,
    branch: str,
    event: str,
    profile: str,
    repository: str,
) -> None:
    actor = run.get("actor")
    head_repository = run.get("head_repository")
    expected_title = f"{CI_RUN_TITLE_PREFIX} - {profile} - {source_sha}"
    if (
        run.get("workflow_id") != workflow_id
        or run.get("path") != CI_WORKFLOW_PATH
        or run.get("head_sha") != source_sha
        or run.get("head_branch") != branch
        or run.get("event") != event
        or run.get("display_title") != expected_title
        or not isinstance(actor, dict)
        or actor.get("login") != AUTHORIZED_ACTOR
        or not isinstance(head_repository, dict)
        or str(head_repository.get("full_name", "")).lower() != repository.lower()
    ):
        raise CiGateError(f"Exact owner-authorized {profile} CI identity does not match")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise CiGateError(f"The latest {profile} CI run did not complete successfully")


def _tree_sha(api_json: Callable[[str], Any], sha: str, label: str) -> str:
    commit = _object(api_json(f"/commits/{sha}"), f"{label} commit")
    tree = _object(_object(commit.get("commit"), f"{label} commit data").get("tree"), f"{label} tree")
    tree_sha = tree.get("sha")
    if not isinstance(tree_sha, str) or SHA_RE.fullmatch(tree_sha) is None:
        raise CiGateError(f"{label} tree identity is malformed")
    return tree_sha


def _workflow_blob_sha(api_json: Callable[[str], Any], source_sha: str) -> str:
    path = urllib.parse.quote(CI_WORKFLOW_PATH, safe="/")
    content = _object(api_json(f"/contents/{path}?ref={source_sha}"), "CI workflow content")
    blob_sha = content.get("sha")
    if content.get("type") != "file" or not isinstance(blob_sha, str) or SHA_RE.fullmatch(blob_sha) is None:
        raise CiGateError("CI workflow content identity is malformed")
    return blob_sha


def _is_production_path(value: str) -> bool:
    path = value.replace("\\", "/").removeprefix("./")
    if path in {
        ".dockerignore",
        "compose.production.yml",
        "frontend/index.html",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/vite.config.ts",
        "frontend/nginx.conf.template",
        "frontend/Dockerfile",
        "backend/.dockerignore",
        "backend/pom.xml",
        "backend/mvnw",
        "backend/Dockerfile",
        "ops/deploy/deploy-compose.sh",
        "ops/deploy/release_manifest.py",
        "ops/deploy/release_trust.py",
        "ops/deploy/prepare_release_images.py",
        "ops/deploy/release_metadata.py",
        "ops/deploy/release-metadata.env",
        "ops/nginx/romanlms-docker.conf",
    }:
        return True
    if path.startswith((
        "frontend/src/",
        "frontend/public/",
        "backend/src/main/",
        "backend/.mvn/",
        "ops/deploy/migration/",
        "ops/deploy/gate/",
        "ops/deploy/preflight/",
    )):
        return True
    if path.startswith((".github/", "tools/ci/", "tools/docs/", "docs/", "android/", "ops/monitoring/")):
        return False
    if path.endswith(".md"):
        return False
    if path.startswith(("frontend/", "backend/", "ops/deploy/tests/", "ops/deploy/security/")):
        return False
    if path.startswith("ops/deploy/"):
        return path != "ops/deploy/tests/npm-production-audit.test.mjs"
    if path.startswith("ops/nginx/"):
        return False
    if path.startswith("ops/"):
        return False
    if path.startswith(("scripts/publish-android-release.ps1",)):
        return False
    if path in {"package.json", "package-lock.json"}:
        return False
    # Unknown files are treated as production inputs so new build inputs fail closed.
    return True


def _production_change(api_json: Callable[[str], Any], source_sha: str) -> tuple[bool, list[str]]:
    commit = _object(api_json(f"/commits/{source_sha}"), "release commit")
    parents = commit.get("parents")
    if not isinstance(parents, list) or len(parents) > 2:
        raise CiGateError("Release commit parent identity is malformed")
    if not parents:
        return True, ["<root-commit>"]
    first_parent = _object(parents[0], "release commit first parent").get("sha")
    if not isinstance(first_parent, str) or SHA_RE.fullmatch(first_parent) is None:
        raise CiGateError("Release commit first parent is malformed")
    comparison = _object(
        api_json(f"/compare/{first_parent}...{source_sha}"),
        "release commit file comparison",
    )
    files = comparison.get("files")
    if not isinstance(files, list) or len(files) >= 300:
        raise CiGateError("Release commit changed-file list is missing or truncated")
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            raise CiGateError("Release commit changed-file list is malformed")
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            raise CiGateError("Release commit contains a malformed changed-file path")
        paths.append(filename)
        previous = item.get("previous_filename")
        if previous is not None:
            if not isinstance(previous, str) or not previous:
                raise CiGateError("Release commit contains a malformed renamed-file path")
            paths.append(previous)
    return any(_is_production_path(path) for path in paths), paths


def verify_ci_release(
    api_json: Callable[[str], Any],
    *,
    source_sha: str,
    repository: str = CANONICAL_REPOSITORY,
    workflow_file: str = "ci.yml",
    expected_workflow_blob_sha: str,
    short_run_id: int | None = None,
    short_run_attempt: int | None = None,
) -> dict[str, Any]:
    """Require exact main SHORT plus a merged PR's pre-merge LONG-tested identical tree."""
    if SHA_RE.fullmatch(source_sha) is None:
        raise CiGateError("Release source SHA is malformed")
    if SHA_RE.fullmatch(expected_workflow_blob_sha) is None:
        raise CiGateError("Approved CI workflow blob SHA is missing or malformed")
    if repository.lower() != CANONICAL_REPOSITORY.lower() or workflow_file != "ci.yml":
        raise CiGateError("Repository or workflow differs from the RomanLMS release policy")
    workflow = _object(api_json(f"/actions/workflows/{urllib.parse.quote(workflow_file, safe='')}"), "CI workflow")
    workflow_id = _positive_int(workflow.get("id"), "CI workflow ID")
    if workflow.get("path") != CI_WORKFLOW_PATH or workflow.get("state") != "active":
        raise CiGateError("Authoritative RomanLMS CI workflow is missing or inactive")

    main = _object(api_json("/branches/main"), "canonical main branch")
    main_commit = _object(main.get("commit"), "canonical main commit")
    if main_commit.get("sha") != source_sha:
        raise CiGateError("Release source is not the current canonical main commit")
    if _workflow_blob_sha(api_json, source_sha) != expected_workflow_blob_sha:
        raise CiGateError("CI workflow content does not match the root-approved workflow identity")

    short_query = urllib.parse.urlencode(
        {"head_sha": source_sha, "branch": "main", "event": "push", "per_page": 100}
    )
    short_runs = _list_runs(
        api_json,
        f"/actions/workflows/{workflow_id}/runs?{short_query}",
        "main SHORT",
    )
    short_runs = [
        run for run in short_runs
        if run.get("head_sha") == source_sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("workflow_id") == workflow_id
        and run.get("path") == CI_WORKFLOW_PATH
    ]
    short_run = _latest_run(short_runs, "main SHORT")
    _require_run_identity(
        short_run,
        workflow_id=workflow_id,
        source_sha=source_sha,
        branch="main",
        event="push",
        profile="SHORT",
        repository=repository,
    )
    selected_short_run_id = _positive_int(short_run.get("id"), "main SHORT run ID")
    selected_short_attempt = _positive_int(short_run.get("run_attempt"), "main SHORT run attempt")
    if short_run_id is not None and selected_short_run_id != short_run_id:
        raise CiGateError("Triggered workflow is not the latest exact-SHA main SHORT run")
    if short_run_attempt is not None and selected_short_attempt != short_run_attempt:
        raise CiGateError("Triggered workflow attempt differs from the latest exact-SHA main SHORT run")
    _required_job(api_json, selected_short_run_id, selected_short_attempt)
    short_finished = _timestamp(short_run.get("updated_at") or short_run.get("created_at"), "main SHORT")
    short_created = _timestamp(short_run.get("created_at"), "main SHORT creation")

    main_tree = _tree_sha(api_json, source_sha, "main release")
    associated = api_json(f"/commits/{source_sha}/pulls?per_page=100")
    if not isinstance(associated, list) or len(associated) >= 100:
        raise CiGateError("Merged pull request association is missing, malformed, or truncated")

    eligible_prs: list[dict[str, Any]] = []
    for pr in associated:
        if not isinstance(pr, dict):
            raise CiGateError("Pull request association contains a malformed record")
        base = pr.get("base")
        head = pr.get("head")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        head_repo = head.get("repo") if isinstance(head, dict) else None
        user = pr.get("user")
        if (
            pr.get("state") == "closed"
            and pr.get("merged_at")
            and pr.get("merge_commit_sha") == source_sha
            and isinstance(base, dict)
            and base.get("ref") == "main"
            and isinstance(base_repo, dict)
            and str(base_repo.get("full_name", "")).lower() == repository.lower()
            and isinstance(head, dict)
            and isinstance(head_repo, dict)
            and str(head_repo.get("full_name", "")).lower() == repository.lower()
            and isinstance(user, dict)
            and user.get("login") == AUTHORIZED_ACTOR
        ):
            eligible_prs.append(pr)
    if not eligible_prs:
        raise CiGateError("Release commit has no merged owner-authored same-repository PR to main")

    proof: tuple[dict[str, Any], dict[str, Any], str] | None = None
    for pr in eligible_prs:
        head = pr.get("head")
        assert isinstance(head, dict)
        head_sha = head.get("sha")
        head_ref = head.get("ref")
        number = pr.get("number")
        if (
            not isinstance(head_sha, str)
            or SHA_RE.fullmatch(head_sha) is None
            or not isinstance(head_ref, str)
            or not head_ref
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
        ):
            raise CiGateError("Merged pull request head identity is malformed")
        candidate_tree = _tree_sha(api_json, head_sha, "pre-merge candidate")
        if candidate_tree != main_tree:
            continue

        merged_at = _timestamp(pr.get("merged_at"), "pull request merge")
        long_query = urllib.parse.urlencode(
            {"head_sha": head_sha, "branch": head_ref, "per_page": 100}
        )
        long_runs = _list_runs(
            api_json,
            f"/actions/workflows/{workflow_id}/runs?{long_query}",
            "pre-merge LONG",
        )
        long_runs = [
            run for run in long_runs
            if run.get("head_sha") == head_sha
            and run.get("head_branch") == head_ref
            and run.get("workflow_id") == workflow_id
            and run.get("path") == CI_WORKFLOW_PATH
            and run.get("event") in {"push", "pull_request"}
        ]
        long_run = _latest_run(long_runs, "pre-merge LONG")
        _require_run_identity(
            long_run,
            workflow_id=workflow_id,
            source_sha=head_sha,
            branch=head_ref,
            event=str(long_run.get("event", "")),
            profile="LONG",
            repository=repository,
        )
        completed_at = _timestamp(long_run.get("updated_at") or long_run.get("created_at"), "pre-merge LONG")
        if completed_at > merged_at:
            raise CiGateError("LONG CI did not pass before the pull request was merged")
        _required_job(
            api_json,
            _positive_int(long_run.get("id"), "pre-merge LONG run ID"),
            _positive_int(long_run.get("run_attempt"), "pre-merge LONG run attempt"),
        )
        proof = (pr, long_run, candidate_tree)
        break

    if proof is None:
        raise CiGateError("Merged PR tree differs from the exact main release tree; new LONG validation is required")
    pr, long_run, long_tree = proof

    long_finished = _timestamp(long_run.get("updated_at") or long_run.get("created_at"), "pre-merge LONG")
    if short_finished < long_finished:
        raise CiGateError("Main SHORT completed before the trusted pre-merge LONG candidate")
    if short_created < merged_at:
        raise CiGateError("Main SHORT began before the pull request was merged")

    production_change, changed_paths = _production_change(api_json, source_sha)

    return {
        "sourceSha": source_sha,
        "sourceTreeSha": main_tree,
        "shortRunId": selected_short_run_id,
        "shortRunAttempt": selected_short_attempt,
        "longRunId": _positive_int(long_run.get("id"), "pre-merge LONG run ID"),
        "longRunAttempt": _positive_int(long_run.get("run_attempt"), "pre-merge LONG run attempt"),
        "pullRequestNumber": int(pr["number"]),
        "preMergeHeadSha": str(pr["head"]["sha"]),
        "preMergeTreeSha": long_tree,
        "productionChange": production_change,
        "changedPaths": changed_paths,
        "ciWorkflowBlobSha": expected_workflow_blob_sha,
    }


def _github_api(repository: str, token: str) -> Callable[[str], Any]:
    def fetch(path: str) -> Any:
        if not path.startswith("/") or ".." in path.split("/"):
            raise CiGateError("GitHub API request path is invalid")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "romanlms-production-ci-gate",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise CiGateError(f"GitHub API returned HTTP {response.status}")
                body = response.read(1_048_577)
                if len(body) > 1_048_576:
                    raise CiGateError("GitHub API response exceeded its size limit")
            return json.loads(body.decode("utf-8"))
        except CiGateError:
            raise
        except urllib.error.HTTPError as error:
            raise CiGateError(f"GitHub API denied a CI verification request (HTTP {error.code})") from None
        except (OSError, urllib.error.URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as error:
            raise CiGateError(f"GitHub API CI verification failed ({type(error).__name__})") from None

    return fetch


def github_api(repository: str, token: str) -> Callable[[str], Any]:
    """Create a bounded read-only GitHub API client for the trusted controller."""
    return _github_api(repository, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=CANONICAL_REPOSITORY)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow-file", default="ci.yml")
    parser.add_argument("--expected-workflow-blob-sha", required=True)
    parser.add_argument("--short-run-id", type=int)
    parser.add_argument("--short-run-attempt", type=int)
    parser.add_argument("--token-env", default="GH_TOKEN")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "")
    if not re.fullmatch(r"[A-Za-z0-9_]{20,8192}", token):
        print("RomanLMS CI release gate failed: GitHub read token is missing or malformed", file=sys.stderr)
        return 1
    try:
        result = verify_ci_release(
            _github_api(args.repository, token),
            source_sha=args.source_sha,
            repository=args.repository,
            workflow_file=args.workflow_file,
            expected_workflow_blob_sha=args.expected_workflow_blob_sha,
            short_run_id=args.short_run_id,
            short_run_attempt=args.short_run_attempt,
        )
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        if args.output:
            args.output.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0
    except (CiGateError, OSError) as error:
        print(f"RomanLMS CI release gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
