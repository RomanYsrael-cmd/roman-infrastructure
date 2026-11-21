#!/usr/bin/env python3
"""Check a reusable GitHub deployment request before granting package write access."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "controller"))
from application_registry import RegistryError, validate_registry  # noqa: E402
from romanlms_ci_gate import CiGateError, github_api, verify_ci_release  # noqa: E402


class AuthorizationError(ValueError):
    pass


def _api(repository: str, path: str, token: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repository}/{path}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "roman-platform-release-authorizer",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read(1024 * 1024))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise AuthorizationError("GitHub release authorization could not be verified") from error
    if not isinstance(payload, dict):
        raise AuthorizationError("GitHub returned a malformed release authorization response")
    return payload


def _authorized_operation(app: dict[str, Any], operation: str, component: str) -> bool:
    profile = app["deployment_profile"]
    if operation == "build-artifact":
        return profile == "build-only"
    if profile == "build-only":
        return False
    if operation in {"deploy", "rollback", "unhealthy-test"}:
        return profile in {"static-web", "backend-api"}
    if operation in {"deploy-service", "rollback-service"}:
        return profile in {"worker", "scheduled-job", "stateful-service", "game-server"}
    if operation in {"deploy-stack", "rollback-stack", "verify-stack", "refresh-stack", "test-stack"}:
        if profile not in {"full-stack", "multi-container"}:
            return False
        if operation == "test-stack":
            return app.get("stack_test_enabled") is True and component in app["components"]
        return True
    if operation in {"verify", "validate-database"}:
        return profile in {"static-web", "backend-api"} if operation == "verify" else (
            isinstance(app.get("database"), dict)
            and app["database"].get("migration_adapter") == "flyway"
        )
    return False


def authorize(args: argparse.Namespace) -> dict[str, Any]:
    try:
        registry = json.loads(args.registry.read_text(encoding="utf-8"))
        validate_registry(registry)
    except (OSError, json.JSONDecodeError, RegistryError) as error:
        raise AuthorizationError("The checked out application registry is invalid") from error
    apps = registry["applications"]
    app = apps.get(args.app_id)
    if not isinstance(app, dict):
        raise AuthorizationError("The application is not registered")
    if app.get("deployment_profile") == "build-only" and args.operation != "build-artifact":
        raise AuthorizationError("The build-only application cannot be used for server deployment")
    if app["repository"] != args.repository or app["workflow_repository"] != args.repository:
        raise AuthorizationError("The calling repository is not registered for this application")
    if not re.fullmatch(r"[0-9a-f]{40}", args.platform_sha):
        raise AuthorizationError("The platform reusable workflow must be pinned to a full commit SHA")
    if args.platform_sha not in app.get("allowed_platform_shas", []):
        raise AuthorizationError("The pinned reusable workflow is not in the root application allowlist")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        raise AuthorizationError("A full source commit SHA is required")
    if args.branch != app["production_branch"] or not _authorized_operation(app, args.operation, args.component):
        raise AuthorizationError("The operation, application profile, or production branch is not authorized")
    if args.release_mode not in {"manual", "automatic"}:
        raise AuthorizationError("The release mode is invalid")
    if args.release_mode == "automatic":
        if (
            app.get("automatic_release_enabled") is not True
            or args.operation not in app.get("automatic_operations", [])
            or args.event != app.get("automatic_workflow_event")
            or not args.trigger_run_id.isdigit()
            or not args.trigger_run_attempt.isdigit()
        ):
            raise AuthorizationError("Automatic releases are not enabled for this operation")
    elif args.event != app.get("workflow_event", "workflow_dispatch"):
        raise AuthorizationError("Manual releases must use the registered workflow event")

    phrases = app.get("confirmation_phrases", {})
    expected_phrase = (
        app.get("automatic_confirmation_phrase")
        if args.release_mode == "automatic"
        else phrases.get(args.operation) if isinstance(phrases, dict) else None
    )
    if expected_phrase is not None and args.confirmation != expected_phrase:
        raise AuthorizationError("The operation confirmation does not match the root application policy")
    if args.operation in {"deploy", "deploy-service", "deploy-stack", "unhealthy-test", "test-stack", "build-artifact"} and not expected_phrase:
        raise AuthorizationError("The application has no root-registered operation confirmation phrase")

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise AuthorizationError("A scoped GitHub API token is unavailable")
    run = _api(args.repository, f"actions/runs/{args.run_id}", token)
    actor = run.get("actor") or {}
    reusable_path = app["reusable_workflow_path"] + "@" + args.platform_sha
    references = run.get("referenced_workflows") or []
    if (
        run.get("status") != "in_progress"
        or run.get("path") != app["workflow_path"]
        or run.get("event") != args.event
        or run.get("head_branch") != args.branch
        or run.get("head_sha") != args.workflow_sha
        or run.get("run_attempt") != int(args.run_attempt)
        or not isinstance(actor, dict)
        or actor.get("login") != args.actor
        or args.actor not in app.get("authorized_actors", [])
        or not any(
            isinstance(item, dict) and item.get("path") == reusable_path and item.get("sha") == args.platform_sha
            for item in references
        )
    ):
        raise AuthorizationError("The current GitHub workflow run is not authorized for this application")
    branch = _api(args.repository, f"branches/{urllib.parse.quote(args.branch, safe='')}", token)
    if branch.get("commit", {}).get("sha") != args.source_sha:
        raise AuthorizationError("The requested application commit is stale or is not the current production branch")

    if app.get("ci_authorization") == "romanlms-production":
        blob_sha = app.get("required_ci_workflow_blob_sha")
        if not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
            raise AuthorizationError("The registered RomanLMS CI workflow identity is invalid")
        try:
            proof = verify_ci_release(
                github_api(args.repository, token),
                source_sha=args.source_sha,
                repository=args.repository,
                workflow_file=app["required_ci_workflow_file"],
                expected_workflow_blob_sha=blob_sha,
                short_run_id=int(args.trigger_run_id) if args.release_mode == "automatic" else None,
                short_run_attempt=int(args.trigger_run_attempt) if args.release_mode == "automatic" else None,
            )
        except (CiGateError, ValueError, TypeError) as error:
            raise AuthorizationError("RomanLMS SHORT/LONG CI release authorization failed") from error
        if args.release_mode == "automatic" and proof.get("productionChange") is not True:
            raise AuthorizationError("Automatic release has no validated production application changes")
    elif args.release_mode == "automatic":
        trigger = _api(args.repository, f"actions/runs/{args.trigger_run_id}", token)
        trigger_actor = trigger.get("actor") or {}
        if (
            trigger.get("run_attempt") != int(args.trigger_run_attempt)
            or trigger.get("path") != ".github/workflows/" + app["required_ci_workflow_file"]
            or trigger.get("event") != "push"
            or trigger.get("head_branch") != args.branch
            or trigger.get("head_sha") != args.source_sha
            or trigger.get("status") != "completed"
            or trigger.get("conclusion") != "success"
            or not isinstance(trigger_actor, dict)
            or trigger_actor.get("login") not in app.get("authorized_actors", [])
        ):
            raise AuthorizationError("The exact source CI run for the automatic release did not pass")
    else:
        workflow = app["required_ci_workflow_file"]
        result = _api(args.repository, f"actions/workflows/{workflow}/runs?head_sha={args.source_sha}&event=push&per_page=100", token)
        runs = result.get("workflow_runs")
        matching = [run for run in runs if isinstance(run, dict) and run.get("head_sha") == args.source_sha and run.get("head_branch") == args.branch and run.get("event") == "push" and isinstance(run.get("id"), int)] if isinstance(runs, list) else []
        if not matching:
            raise AuthorizationError("The required CI workflow has not run for the exact source commit")
        latest = max(matching, key=lambda item: (item["id"], item.get("run_attempt", 0)))
        ci_actor = latest.get("actor") or {}
        if latest.get("status") != "completed" or latest.get("conclusion") != "success" or not isinstance(ci_actor, dict) or ci_actor.get("login") not in app.get("authorized_actors", []):
            raise AuthorizationError("The latest required CI run for the exact source commit did not pass")
    return {
        "app_id": args.app_id,
        "source_sha": args.source_sha,
        "operation": args.operation,
        "environment": app["environment"],
        "strategy": app["strategy"],
        **({"build_system": app["artifact_build"]["system"]} if app["deployment_profile"] == "build-only" else {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=__import__("pathlib").Path, required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--component", default="")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--platform-sha", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--release-mode", choices=("manual", "automatic"), required=True)
    parser.add_argument("--trigger-run-id", default="")
    parser.add_argument("--trigger-run-attempt", default="")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args(argv)
    try:
        authorized = authorize(args)
    except AuthorizationError as error:
        print(f"release authorization failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(authorized, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
