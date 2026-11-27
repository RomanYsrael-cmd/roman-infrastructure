#!/usr/bin/env python3
"""Restricted, allowlist-based deployment entry point for roman-server."""

from __future__ import annotations

import contextlib
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from application_registry import RegistryError, validate_registry
from forward_migration_plan import (
    ForwardMigrationPlan,
    MigrationPlanError,
    bind_flyway_checksums,
    migration_artifact_checksums,
    validate_forward_plan,
    validate_migration_result,
)
from oidc_provenance import ProvenanceError, verify_release_provenance
from romanlms_ci_gate import CiGateError, github_api, verify_ci_release


CONFIG_PATH = Path("/etc/roman-platform/applications.json")
MAX_REQUEST_BYTES = 32 * 1024
PRIVATE_NETWORK_READY_TIMEOUT_SECONDS = 5
PRIVATE_NETWORK_POLL_INTERVAL_SECONDS = 0.1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
PROVENANCE_OPERATIONS = frozenset({
    "deploy", "deploy-service", "unhealthy-test", "validate-database",
    "deploy-stack", "test-stack",
})


def _validate_romanlms_capacity_policy(app: dict[str, Any]) -> None:
    if app.get("app_id") != "romanlms-production":
        return
    if app.get("single_backend_maintenance") is not True:
        raise DeploymentError("RomanLMS production requires the single-backend maintenance rollout policy.")
    if (
        app.get("automatic_workflow_event") != "workflow_run"
        or not isinstance(app.get("automatic_release_enabled"), bool)
        or not isinstance(app.get("required_ci_workflow_blob_sha"), str)
        or not SHA_RE.fullmatch(app["required_ci_workflow_blob_sha"])
    ):
        raise DeploymentError("RomanLMS production requires a root-approved automatic release and CI workflow policy.")
    components = app.get("components")
    backend = components.get("backend") if isinstance(components, dict) else None
    slots = backend.get("container_environment_by_slot") if isinstance(backend, dict) else None
    for slot in ("blue", "green"):
        environment = slots.get(slot) if isinstance(slots, dict) else None
        if not isinstance(environment, dict) or environment.get("DB_POOL_MAX_SIZE") != "50" or environment.get("DB_POOL_MIN_IDLE") != "2":
            raise DeploymentError("RomanLMS production backend slots must set Hikari maximum 50 and minimum idle 2.")


class DeploymentError(Exception):
    """A safe, user-facing deployment rejection or operational failure."""


class MigrationOutcomeUnknown(DeploymentError):
    """Flyway may still be running, so the old backend must remain stopped."""


def _text(value: Any, name: str, limit: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value or "\r" in value:
        raise DeploymentError(f"Invalid {name}.")
    return value


def _canonical_json_sha256(value: Any) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DeploymentError("The registered build specification is invalid.") from exc
    return hashlib.sha256(payload).hexdigest()


def _provenance_image_map(request: dict[str, Any]) -> dict[str, str]:
    operation = request["operation"]
    if operation == "deploy-stack":
        images = dict(request["images"])
    elif operation == "test-stack":
        images = {request["component"]: request["image"]}
    elif operation in {"validate-database", "refresh-stack"}:
        images = {"migrator": request["migration_image"]}
    else:
        images = {"main": request["image"]}
    if operation in {"deploy-stack", "validate-database"} and request.get("migration_image"):
        images["migrator"] = request["migration_image"]
    return images


def _secret(value: Any, name: str) -> str:
    if not isinstance(value, str) or not 20 <= len(value) <= 8192 or "\n" in value or "\r" in value:
        raise DeploymentError(f"Missing or invalid {name}.")
    return value


def validate_request(request: Any, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(request, dict):
        raise DeploymentError("Request must be a JSON object.")

    operation = request.get("operation")
    common = {
        "operation", "app_id", "repository", "branch", "commit_sha",
        "workflow_sha", "platform_sha", "environment", "release", "github_token",
    }
    operation_fields = {
        "deploy": {"image", "strategy", "registry_token"},
        "deploy-service": {"image", "strategy", "registry_token"},
        "rollback-service": set(),
        "unhealthy-test": {"image", "registry_token"},
        "validate-database": {"migration_image", "registry_token"},
        "deploy-stack": {"images", "strategy", "registry_token"},
        "refresh-stack": set(),
        "rollback-stack": set(),
        "test-stack": {"image", "component", "registry_token"},
        "verify-stack": set(),
        "verify": set(),
        "rollback": set(),
    }
    if operation not in operation_fields:
        raise DeploymentError("Request contains unsupported fields or operation.")
    optional_fields = {"migration_image"} if operation == "deploy-stack" else set()
    provenance_fields = {"build_spec_sha256", "provenance_token"} if operation in PROVENANCE_OPERATIONS else set()
    allowed = common | operation_fields[operation] | optional_fields | provenance_fields
    if set(request) - allowed:
        raise DeploymentError("Request contains unsupported fields or operation.")
    required = common | operation_fields[operation]
    if required - set(request):
        raise DeploymentError("Request is missing required deployment metadata.")

    app_id = _text(request["app_id"], "application identifier", 48)
    if not APP_ID_RE.fullmatch(app_id):
        raise DeploymentError("Invalid application identifier.")
    try:
        app = config["applications"][app_id]
    except (KeyError, TypeError):
        raise DeploymentError("Application is not on the server allowlist.") from None

    _validate_romanlms_capacity_policy(app)

    if request["repository"] != app["repository"]:
        raise DeploymentError("Repository is not authorized for this application.")
    if request["branch"] != app["production_branch"]:
        raise DeploymentError("Only the configured production branch is eligible.")
    if request["environment"] != app["environment"]:
        raise DeploymentError("Deployment environment is not authorized.")
    commit_sha = _text(request["commit_sha"], "commit SHA", 40)
    if not SHA_RE.fullmatch(commit_sha):
        raise DeploymentError("Commit must be a full 40-character lowercase Git SHA.")
    workflow_sha = _text(request["workflow_sha"], "workflow commit SHA", 40)
    if not SHA_RE.fullmatch(workflow_sha):
        raise DeploymentError("Workflow commit must be a full 40-character lowercase Git SHA.")
    platform_sha = _text(request["platform_sha"], "reusable workflow SHA", 40)
    if not SHA_RE.fullmatch(platform_sha):
        raise DeploymentError("Reusable workflow must be pinned to a full commit SHA.")
    if platform_sha not in app.get("allowed_platform_shas", []):
        raise DeploymentError("Reusable workflow version is not on the server allowlist.")

    release = request["release"]
    base_release_fields = {"run_id", "run_attempt", "actor"}
    automatic_release_fields = {"mode", "trigger_run_id", "trigger_run_attempt"}
    if (
        not isinstance(release, dict)
        or not base_release_fields.issubset(release)
        or set(release) - base_release_fields - automatic_release_fields
    ):
        raise DeploymentError("Request is missing approved release metadata.")
    for key in ("run_id", "run_attempt"):
        if not isinstance(release[key], str) or not release[key].isdigit() or len(release[key]) > 20:
            raise DeploymentError("Invalid workflow run metadata.")
    _text(release["actor"], "workflow actor", 100)
    release_mode = release.get("mode", "manual")
    if release_mode not in {"manual", "automatic"}:
        raise DeploymentError("Invalid release mode.")
    if release_mode == "automatic":
        automatic_operations = app.get("automatic_operations", [])
        if (
            app.get("automatic_release_enabled") is not True
            or not isinstance(automatic_operations, list)
            or operation not in automatic_operations
            or not automatic_release_fields.issubset(release)
        ):
            raise DeploymentError("Automatic release is not enabled for this application and operation.")
        for key in ("trigger_run_id", "trigger_run_attempt"):
            if not isinstance(release[key], str) or not release[key].isdigit() or len(release[key]) > 20:
                raise DeploymentError("Invalid triggering CI run metadata.")
    elif set(release) - base_release_fields - {"mode"}:
        raise DeploymentError("Manual releases cannot include automatic CI trigger metadata.")
    _secret(request["github_token"], "GitHub token")

    if operation in PROVENANCE_OPERATIONS and app.get("provenance_mode") == "github-oidc":
        if provenance_fields - set(request):
            raise DeploymentError("Request is missing its signed build provenance.")
        registered_build = app.get("build")
        build_hash = _text(request.get("build_spec_sha256"), "build specification SHA256", 64)
        if not re.fullmatch(r"[0-9a-f]{64}", build_hash) or not isinstance(registered_build, dict):
            raise DeploymentError("The root-registered build specification is unavailable or invalid.")
        if build_hash != _canonical_json_sha256(registered_build):
            raise DeploymentError("The release build specification differs from the root application registry.")
        token = _text(request.get("provenance_token"), "GitHub OIDC provenance token", 12_000)
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise DeploymentError("GitHub OIDC provenance token is malformed.")

    requires_flyway_image = operation in {"validate-database", "deploy-stack"} and app.get("database", {}).get("migration_adapter") == "flyway"
    if requires_flyway_image and "migration_image" not in request:
        raise DeploymentError("A verified Flyway image is required to inspect this application's database history.")
    if operation in {"deploy", "deploy-service", "unhealthy-test"}:
        if operation == "deploy-service" and app.get("deployment_profile") not in {"worker", "scheduled-job", "stateful-service", "game-server"}:
            raise DeploymentError("The application profile has no approved service deployment adapter.")
        if operation == "unhealthy-test" and not app.get("unhealthy_test_enabled"):
            raise DeploymentError("Unhealthy-candidate testing is not enabled for this application.")
        if operation in {"deploy", "deploy-service"} and request.get("strategy") != app["strategy"]:
            raise DeploymentError("Deployment strategy does not match server policy.")
        image = _text(request["image"], "image digest reference", 300)
        image_namespace = re.escape(app["image_namespace"])
        if not re.fullmatch(image_namespace + r"@sha256:[0-9a-f]{64}", image):
            raise DeploymentError("Image must use the allowlisted GHCR package and immutable digest.")
        _secret(request["registry_token"], "registry token")
    elif operation == "rollback-service":
        if app.get("deployment_profile") not in {"worker", "scheduled-job", "stateful-service", "game-server"}:
            raise DeploymentError("The application profile has no approved service rollback adapter.")
    elif operation in {"validate-database", "deploy-stack"} and (
        operation == "validate-database" or "migration_image" in request
    ):
        namespace = app.get("migration_image_namespace")
        if not isinstance(namespace, str) or not namespace:
            raise DeploymentError("Database validation is not configured for this application.")
        image = _text(request["migration_image"], "migration image digest reference", 300)
        if not re.fullmatch(re.escape(namespace) + r"@sha256:[0-9a-f]{64}", image):
            raise DeploymentError("Migration image must use the allowlisted GHCR package and immutable digest.")
        _secret(request["registry_token"], "registry token")

    if operation in {"deploy-stack", "rollback-stack"}:
        components = app.get("components")
        if not isinstance(components, dict) or not components:
            raise DeploymentError("Application components are not configured.")
        if operation == "deploy-stack":
            if request["strategy"] != app["strategy"]:
                raise DeploymentError("Deployment strategy does not match server policy.")
            images = request["images"]
            if not isinstance(images, dict) or set(images) != set(components):
                raise DeploymentError("A digest is required for every approved application component.")
            for name, component in components.items():
                if not isinstance(component, dict) or not isinstance(component.get("image_namespace"), str):
                    raise DeploymentError("Application component image policy is invalid.")
                image = _text(images[name], "component image digest", 300)
                if not re.fullmatch(re.escape(component["image_namespace"]) + r"@sha256:[0-9a-f]{64}", image):
                    raise DeploymentError("Component image must use an allowlisted immutable digest.")
    elif operation == "test-stack":
        components = app.get("components")
        name = request.get("component")
        if not app.get("stack_test_enabled") or not isinstance(components, dict) or not isinstance(name, str) or name not in components:
            raise DeploymentError("The requested application failure test is not enabled.")
        namespace = components[name].get("image_namespace") if isinstance(components[name], dict) else None
        image = _text(request["image"], "component image digest", 300)
        if not isinstance(namespace, str) or not re.fullmatch(re.escape(namespace) + r"@sha256:[0-9a-f]{64}", image):
            raise DeploymentError("Component image must use an allowlisted immutable digest.")
        _secret(request["registry_token"], "registry token")
    elif operation == "verify-stack" and not isinstance(app.get("components"), dict):
        raise DeploymentError("Application components are not configured.")
    elif operation == "refresh-stack":
        if app.get("runtime_refresh_enabled") is not True or not isinstance(app.get("components"), dict) or not app["components"]:
            raise DeploymentError("Runtime-only refresh is not enabled for this application.")

    return app, request


def github_json(repository: str, path: str, token: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repository}/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "roman-platform-deployer",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read(1024 * 1024))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise DeploymentError("Could not verify the release against GitHub.") from exc
    if not isinstance(payload, dict):
        raise DeploymentError("GitHub returned an invalid verification response.")
    return payload


def verify_release_origin(request: dict[str, Any], app: dict[str, Any]) -> None:
    release_mode = request["release"].get("mode", "manual")
    if release_mode == "automatic" and (
        app.get("automatic_release_enabled") is not True
        or app.get("automatic_workflow_event") != "workflow_run"
        or request["operation"] not in app.get("automatic_operations", [])
    ):
        raise DeploymentError("Automatic release is not enabled by the server policy.")
    if release_mode == "automatic" and (
        request["release"].get("actor") not in app.get("authorized_actors", [])
        or not request["release"].get("trigger_run_id")
        or not request["release"].get("trigger_run_attempt")
    ):
        raise DeploymentError("Automatic release trigger is not authorized by server policy.")

    # Check the signed, image-bound identity as soon as the request reaches the
    # controller. The Actions token is short-lived; defer no signature, issuer,
    # audience, or claim checks until after the GitHub API and CI lookups below.
    if request["operation"] in PROVENANCE_OPERATIONS and app.get("provenance_mode") == "github-oidc":
        try:
            verify_release_provenance(
                request["provenance_token"],
                request,
                app,
                _provenance_image_map(request),
                request["build_spec_sha256"],
            )
        except ProvenanceError as exc:
            raise DeploymentError(f"Image provenance verification failed: {exc}.") from exc

    run_id = request["release"]["run_id"]
    run = github_json(
        app["workflow_repository"],
        f"actions/runs/{run_id}",
        request["github_token"],
    )
    expected_path = app["workflow_path"]
    if release_mode == "automatic":
        expected_event = app["automatic_workflow_event"]
    else:
        expected_event = app["workflow_event"]
    actor = run.get("actor") or {}
    actor_login = actor.get("login") if isinstance(actor, dict) else None
    workflow_refs = run.get("referenced_workflows") or []
    expected_reusable_path = app["reusable_workflow_path"] + "@" + request["platform_sha"]
    reusable_is_pinned = any(
        item.get("path") == expected_reusable_path
        and item.get("sha") == request["platform_sha"]
        for item in workflow_refs
        if isinstance(item, dict)
    )
    if (
        run.get("event") != expected_event
        or run.get("status") != "in_progress"
        or run.get("head_branch") != app["production_branch"]
        or run.get("head_sha") != request["workflow_sha"]
        or run.get("run_attempt") != int(request["release"]["run_attempt"])
        or actor_login != request["release"]["actor"]
        or actor_login not in app.get("authorized_actors", [])
        or run.get("path") != expected_path
        or not reusable_is_pinned
    ):
        raise DeploymentError("Workflow run is not an authorized in-progress production release.")

    workflow_branch = github_json(
        app["workflow_repository"],
        f"branches/{app['production_branch']}",
        request["github_token"],
    )
    if workflow_branch.get("commit", {}).get("sha") != request["workflow_sha"]:
        raise DeploymentError("Workflow commit is not the current head of the approved branch.")

    repo = app["repository"]
    branch = app["production_branch"]
    branch_info = github_json(repo, f"branches/{branch}", request["github_token"])
    current_sha = branch_info.get("commit", {}).get("sha")
    if current_sha != request["commit_sha"]:
        raise DeploymentError("Release commit is not the current head of the approved production branch.")
    commit_info = github_json(repo, f"commits/{request['commit_sha']}", request["github_token"])
    if commit_info.get("sha") != request["commit_sha"]:
        raise DeploymentError("GitHub did not confirm the requested commit.")
    if request["operation"] in {"deploy", "deploy-service", "deploy-stack", "rollback-stack", "test-stack", "verify-stack", "refresh-stack"}:
        verify_required_ci(request, app)


def verify_required_ci(request: dict[str, Any], app: dict[str, Any]) -> None:
    workflow = app.get("required_ci_workflow_file")
    if not isinstance(workflow, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow):
        raise DeploymentError("The required CI workflow is not configured.")
    if app.get("ci_authorization") == "romanlms-production":
        expected_blob_sha = app.get("required_ci_workflow_blob_sha")
        if not isinstance(expected_blob_sha, str) or not SHA_RE.fullmatch(expected_blob_sha):
            raise DeploymentError("The root-approved RomanLMS CI workflow identity is not configured.")
        release = request["release"]
        try:
            proof = verify_ci_release(
                github_api(app["repository"], request["github_token"]),
                source_sha=request["commit_sha"],
                repository=app["repository"],
                workflow_file=workflow,
                expected_workflow_blob_sha=expected_blob_sha,
                short_run_id=int(release["trigger_run_id"]) if release.get("mode") == "automatic" else None,
                short_run_attempt=int(release["trigger_run_attempt"]) if release.get("mode") == "automatic" else None,
            )
        except (CiGateError, ValueError, TypeError) as exc:
            raise DeploymentError(f"RomanLMS CI release authorization failed: {exc}") from exc
        if release.get("mode") == "automatic" and proof.get("productionChange") is not True:
            raise DeploymentError("Automatic release has no production application changes.")
        return
    payload = github_json(app["repository"], f"actions/workflows/{workflow}/runs?head_sha={request['commit_sha']}&event=push&per_page=20", request["github_token"])
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise DeploymentError("The required CI result is unavailable.")
    matching = [item for item in runs if isinstance(item, dict) and item.get("head_sha") == request["commit_sha"] and item.get("head_branch") == app["production_branch"] and item.get("event") == "push" and isinstance(item.get("id"), int)]
    if not matching:
        raise DeploymentError("The required CI has not run for this release commit.")
    latest = max(matching, key=lambda item: (item["id"], item.get("run_attempt") if isinstance(item.get("run_attempt"), int) else 0))
    actor = latest.get("actor") or {}
    if latest.get("status") != "completed" or latest.get("conclusion") != "success" or not isinstance(actor, dict) or actor.get("login") not in app.get("authorized_actors", []):
        raise DeploymentError("The latest required CI run for this release did not pass under an authorized actor.")


def run_command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 60,
    check: bool = True,
    redactions: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError(f"Could not run required tool: {Path(args[0]).name}.") from exc
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "command failed").strip()[-700:]
        for secret in redactions:
            if secret:
                message = message.replace(secret, "[redacted]")
        raise DeploymentError(f"{Path(args[0]).name} failed: {message}")
    return result


def available_memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    raise DeploymentError("Unable to determine available memory; refusing deployment.")


def docker_root() -> str:
    result = run_command(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=15)
    return result.stdout.strip()


def service_is_active(service: str) -> bool:
    result = run_command(["systemctl", "is-active", "--quiet", service], timeout=10, check=False)
    return result.returncode == 0


def _secure_env_file(path_value: Any) -> Path:
    if not isinstance(path_value, str) or not path_value.startswith("/"):
        raise DeploymentError("A root-controlled environment file is not configured.")
    path = Path(path_value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeploymentError("A required root-controlled environment file is unavailable.") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise DeploymentError("A required environment file is not a private regular file.")
    if os.name == "posix" and info.st_uid != 0:
        raise DeploymentError("A required environment file is not root-owned.")
    return path


def _env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DeploymentError("A required environment file cannot be read.") from exc
    for line in lines:
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise DeploymentError("A required environment file has invalid syntax.")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or "\n" in value or "\r" in value:
            raise DeploymentError("A required environment file has invalid syntax.")
        values[key] = value
    return values


def _migration_database_env(app: dict[str, Any], runtime_values: dict[str, str]) -> tuple[Path, dict[str, str]]:
    path = _secure_env_file(app.get("migration_database_env_file"))
    values = _env_file_values(path)
    required = {"DB_URL", "DB_USERNAME", "DB_PASSWORD"}
    configured_user = app.get("migration_database_username")
    runtime_user = runtime_values.get("DB_USERNAME", "")
    expected_url = runtime_values.get("DB_URL", "")
    allowed_urls = app.get("allowed_database_urls")
    if (
        set(values) != required
        or any(not values.get(key, "").strip() for key in required)
        or not isinstance(configured_user, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", configured_user)
        or values.get("DB_USERNAME") != configured_user
        or configured_user == runtime_user
        or configured_user.lower() in {"postgres", "root", "admin"}
        or not re.fullmatch(r"jdbc:postgresql://[^\s/]+/[A-Za-z0-9_]+", values.get("DB_URL", ""))
        or values.get("DB_URL") != expected_url
        or not isinstance(allowed_urls, list)
        or values.get("DB_URL") not in allowed_urls
    ):
        raise DeploymentError("The dedicated migration database credentials are invalid or target a different database.")
    return path, values


def _image_source_revision(request: dict[str, Any], app: dict[str, Any]) -> str:
    if app.get("image_revision_mode", "platform") == "caller":
        return request["commit_sha"]
    return request["platform_sha"]


def _verify_image(image: str, request: dict[str, Any], app: dict[str, Any], env: dict[str, str]) -> None:
    verify_image_labels(
        image,
        _image_source_revision(request, app),
        request["commit_sha"],
        request["platform_sha"],
        app["image_package_repository"],
        app["image_source_repository"],
        env,
    )


def preflight(app: dict[str, Any], *, capacity_checks: bool = True, memory_check: bool = True) -> None:
    require_storage_mounts(app)
    root = os.path.realpath(docker_root())
    expected_root = os.path.realpath(app["docker_root_prefix"])
    if root != expected_root and not root.startswith(expected_root.rstrip(os.sep) + os.sep):
        raise DeploymentError("Docker storage is outside the approved applications SSD.")
    storage_root = app["storage_root"]
    if not os.path.isdir(storage_root):
        raise DeploymentError("Approved application storage directory is missing.")
    if capacity_checks:
        free_bytes = shutil.disk_usage(storage_root).free
        if free_bytes < int(app["minimum_free_gib"]) * 1024**3:
            raise DeploymentError("Insufficient free space on the applications SSD.")
        if os.cpu_count() is None or os.cpu_count() < int(app["minimum_cpu_cores"]):
            raise DeploymentError("Insufficient CPU capacity for the configured deployment strategy.")
    if memory_check:
        free_memory_mb = available_memory_bytes() // (1024 * 1024)
        if free_memory_mb < int(app["minimum_available_memory_mb"]):
            raise DeploymentError("Insufficient available memory for the candidate release.")
    for service in app["required_services"]:
        if not service_is_active(service):
            raise DeploymentError(f"Required service is not active: {service}.")
    network = app.get("container_network")
    network_mode = app.get("container_network_mode", "bridge")
    if network_mode == "host":
        if not app.get("allow_host_network") or network != "host":
            raise DeploymentError("Host networking is not explicitly authorized for this application.")
        result = run_command(
            ["docker", "network", "inspect", "--format", "{{.Name}}", "host"],
            timeout=15,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "host":
            raise DeploymentError("The required Docker host network is unavailable.")
    elif network:
        if not isinstance(network, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", network):
            raise DeploymentError("The configured private container network name is invalid.")
        result = run_command(
            ["docker", "network", "inspect", "--format", "{{.Internal}}", network],
            timeout=15,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip().lower() != "true":
            raise DeploymentError("The required container network is missing or not isolated from external networks.")
        if app.get("network_subnet"):
            result = run_command(["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", network], timeout=15, check=False)
            try:
                entries = json.loads(result.stdout)
                subnets = {entry["Subnet"] for entry in entries if isinstance(entry, dict) and "Subnet" in entry}
                expected_subnet = str(ipaddress.ip_network(app["network_subnet"], strict=True))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise DeploymentError("The approved private network subnet is invalid.") from exc
            if result.returncode != 0 or subnets != {expected_subnet}:
                raise DeploymentError("The isolated network subnet does not match server policy.")
            gateways = {entry.get("Gateway") for entry in entries if isinstance(entry, dict) and entry.get("Gateway")}
            component_ips = {address for component in app.get("components", {}).values() for address in component.get("slot_ips", {}).values()}
            if gateways & component_ips:
                raise DeploymentError("A component address conflicts with the private network gateway.")
    for container in app.get("required_healthy_containers", []):
        if not isinstance(container, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", container):
            raise DeploymentError("The required service container name is invalid.")
        result = run_command(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            timeout=15,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip().lower() != "healthy":
            raise DeploymentError(f"Required service container is not healthy: {container}.")
    for key in ("container_env_file", "database_env_file"):
        if app.get(key):
            _secure_env_file(app[key])
    marker = app.get("backup_marker")
    if marker:
        path = Path(marker)
        max_age = int(app["backup_max_age_hours"]) * 3600
        if not path.is_file() or time.time() - path.stat().st_mtime > max_age:
            raise DeploymentError("The application backup gate is not satisfied.")
        if app.get("backup_marker_expected_source_sha256"):
            _secure_env_file(marker)
            try:
                evidence = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DeploymentError("The staging restore evidence is invalid.") from exc
            if not isinstance(evidence, dict) or evidence.get("source_sha256") != app["backup_marker_expected_source_sha256"] or evidence.get("schema_version") != str(app["restore_expected_schema_version"]):
                raise DeploymentError("The staging restore evidence does not match the approved backup and schema.")


@contextlib.contextmanager
def registry_session(app: dict[str, Any], request: dict[str, Any]):
    runtime_tmp = app["runtime_tmp_dir"]
    Path(runtime_tmp).mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ghcr-", dir=runtime_tmp) as docker_config:
        os.chmod(docker_config, 0o700)
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = docker_config
        token = request["registry_token"]
        actor = request["release"]["actor"]
        run_command(
            ["docker", "login", "ghcr.io", "--username", actor, "--password-stdin"],
            env=env,
            input_text=token + "\n",
            timeout=20,
            redactions=(token,),
        )
        try:
            yield env
        finally:
            run_command(["docker", "logout", "ghcr.io"], env=env, timeout=15, check=False)


def _container_run_args(
    app: dict[str, Any],
    image: str,
    container_name: str,
    slot: str,
    port: int,
    caller_revision: str,
    *,
    test_mode: bool = False,
) -> list[str]:
    restart_policy = "no" if test_mode else app.get("container_restart_policy", "unless-stopped")
    if restart_policy not in {"no", "unless-stopped", "on-failure:3"}:
        raise DeploymentError("The configured container restart policy is invalid.")
    args = [
        "docker", "run", "--detach",
        "--name", container_name,
        "--restart", restart_policy,
        "--read-only",
    ]
    tmpfs_mounts = app.get("container_tmpfs", ["/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777"])
    if not isinstance(tmpfs_mounts, list) or any(not isinstance(item, str) or "\n" in item or "\r" in item for item in tmpfs_mounts):
        raise DeploymentError("The configured temporary mounts are invalid.")
    for mount in tmpfs_mounts:
        args.extend(["--tmpfs", mount])
    args.extend([
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(int(app["pids_limit"])),
        "--memory", app["container_memory"],
        "--cpus", str(app["container_cpus"]),
    ])
    network = app.get("container_network")
    if network:
        args.extend(["--network", network])
    slot_ips = app.get("slot_ips")
    if slot_ips:
        try:
            address = str(ipaddress.IPv4Address(slot_ips[slot]))
        except (ValueError, KeyError, TypeError) as exc:
            raise DeploymentError("The configured private component address is invalid.") from exc
        args.extend(["--ip", address])
    if app.get("container_publish", True):
        args.extend(["--publish", f"127.0.0.1:{port}:{int(app['container_port'])}"])
    env_file = app.get("container_env_file")
    if env_file:
        secure_env_file = _secure_env_file(env_file)
        args.extend(["--env-file", str(secure_env_file)])
        if app.get("container_secret_file_mounts"):
            args.extend(_secret_file_mount_args(app, _env_file_values(Path(secure_env_file))))
    env_sets = app.get("container_environment_by_slot", {})
    if test_mode:
        env_sets = {**env_sets, "test": app.get("unhealthy_test_environment", {})}
    env_values = env_sets.get(slot, {}) if isinstance(env_sets, dict) else {}
    if not isinstance(env_values, dict):
        raise DeploymentError("The configured slot environment is invalid.")
    for key, value in sorted(env_values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str) or "\n" in value or "\r" in value:
            raise DeploymentError("The configured slot environment is invalid.")
        args.extend(["--env", f"{key}={value}"])
    revision_env = app.get("revision_environment_variable")
    if revision_env:
        if not isinstance(revision_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", revision_env) or revision_env in env_values or not SHA_RE.fullmatch(caller_revision):
            raise DeploymentError("The configured release revision environment is invalid.")
        args.extend(["--env", f"{revision_env}={caller_revision}"])
    args.extend([
        "--label", f"roman.platform.app={app['app_id']}",
        "--label", f"roman.platform.slot={slot}",
        "--label", f"io.roman.deployment.caller-revision={caller_revision}",
        image,
    ])
    return args


def _secret_file_mount_args(app: dict[str, Any], environment: dict[str, str]) -> list[str]:
    mounts = app.get("container_secret_file_mounts", [])
    if not isinstance(mounts, list):
        raise DeploymentError("The configured secret-file mounts are invalid.")
    fcm_enabled = environment.get("ROMANLMS_NOTIFICATIONS_FCM_ENABLED", "false").lower() == "true"
    firebase_project = environment.get("FIREBASE_PROJECT_ID", "")
    credentials_path = environment.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    targets: set[str] = set()
    arguments: list[str] = []
    firebase_mount: tuple[str, str] | None = None
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) != {"source", "target", "group_id", "mode"}:
            raise DeploymentError("The configured secret-file mount is invalid.")
        source = _text(mount["source"], "secret-file source", 240)
        target = _text(mount["target"], "secret-file target", 120)
        group_id = mount["group_id"]
        mode = mount["mode"]
        source_prefix = f"/etc/roman-platform/{app.get('root_app_id', app['app_id'])}/"
        if (
            not source.startswith(source_prefix)
            or any(part in {".", ".."} for part in source.split("/"))
            or not re.fullmatch(r"/run/secrets/[A-Za-z0-9_.-]+", target)
            or target in targets
            or isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or not isinstance(mode, str)
            or mode != "0440"
        ):
            raise DeploymentError("The configured secret-file mount is outside the approved policy.")
        targets.add(target)
        source_path = Path(source)
        try:
            metadata = source_path.lstat()
        except FileNotFoundError:
            if fcm_enabled or credentials_path == target:
                raise DeploymentError("The enabled Firebase credential file is missing.") from None
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or source_path.is_symlink()
            or os.path.realpath(source) != source
            or metadata.st_uid != 0
            or metadata.st_gid != group_id
            or stat.S_IMODE(metadata.st_mode) != int(mode, 8)
        ):
            raise DeploymentError("A secret file does not have the required root-owned read-only permissions.")
        if target.endswith("/firebase-service-account.json"):
            firebase_mount = (source, target)
        arguments.extend(["--mount", f"type=bind,src={source},dst={target},readonly"])

    if credentials_path and credentials_path not in targets:
        raise DeploymentError("The configured application-credentials path is not an approved mounted secret.")
    if fcm_enabled:
        if not firebase_project or firebase_mount is None or credentials_path != firebase_mount[1]:
            raise DeploymentError("Firebase delivery requires a project ID and its approved mounted service account.")
        encoded_key = environment.get("NOTIFICATION_DEVICE_TOKEN_ENCRYPTION_KEY", "")
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, TypeError):
            key = b""
        if len(key) != 32:
            raise DeploymentError("Firebase delivery requires a valid 32-byte notification-token encryption key.")
        try:
            with open(firebase_mount[0], encoding="utf-8") as stream:
                service_account = json.load(stream)
        except (OSError, json.JSONDecodeError):
            raise DeploymentError("The Firebase service-account file is invalid.") from None
        if (
            not isinstance(service_account, dict)
            or service_account.get("type") != "service_account"
            or service_account.get("project_id") != firebase_project
            or not isinstance(service_account.get("client_email"), str)
            or not isinstance(service_account.get("private_key"), str)
        ):
            raise DeploymentError("The Firebase service account does not match the configured project.")
    return arguments


def verify_image_labels(
    image: str,
    source_revision: str,
    caller_revision: str,
    platform_sha: str,
    package_repository: str,
    source_repository: str,
    env: dict[str, str],
) -> None:
    result = run_command(
        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
        env=env,
        timeout=15,
    )
    try:
        labels = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise DeploymentError("Image metadata could not be verified.") from exc
    expected_package_repository = f"https://github.com/{package_repository}"
    expected_source_repository = f"https://github.com/{source_repository}"
    if labels.get("org.opencontainers.image.revision") != source_revision:
        raise DeploymentError("Image revision label does not match the approved source revision.")
    if labels.get("org.opencontainers.image.source") != expected_package_repository:
        raise DeploymentError("OCI source label does not match the package repository.")
    if labels.get("io.roman.platform.source-repository") != expected_source_repository:
        raise DeploymentError("Image source-repository label does not match the approved infrastructure repository.")
    if labels.get("io.roman.platform.source-revision") != platform_sha:
        raise DeploymentError("Image platform-source label does not match the pinned reusable workflow.")
    if labels.get("io.roman.deployment.package-repository") != expected_package_repository:
        raise DeploymentError("Image package-repository label does not match the authorized GHCR package repository.")
    if labels.get("io.roman.deployment.caller-revision") != caller_revision:
        raise DeploymentError("Image caller-revision label does not match the authorized source commit.")


def _atomic_write(path: str | Path, content: str, mode: int = 0o640) -> None:
    target = Path(path)
    target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _read_state(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError("Release state is unreadable; refusing to change traffic.") from exc
    if not isinstance(value, dict):
        raise DeploymentError("Release state is invalid; refusing to change traffic.")
    return value


def _append_audit(app: dict[str, Any], event: dict[str, Any]) -> None:
    require_storage_mounts(app)
    path = Path(app["history_file"])
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    safe_event = {key: value for key, value in event.items() if key not in {"github_token", "registry_token"}}
    with path.open("a", encoding="utf-8") as stream:
        os.chmod(path, 0o640)
        stream.write(json.dumps(safe_event, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _write_metrics(
    app: dict[str, Any],
    state: dict[str, Any],
    outcome: str,
    operation: str,
    duration_seconds: float,
    workflow_run_id: str,
    request_sha: str,
) -> None:
    require_storage_mounts(app)
    active = state.get("active") or {}
    app_id = _prometheus_label(app["app_id"])
    operation = _prometheus_label(operation)
    outcome = _prometheus_label(outcome)
    event_labels = (
        f'app_id="{app_id}",operation="{operation}",outcome="{outcome}",'
        f'workflow_run_id="{_prometheus_label(workflow_run_id)}",request_sha="{_prometheus_label(request_sha)}"'
    )
    app_labels = f'app_id="{app_id}"'
    lines = [
        "# HELP roman_platform_last_deployment_timestamp_seconds Unix time of the latest deployment outcome.",
        "# TYPE roman_platform_last_deployment_timestamp_seconds gauge",
        f"roman_platform_last_deployment_timestamp_seconds{{{event_labels}}} {time.time():.0f}",
        "# HELP roman_platform_last_deployment_duration_seconds Duration of the latest deployment operation.",
        "# TYPE roman_platform_last_deployment_duration_seconds gauge",
        f"roman_platform_last_deployment_duration_seconds{{{event_labels}}} {max(0.0, duration_seconds):.3f}",
        "# HELP roman_platform_last_deployment_info Latest deployment operation and outcome.",
        "# TYPE roman_platform_last_deployment_info gauge",
        f"roman_platform_last_deployment_info{{{event_labels}}} 1",
    ]
    if active.get("commit_sha"):
        lines.extend([
            "# HELP roman_platform_active_release_info Active release caller revision.",
            "# TYPE roman_platform_active_release_info gauge",
            f'roman_platform_active_release_info{{{app_labels},revision="{_prometheus_label(active["commit_sha"])}",slot="{_prometheus_label(active["slot"])}"}} 1',
        ])
    if active.get("platform_sha"):
        lines.extend([
            "# HELP roman_platform_active_platform_revision_info Active pinned infrastructure workflow revision.",
            "# TYPE roman_platform_active_platform_revision_info gauge",
            f'roman_platform_active_platform_revision_info{{{app_labels},revision="{_prometheus_label(active["platform_sha"])}"}} 1',
        ])
    # Node Exporter reads this root-owned directory as its unprivileged user.
    # The metric labels contain release metadata only, so the scrape file must
    # be world-readable while remaining atomically replaced by the root controller.
    _atomic_write(app["metrics_file"], "\n".join(lines) + "\n", 0o644)


def _lock(app: dict[str, Any]):
    lock_dir = Path(app["lock_dir"])
    lock_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    if fcntl is None:
        raise DeploymentError("The deployment controller requires Linux file locking.")
    handle = (lock_dir / f"{app['app_id']}.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise DeploymentError("Another deployment for this application is already running.") from None
    return handle


def require_storage_mounts(app: dict[str, Any]) -> None:
    for mount in app["required_mounts"]:
        if not os.path.ismount(mount):
            raise DeploymentError(f"Required storage mount is unavailable: {mount}.")


def _container_name(app: dict[str, Any], slot: str, commit_sha: str) -> str:
    return f"roman-platform-{app['app_id']}-{slot}-{commit_sha[:12]}"


def _wait_http(url: str, expected_text: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=2) as response:
                body = response.read(8192).decode("utf-8", errors="replace")
                if response.status == 200 and expected_text in body:
                    return
                last_error = f"HTTP {response.status} or unexpected response"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} or unexpected response"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            last_error = f"URLError ({type(reason).__name__})" if reason is not None else "URLError"
        except (TimeoutError, OSError) as exc:
            last_error = type(exc).__name__
        time.sleep(1)
    raise DeploymentError(f"Readiness check failed: {last_error}.")


def _candidate_startup_diagnostic(container: str) -> dict[str, Any]:
    """Return a redacted container state and stack signature for failed candidates."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", container):
        return {}
    state_result = run_command(
        ["docker", "inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}|{{.RestartCount}}|{{.State.OOMKilled}}", container],
        timeout=15,
        check=False,
    )
    state: dict[str, Any] = {}
    if state_result.returncode == 0:
        fields = state_result.stdout.strip().split("|")
        if len(fields) == 4:
            status, exit_code, restart_count, oom_killed = fields
            if status in {"created", "restarting", "running", "removing", "paused", "exited", "dead"}:
                state["status"] = status
            if exit_code.isdecimal():
                state["exit_code"] = int(exit_code)
            if restart_count.isdecimal():
                state["restart_count"] = int(restart_count)
            if oom_killed.lower() in {"true", "false"}:
                state["oom_killed"] = oom_killed.lower() == "true"

    logs_result = run_command(["docker", "logs", "--tail", "150", container], timeout=15, check=False)
    logs = (logs_result.stdout or "") + "\n" + (logs_result.stderr or "")
    # Docker/Spring logs may prefix exception lines with timestamps and log
    # levels, so searching only at the start of a line misses the exception.
    # Require the class name to end in a colon; this avoids matching JVM options
    # such as -XX:+ExitOnOutOfMemoryError. Never include exception messages.
    exception_pattern = re.compile(
        r"\b([A-Za-z_$][A-Za-z0-9_.$]*(?:Exception|Error))(?=:)"
    )
    exception_types: list[str] = []
    for match in exception_pattern.finditer(logs):
        class_name = match.group(1)
        if class_name not in exception_types:
            exception_types.append(class_name)
        if len(exception_types) == 3:
            break
    frame_pattern = re.compile(
        r"\bat\s+(com\.romanlms\.[A-Za-z0-9_.$]+)\(([A-Za-z0-9_$.]+\.java:\d+)\)"
    )
    stack_frames: list[str] = []
    for line in logs.splitlines():
        match = frame_pattern.search(line)
        if match:
            frame = f"{match.group(1)}({match.group(2)})"
            if frame not in stack_frames:
                stack_frames.append(frame)
        if len(stack_frames) == 5:
            break
    diagnostic: dict[str, Any] = {}
    if state:
        diagnostic["container_state"] = state
    if exception_types:
        diagnostic["startup_exception_types"] = exception_types
    if stack_frames:
        diagnostic["startup_stack_frames"] = stack_frames
    if "APPLICATION FAILED TO START" in logs:
        diagnostic["spring_boot_failure_report"] = True
    return diagnostic


def _format_candidate_startup_diagnostic(diagnostic: dict[str, Any]) -> str:
    state = diagnostic.get("container_state")
    parts: list[str] = []
    if isinstance(state, dict):
        fields = [
            f"status={state['status']}" if "status" in state else None,
            f"exit_code={state['exit_code']}" if "exit_code" in state else None,
            f"restarts={state['restart_count']}" if "restart_count" in state else None,
            f"oom_killed={str(state['oom_killed']).lower()}" if "oom_killed" in state else None,
        ]
        summary = ",".join(value for value in fields if value)
        if summary:
            parts.append(f"container({summary})")
    exception_types = diagnostic.get("startup_exception_types")
    if isinstance(exception_types, list) and exception_types:
        parts.append("startup_exception_types=" + ",".join(exception_types))
    stack_frames = diagnostic.get("startup_stack_frames")
    if isinstance(stack_frames, list) and stack_frames:
        parts.append("startup_stack_frames=" + ",".join(stack_frames))
    if diagnostic.get("spring_boot_failure_report") is True:
        parts.append("spring_boot_failure_report=true")
    return "; ".join(parts)


def _route_content(app: dict[str, Any], port: int) -> str:
    return (
        f"proxy_pass http://127.0.0.1:{port};\n"
        "proxy_set_header Host $host;\n"
        "proxy_set_header X-Real-IP $remote_addr;\n"
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "proxy_connect_timeout 2s;\n"
        "proxy_read_timeout 10s;\n"
    )


def _stack_route_content(components: dict[str, Any], slot: str) -> str:
    """Render all component routes together so a release switches in one reload."""
    if slot not in {"blue", "green"} or not isinstance(components, dict) or not components:
        raise DeploymentError("The application route policy is invalid.")
    prefixes: set[str] = set()
    health_paths: set[str] = set()
    endpoints: set[tuple[str, int]] = set()
    blocks: list[str] = []
    for name, component in sorted(components.items()):
        if not isinstance(name, str) or not APP_ID_RE.fullmatch(name) or not isinstance(component, dict):
            raise DeploymentError("The application component policy is invalid.")
        prefix = component.get("route_prefix")
        if not isinstance(prefix, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", prefix) or prefix in prefixes:
            raise DeploymentError("The application route prefix is invalid or duplicated.")
        prefixes.add(prefix)
        try:
            if component.get("slot_ips"):
                address = str(ipaddress.IPv4Address(component["slot_ips"][slot]))
                port = component["container_port"]
            else:
                address = "127.0.0.1"
                port = component["slot_ports"][slot]
        except (KeyError, TypeError, ValueError) as exc:
            raise DeploymentError("The application component port is missing.") from exc
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise DeploymentError("The application component port is invalid.")
        if (address, port) in endpoints:
            raise DeploymentError("Application components cannot share a slot endpoint.")
        endpoints.add((address, port))
        modifier = "^~ " if prefix != "/" else ""
        forwarded_proto = component.get("forwarded_proto_variable", "$scheme")
        if forwarded_proto not in {"$scheme", "$http_x_forwarded_proto"}:
            raise DeploymentError("The application forwarded-protocol policy is invalid.")
        proxy_read_timeout = component.get("proxy_read_timeout_seconds", 300)
        if isinstance(proxy_read_timeout, bool) or not isinstance(proxy_read_timeout, int) or not 1 <= proxy_read_timeout <= 3600:
            raise DeploymentError("The application proxy timeout policy is invalid.")
        sse_enabled = component.get("sse_enabled", False)
        if not isinstance(sse_enabled, bool):
            raise DeploymentError("The application event-stream policy is invalid.")
        public_health_path = component.get("public_health_path")
        if public_health_path is not None:
            upstream_health_path = component.get("health_path")
            if (
                not isinstance(public_health_path, str)
                or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", public_health_path)
                or not public_health_path.startswith(prefix)
                or public_health_path in health_paths
                or not isinstance(upstream_health_path, str)
                or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", upstream_health_path)
            ):
                raise DeploymentError("The application public health route is invalid.")
            health_paths.add(public_health_path)
            blocks.append(
                f"location = {public_health_path} {{\n"
                f"    proxy_pass http://{address}:{port}{upstream_health_path};\n"
                "    proxy_http_version 1.1;\n"
                "    proxy_set_header Host $host;\n"
                "    proxy_set_header X-Real-IP $remote_addr;\n"
                "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                f"    proxy_set_header X-Forwarded-Proto {forwarded_proto};\n"
                "    proxy_connect_timeout 2s;\n"
                "    proxy_read_timeout 5s;\n"
                "}\n"
            )
        sse_directives = (
            "    proxy_buffering off;\n"
            "    proxy_cache off;\n"
        ) if sse_enabled else ""
        blocks.append(
            f"location {modifier}{prefix} {{\n"
            f"    proxy_pass http://{address}:{port};\n"
            "    proxy_http_version 1.1;\n"
            "    proxy_set_header Host $host;\n"
            "    proxy_set_header X-Real-IP $remote_addr;\n"
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            f"    proxy_set_header X-Forwarded-Proto {forwarded_proto};\n"
            "    proxy_set_header Upgrade $http_upgrade;\n"
            "    proxy_set_header Connection $http_connection;\n"
            "    proxy_connect_timeout 2s;\n"
            f"    proxy_read_timeout {proxy_read_timeout}s;\n"
            "    proxy_send_timeout 300s;\n"
            "    proxy_request_buffering off;\n"
            f"{sse_directives}"
            "}\n"
        )
    if "/" not in prefixes:
        raise DeploymentError("The application route policy has no root component.")
    return "\n".join(blocks)


def switch_route(app: dict[str, Any], port: int) -> None:
    route_file = Path(app["route_file"])
    try:
        prior = route_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentError("The managed Nginx route file is unavailable.") from exc
    _atomic_write(route_file, _route_content(app, port))
    try:
        run_command(["nginx", "-t"], timeout=20)
        run_command(["systemctl", "reload", "nginx"], timeout=20)
        _wait_http(app["route_health_url"], app["expected_health_text"], int(app["readiness_timeout_seconds"]))
    except Exception:
        _atomic_write(route_file, prior)
        run_command(["nginx", "-t"], timeout=20, check=False)
        run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
        raise


def _active_containers(app: dict[str, Any], slot: str) -> list[str]:
    result = run_command(
        [
            "docker", "ps", "-aq",
            "--filter", f"label=roman.platform.app={app['app_id']}",
            "--filter", f"label=roman.platform.slot={slot}",
        ],
        timeout=15,
        check=False,
    )
    return [item for item in result.stdout.splitlines() if re.fullmatch(r"[0-9a-f]{12,64}", item)]


def _release_container_available(app: dict[str, Any], release: dict[str, Any]) -> bool:
    slot = release.get("slot")
    commit_sha = release.get("commit_sha")
    if slot not in app.get("slot_ips", app.get("slot_ports", {})) or not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        return False
    expected_name = _container_name(app, slot, commit_sha)
    if release.get("container") != expected_name:
        return False
    result = run_command(
        [
            "docker", "inspect", "--format",
            '{{.Name}}|{{index .Config.Labels "roman.platform.app"}}|{{index .Config.Labels "roman.platform.slot"}}',
            expected_name,
        ],
        timeout=15,
        check=False,
    )
    return (
        result.returncode == 0
        and result.stdout.strip() == f"/{expected_name}|{app['app_id']}|{slot}"
    )


def _remove_inactive_slot(app: dict[str, Any], slot: str, active: dict[str, Any] | None) -> None:
    if active and active.get("slot") == slot:
        raise DeploymentError("Refusing to remove the currently active release.")
    containers = _active_containers(app, slot)
    for container in containers:
        run_command(["docker", "rm", "-f", container], timeout=30)


def _deploy(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    state_path = app["state_file"]
    state = _read_state(state_path)
    active = state.get("active")
    if active and active.get("slot") not in app["slot_ports"]:
        raise DeploymentError("Active release state has an invalid slot.")
    candidate_slot = "green" if active and active["slot"] == "blue" else "blue"
    candidate_port = int(app["slot_ports"][candidate_slot])
    candidate_name = _container_name(app, candidate_slot, request["commit_sha"])

    with registry_session(app, request) as env:
        run_command(["docker", "pull", request["image"]], env=env, timeout=600)
        _verify_image(request["image"], request, app, env)

    previous = state.get("previous")
    if previous:
        _remove_inactive_slot(app, previous["slot"], active)
        state["previous"] = None
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    else:
        _remove_inactive_slot(app, candidate_slot, active)

    run_command(
        _container_run_args(app, request["image"], candidate_name, candidate_slot, candidate_port, request["commit_sha"]),
        timeout=60,
    )

    route_path = Path(app["route_file"])
    prior_route = route_path.read_text(encoding="utf-8")
    state_path_obj = Path(state_path)
    prior_state = state_path_obj.read_text(encoding="utf-8") if state_path_obj.exists() else None
    route_switched = False
    try:
        _wait_http(
            f"http://127.0.0.1:{candidate_port}{app['health_path']}",
            app["expected_health_text"],
            int(app["readiness_timeout_seconds"]),
        )
        verify_release_origin(request, app)
        switch_route(app, candidate_port)
        route_switched = True
        new_active = {
            "app_id": app["app_id"],
            "commit_sha": request["commit_sha"],
            "image": request["image"],
            "platform_sha": request["platform_sha"],
            "slot": candidate_slot,
            "container": candidate_name,
            "deployed_at": datetime.now(timezone.utc).isoformat(),
        }
        new_state = {
            "active": new_active,
            "previous": active,
            "updated_at": new_active["deployed_at"],
        }
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
        duration = round(time.time() - started, 3)
        _append_audit(app, {
            "operation": "deploy",
            "outcome": "success",
            "commit_sha": request["commit_sha"],
            "image": request["image"],
            "platform_sha": request["platform_sha"],
            "slot": candidate_slot,
            "workflow_run_id": request["release"]["run_id"],
            "actor": request["release"]["actor"],
            "duration_seconds": duration,
            "timestamp": new_active["deployed_at"],
        })
        metrics_warning = False
        try:
            _write_metrics(
                app,
                new_state,
                "success",
                "deploy",
                duration,
                request["release"]["run_id"],
                request["commit_sha"],
            )
        except OSError:
            metrics_warning = True
        return {
            "outcome": "success",
            "app_id": app["app_id"],
            "commit_sha": request["commit_sha"],
            "image": request["image"],
            "slot": candidate_slot,
            "metrics_warning": metrics_warning,
        }
    except Exception as exc:
        if route_switched:
            _atomic_write(route_path, prior_route)
            run_command(["nginx", "-t"], timeout=20, check=False)
            run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
            if prior_state is None:
                state_path_obj.unlink(missing_ok=True)
            else:
                _atomic_write(state_path, prior_state)
        run_command(["docker", "rm", "-f", candidate_name], timeout=30, check=False)
        if not route_switched and active:
            try:
                active_port = int(app["slot_ports"][active["slot"]])
                _wait_http(
                    f"http://127.0.0.1:{active_port}{app['health_path']}",
                    app["expected_health_text"],
                    int(app["readiness_timeout_seconds"]),
                )
                _wait_http(
                    app["route_health_url"],
                    app["expected_health_text"],
                    int(app["readiness_timeout_seconds"]),
                )
            except Exception as health_exc:
                raise DeploymentError("The candidate was rejected and the retained release failed its health check.") from health_exc
        raise exc


def _service_network(app: dict[str, Any]) -> None:
    runtime = app["runtime"]
    network = runtime["network"]
    inspected = run_command(
        ["docker", "network", "inspect", "--format", '{{json .Labels}}', network],
        timeout=15,
        check=False,
    )
    if inspected.returncode == 0:
        try:
            labels = json.loads(inspected.stdout.strip())
        except json.JSONDecodeError as exc:
            raise DeploymentError("The application network labels could not be verified.") from exc
        if not isinstance(labels, dict) or labels.get("roman.platform.app") != app["app_id"]:
            raise DeploymentError("The configured application network is not owned by this application.")
        return
    if "no such network" not in (inspected.stderr + inspected.stdout).lower():
        raise DeploymentError("The configured application network could not be inspected.")
    run_command([
        "docker", "network", "create", "--driver", "bridge",
        "--label", f"roman.platform.app={app['app_id']}",
        "--label", "roman.platform.managed-by=roman-infrastructure",
        network,
    ], timeout=30)


def _service_container_name(app: dict[str, Any], commit_sha: str) -> str:
    return f"roman-platform-{app['app_id']}-{commit_sha[:12]}"


def _service_runtime_env_file(app: dict[str, Any]) -> Path | None:
    runtime = app["runtime"]
    value = runtime.get("env_file")
    if value is None:
        return None
    if value not in app.get("secret_refs", []):
        raise DeploymentError("The service environment file is not in the app-scoped secret allowlist.")
    path = _secure_env_file(value)
    _env_file_values(path)
    return path


def _service_container_args(
    app: dict[str, Any],
    request: dict[str, Any],
    image: str,
    container_name: str,
    *,
    scheduled: bool = False,
) -> list[str]:
    runtime = app["runtime"]
    resources = app["resources"]
    memory = f"{resources['memory_mb']}m"
    args = ["docker", "run"]
    args.extend(["--rm"] if scheduled else ["--detach"])
    args.extend([
        "--name", container_name,
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(resources["pids_limit"]),
        "--memory", memory,
        "--cpus", str(resources["cpu_cores"]),
        "--log-driver", "local",
        "--log-opt", "max-size=10m",
        "--log-opt", "max-file=3",
        "--network", runtime["network"],
        "--user", f"{runtime['uid']}:{runtime['gid']}",
        "--label", f"roman.platform.app={app['app_id']}",
        "--label", f"roman.platform.profile={app['deployment_profile']}",
        "--label", f"io.roman.deployment.caller-revision={request['commit_sha']}",
    ])
    if not scheduled:
        args.extend(["--restart", "unless-stopped"])
    if app["deployment_profile"] in {"stateful-service", "game-server"}:
        data_root = _service_data_root(app)
        if data_root.parent.is_symlink() or data_root.is_symlink():
            raise DeploymentError("Application persistent storage cannot follow symlinks.")
        data_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chown(data_root, 0, runtime["gid"])
        data_root.chmod(0o770)
        args.extend(["--mount", f"type=bind,src={data_root},dst={runtime.get('storage_container_path', '/data')}"])
    for item in runtime.get("ports", []):
        args.extend(["--publish", f"{item['bind_address']}:{item['host_port']}:{item['container_port']}/{item['protocol']}"])
    env_file = _service_runtime_env_file(app)
    if env_file is not None:
        args.extend(["--env-file", str(env_file)])
    environment = runtime.get("environment", {})
    for key, value in sorted(environment.items()):
        args.extend(["--env", f"{key}={value}"])
    args.append(image)
    return args


def _wait_service_container(container_name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run_command(
            ["docker", "inspect", "--format", '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}|{{.State.Status}}', container_name],
            timeout=15,
            check=False,
        )
        status = result.stdout.strip().split("|", 1) if result.returncode == 0 else []
        if status and status[0] == "healthy" and status[1] == "running":
            return
        if status and (status[0] == "unhealthy" or status[1] in {"exited", "dead"}):
            raise DeploymentError("The application container failed its configured Docker health check.")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise DeploymentError("The application container did not become healthy before the readiness deadline.")


def _service_container_available(app: dict[str, Any], release: dict[str, Any]) -> bool:
    commit_sha = release.get("commit_sha")
    image = release.get("image")
    name = release.get("container")
    if (
        not isinstance(commit_sha, str)
        or not SHA_RE.fullmatch(commit_sha)
        or name != _service_container_name(app, commit_sha)
        or not isinstance(image, str)
        or not re.fullmatch(re.escape(app["image_namespace"]) + r"@sha256:[0-9a-f]{64}", image)
    ):
        return False
    result = run_command(
        ["docker", "inspect", "--format", '{{.Name}}|{{index .Config.Labels "roman.platform.app"}}|{{.Config.Image}}|{{index .Config.Labels "io.roman.deployment.caller-revision"}}', name],
        timeout=15,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == f"/{name}|{app['app_id']}|{image}|{commit_sha}"


def _service_data_root(app: dict[str, Any]) -> Path:
    runtime = app["runtime"]
    root = Path(runtime["storage_root"])
    if str(root) != f"/srv/apps/{app['app_id']}":
        raise DeploymentError("The application persistent storage path is not isolated.")
    return root / "data"


def _volume_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_volume_archive(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise DeploymentError("The application volume backup is not a regular file.")
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise DeploymentError("The application volume backup cannot be read.") from exc
    if len(members) > 250_000 or any(
        member.issym()
        or member.islnk()
        or not (member.isdir() or member.isfile())
        or PurePosixPath(member.name).is_absolute()
        or ".." in PurePosixPath(member.name).parts
        or member.size > 10 * 1024**3
        for member in members
    ):
        raise DeploymentError("The application volume backup contains an unsafe archive entry.")


def _create_service_volume_backup(app: dict[str, Any], commit_sha: str) -> dict[str, str]:
    if app["runtime"].get("backup_adapter") != "verified-volume-archive":
        raise DeploymentError("The application persistent data has no supported backup adapter.")
    data_root = _service_data_root(app)
    if data_root.parent.is_symlink() or data_root.is_symlink():
        raise DeploymentError("Application persistent storage cannot follow symlinks.")
    data_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    for current, directories, files in os.walk(data_root, followlinks=False):
        for name in directories + files:
            item = Path(current) / name
            metadata = item.lstat()
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise DeploymentError("Persistent application data contains a symlink or special file; backup was not started.")
    size_result = run_command(["du", "-sb", str(data_root)], timeout=30)
    try:
        bytes_required = int(size_result.stdout.split()[0])
    except (ValueError, IndexError) as exc:
        raise DeploymentError("Persistent application data size could not be verified.") from exc
    backup_dir = data_root.parent / "backups"
    if backup_dir.is_symlink():
        raise DeploymentError("The application backup directory cannot be a symlink.")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(backup_dir).free < bytes_required + 512 * 1024**2:
        raise DeploymentError("There is not enough free storage for a verified application volume backup.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = backup_dir / f"{stamp}-{commit_sha[:12]}.tar.gz"
    result = run_command(["tar", "--numeric-owner", "--create", "--gzip", "--file", str(archive_path), "--directory", str(data_root), "."], timeout=1800, check=False)
    if result.returncode != 0:
        archive_path.unlink(missing_ok=True)
        raise DeploymentError("The consistent application volume backup failed.")
    try:
        _verify_volume_archive(archive_path)
        digest = _volume_digest(archive_path)
        manifest = {
            "app_id": app["app_id"],
            "commit_sha": commit_sha,
            "archive": archive_path.name,
            "sha256": digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(archive_path.with_suffix(".json"), json.dumps(manifest, sort_keys=True) + "\n", 0o600)
        os.chmod(archive_path, 0o600)
        return {"path": str(archive_path), "sha256": digest}
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _restore_service_volume_backup(app: dict[str, Any], backup: Any) -> None:
    if not isinstance(backup, dict) or not isinstance(backup.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(backup.get("sha256", ""))):
        raise DeploymentError("A verified application volume backup is not recorded.")
    data_root = _service_data_root(app)
    archive_path = Path(backup["path"])
    if archive_path.parent != data_root.parent / "backups" or archive_path.is_symlink() or os.path.realpath(archive_path) != str(archive_path):
        raise DeploymentError("The recorded application volume backup is outside the app-scoped backup directory.")
    manifest_path = archive_path.with_suffix(".json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError("The application volume backup recovery metadata is unavailable.") from exc
    archive_stat = archive_path.lstat()
    manifest_stat = manifest_path.lstat()
    if (
        archive_stat.st_uid != 0
        or stat.S_IMODE(archive_stat.st_mode) != 0o600
        or manifest_stat.st_uid != 0
        or stat.S_IMODE(manifest_stat.st_mode) != 0o600
        or manifest.get("app_id") != app["app_id"]
        or manifest.get("archive") != archive_path.name
        or manifest.get("sha256") != backup["sha256"]
        or _volume_digest(archive_path) != backup["sha256"]
    ):
        raise DeploymentError("The application volume backup checksum or identity is invalid.")
    _verify_volume_archive(archive_path)
    restore_dir = Path(tempfile.mkdtemp(prefix=".restore-", dir=data_root.parent))
    displaced = data_root.parent / f"data.failed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(restore_dir)
        if data_root.exists():
            data_root.replace(displaced)
        restore_dir.replace(data_root)
        os.chown(data_root, 0, app["runtime"]["gid"])
    except (OSError, tarfile.TarError) as exc:
        if not data_root.exists() and displaced.exists():
            displaced.replace(data_root)
        raise DeploymentError("The application volume restore failed; the old application remains stopped.") from exc
    finally:
        if restore_dir.exists():
            shutil.rmtree(restore_dir, ignore_errors=True)


def _scheduled_unit_content(app: dict[str, Any], request: dict[str, Any], image: str) -> tuple[str, str]:
    runtime = app["runtime"]
    name = f"roman-platform-{app['app_id']}"
    args = _service_container_args(app, request, image, f"{name}-job", scheduled=True)
    command = " ".join(args[1:])
    service = (
        "[Unit]\nDescription=Roman Platform scheduled job " + app["app_id"] + "\n"
        "# Roman Platform managed unit\n[Service]\nType=oneshot\n"
        f"ExecStart=/usr/bin/docker {command}\nTimeoutStartSec=1800\n"
    )
    timer = (
        "[Unit]\nDescription=Roman Platform schedule for " + app["app_id"] + "\n"
        "# Roman Platform managed unit\n[Timer]\n"
        f"OnCalendar={runtime['systemd_calendar']}\nPersistent=true\nUnit={name}.service\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def _install_scheduled_service(app: dict[str, Any], service: str, timer: str) -> None:
    name = f"roman-platform-{app['app_id']}"
    service_path = Path("/etc/systemd/system") / f"{name}.service"
    timer_path = Path("/etc/systemd/system") / f"{name}.timer"
    for path in (service_path, timer_path):
        if path.exists() and "# Roman Platform managed unit" not in path.read_text(encoding="utf-8"):
            raise DeploymentError("A systemd unit with the application name already exists outside Roman Platform management.")
    _atomic_write(service_path, service, 0o644)
    _atomic_write(timer_path, timer, 0o644)
    run_command(["systemctl", "daemon-reload"], timeout=20)
    run_command(["systemctl", "enable", "--now", f"{name}.timer"], timeout=30)
    run_command(["systemctl", "restart", f"{name}.timer"], timeout=30)
    status = run_command(["systemctl", "is-active", "--quiet", f"{name}.timer"], timeout=15, check=False)
    if status.returncode != 0:
        raise DeploymentError("The scheduled job timer did not become active.")


def _verify_service_release_image(app: dict[str, Any], release: dict[str, Any]) -> None:
    image = release.get("image")
    commit_sha = release.get("commit_sha")
    platform_sha = release.get("platform_sha")
    if (
        not isinstance(image, str)
        or not re.fullmatch(re.escape(app["image_namespace"]) + r"@sha256:[0-9a-f]{64}", image)
        or not isinstance(commit_sha, str)
        or not SHA_RE.fullmatch(commit_sha)
        or platform_sha not in app.get("allowed_platform_shas", [])
    ):
        raise DeploymentError("The retained application release identity is invalid.")
    digest = run_command(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], timeout=15)
    try:
        values = json.loads(digest.stdout.strip())
    except json.JSONDecodeError as exc:
        raise DeploymentError("The retained application image digest could not be verified.") from exc
    if not isinstance(values, list) or image not in values:
        raise DeploymentError("The retained application image no longer matches its immutable registry digest.")
    verify_image_labels(
        image,
        commit_sha if app.get("image_revision_mode", "platform") == "caller" else platform_sha,
        commit_sha,
        platform_sha,
        app["image_package_repository"],
        app["image_source_repository"],
        os.environ.copy(),
    )


def _deploy_service(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    runtime = app["runtime"]
    profile = app["deployment_profile"]
    state_path = Path(app["state_file"])
    state = _read_state(str(state_path))
    active = state.get("active")
    if active and (active.get("app_id") != app["app_id"] or active.get("profile") != profile):
        raise DeploymentError("Active service state belongs to a different application profile.")
    image = request["image"]
    with registry_session(app, request) as env:
        run_command(["docker", "pull", image], env=env, timeout=600)
        _verify_image(image, request, app, env)
    _service_network(app)
    if profile in {"stateful-service", "game-server"}:
        require_storage_mounts(app)
    if profile == "scheduled-job":
        service, timer = _scheduled_unit_content(app, request, image)
        name = f"roman-platform-{app['app_id']}"
        service_path = Path("/etc/systemd/system") / f"{name}.service"
        timer_path = Path("/etc/systemd/system") / f"{name}.timer"
        prior_service = service_path.read_text(encoding="utf-8") if service_path.exists() else None
        prior_timer = timer_path.read_text(encoding="utf-8") if timer_path.exists() else None
        try:
            _install_scheduled_service(app, service, timer)
            verify_release_origin(request, app)
            timestamp = datetime.now(timezone.utc).isoformat()
            release = {
                "app_id": app["app_id"], "profile": profile, "commit_sha": request["commit_sha"],
                "platform_sha": request["platform_sha"], "image": image, "deployed_at": timestamp,
            }
            new_state = {"active": release, "previous": active, "updated_at": timestamp}
            _atomic_write(state_path, json.dumps(new_state, sort_keys=True, indent=2) + "\n")
        except Exception:
            if prior_service is not None and prior_timer is not None:
                _atomic_write(service_path, prior_service, 0o644)
                _atomic_write(timer_path, prior_timer, 0o644)
                run_command(["systemctl", "daemon-reload"], timeout=20, check=False)
                run_command(["systemctl", "restart", f"{name}.timer"], timeout=30, check=False)
            else:
                run_command(["systemctl", "disable", "--now", f"{name}.timer"], timeout=30, check=False)
                service_path.unlink(missing_ok=True)
                timer_path.unlink(missing_ok=True)
                run_command(["systemctl", "daemon-reload"], timeout=20, check=False)
            raise
    else:
        if active and active.get("commit_sha") == request["commit_sha"] and active.get("image") == image:
            container = active.get("container")
            if _service_container_available(app, active):
                _wait_service_container(container, int(runtime.get("readiness_timeout_seconds", 180)))
                return {"outcome": "success", "app_id": app["app_id"], "commit_sha": request["commit_sha"], "image": image, "container": container, "already_active": True}
        rolling = profile == "worker" and app["strategy"] == "rolling"
        old_stopped = False
        if active:
            if not _service_container_available(app, active):
                raise DeploymentError("The active service container identity could not be verified.")
        volume_backup = None
        candidate = _service_container_name(app, request["commit_sha"])
        existing = run_command(["docker", "inspect", candidate], timeout=15, check=False)
        if existing.returncode == 0:
            raise DeploymentError("A candidate service container with this release identity already exists; inspect it before retrying.")
        if not any(text in (existing.stderr + existing.stdout).lower() for text in ("no such object", "no such container")):
            raise DeploymentError("The candidate service container state could not be verified.")
        candidate_started = False
        try:
            if active and not rolling:
                run_command(["docker", "stop", "--time", "30", active["container"]], timeout=45)
                old_stopped = True
            if profile in {"stateful-service", "game-server"}:
                volume_backup = _create_service_volume_backup(app, active["commit_sha"] if active else request["commit_sha"])
            candidate_started = True
            run_command(_service_container_args(app, request, image, candidate), timeout=60)
            _wait_service_container(candidate, int(runtime.get("readiness_timeout_seconds", 180)))
            verify_release_origin(request, app)
            if active and rolling:
                run_command(["docker", "stop", "--time", "30", active["container"]], timeout=45)
                old_stopped = True
            timestamp = datetime.now(timezone.utc).isoformat()
            previous = ({**active, "volume_backup": volume_backup} if active else None)
            release = {
                "app_id": app["app_id"], "profile": profile, "commit_sha": request["commit_sha"],
                "platform_sha": request["platform_sha"], "image": image, "container": candidate,
                "deployed_at": timestamp,
            }
            new_state = {"active": release, "previous": previous, "updated_at": timestamp}
            _atomic_write(state_path, json.dumps(new_state, sort_keys=True, indent=2) + "\n")
        except Exception as exc:
            if candidate_started:
                run_command(["docker", "rm", "-f", candidate], timeout=30, check=False)
            if profile in {"stateful-service", "game-server"} and volume_backup is not None:
                _restore_service_volume_backup(app, volume_backup)
            if active and old_stopped:
                run_command(["docker", "start", active["container"]], timeout=30, check=False)
                _wait_service_container(active["container"], int(runtime.get("readiness_timeout_seconds", 180)))
            raise exc
        old_previous = state.get("previous")
        if old_previous and old_previous.get("container") and old_previous.get("container") != (active or {}).get("container"):
            with contextlib.suppress(DeploymentError, OSError):
                _remove_service_container(app, old_previous)
    timestamp = new_state["updated_at"]
    _append_audit(app, {
        "operation": "deploy-service", "outcome": "success", "profile": profile,
        "commit_sha": request["commit_sha"], "image": image,
        "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"],
        "timestamp": timestamp,
    })
    metrics_warning = False
    try:
        _write_metrics(app, new_state, "success", "deploy-service", time.time() - started, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {"outcome": "success", "app_id": app["app_id"], "profile": profile, "commit_sha": request["commit_sha"], "image": image, "metrics_warning": metrics_warning}


def _remove_service_container(app: dict[str, Any], release: dict[str, Any]) -> None:
    if _service_container_available(app, release):
        run_command(["docker", "rm", "-f", release["container"]], timeout=30, check=False)


def _rollback_service(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    state_path = Path(app["state_file"])
    state = _read_state(str(state_path))
    active, previous = state.get("active"), state.get("previous")
    if not active or not previous or active.get("profile") != app["deployment_profile"] or previous.get("profile") != app["deployment_profile"]:
        raise DeploymentError("No retained compatible service release is available for rollback.")
    profile = app["deployment_profile"]
    runtime = app["runtime"]
    if profile == "scheduled-job":
        name = f"roman-platform-{app['app_id']}"
        service_path = Path("/etc/systemd/system") / f"{name}.service"
        timer_path = Path("/etc/systemd/system") / f"{name}.timer"
        running = run_command(["systemctl", "is-active", "--quiet", f"{name}.service"], timeout=15, check=False)
        if running.returncode == 0:
            raise DeploymentError("The scheduled job is running; rollback waits until the current job finishes.")
        _verify_service_release_image(app, previous)
        old_service = service_path.read_text(encoding="utf-8") if service_path.exists() else None
        old_timer = timer_path.read_text(encoding="utf-8") if timer_path.exists() else None
        prior_request = {**request, "commit_sha": previous["commit_sha"], "platform_sha": previous["platform_sha"]}
        service, timer = _scheduled_unit_content(app, prior_request, previous["image"])
        try:
            _install_scheduled_service(app, service, timer)
            verify_release_origin(request, app)
            timestamp = datetime.now(timezone.utc).isoformat()
            new_state = {"active": previous, "previous": active, "updated_at": timestamp}
            _atomic_write(state_path, json.dumps(new_state, sort_keys=True, indent=2) + "\n")
        except Exception:
            if old_service is not None and old_timer is not None:
                _atomic_write(service_path, old_service, 0o644)
                _atomic_write(timer_path, old_timer, 0o644)
                run_command(["systemctl", "daemon-reload"], timeout=20, check=False)
                run_command(["systemctl", "restart", f"{name}.timer"], timeout=30, check=False)
            raise
        _append_audit(app, {"operation": "rollback-service", "outcome": "success", "from_commit_sha": active["commit_sha"], "to_commit_sha": previous["commit_sha"], "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": timestamp})
        metrics_warning = False
        try:
            _write_metrics(app, new_state, "success", "rollback-service", time.time() - started, request["release"]["run_id"], request["commit_sha"])
        except OSError:
            metrics_warning = True
        return {"outcome": "success", "app_id": app["app_id"], "commit_sha": previous["commit_sha"], "operation": "rollback-service", "metrics_warning": metrics_warning}
    _verify_service_release_image(app, previous)
    if not _service_container_available(app, active) or not _service_container_available(app, previous):
        raise DeploymentError("The active or retained service container identity could not be verified.")
    current_backup = None
    run_command(["docker", "stop", "--time", "30", active["container"]], timeout=45)
    try:
        if profile in {"stateful-service", "game-server"}:
            current_backup = _create_service_volume_backup(app, active["commit_sha"])
            _restore_service_volume_backup(app, previous.get("volume_backup"))
        run_command(["docker", "start", previous["container"]], timeout=30)
        _wait_service_container(previous["container"], int(runtime.get("readiness_timeout_seconds", 180)))
        verify_release_origin(request, app)
        timestamp = datetime.now(timezone.utc).isoformat()
        new_state = {"active": previous, "previous": {**active, "volume_backup": current_backup}, "updated_at": timestamp}
        _atomic_write(state_path, json.dumps(new_state, sort_keys=True, indent=2) + "\n")
    except Exception:
        if profile in {"stateful-service", "game-server"} and current_backup is not None:
            _restore_service_volume_backup(app, current_backup)
        run_command(["docker", "start", active["container"]], timeout=30, check=False)
        _wait_service_container(active["container"], int(runtime.get("readiness_timeout_seconds", 180)))
        raise
    _append_audit(app, {"operation": "rollback-service", "outcome": "success", "from_commit_sha": active["commit_sha"], "to_commit_sha": previous["commit_sha"], "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": timestamp})
    metrics_warning = False
    try:
        _write_metrics(app, new_state, "success", "rollback-service", time.time() - started, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {"outcome": "success", "app_id": app["app_id"], "commit_sha": previous["commit_sha"], "operation": "rollback-service", "metrics_warning": metrics_warning}


def _verify_flyway_info(
    output: str,
    expected_version: str | None = None,
    allowed_exceptions: list[dict[str, str]] | None = None,
    *,
    flyway_version: str | None = None,
    schema_name: str | None = None,
    max_pending: int = 32,
    allow_initial_schema: bool = False,
) -> ForwardMigrationPlan:
    try:
        plan = validate_forward_plan(
            output,
            max_pending=max_pending,
            allowed_exceptions=allowed_exceptions,
            expected_flyway_version=flyway_version,
            expected_schema_name=schema_name,
            allow_initial_schema=allow_initial_schema,
        )
    except MigrationPlanError as exc:
        raise DeploymentError(f"Flyway migration history is invalid: {exc}") from exc
    if expected_version is not None and plan.source_version != expected_version:
        raise DeploymentError("Database schema version does not match the requested observed version.")
    return plan


def _flyway_image_checksums(
    image: str,
    app: dict[str, Any],
    env: dict[str, str] | None = None,
    *,
    temp_parent: Path | None = None,
) -> dict[str, int]:
    """Read migration SQL from the already verified immutable image without running it."""
    parent = temp_parent if temp_parent is not None else Path(app.get("runtime_tmp_dir", ""))
    if not parent.is_absolute() or parent.is_symlink() or not parent.is_dir():
        raise DeploymentError("The private migration-inspection directory is unavailable.")
    try:
        info = parent.lstat()
    except OSError as exc:
        raise DeploymentError("The private migration-inspection directory is unavailable.") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise DeploymentError("The private migration-inspection directory is not protected.")
    if temp_parent is None and info.st_uid != 0:
        raise DeploymentError("The migration-inspection directory is not root-owned.")

    token = uuid.uuid4().hex
    container = f"roman-platform-migration-inspect-{app['app_id']}-{token}"
    with tempfile.TemporaryDirectory(prefix=f"migration-plan-{app['app_id']}-", dir=parent) as temp:
        created = run_command(
            ["docker", "create", "--read-only", "--network", "none", "--name", container, image],
            env=env,
            timeout=60,
            check=False,
        )
        if created.returncode != 0:
            raise DeploymentError("The verified migration image could not be inspected safely.")
        try:
            run_command(["docker", "cp", f"{container}:/flyway/sql", temp], env=env, timeout=60)
            try:
                return migration_artifact_checksums(Path(temp) / "sql")
            except MigrationPlanError as exc:
                raise DeploymentError(f"The verified migration image is invalid: {exc}") from exc
        finally:
            run_command(["docker", "rm", container], env=env, timeout=30, check=False)


def _bind_flyway_plan_checksums(
    info_output: str,
    history_output: str,
    image: str,
    app: dict[str, Any],
    env: dict[str, str] | None = None,
) -> str:
    try:
        report = json.loads(info_output)
    except (TypeError, ValueError) as exc:
        raise DeploymentError("Flyway did not return valid migration JSON.") from exc
    if not isinstance(report, dict) or not isinstance(report.get("migrations"), list):
        raise DeploymentError("Flyway migration history is unavailable.")
    pending = any(
        isinstance(entry, dict) and entry.get("state") == "Pending"
        for entry in report["migrations"]
    )
    checksums = _flyway_image_checksums(image, app, env) if pending else None
    try:
        return bind_flyway_checksums(info_output, history_output, checksums)
    except MigrationPlanError as exc:
        raise DeploymentError(f"Flyway history checksums do not match the verified release: {exc}") from exc


def _migration_plan_data(plan: ForwardMigrationPlan) -> dict[str, Any]:
    return {
        "source_version": plan.source_version,
        "target_version": plan.target_version,
        "pending": [list(row) for row in plan.pending],
        "fingerprint": plan.fingerprint,
        "release_fingerprint": plan.release_fingerprint,
    }


def _release_plan_fingerprint(
    plan: ForwardMigrationPlan,
    request: dict[str, Any],
    app: dict[str, Any],
    database_values: dict[str, str],
) -> str:
    """Bind observed Flyway history to one app, release, migrator, and database."""
    release = request.get("release")
    image = request.get("migration_image")
    commit_sha = request.get("commit_sha")
    platform_sha = request.get("platform_sha")
    database_url = database_values.get("DB_URL")
    database_username = database_values.get("DB_USERNAME")
    if (
        not isinstance(release, dict)
        or not isinstance(release.get("run_id"), str)
        or not release["run_id"].isdigit()
        or not isinstance(release.get("run_attempt"), str)
        or not release["run_attempt"].isdigit()
        or not isinstance(release.get("actor"), str)
        or not isinstance(image, str)
        or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image)
        or not isinstance(commit_sha, str)
        or not SHA_RE.fullmatch(commit_sha)
        or not isinstance(platform_sha, str)
        or not SHA_RE.fullmatch(platform_sha)
        or not isinstance(database_url, str)
        or not re.fullmatch(r"jdbc:postgresql://[^\s/]+/[^\s]+", database_url)
        or not isinstance(database_username, str)
        or not database_username
    ):
        raise DeploymentError("The Flyway plan cannot be bound to an exact release and database identity.")
    try:
        parsed_url = urlsplit(database_url.removeprefix("jdbc:"))
    except ValueError as exc:
        raise DeploymentError("The Flyway plan database identity is malformed.") from exc
    if parsed_url.username or parsed_url.password:
        raise DeploymentError("Database credentials must use the dedicated environment variables, not the JDBC URL.")

    migration_policy = _schema_migration_policy(app)
    binding = {
        "plan_fingerprint": plan.fingerprint,
        "app_id": app.get("app_id"),
        "source_commit": commit_sha,
        "platform_commit": platform_sha,
        "migrator_image": image,
        "database_url_sha256": hashlib.sha256(database_url.encode("utf-8")).hexdigest(),
        "database_username": database_username,
        "schema": app.get("flyway_schema"),
        "execution_role": migration_policy.get("execution_role") if migration_policy else None,
        "workflow_run_id": release["run_id"],
        "workflow_run_attempt": release["run_attempt"],
        "release_actor": release["actor"],
        "release_mode": release.get("mode", "manual"),
    }
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _empty_database_status() -> dict[str, Any]:
    fingerprint = hashlib.sha256(b"roman-platform-no-flyway-database").hexdigest()
    plan = ForwardMigrationPlan(None, None, (), fingerprint)
    return {
        "schema_version": None,
        "migration_target_version": None,
        "migration_pending": False,
        "migration_plan": _migration_plan_data(plan),
    }


def _migration_plan_from_data(value: Any) -> ForwardMigrationPlan:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("pending"), list)
        or not isinstance(value.get("fingerprint"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
    ):
        raise DeploymentError("The validated Flyway plan is unavailable.")
    rows: list[tuple[str, str, str, int]] = []
    for row in value["pending"]:
        if (
            not isinstance(row, list)
            or len(row) != 4
            or not all(isinstance(item, str) for item in row[:3])
            or isinstance(row[3], bool)
            or not isinstance(row[3], int)
        ):
            raise DeploymentError("The validated Flyway plan is malformed.")
        rows.append((row[0], row[1], row[2], row[3]))
    source = value.get("source_version")
    target = value.get("target_version")
    release_fingerprint = value.get("release_fingerprint")
    if source is not None and not isinstance(source, str):
        raise DeploymentError("The validated Flyway source version is malformed.")
    if target is not None and not isinstance(target, str):
        raise DeploymentError("The validated Flyway target version is malformed.")
    if release_fingerprint is not None and (
        not isinstance(release_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", release_fingerprint)
    ):
        raise DeploymentError("The validated Flyway release binding is malformed.")
    return ForwardMigrationPlan(source, target, tuple(rows), value["fingerprint"], (), release_fingerprint)


def _schema_migration_policy(app: dict[str, Any]) -> dict[str, Any] | None:
    policy = app.get("schema_migration")
    if policy is None:
        return None
    fields = {
        "adapter", "execution_role", "max_pending", "writer_usernames",
        "rollback_after_migration", "allow_initial_schema",
    }
    if (
        not isinstance(policy, dict)
        or set(policy) != fields
        or policy.get("adapter") != "flyway"
        or not isinstance(policy.get("execution_role"), str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", policy["execution_role"])
        or isinstance(policy["max_pending"], bool)
        or not isinstance(policy["max_pending"], int)
        or not 1 <= policy["max_pending"] <= 100
        or not isinstance(policy["writer_usernames"], list)
        or not policy["writer_usernames"]
        or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name) for name in policy["writer_usernames"])
        or policy["rollback_after_migration"] != "forward-only"
        or not isinstance(policy["allow_initial_schema"], bool)
    ):
        raise DeploymentError("The production schema migration policy is invalid.")
    database = app.get("database")
    backup_adapter = database.get("backup_adapter") if isinstance(database, dict) else None
    if backup_adapter == "pgbackrest":
        backup = app.get("production_backup")
        if (
            not isinstance(backup, dict)
            or set(backup) != {"config_file", "stanza", "repository", "timeout_seconds"}
            or backup.get("config_file") != "/etc/pgbackrest/pgbackrest.conf"
            or backup.get("stanza") != "roman-platform"
            or isinstance(backup.get("repository"), bool)
            or backup.get("repository") != 1
            or isinstance(backup.get("timeout_seconds"), bool)
            or not isinstance(backup.get("timeout_seconds"), int)
            or not 60 <= backup["timeout_seconds"] <= 1800
        ):
            raise DeploymentError("The configured pgBackRest database backup policy is invalid.")
    elif backup_adapter == "verified-dump":
        backup = app.get("database_backup")
        if (
            not isinstance(backup, dict)
            or set(backup) != {"adapter", "env_file", "directory", "timeout_seconds", "keep_last"}
            or backup.get("adapter") != "verified-dump"
            or backup.get("env_file") != f"/etc/roman-platform/{app.get('app_id')}/backup.env"
            or backup.get("directory") != f"/srv/apps/{app.get('app_id')}/backups"
            or backup.get("env_file") not in app.get("secret_refs", [])
            or isinstance(backup.get("timeout_seconds"), bool)
            or not isinstance(backup.get("timeout_seconds"), int)
            or not 60 <= backup["timeout_seconds"] <= 7200
            or isinstance(backup.get("keep_last"), bool)
            or not isinstance(backup.get("keep_last"), int)
            or not 2 <= backup["keep_last"] <= 50
        ):
            raise DeploymentError("The configured verified-dump backup policy is invalid.")
    else:
        raise DeploymentError("A supported database backup adapter is required before migration.")
    return policy


def _verify_flyway_migration_plan(
    output: str,
    app: dict[str, Any],
    allowed_exceptions: list[dict[str, str]] | None = None,
    *,
    flyway_version: str | None = None,
    schema_name: str | None = None,
) -> ForwardMigrationPlan:
    policy = _schema_migration_policy(app)
    if policy is None:
        raise DeploymentError("No production migration is approved for this application.")
    return _verify_flyway_info(
        output,
        allowed_exceptions=allowed_exceptions,
        flyway_version=flyway_version,
        schema_name=schema_name,
        max_pending=policy["max_pending"],
        allow_initial_schema=policy["allow_initial_schema"],
    )


def _flyway_container_args(
    request: dict[str, Any],
    app: dict[str, Any],
    env_file: Path,
    command: str,
    *,
    container_name: str | None = None,
    migration_plan: ForwardMigrationPlan | None = None,
) -> list[str]:
    if command not in {"validate", "migrate", "info", "history"}:
        raise DeploymentError("The requested Flyway command is not approved.")
    expected_container = re.compile(
        rf"roman-platform-migration-{re.escape(app['app_id'])}-[0-9]{{1,20}}-[0-9]{{1,20}}"
    )
    if container_name is not None and not expected_container.fullmatch(container_name):
        raise DeploymentError("The Flyway migration container identity is invalid.")
    policy = None
    if command == "migrate":
        policy = _schema_migration_policy(app)
        if policy is None or migration_plan is None or not migration_plan.needs_migration or migration_plan.target_version is None:
            raise DeploymentError("No validated pending migration plan is available for execution.")
    args = [
        "docker", "run", "--rm", "--read-only",
        "--network", app["container_network"],
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "64",
        "--memory", app.get("migration_memory", "512m"),
        "--cpus", "1",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
        "--env-file", str(env_file),
        "--env", f"FLYWAY_CLI_VERSION={app['flyway_cli_version']}",
        "--env", f"FLYWAY_SCHEMA={app['flyway_schema']}",
    ]
    if command == "migrate":
        args.extend([
            "--env", f"MIGRATION_TARGET_VERSION={migration_plan.target_version}",
            "--env", f"MIGRATION_EXECUTION_ROLE={policy['execution_role']}",
        ])
    if container_name is not None:
        args.extend(["--name", container_name])
    args.append(request["migration_image"])
    if command == "migrate":
        args.append("migrate")
    elif command == "info":
        args.extend(["info", "-outputType=json"])
    elif command == "history":
        args.append("history")
    else:
        args.append(command)
    return args


def _secure_pgbackrest_config(path: str) -> Path:
    try:
        import pwd
        postgres = pwd.getpwnam("postgres")
    except (ImportError, KeyError) as exc:
        raise DeploymentError("The production backup account is unavailable.") from exc
    config = Path(path)
    try:
        metadata = config.lstat()
    except OSError as exc:
        raise DeploymentError("The production backup configuration is unavailable.") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or config.is_symlink()
        or os.path.realpath(path) != path
        or metadata.st_uid != postgres.pw_uid
        or metadata.st_gid != postgres.pw_gid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise DeploymentError("The production backup configuration is not securely protected.")
    return config


def _pgbackrest_json(app: dict[str, Any]) -> list[dict[str, Any]]:
    policy = app.get("production_backup")
    if not isinstance(policy, dict):
        raise DeploymentError("A production database backup is required before migration.")
    config = _secure_pgbackrest_config(policy.get("config_file", ""))
    args = [
        "/usr/sbin/runuser", "-u", "postgres", "--", "/usr/bin/pgbackrest",
        f"--config={config}", f"--stanza={policy['stanza']}",
        f"--repo={policy['repository']}", "--output=json", "info",
    ]
    result = run_command(args, timeout=60, check=False)
    if result.returncode != 0:
        raise DeploymentError("The production backup repository could not be verified.")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentError("The production backup repository returned invalid evidence.") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise DeploymentError("The production backup repository returned invalid evidence.")
    return value


def _ensure_flyway_container_stopped(container_name: str) -> bool:
    try:
        removed = run_command(["docker", "rm", "--force", container_name], timeout=30, check=False)
        inspected = run_command(["docker", "inspect", container_name], timeout=15, check=False)
    except DeploymentError:
        return False
    if inspected.returncode == 0:
        return False
    missing_container = (inspected.stderr + inspected.stdout).lower()
    return removed.returncode == 0 or "no such object" in missing_container or "no such container" in missing_container


def _verify_production_backup_integrity(app: dict[str, Any], label: str) -> None:
    policy = app.get("production_backup")
    if (
        not isinstance(policy, dict)
        or not isinstance(label, str)
        or not re.fullmatch(r"[0-9]{8}-[0-9]{6}F", label)
    ):
        raise DeploymentError("The production backup integrity check is not configured safely.")
    config = _secure_pgbackrest_config(policy.get("config_file", ""))
    result = run_command(
        [
            "/usr/sbin/runuser", "-u", "postgres", "--", "/usr/bin/pgbackrest",
            f"--config={config}", f"--stanza={policy['stanza']}", f"--repo={policy['repository']}",
            f"--set={label}", "verify",
        ],
        timeout=policy["timeout_seconds"],
        check=False,
    )
    if result.returncode != 0:
        raise DeploymentError("Fresh full production backup integrity verification failed; the migration was not started.")


def _pg_connection_uri(jdbc_url: str) -> str:
    if not isinstance(jdbc_url, str) or not re.fullmatch(
        r"jdbc:postgresql://[A-Za-z0-9_.:-]+/[A-Za-z0-9_]+(?:\?[A-Za-z0-9_.=&%-]+)?",
        jdbc_url,
    ):
        raise DeploymentError("The PostgreSQL backup destination URL is invalid.")
    return jdbc_url.removeprefix("jdbc:")


def _database_backup_credentials(app: dict[str, Any], runtime_values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    policy = app.get("database_backup")
    if not isinstance(policy, dict):
        raise DeploymentError("The verified-dump backup credentials are not configured.")
    path = _secure_env_file(policy["env_file"])
    values = _env_file_values(path)
    required = {"DB_URL", "DB_USERNAME", "DB_PASSWORD"}
    username = values.get("DB_USERNAME", "")
    if (
        set(values) != required
        or any(not values.get(key, "").strip() for key in required)
        or values.get("DB_URL") != runtime_values.get("DB_URL")
        or values.get("DB_URL") not in app.get("allowed_database_urls", [])
        or username == runtime_values.get("DB_USERNAME")
        or username.lower() in {"postgres", "root", "admin"}
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", username)
    ):
        raise DeploymentError("The verified-dump backup credentials are invalid or target a different database.")
    environment = os.environ.copy()
    environment["PGPASSWORD"] = values["DB_PASSWORD"]
    return values, environment


def _verified_pg_dump_listing(archive_path: Path) -> str:
    listing = run_command(
        ["/usr/bin/pg_restore", "--list", str(archive_path)], timeout=120, check=False
    )
    # pg_restore 18 writes a standalone comment separator before the archive header.
    lines = listing.stdout.splitlines()
    if lines[:1] == [";"]:
        lines = lines[1:]
    if listing.returncode != 0 or not lines or not lines[0].startswith("; Archive created at "):
        raise DeploymentError("The PostgreSQL dump failed archive integrity verification.")
    return listing.stdout


def _verify_dump_backup(app: dict[str, Any], archive_path: Path, manifest: dict[str, Any]) -> str:
    try:
        metadata = archive_path.lstat()
        manifest_path = archive_path.with_suffix(".json")
        manifest_info = manifest_path.lstat()
    except OSError as exc:
        raise DeploymentError("The PostgreSQL dump backup or its recovery metadata is unavailable.") from exc
    if (
        archive_path.is_symlink()
        or manifest_path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not stat.S_ISREG(manifest_info.st_mode)
        or metadata.st_uid != 0
        or manifest_info.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or stat.S_IMODE(manifest_info.st_mode) != 0o600
        or manifest.get("app_id") != app["app_id"]
        or manifest.get("archive") != archive_path.name
        or manifest.get("adapter") != "verified-dump"
    ):
        raise DeploymentError("The PostgreSQL dump backup identity or permissions are invalid.")
    digest = _volume_digest(archive_path)
    if manifest.get("sha256") != digest:
        raise DeploymentError("The PostgreSQL dump backup checksum is invalid.")
    _verified_pg_dump_listing(archive_path)
    return digest


def _prune_verified_dump_backups(app: dict[str, Any], keep_last: int) -> None:
    backup_dir = Path(app["database_backup"]["directory"])
    archives: list[tuple[float, Path]] = []
    for path in backup_dir.glob("*.dump"):
        manifest_path = path.with_suffix(".json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            info = path.lstat()
        except (OSError, json.JSONDecodeError):
            continue
        if (
            path.is_symlink()
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or manifest.get("app_id") != app["app_id"]
            or manifest.get("adapter") != "verified-dump"
            or manifest.get("archive") != path.name
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", "")))
        ):
            continue
        archives.append((info.st_mtime, path))
    for _, stale in sorted(archives, reverse=True)[keep_last:]:
        manifest_path = stale.with_suffix(".json")
        try:
            manifest_path.unlink()
            stale.unlink()
        except OSError:
            # Retain anything that cannot be pruned cleanly; it does not affect
            # the verified newest backup or the migration gate.
            continue


def _create_verified_dump_backup(
    app: dict[str, Any],
    request: dict[str, Any],
    plan: ForwardMigrationPlan,
    runtime_values: dict[str, str],
) -> str:
    policy = app["database_backup"]
    backup_dir = Path(policy["directory"])
    if backup_dir.is_symlink() or str(backup_dir) != f"/srv/apps/{app['app_id']}/backups":
        raise DeploymentError("The verified-dump backup directory is not isolated under the application storage root.")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_info = backup_dir.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != 0
        or stat.S_IMODE(directory_info.st_mode) & 0o077
        or os.path.realpath(backup_dir) != str(backup_dir)
    ):
        raise DeploymentError("The verified-dump backup directory is not root-owned and private.")
    values, env = _database_backup_credentials(app, runtime_values)
    database_uri = _pg_connection_uri(values["DB_URL"])
    query = run_command(
        ["/usr/bin/psql", "--dbname", database_uri, "--username", values["DB_USERNAME"], "-At", "-v", "ON_ERROR_STOP=1", "-c", "SELECT pg_database_size(current_database())"],
        env=env,
        timeout=60,
        check=False,
        redactions=(values["DB_PASSWORD"], values["DB_URL"], values["DB_USERNAME"]),
    )
    try:
        database_size = int(query.stdout.strip())
    except ValueError as exc:
        raise DeploymentError("The database backup size could not be measured safely.") from exc
    if query.returncode != 0 or database_size <= 0:
        raise DeploymentError("The database backup size could not be measured safely.")
    if shutil.disk_usage(backup_dir).free < database_size + 2 * 1024**3:
        raise DeploymentError("There is not enough free storage for the verified PostgreSQL dump and recovery reserve.")

    run_id = str(request["release"]["run_id"])
    attempt = str(request["release"]["run_attempt"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"{stamp}-{run_id}-{attempt}.dump"
    archive_path = backup_dir / archive_name
    if archive_path.exists() or archive_path.is_symlink():
        raise DeploymentError("A PostgreSQL backup with this release identity already exists.")
    fd, temp_name = tempfile.mkstemp(prefix=".database-backup-", suffix=".dump", dir=backup_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    os.chmod(temp_path, 0o600)
    try:
        dump = run_command(
            [
                "/usr/bin/pg_dump", "--format=custom", "--no-owner", "--no-privileges",
                "--file", str(temp_path), "--dbname", database_uri, "--username", values["DB_USERNAME"],
            ],
            env=env,
            timeout=policy["timeout_seconds"],
            check=False,
            redactions=(values["DB_PASSWORD"], values["DB_URL"], values["DB_USERNAME"]),
        )
        if dump.returncode != 0 or not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise DeploymentError("The consistent PostgreSQL dump failed; the migration was not started.")
        _verified_pg_dump_listing(temp_path)
        digest = _volume_digest(temp_path)
        os.replace(temp_path, archive_path)
        manifest = {
            "adapter": "verified-dump",
            "app_id": app["app_id"],
            "archive": archive_name,
            "sha256": digest,
            "database_url_sha256": hashlib.sha256(values["DB_URL"].encode("utf-8")).hexdigest(),
            "schema_source_version": plan.source_version,
            "schema_target_version": plan.target_version,
            "migration_plan_fingerprint": plan.fingerprint,
            "pending_migrations": [list(row) for row in plan.pending],
            "commit_sha": request["commit_sha"],
            "workflow_run_id": run_id,
            "workflow_run_attempt": attempt,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(archive_path.with_suffix(".json"), json.dumps(manifest, sort_keys=True) + "\n", 0o600)
        _verify_dump_backup(app, archive_path, manifest)
        _prune_verified_dump_backups(app, policy["keep_last"])
    except Exception:
        temp_path.unlink(missing_ok=True)
        if archive_path.exists() and not archive_path.with_suffix(".json").exists():
            archive_path.unlink(missing_ok=True)
        raise
    _append_audit(app, {
        "operation": "database-backup",
        "outcome": "success",
        "backup_adapter": "verified-dump",
        "backup_label": archive_name,
        "backup_sha256": digest,
        "schema_source_version": plan.source_version,
        "schema_target_version": plan.target_version,
        "migration_plan_fingerprint": plan.fingerprint,
        "pending_migrations": [row[0] for row in plan.pending],
        "commit_sha": request["commit_sha"],
        "workflow_run_id": request["release"]["run_id"],
        "actor": request["release"]["actor"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return archive_name


def _create_verified_production_backup(
    app: dict[str, Any],
    request: dict[str, Any],
    plan: ForwardMigrationPlan,
) -> str:
    policy = _schema_migration_policy(app)
    if policy is None or not plan.needs_migration:
        raise DeploymentError("No production migration is approved for this application.")
    if app.get("database", {}).get("backup_adapter") == "verified-dump":
        env_file = _secure_env_file(app.get("database_env_file"))
        runtime_values = _env_file_values(env_file)
        return _create_verified_dump_backup(app, request, plan, runtime_values)
    backup_policy = app["production_backup"]
    config = _secure_pgbackrest_config(backup_policy["config_file"])
    started = time.time()
    result = run_command(
        [
            "/usr/sbin/runuser", "-u", "postgres", "--", "/usr/bin/pgbackrest", f"--config={config}",
            f"--stanza={backup_policy['stanza']}", f"--repo={backup_policy['repository']}",
            "--type=full", "backup",
        ],
        timeout=backup_policy["timeout_seconds"],
        check=False,
    )
    if result.returncode != 0:
        raise DeploymentError("A fresh full production backup failed; the migration was not started.")
    stanzas = _pgbackrest_json(app)
    stanza = next((item for item in stanzas if item.get("name") == backup_policy["stanza"]), None)
    backups = stanza.get("backup") if isinstance(stanza, dict) else None
    if not isinstance(backups, list):
        raise DeploymentError("The production backup repository did not record the new backup.")
    fresh = []
    for item in backups:
        timestamp = item.get("timestamp") if isinstance(item, dict) else None
        if not isinstance(timestamp, dict):
            continue
        start_time, stop_time = timestamp.get("start"), timestamp.get("stop")
        if (
            item.get("type") == "full"
            and isinstance(start_time, (int, float))
            and isinstance(stop_time, (int, float))
            and start_time >= started - 10
            and stop_time >= start_time
            and stop_time <= time.time() + 10
        ):
            fresh.append(item)
    if not fresh:
        raise DeploymentError("The fresh full production backup could not be verified; the migration was not started.")
    backup = max(fresh, key=lambda item: item["timestamp"]["stop"])
    label = backup.get("label")
    if not isinstance(label, str) or not re.fullmatch(r"[0-9]{8}-[0-9]{6}F", label):
        raise DeploymentError("The fresh full production backup identity is invalid; the migration was not started.")
    _verify_production_backup_integrity(app, label)
    _append_audit(app, {
        "operation": "database-backup",
        "outcome": "success",
        "backup_label": label,
        "backup_type": "full",
        "schema_source_version": plan.source_version,
        "schema_target_version": plan.target_version,
        "migration_plan_fingerprint": plan.fingerprint,
        "pending_migrations": [row[0] for row in plan.pending],
        "commit_sha": request["commit_sha"],
        "workflow_run_id": request["release"]["run_id"],
        "actor": request["release"]["actor"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return label


def _assert_no_application_writers(app: dict[str, Any], env_values: dict[str, str]) -> None:
    policy = _schema_migration_policy(app)
    configured = policy.get("writer_usernames") if policy else None
    usernames = configured if isinstance(configured, list) else [env_values.get("DB_USERNAME", "")]
    url = env_values.get("DB_URL", "")
    match = re.fullmatch(r"jdbc:postgresql://([^/]+)/([A-Za-z0-9_]+)(?:\?.*)?", url)
    if not match or not usernames or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name) for name in usernames):
        raise DeploymentError("The production database identity is invalid for writer quiescence checks.")
    # Every role is constrained to alphanumeric/underscore characters before it
    # is embedded in SQL. psql does not expand :'var' inside -c on all versions.
    role_list = ",".join(f"'{name}'" for name in usernames)
    query = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        f"AND usename IN ({role_list}) AND pid <> pg_backend_pid()"
    )
    host = match.group(1).split(":", 1)[0].strip("[]").lower()
    observer_file = app.get("writer_observer_database_env_file")
    if host in {"localhost", "127.0.0.1", "::1"} and observer_file is None:
        result = run_command(
            [
                "/usr/sbin/runuser", "-u", "postgres", "--", "/usr/bin/psql",
                "-d", match.group(2), "-At", "-v", "ON_ERROR_STOP=1", "-c", query,
            ],
            timeout=15,
        )
    else:
        if (
            not isinstance(observer_file, str)
            or observer_file not in app.get("secret_refs", [])
            or observer_file != f"/etc/roman-platform/{app['app_id']}/writer-observer.env"
        ):
            raise DeploymentError("A dedicated read-only PostgreSQL session observer is required for this database host.")
        observer_path = _secure_env_file(observer_file)
        observer_values = _env_file_values(observer_path)
        if (
            set(observer_values) != {"DB_URL", "DB_USERNAME", "DB_PASSWORD"}
            or observer_values.get("DB_URL") != url
            or observer_values.get("DB_USERNAME", "").lower() in {"postgres", "root", "admin"}
            or observer_values.get("DB_USERNAME") == env_values.get("DB_USERNAME")
            or not observer_values.get("DB_PASSWORD", "").strip()
        ):
            raise DeploymentError("The dedicated PostgreSQL session observer credentials are invalid.")
        observer_env = os.environ.copy()
        observer_env["PGPASSWORD"] = observer_values["DB_PASSWORD"]
        dsn = _pg_connection_uri(url)
        privileges = run_command(
            ["/usr/bin/psql", "--dbname", dsn, "--username", observer_values["DB_USERNAME"], "-At", "-v", "ON_ERROR_STOP=1", "-c", "SELECT pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER')"],
            env=observer_env,
            timeout=15,
            check=False,
            redactions=(observer_values["DB_PASSWORD"], url, observer_values["DB_USERNAME"]),
        )
        if privileges.returncode != 0 or privileges.stdout.strip().lower() != "t":
            raise DeploymentError("The PostgreSQL session observer lacks the required pg_read_all_stats role.")
        result = run_command(
            ["/usr/bin/psql", "--dbname", dsn, "--username", observer_values["DB_USERNAME"], "-At", "-v", "ON_ERROR_STOP=1", "-c", query],
            env=observer_env,
            timeout=15,
            redactions=(observer_values["DB_PASSWORD"], url, observer_values["DB_USERNAME"]),
        )
    try:
        writer_count = int(result.stdout.strip())
    except ValueError as exc:
        raise DeploymentError("The production database writer state could not be verified.") from exc
    if writer_count != 0:
        raise DeploymentError("Application database writers remain active; the migration was not started.")


def _run_approved_migration(
    request: dict[str, Any],
    app: dict[str, Any],
    env_file: Path,
    env_values: dict[str, str],
    plan: ForwardMigrationPlan,
) -> None:
    policy = _schema_migration_policy(app)
    if policy is None or not plan.needs_migration:
        raise DeploymentError("No production migration is approved for this application.")
    expected_release_fingerprint = _release_plan_fingerprint(plan, request, app, env_values)
    if plan.release_fingerprint != expected_release_fingerprint:
        raise DeploymentError("The migration plan is not bound to this exact release and database.")
    release = request.get("release")
    if not isinstance(release, dict):
        raise DeploymentError("Approved migration release identity is unavailable.")
    run_id, run_attempt = release.get("run_id"), release.get("run_attempt")
    if (
        not isinstance(run_id, str)
        or not run_id.isdigit()
        or len(run_id) > 20
        or not isinstance(run_attempt, str)
        or not run_attempt.isdigit()
        or len(run_attempt) > 20
    ):
        raise DeploymentError("Approved migration release identity is invalid.")
    container_name = f"roman-platform-migration-{app['app_id']}-{run_id}-{run_attempt}"
    audit_event = {
        "operation": "database-migration",
        "schema_source_version": plan.source_version,
        "schema_target_version": plan.target_version,
        "pending_migrations": [
            {"version": version, "script": filename, "description": description, "checksum": checksum}
            for version, filename, description, checksum in plan.pending
        ],
        "migration_plan_fingerprint": plan.fingerprint,
        "release_binding_fingerprint": plan.release_fingerprint,
        "commit_sha": request["commit_sha"],
        "migration_image": request["migration_image"],
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "actor": release.get("actor", ""),
    }
    try:
        existing = run_command(["docker", "inspect", container_name], timeout=15, check=False)
    except DeploymentError as exc:
        _append_audit(app, {**audit_event, "outcome": "unknown", "timestamp": datetime.now(timezone.utc).isoformat()})
        raise MigrationOutcomeUnknown("The Flyway migration container state could not be verified.") from exc
    if existing.returncode == 0:
        _append_audit(app, {**audit_event, "outcome": "unknown", "timestamp": datetime.now(timezone.utc).isoformat()})
        raise MigrationOutcomeUnknown("A Flyway migration container already exists; its database outcome must be verified.")
    missing_container = (existing.stderr + existing.stdout).lower()
    if not ("no such object" in missing_container or "no such container" in missing_container):
        _append_audit(app, {**audit_event, "outcome": "unknown", "timestamp": datetime.now(timezone.utc).isoformat()})
        raise MigrationOutcomeUnknown("The Flyway migration container state could not be verified.")

    _append_audit(app, {**audit_event, "outcome": "started", "timestamp": datetime.now(timezone.utc).isoformat()})
    try:
        result = run_command(
            _flyway_container_args(
                request, app, env_file, "migrate", container_name=container_name,
                migration_plan=plan,
            ),
            timeout=int(app.get("migration_timeout_seconds", 180)),
            check=False,
            redactions=(env_values["DB_URL"], env_values["DB_USERNAME"], env_values["DB_PASSWORD"]),
        )
    except DeploymentError as exc:
        _ensure_flyway_container_stopped(container_name)
        _append_audit(app, {**audit_event, "outcome": "unknown", "timestamp": datetime.now(timezone.utc).isoformat()})
        raise MigrationOutcomeUnknown("The Flyway command outcome is unknown; the retained backend remains stopped for recovery.") from exc
    if result.returncode != 0:
        _ensure_flyway_container_stopped(container_name)
        _append_audit(app, {**audit_event, "outcome": "unknown", "timestamp": datetime.now(timezone.utc).isoformat()})
        raise MigrationOutcomeUnknown("Flyway reported a migration failure; the schema outcome requires read-only recovery verification.")
    _append_audit(app, {
        **audit_event,
        "outcome": "success",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _validate_database(
    request: dict[str, Any],
    app: dict[str, Any],
    *,
    allow_pending_migration: bool = False,
    expected_plan: ForwardMigrationPlan | None = None,
) -> dict[str, Any]:
    local_release_image = False
    if "migration_image" not in request:
        database = app.get("database")
        has_flyway = app.get("schema_migration") is not None or (
            isinstance(database, dict) and database.get("migration_adapter") == "flyway"
        )
        if has_flyway:
            if request.get("operation") not in {"verify-stack", "rollback-stack", "refresh-stack"}:
                raise DeploymentError("A verified Flyway image is required to inspect this application's database history.")
            active = _read_state(app["state_file"]).get("active")
            if not isinstance(active, dict) or not isinstance(active.get("migration_image"), str):
                raise DeploymentError("The active release has no retained migration image for schema verification.")
            request = {
                **request,
                "migration_image": active["migration_image"],
                "commit_sha": active["commit_sha"],
                "platform_sha": active["platform_sha"],
            }
            local_release_image = True
        else:
            return _empty_database_status()
    env_file = _secure_env_file(app.get("database_env_file"))
    env_values = _env_file_values(env_file)
    required = {"DB_URL", "DB_USERNAME", "DB_PASSWORD"}
    if required - set(env_values) or any(not env_values.get(key, "").strip() for key in required):
        raise DeploymentError("The staging database environment is incomplete.")
    if not re.fullmatch(r"jdbc:postgresql://[^\s/]+/[^\s]+", env_values["DB_URL"]):
        raise DeploymentError("The staging database connection URL is invalid.")
    allowed_urls = app.get("allowed_database_urls")
    if request.get("operation") in {"deploy-stack", "rollback-stack", "verify-stack", "refresh-stack"} and (not isinstance(allowed_urls, list) or not allowed_urls):
        raise DeploymentError("The isolated database destination is not allowlisted.")
    if allowed_urls is not None and (not isinstance(allowed_urls, list) or env_values["DB_URL"] not in allowed_urls):
        raise DeploymentError("The database destination is not allowlisted.")
    image = request["migration_image"]
    migration_policy = _schema_migration_policy(app)
    started = time.time()
    session = contextlib.nullcontext(os.environ.copy()) if local_release_image else registry_session(app, request)
    with session as env:
        if local_release_image:
            local_digests = run_command(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], env=env, timeout=15)
            try:
                available_digests = json.loads(local_digests.stdout.strip())
            except json.JSONDecodeError as exc:
                raise DeploymentError("The retained migration image digest could not be verified locally.") from exc
            if local_digests.returncode != 0 or not isinstance(available_digests, list) or image not in available_digests:
                raise DeploymentError("The retained migration image is not available at its immutable registry digest.")
        else:
            run_command(["docker", "pull", image], env=env, timeout=600)
        _verify_image(image, request, app, env)
        args = _flyway_container_args(request, app, env_file, "validate")
        run_command(
            args,
            env=env,
            timeout=int(app.get("migration_timeout_seconds", 180)),
            redactions=(env_values["DB_URL"], env_values["DB_USERNAME"], env_values["DB_PASSWORD"]),
        )
        info_args = _flyway_container_args(request, app, env_file, "info")
        result = run_command(
            info_args,
            env=env,
            timeout=int(app.get("migration_timeout_seconds", 180)),
            redactions=(env_values["DB_URL"], env_values["DB_USERNAME"], env_values["DB_PASSWORD"]),
        )
        history_args = _flyway_container_args(request, app, env_file, "history")
        history_result = run_command(
            history_args,
            env=env,
            timeout=int(app.get("migration_timeout_seconds", 180)),
            redactions=(env_values["DB_URL"], env_values["DB_USERNAME"], env_values["DB_PASSWORD"]),
        )
        bound_info = _bind_flyway_plan_checksums(
            result.stdout,
            history_result.stdout,
            image,
            app,
            env,
        )
        max_pending = migration_policy["max_pending"] if migration_policy else 32
        allow_initial = migration_policy["allow_initial_schema"] if migration_policy else False
        if expected_plan is not None:
            try:
                plan = validate_migration_result(
                    bound_info,
                    expected_plan,
                    allowed_exceptions=app.get("flyway_history_exceptions"),
                    expected_flyway_version=app["flyway_cli_version"],
                    expected_schema_name=app.get("flyway_schema"),
                )
            except MigrationPlanError as exc:
                raise DeploymentError(f"Flyway postflight did not match the approved migration plan: {exc}") from exc
        else:
            plan = _verify_flyway_info(
                bound_info,
                allowed_exceptions=app.get("flyway_history_exceptions"),
                flyway_version=app["flyway_cli_version"],
                schema_name=app.get("flyway_schema"),
                max_pending=max_pending,
                allow_initial_schema=allow_initial,
            )
        migration_pending = plan.needs_migration
        if migration_pending and migration_policy is None:
            raise DeploymentError("Pending migrations exist but this application has no approved migration and backup adapters.")
        if migration_pending and not allow_pending_migration:
            raise DeploymentError("Pending migrations require an authorized forward deployment.")
        binding_database_values = env_values
        if migration_pending or (expected_plan is not None and expected_plan.needs_migration):
            _, binding_database_values = _migration_database_env(app, env_values)
        if expected_plan is not None:
            expected_release_fingerprint = _release_plan_fingerprint(
                expected_plan, request, app, binding_database_values,
            )
            if expected_plan.release_fingerprint != expected_release_fingerprint:
                raise DeploymentError("The validated migration plan is bound to a different release or database.")
            plan = ForwardMigrationPlan(
                plan.source_version,
                plan.target_version,
                plan.pending,
                plan.fingerprint,
                plan.history,
                expected_plan.release_fingerprint,
            )
        else:
            plan = ForwardMigrationPlan(
                plan.source_version,
                plan.target_version,
                plan.pending,
                plan.fingerprint,
                plan.history,
                _release_plan_fingerprint(plan, request, app, binding_database_values),
            )
    duration = round(time.time() - started, 3)
    timestamp = datetime.now(timezone.utc).isoformat()
    _append_audit(app, {
        "operation": "deploy-database-preflight" if allow_pending_migration else "validate-database",
        "outcome": "success",
        "commit_sha": request["commit_sha"],
        "migration_image": image,
        "schema_source_version": plan.source_version,
        "schema_target_version": plan.target_version,
        "pending_migrations": [row[0] for row in plan.pending],
        "migration_plan_fingerprint": plan.fingerprint,
        "release_binding_fingerprint": plan.release_fingerprint,
        "workflow_run_id": request["release"]["run_id"],
        "actor": request["release"]["actor"],
        "duration_seconds": duration,
        "timestamp": timestamp,
    })
    metrics_warning = False
    try:
        _write_metrics(
            app,
            _read_state(app["state_file"]),
            "success",
            "deploy-database-preflight" if allow_pending_migration else "validate-database",
            duration,
            request["release"]["run_id"],
            request["commit_sha"],
        )
    except OSError:
        metrics_warning = True
    return {
        "outcome": "success",
        "app_id": app["app_id"],
        "operation": "validate-database",
        "schema_version": plan.source_version,
        "migration_target_version": plan.target_version,
        "migration_image": image,
        "migration_pending": migration_pending,
        "migration_plan": _migration_plan_data(plan),
        "metrics_warning": metrics_warning,
    }


def _verify_active(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    state = _read_state(app["state_file"])
    active = state.get("active")
    if not active or not _release_container_available(app, active):
        raise DeploymentError("There is no verified active release for this application.")
    port = int(app["slot_ports"][active["slot"]])
    _wait_http(
        f"http://127.0.0.1:{port}{app['health_path']}",
        app["expected_health_text"],
        int(app["readiness_timeout_seconds"]),
    )
    _wait_http(app["route_health_url"], app["expected_health_text"], int(app["readiness_timeout_seconds"]))
    return {
        "outcome": "healthy",
        "app_id": app["app_id"],
        "commit_sha": active["commit_sha"],
        "image": active["image"],
        "slot": active["slot"],
    }


def _unhealthy_test(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    if not app.get("unhealthy_test_enabled"):
        raise DeploymentError("Unhealthy-candidate testing is not enabled for this application.")
    state = _read_state(app["state_file"])
    active = state.get("active")
    if not active or not _release_container_available(app, active):
        raise DeploymentError("A healthy retained release is required before an unhealthy-candidate test.")
    _verify_active(request, app)

    image = request["image"]
    with registry_session(app, request) as env:
        run_command(["docker", "pull", image], env=env, timeout=600)
        _verify_image(image, request, app, env)

    run_id = request["release"]["run_id"]
    attempt = request["release"]["run_attempt"]
    container_name = f"roman-platform-{app['app_id']}-unhealthy-{run_id}-{attempt}"
    port = int(app["unhealthy_test_port"])
    started = time.time()
    run_command(
        _container_run_args(app, image, container_name, "test", port, request["commit_sha"], test_mode=True),
        timeout=60,
    )
    readiness_failed = False
    try:
        deadline = time.monotonic() + int(app["unhealthy_test_timeout_seconds"])
        url = f"http://127.0.0.1:{port}{app['health_path']}"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    body = response.read(8192).decode("utf-8", errors="replace")
                    if response.status == 200 and app["expected_health_text"] in body:
                        raise DeploymentError("The deliberately unhealthy candidate unexpectedly passed readiness.")
                    readiness_failed = True
                    break
            except urllib.error.HTTPError:
                readiness_failed = True
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1)
        if not readiness_failed:
            raise DeploymentError("The unhealthy-candidate test did not produce a readiness failure in time.")
    finally:
        run_command(["docker", "rm", "-f", container_name], timeout=30, check=False)

    # The failed candidate must not disturb either the retained container or its route.
    _verify_active(request, app)
    duration = round(time.time() - started, 3)
    timestamp = datetime.now(timezone.utc).isoformat()
    _append_audit(app, {
        "operation": "unhealthy-test",
        "outcome": "failure",
        "commit_sha": request["commit_sha"],
        "image": image,
        "platform_sha": request["platform_sha"],
        "retained_commit_sha": active["commit_sha"],
        "workflow_run_id": run_id,
        "actor": request["release"]["actor"],
        "error": "The candidate failed the configured application readiness check; the prior release stayed healthy.",
        "duration_seconds": duration,
        "timestamp": timestamp,
    })
    metrics_warning = False
    try:
        _write_metrics(
            app,
            _read_state(app["state_file"]),
            "failure",
            "unhealthy-test",
            duration,
            run_id,
            request["commit_sha"],
        )
    except OSError:
        metrics_warning = True
    return {
        "outcome": "expected-rejection",
        "app_id": app["app_id"],
        "candidate_image": image,
        "retained_commit_sha": active["commit_sha"],
        "metrics_warning": metrics_warning,
    }


def _stack_component_app(app: dict[str, Any], name: str) -> dict[str, Any]:
    component = app["components"][name]
    if not APP_ID_RE.fullmatch(name) or not isinstance(component, dict):
        raise DeploymentError("The application component policy is invalid.")
    component_app = {**app, **component, "app_id": f"{app['app_id']}-{name}", "root_app_id": app["app_id"]}
    if not APP_ID_RE.fullmatch(component_app["app_id"]):
        raise DeploymentError("The application component identifier is too long.")
    network_mode = app.get("container_network_mode", "bridge")
    if not app.get("container_network") or component_app.get("container_network") != app["container_network"]:
        raise DeploymentError("Every component must use the approved application network.")
    if component_app.get("container_publish", True):
        raise DeploymentError("Stack components must use private endpoints without published ports.")
    if network_mode == "host":
        if not app.get("allow_host_network") or app.get("container_network") != "host":
            raise DeploymentError("Host networking is not explicitly authorized for this application.")
        if component_app.get("slot_ips") or not isinstance(component_app.get("slot_ports"), dict):
            raise DeploymentError("Host-network stack components require fixed loopback slot ports.")
        bind_variable = component_app.get("host_network_bind_environment_variable")
        if not isinstance(bind_variable, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", bind_variable):
            raise DeploymentError("The host-network loopback bind policy is invalid.")
        port_variable = component_app.get("host_network_port_environment_variable")
        if not isinstance(port_variable, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", port_variable):
            raise DeploymentError("The host-network port policy is invalid.")
        environment_by_slot = component_app.get("container_environment_by_slot")
        if not isinstance(environment_by_slot, dict):
            raise DeploymentError("Host-network slot environments are unavailable.")
        for slot in ("blue", "green"):
            env = environment_by_slot.get(slot)
            port = component_app["slot_ports"].get(slot)
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not isinstance(env, dict)
                or env.get(bind_variable) != "127.0.0.1"
                or env.get(port_variable) != str(port)
            ):
                raise DeploymentError("Host-network components must bind only to loopback.")
        if app.get("stack_test_enabled"):
            test_env = component_app.get("unhealthy_test_environment")
            test_port = component_app["slot_ports"].get("test")
            if (
                isinstance(test_port, bool)
                or not isinstance(test_port, int)
                or not isinstance(test_env, dict)
                or test_env.get(bind_variable) != "127.0.0.1"
                or test_env.get(port_variable) != str(test_port)
            ):
                raise DeploymentError("Host-network failure tests must bind only to loopback.")
    elif network_mode == "bridge":
        if not isinstance(app.get("container_network"), str) or not component_app.get("slot_ips"):
            raise DeploymentError("Stack components require fixed addresses on the approved private network.")
    else:
        raise DeploymentError("The application container network mode is unsupported.")
    listener_port = app.get("private_listener_port")
    if isinstance(listener_port, bool) or not isinstance(listener_port, int) or not 1024 <= listener_port <= 65535:
        raise DeploymentError("The private listener port is invalid.")
    if not re.fullmatch(rf"http://127\.0\.0\.1:{listener_port}/[^\s]*", str(component_app.get("route_health_url", ""))):
        raise DeploymentError("Component route health checks must use a loopback URL.")
    return component_app


def _stack_validate_network(app: dict[str, Any]) -> None:
    if app.get("container_network_mode", "bridge") == "host":
        try:
            if not app.get("allow_host_network") or app.get("container_network") != "host":
                raise ValueError("host mode is not authorized")
            slots = ("blue", "green", "test") if app.get("stack_test_enabled") else ("blue", "green")
            used: set[int] = set()
            for name in sorted(app["components"]):
                component = _stack_component_app(app, name)
                for slot in slots:
                    if slot == "test" and not app.get("stack_test_enabled"):
                        continue
                    port = component["slot_ports"].get(slot)
                    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 or port in used:
                        raise ValueError("host slot port is invalid or duplicated")
                    used.add(port)
            listener_port = app.get("private_listener_port")
            if isinstance(listener_port, bool) or not isinstance(listener_port, int) or not 1024 <= listener_port <= 65535:
                raise ValueError("production listener port is invalid")
            if listener_port in used:
                raise ValueError("production listener conflicts with an application port")
        except (KeyError, TypeError, ValueError) as exc:
            raise DeploymentError("The application host-network port policy is invalid.") from exc
        return
    if app.get("container_network_mode", "bridge") != "bridge":
        raise DeploymentError("The application container network mode is unsupported.")
    try:
        network = ipaddress.ip_network(app["network_subnet"], strict=True)
        if not isinstance(network, ipaddress.IPv4Network) or not any(network.subnet_of(private) for private in (
            ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16")
        )):
            raise ValueError("network must be private IPv4")
        reserved = {ipaddress.IPv4Address(value) for value in app.get("reserved_network_ips", [])}
        if any(address not in network or address in {network.network_address, network.broadcast_address} for address in reserved):
            raise ValueError("reserved address is outside the subnet")
        used = set(reserved)
        for name in sorted(app["components"]):
            component = _stack_component_app(app, name)
            slots = ("blue", "green", "test") if app.get("stack_test_enabled") else ("blue", "green")
            for slot in slots:
                address = ipaddress.IPv4Address(component["slot_ips"][slot])
                if address not in network or address in {network.network_address, network.broadcast_address} or address in used:
                    raise ValueError("component address is outside subnet or duplicated")
                used.add(address)
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentError("The application private network address policy is invalid.") from exc


def _stack_component_network_ip(
    app: dict[str, Any], component_app: dict[str, Any], container: str, slot: str
) -> str:
    network_name = app.get("container_network")
    try:
        expected_ip = str(ipaddress.IPv4Address(component_app["slot_ips"][slot]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentError("The configured private component address is invalid.") from exc
    deadline = time.monotonic() + PRIVATE_NETWORK_READY_TIMEOUT_SECONDS
    while True:
        result = run_command(
            ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container],
            timeout=15,
            check=False,
        )
        try:
            networks = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DeploymentError("The application component network could not be verified.") from exc
        if result.returncode != 0 or not isinstance(networks, dict) or network_name not in networks:
            raise DeploymentError("The application component network could not be verified.")
        if set(networks) != {network_name}:
            raise DeploymentError("The application component is attached to an unapproved private network.")
        network = networks[network_name]
        if not isinstance(network, dict):
            raise DeploymentError("The application component network could not be verified.")
        actual_ip = network.get("IPAddress")
        if isinstance(actual_ip, str) and actual_ip:
            try:
                actual_ip = str(ipaddress.IPv4Address(actual_ip))
            except ipaddress.AddressValueError as exc:
                raise DeploymentError("The application component is not on its approved private address.") from exc
            if actual_ip != expected_ip:
                raise DeploymentError("The application component is not on its approved private address.")
            return actual_ip
        if time.monotonic() >= deadline:
            raise DeploymentError("The application component did not receive its approved private address.")
        time.sleep(PRIVATE_NETWORK_POLL_INTERVAL_SECONDS)


def _stack_release_healthy(app: dict[str, Any], release: dict[str, Any]) -> None:
    slot = release.get("slot")
    components = release.get("components")
    if slot not in {"blue", "green"} or not isinstance(components, dict) or set(components) != set(app["components"]):
        raise DeploymentError("The retained application release state is invalid.")
    for name in sorted(components):
        component_app = _stack_component_app(app, name)
        record = components[name]
        if not isinstance(record, dict) or record.get("slot") != slot or record.get("commit_sha") != release.get("commit_sha"):
            raise DeploymentError("The retained component does not belong to the application release.")
        if not _release_container_available(component_app, record):
            raise DeploymentError("A retained application component is unavailable.")
        if app.get("container_network_mode", "bridge") == "host":
            result = run_command(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", record["container"]], timeout=15, check=False)
            if result.returncode != 0 or result.stdout.strip() != "host":
                raise DeploymentError("The application component is not using its approved loopback host network.")
            actual_ip = "127.0.0.1"
            port = int(component_app["slot_ports"][slot])
        else:
            actual_ip = _stack_component_network_ip(app, component_app, record["container"], slot)
            port = int(component_app["container_port"])
        try:
            _wait_http(
                f"http://{actual_ip}:{port}{component_app['health_path']}",
                component_app["expected_health_text"], int(component_app["readiness_timeout_seconds"]),
            )
        except DeploymentError as exc:
            diagnostic = _format_candidate_startup_diagnostic(
                _candidate_startup_diagnostic(record["container"])
            )
            if diagnostic:
                raise DeploymentError(f"{exc} Component {name} startup diagnostic: {diagnostic}.") from exc
            raise


def _stack_route_healthy(app: dict[str, Any]) -> None:
    for name in sorted(app["components"]):
        component = _stack_component_app(app, name)
        _wait_http(component["route_health_url"], component["expected_health_text"], int(component["readiness_timeout_seconds"]))


def _check_backend_start_memory(app: dict[str, Any]) -> None:
    free_memory_mb = available_memory_bytes() // (1024 * 1024)
    if free_memory_mb < int(app["minimum_available_memory_mb"]):
        raise DeploymentError("Insufficient available memory to start a backend after releasing the active pool.")


def _stop_stack_backend(app: dict[str, Any], release: dict[str, Any]) -> None:
    components = release.get("components")
    record = components.get("backend") if isinstance(components, dict) else None
    backend = _stack_component_app(app, "backend")
    if (
        not isinstance(record, dict)
        or record.get("slot") != release.get("slot")
        or record.get("commit_sha") != release.get("commit_sha")
        or not _release_container_available(backend, record)
    ):
        raise DeploymentError("The active backend identity is invalid; maintenance deployment was not started.")
    status = run_command(["docker", "inspect", "--format", "{{.State.Running}}", record["container"]], timeout=15, check=False)
    if status.returncode != 0 or status.stdout.strip().lower() not in {"true", "false"}:
        raise DeploymentError("The active backend state could not be verified; maintenance deployment was not started.")
    if status.stdout.strip().lower() == "true":
        run_command(["docker", "stop", "--time", "30", record["container"]], timeout=45)
        stopped = run_command(["docker", "inspect", "--format", "{{.State.Running}}", record["container"]], timeout=15, check=False)
        if stopped.returncode != 0 or stopped.stdout.strip().lower() != "false":
            raise DeploymentError("The active backend did not stop before the candidate backend was started.")


def _restart_stack_backend(app: dict[str, Any], release: dict[str, Any]) -> None:
    components = release.get("components")
    record = components.get("backend") if isinstance(components, dict) else None
    backend = _stack_component_app(app, "backend")
    if (
        not isinstance(record, dict)
        or record.get("slot") != release.get("slot")
        or record.get("commit_sha") != release.get("commit_sha")
        or not _release_container_available(backend, record)
    ):
        raise DeploymentError("The retained backend identity is invalid; automatic recovery could not continue.")
    status = run_command(["docker", "inspect", "--format", "{{.State.Running}}", record["container"]], timeout=15, check=False)
    if status.returncode != 0:
        raise DeploymentError("The retained backend state could not be verified; automatic recovery could not continue.")
    if status.stdout.strip().lower() != "true":
        run_command(["docker", "start", record["container"]], timeout=60)
    _stack_release_healthy(app, release)
    _stack_route_healthy(app)


def _verify_stack_active(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    _stack_validate_network(app)
    state = _read_state(app["state_file"])
    active = state.get("active")
    if not isinstance(active, dict):
        raise DeploymentError("There is no active application release to verify.")
    database_status = _validate_database(request, app)
    if active.get("schema_version") != database_status["schema_version"]:
        raise DeploymentError("The active application release does not match the observed database schema.")
    _stack_release_healthy(app, active)
    _stack_route_healthy(app)
    _append_audit(app, {"operation": "verify-stack", "outcome": "success", "commit_sha": active["commit_sha"], "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": datetime.now(timezone.utc).isoformat()})
    metrics_warning = False
    try:
        _write_metrics(app, state, "success", "verify-stack", time.time() - started, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {"outcome": "healthy", "app_id": app["app_id"], "slot": active["slot"], "commit_sha": active["commit_sha"], "metrics_warning": metrics_warning}


def _test_stack_failure(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    _stack_validate_network(app)
    active = _read_state(app["state_file"]).get("active")
    if not isinstance(active, dict):
        raise DeploymentError("A healthy active release is required for a failure test.")
    _stack_release_healthy(app, active)
    _stack_route_healthy(app)
    name = request["component"]
    component = _stack_component_app(app, name)
    test_env = component.get("unhealthy_test_environment")
    if not isinstance(test_env, dict) or not test_env:
        raise DeploymentError("The fixed unhealthy component configuration is unavailable.")
    image = request["image"]
    with registry_session(app, request) as env:
        run_command(["docker", "pull", image], env=env, timeout=600)
        _verify_image(image, request, app, env)
    container = f"roman-platform-{app['app_id']}-{name}-test-{request['release']['run_id']}-{request['release']['run_attempt']}"
    if len(container) > 180 or run_command(["docker", "inspect", container], timeout=15, check=False).returncode == 0:
        raise DeploymentError("The isolated failure-test container name is unavailable.")
    _append_audit(app, {"operation": "test-stack", "outcome": "started", "component": name, "commit_sha": request["commit_sha"], "image": image, "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": datetime.now(timezone.utc).isoformat()})
    attempted = False
    try:
        attempted = True
        run_command(_container_run_args(component, image, container, "test", int(component["container_port"]), request["commit_sha"], test_mode=True), timeout=60)
        try:
            if app.get("container_network_mode", "bridge") == "host":
                test_url = f"http://127.0.0.1:{int(component['slot_ports']['test'])}{component['health_path']}"
            else:
                test_url = f"http://{component['slot_ips']['test']}:{int(component['container_port'])}{component['health_path']}"
            _wait_http(test_url, component["expected_health_text"], int(component["unhealthy_test_timeout_seconds"]))
        except DeploymentError:
            pass
        else:
            raise DeploymentError("The deliberately unhealthy component unexpectedly passed readiness.")
    finally:
        if attempted:
            run_command(["docker", "rm", "-f", container], timeout=30)
    _stack_release_healthy(app, active)
    _stack_route_healthy(app)
    duration = round(time.time() - started, 3)
    _append_audit(app, {"operation": "test-stack", "outcome": "failure", "component": name, "commit_sha": request["commit_sha"], "image": image, "retained_commit_sha": active["commit_sha"], "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": datetime.now(timezone.utc).isoformat(), "error": "The fixed unhealthy candidate was rejected; the active application remained healthy."})
    metrics_warning = False
    try:
        _write_metrics(app, _read_state(app["state_file"]), "failure", "test-stack", duration, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {"outcome": "expected-rejection", "app_id": app["app_id"], "component": name, "retained_commit_sha": active["commit_sha"], "metrics_warning": metrics_warning}


def _switch_stack_route(app: dict[str, Any], slot: str) -> None:
    route_path = Path(app["route_file"])
    try:
        previous_route = route_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentError("The managed application route is unavailable.") from exc
    _atomic_write(route_path, _stack_route_content(app["components"], slot))
    try:
        run_command(["nginx", "-t"], timeout=20)
        run_command(["systemctl", "reload", "nginx"], timeout=20)
        _stack_route_healthy(app)
    except Exception:
        _atomic_write(route_path, previous_route)
        run_command(["nginx", "-t"], timeout=20, check=False)
        run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
        raise


def _deploy_stack(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    database_status = _validate_database(request, app, allow_pending_migration=True)
    validated_plan = _migration_plan_from_data(database_status["migration_plan"])
    state_path = Path(app["state_file"])
    state = _read_state(str(state_path))
    active = state.get("active")
    if not active and state.get("previous"):
        raise DeploymentError("Application release state has a previous release without an active release.")
    if active:
        try:
            _stack_release_healthy(app, active)
        except DeploymentError as exc:
            # Recovery deployments must be able to replace a release that is
            # already failing its HTTP readiness checks. Keep its route intact
            # while the candidate is built and validated; only the candidate's
            # complete health gate may authorize the route switch.
            if (
                not app.get("allow_unhealthy_active_replacement")
                or not str(exc).startswith("Readiness check failed:")
            ):
                raise
            _append_audit(app, {
                "operation": "deploy-stack",
                "outcome": "active-readiness-failed",
                "active_commit_sha": active.get("commit_sha"),
                "workflow_run_id": request["release"]["run_id"],
                "actor": request["release"]["actor"],
                "error": str(exc)[:160],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    slot = "green" if active and active["slot"] == "blue" else "blue"
    # Validate the entire route before pulling images or disturbing retained containers.
    _stack_validate_network(app)
    _stack_route_content(app["components"], "blue")
    _stack_route_content(app["components"], "green")
    with registry_session(app, request) as env:
        for name in sorted(app["components"]):
            image = request["images"][name]
            run_command(["docker", "pull", image], env=env, timeout=600)
            _verify_image(image, request, app, env)
    route_path = Path(app["route_file"])
    prior_route = route_path.read_text(encoding="utf-8")
    prior_state = state_path.read_text(encoding="utf-8") if state_path.exists() else None
    started_names: list[str] = []
    route_switched = False
    backend_maintenance = app.get("single_backend_maintenance") is True and active is not None
    records: dict[str, Any] = {}
    migration_started = False
    try:
        if validated_plan.needs_migration:
            if app.get("single_backend_maintenance") is not True:
                raise DeploymentError("Production migration requires the approved single-backend maintenance policy.")
            migration_policy = _schema_migration_policy(app)
            if migration_policy is None:
                raise DeploymentError("No production migration and backup adapters are approved for this application.")
            if active and active.get("schema_version") != validated_plan.source_version:
                raise DeploymentError("The retained application release does not match the observed migration source schema.")
            backup_label = _create_verified_production_backup(app, request, validated_plan)
            if active:
                _stop_stack_backend(app, active)
            env_file = _secure_env_file(app.get("database_env_file"))
            env_values = _env_file_values(env_file)
            _assert_no_application_writers(app, env_values)
            # Re-read the release-scoped plan after backup and writer quiescence.
            # A concurrent migration invalidates this attempt before Flyway runs.
            latest_database_status = _validate_database(request, app, allow_pending_migration=True)
            latest_plan = _migration_plan_from_data(latest_database_status["migration_plan"])
            if (
                latest_plan.fingerprint != validated_plan.fingerprint
                or latest_plan.release_fingerprint != validated_plan.release_fingerprint
            ):
                raise DeploymentError("Database migration history changed after backup; no migration was run.")
            migration_started = True
            migration_env_file, migration_env_values = _migration_database_env(app, env_values)
            _run_approved_migration(request, app, migration_env_file, migration_env_values, validated_plan)
            database_status = _validate_database(
                request,
                app,
                allow_pending_migration=True,
                expected_plan=validated_plan,
            )
            if database_status["migration_pending"] or database_status["schema_version"] != validated_plan.target_version:
                raise DeploymentError("The verified database schema did not reach the migration plan target.")
            _append_audit(app, {
                "operation": "database-migration",
                "outcome": "verified",
                "backup_label": backup_label,
                "schema_source_version": validated_plan.source_version,
                "schema_target_version": validated_plan.target_version,
                "migration_plan_fingerprint": validated_plan.fingerprint,
                "release_binding_fingerprint": validated_plan.release_fingerprint,
                "pending_migrations": [row[0] for row in validated_plan.pending],
                "commit_sha": request["commit_sha"],
                "workflow_run_id": request["release"]["run_id"],
                "actor": request["release"]["actor"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        _append_audit(app, {"operation": "deploy-stack", "outcome": "started", "commit_sha": request["commit_sha"], "images": request["images"], "platform_sha": request["platform_sha"], "slot": slot, "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": datetime.now(timezone.utc).isoformat()})
        if state.get("previous"):
            # The candidate reuses the retained slot. Clear the pointer before removing
            # anything so an interrupted cleanup cannot advertise a nonexistent rollback.
            state["previous"] = None
            require_storage_mounts(app)
            _atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        component_order = sorted(app["components"])
        if app.get("single_backend_maintenance") is True:
            component_order.sort(key=lambda name: (name == "backend", name))
        for name in component_order:
            if name == "backend" and backend_maintenance and not validated_plan.needs_migration:
                _stop_stack_backend(app, active)
            if name == "backend" and app.get("single_backend_maintenance") is True:
                _check_backend_start_memory(app)
            component_app = _stack_component_app(app, name)
            active_component = (active.get("components") or {}).get(name) if active else None
            _remove_inactive_slot(component_app, slot, active_component)
            container = _container_name(component_app, slot, request["commit_sha"])
            # A Docker client timeout can occur after the daemon created the container.
            # Record its fixed name first so cleanup also covers that case.
            started_names.append(container)
            run_command(_container_run_args(component_app, request["images"][name], container, slot, int(component_app["container_port"]), request["commit_sha"]), timeout=60)
            records[name] = {"slot": slot, "commit_sha": request["commit_sha"], "container": container, "image": request["images"][name]}
        release = {
            "app_id": app["app_id"],
            "slot": slot,
            "commit_sha": request["commit_sha"],
            "platform_sha": request["platform_sha"],
            "schema_version": database_status["schema_version"],
            "migration_image": request.get("migration_image"),
            "components": records,
            "deployed_at": datetime.now(timezone.utc).isoformat(),
        }
        _stack_release_healthy(app, release)
        verify_release_origin(request, app)
        if not validated_plan.needs_migration:
            # App-only releases do not stop writers or create a database backup,
            # but an unexpected schema change during candidate startup must not
            # be hidden by switching traffic using the stale observation.
            latest_database_status = _validate_database(request, app)
            latest_plan = _migration_plan_from_data(latest_database_status["migration_plan"])
            if (
                latest_plan.fingerprint != validated_plan.fingerprint
                or latest_plan.release_fingerprint != validated_plan.release_fingerprint
            ):
                raise DeploymentError("Database migration history changed during the application rollout; traffic was not switched.")
            database_status = latest_database_status
        _switch_stack_route(app, slot)
        route_switched = True
        previous_release = active
        if migration_started or (active and active.get("schema_version") != database_status["schema_version"]):
            # SQL is forward-only. Never advertise an application from a different
            # observed schema as a safe rollback target.
            previous_release = None
        new_state = {"active": release, "previous": previous_release, "updated_at": release["deployed_at"]}
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
        _append_audit(app, {"operation": "deploy-stack", "outcome": "success", "commit_sha": request["commit_sha"], "images": request["images"], "platform_sha": request["platform_sha"], "slot": slot, "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": release["deployed_at"]})
    except Exception as deployment_exc:
        if route_switched:
            _atomic_write(route_path, prior_route)
            run_command(["nginx", "-t"], timeout=20, check=False)
            run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
        if prior_state is None:
            state_path.unlink(missing_ok=True)
        else:
            _atomic_write(state_path, prior_state)
        for container in reversed(started_names):
            run_command(["docker", "rm", "-f", container], timeout=30, check=False)
        if backend_maintenance:
            try:
                if migration_started:
                    if isinstance(deployment_exc, MigrationOutcomeUnknown):
                        raise DeploymentError("The migration outcome could not be verified; the retained backend remains stopped for safe recovery.") from deployment_exc
                    raise DeploymentError("The database was migrated forward; the retained release stays stopped until a compatible fix-forward release or approved restore is available.") from deployment_exc
                _restart_stack_backend(app, active)
            except Exception as recovery_exc:
                if isinstance(deployment_exc, MigrationOutcomeUnknown):
                    message = "The migration outcome could not be verified; the retained backend remains stopped for safe recovery."
                elif migration_started:
                    message = "The database migrated forward; the retained backend remains stopped until a compatible fix-forward release or approved restore is available."
                else:
                    message = "The candidate failed and the retained backend could not be restored automatically."
                raise DeploymentError(message) from recovery_exc
        raise
    metrics_warning = False
    try:
        _write_metrics(app, new_state, "success", "deploy-stack", time.time() - started, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {"outcome": "success", "app_id": app["app_id"], "slot": slot, "commit_sha": request["commit_sha"], "images": request["images"], "metrics_warning": metrics_warning}


def _verify_retained_stack_images(app: dict[str, Any], release: dict[str, Any]) -> None:
    commit_sha = release.get("commit_sha")
    platform_sha = release.get("platform_sha")
    components = release.get("components")
    if (
        not isinstance(commit_sha, str)
        or not SHA_RE.fullmatch(commit_sha)
        or platform_sha not in app.get("allowed_platform_shas", [])
        or not isinstance(platform_sha, str)
        or not SHA_RE.fullmatch(platform_sha)
        or not isinstance(components, dict)
        or set(components) != set(app["components"])
    ):
        raise DeploymentError("The active release provenance is incomplete; runtime refresh was not started.")
    source_revision = _image_source_revision({"commit_sha": commit_sha, "platform_sha": platform_sha}, app)
    for name in sorted(components):
        component_app = _stack_component_app(app, name)
        record = components[name]
        image = record.get("image") if isinstance(record, dict) else None
        namespace = component_app.get("image_namespace")
        if (
            not isinstance(namespace, str)
            or not isinstance(image, str)
            or not re.fullmatch(re.escape(namespace) + r"@sha256:[0-9a-f]{64}", image)
            or record.get("commit_sha") != commit_sha
            or record.get("slot") != release.get("slot")
        ):
            raise DeploymentError("The active release image identity is invalid; runtime refresh was not started.")
        repo_digests = run_command(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], timeout=15)
        try:
            available_digests = json.loads(repo_digests.stdout.strip())
        except json.JSONDecodeError as exc:
            raise DeploymentError("The active image digest could not be verified.") from exc
        if not isinstance(available_digests, list) or image not in available_digests:
            raise DeploymentError("The active image no longer matches its immutable registry digest.")
        configured_image = run_command(["docker", "inspect", "--format", "{{.Config.Image}}", record["container"]], timeout=15)
        if configured_image.stdout.strip() != image:
            raise DeploymentError("The active container image does not match its release record.")
        verify_image_labels(
            image,
            source_revision,
            commit_sha,
            platform_sha,
            app["image_package_repository"],
            app["image_source_repository"],
            os.environ.copy(),
        )


def _refresh_stack(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    database_status = _validate_database(request, app)
    _stack_validate_network(app)
    state_path = Path(app["state_file"])
    state = _read_state(str(state_path))
    active = state.get("active")
    if (
        not isinstance(active, dict)
        or active.get("schema_version") != database_status["schema_version"]
        or active.get("slot") not in {"blue", "green"}
    ):
        raise DeploymentError("A current active release is required before runtime-only refresh.")
    _stack_release_healthy(app, active)
    _stack_route_healthy(app)
    _verify_retained_stack_images(app, active)

    slot = "green" if active["slot"] == "blue" else "blue"
    route_path = Path(app["route_file"])
    prior_route = route_path.read_text(encoding="utf-8")
    if state.get("previous"):
        # The inactive slot is reused below, so its older retained release cannot remain rollbackable.
        state["previous"] = None
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    prior_state = state_path.read_text(encoding="utf-8") if state_path.exists() else None
    _append_audit(app, {
        "operation": "refresh-stack",
        "outcome": "started",
        "commit_sha": active["commit_sha"],
        "workflow_sha": request["workflow_sha"],
        "platform_sha": request["platform_sha"],
        "workflow_run_id": request["release"]["run_id"],
        "actor": request["release"]["actor"],
        "slot": slot,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    backend_maintenance = app.get("single_backend_maintenance") is True
    started_names: list[str] = []
    route_switched = False
    records: dict[str, Any] = {}
    try:
        component_order = sorted(app["components"])
        if backend_maintenance:
            component_order.sort(key=lambda name: (name == "backend", name))
        for name in component_order:
            component_app = _stack_component_app(app, name)
            active_component = (active.get("components") or {}).get(name)
            if name == "backend" and backend_maintenance:
                _stop_stack_backend(app, active)
                _check_backend_start_memory(app)
            _remove_inactive_slot(component_app, slot, active_component)
            record = active_component
            container = _container_name(component_app, slot, active["commit_sha"])
            started_names.append(container)
            run_command(
                _container_run_args(
                    component_app,
                    record["image"],
                    container,
                    slot,
                    int(component_app["container_port"]),
                    active["commit_sha"],
                ),
                timeout=60,
            )
            records[name] = {
                "slot": slot,
                "commit_sha": active["commit_sha"],
                "container": container,
                "image": record["image"],
            }
        timestamp = datetime.now(timezone.utc).isoformat()
        refreshed_release = {
            **active,
            "slot": slot,
            "components": records,
            "runtime_config_refreshed_at": timestamp,
            "runtime_config_workflow_sha": request["workflow_sha"],
            "runtime_config_platform_sha": request["platform_sha"],
            "runtime_config_run_id": request["release"]["run_id"],
        }
        _stack_release_healthy(app, refreshed_release)
        verify_release_origin(request, app)
        _switch_stack_route(app, slot)
        route_switched = True
        new_state = {"active": refreshed_release, "previous": active, "updated_at": timestamp}
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
        _append_audit(app, {
            "operation": "refresh-stack",
            "outcome": "success",
            "commit_sha": active["commit_sha"],
            "workflow_sha": request["workflow_sha"],
            "platform_sha": request["platform_sha"],
            "workflow_run_id": request["release"]["run_id"],
            "actor": request["release"]["actor"],
            "slot": slot,
            "timestamp": timestamp,
        })
    except Exception:
        if route_switched:
            _atomic_write(route_path, prior_route)
            run_command(["nginx", "-t"], timeout=20, check=False)
            run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
            if prior_state is None:
                state_path.unlink(missing_ok=True)
            else:
                _atomic_write(state_path, prior_state)
        for container in reversed(started_names):
            run_command(["docker", "rm", "-f", container], timeout=30, check=False)
        if backend_maintenance:
            try:
                _restart_stack_backend(app, active)
            except Exception as recovery_exc:
                raise DeploymentError("Runtime refresh failed and the retained backend could not be restored automatically.") from recovery_exc
        raise

    metrics_warning = False
    try:
        _write_metrics(app, new_state, "success", "refresh-stack", time.time() - started, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {
        "outcome": "success",
        "operation": "refresh-stack",
        "app_id": app["app_id"],
        "slot": slot,
        "commit_sha": active["commit_sha"],
        "images": {name: record["image"] for name, record in records.items()},
        "metrics_warning": metrics_warning,
    }


def _start_retained_stack(app: dict[str, Any], release: dict[str, Any]) -> list[str]:
    components = release.get("components")
    if not isinstance(components, dict) or set(components) != set(app["components"]):
        raise DeploymentError("The retained application release state is invalid.")
    started: list[str] = []
    try:
        for name in sorted(components):
            component = _stack_component_app(app, name)
            record = components[name]
            if not isinstance(record, dict) or record.get("slot") != release.get("slot") or record.get("commit_sha") != release.get("commit_sha") or not _release_container_available(component, record):
                raise DeploymentError("A retained application component has an invalid identity.")
            result = run_command(["docker", "inspect", "--format", "{{.State.Running}}", record["container"]], timeout=15)
            if result.stdout.strip().lower() != "true":
                started.append(record["container"])
                run_command(["docker", "start", record["container"]], timeout=60)
        _stack_release_healthy(app, release)
        return started
    except Exception:
        for name in reversed(started):
            run_command(["docker", "stop", name], timeout=60, check=False)
        raise


def _rollback_stack(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    database_status = _validate_database(request, app)
    state_path = Path(app["state_file"])
    state = _read_state(str(state_path))
    active, previous = state.get("active"), state.get("previous")
    if (
        not active
        or not previous
        or active.get("schema_version") != database_status["schema_version"]
        or previous.get("schema_version") != database_status["schema_version"]
        or active.get("slot") == previous.get("slot")
    ):
        raise DeploymentError("No retained application release matches the current schema policy.")
    _stack_validate_network(app)
    _append_audit(app, {"operation": "rollback-stack", "outcome": "started", "from_commit_sha": active["commit_sha"], "to_commit_sha": previous["commit_sha"], "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": datetime.now(timezone.utc).isoformat()})
    route_path = Path(app["route_file"])
    prior_route = route_path.read_text(encoding="utf-8")
    prior_state = state_path.read_text(encoding="utf-8")
    switched = False
    started_names: list[str] = []
    backend_maintenance = app.get("single_backend_maintenance") is True
    try:
        if backend_maintenance:
            _stop_stack_backend(app, active)
            _check_backend_start_memory(app)
        started_names = _start_retained_stack(app, previous)
        verify_release_origin(request, app)
        _switch_stack_route(app, previous["slot"])
        switched = True
        timestamp = datetime.now(timezone.utc).isoformat()
        new_state = {"active": previous, "previous": active, "updated_at": timestamp}
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
        _append_audit(app, {"operation": "rollback-stack", "outcome": "success", "from_commit_sha": active["commit_sha"], "to_commit_sha": previous["commit_sha"], "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": timestamp})
    except Exception:
        if switched:
            _atomic_write(route_path, prior_route)
            run_command(["nginx", "-t"], timeout=20, check=False)
            run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
            _atomic_write(state_path, prior_state)
        for name in reversed(started_names):
            run_command(["docker", "stop", name], timeout=60, check=False)
        if backend_maintenance:
            try:
                _restart_stack_backend(app, active)
            except Exception as recovery_exc:
                raise DeploymentError("Rollback failed and the active backend could not be restored automatically.") from recovery_exc
        raise
    metrics_warning = False
    try:
        _write_metrics(app, new_state, "success", "rollback-stack", time.time() - started, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {"outcome": "success", "app_id": app["app_id"], "slot": previous["slot"], "commit_sha": previous["commit_sha"], "operation": "rollback-stack", "metrics_warning": metrics_warning}

def _rollback(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    state_path = app["state_file"]
    state = _read_state(state_path)
    active = state.get("active")
    previous = state.get("previous")
    if not active or not previous:
        raise DeploymentError("There is no retained previous release to roll back to.")
    if previous.get("slot") not in app["slot_ports"]:
        raise DeploymentError("Previous release state has an invalid slot.")
    if not _release_container_available(app, previous):
        raise DeploymentError("Previous release container is no longer available.")
    port = int(app["slot_ports"][previous["slot"]])
    _wait_http(
        f"http://127.0.0.1:{port}{app['health_path']}",
        app["expected_health_text"],
        int(app["readiness_timeout_seconds"]),
    )
    route_path = Path(app["route_file"])
    prior_route = route_path.read_text(encoding="utf-8")
    prior_state = Path(state_path).read_text(encoding="utf-8")
    route_switched = False
    try:
        switch_route(app, port)
        route_switched = True
        timestamp = datetime.now(timezone.utc).isoformat()
        new_state = {"active": previous, "previous": active, "updated_at": timestamp}
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
        _append_audit(app, {
            "operation": "rollback",
            "outcome": "success",
            "from_commit_sha": active["commit_sha"],
            "to_commit_sha": previous["commit_sha"],
            "workflow_run_id": request["release"]["run_id"],
            "actor": request["release"]["actor"],
            "timestamp": timestamp,
        })
    except Exception:
        if route_switched:
            _atomic_write(route_path, prior_route)
            run_command(["nginx", "-t"], timeout=20, check=False)
            run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
            _atomic_write(state_path, prior_state)
        raise
    metrics_warning = False
    try:
        _write_metrics(
            app,
            new_state,
            "success",
            "rollback",
            time.time() - started,
            request["release"]["run_id"],
            request["commit_sha"],
        )
    except OSError:
        metrics_warning = True
    return {
        "outcome": "success",
        "app_id": app["app_id"],
        "commit_sha": previous["commit_sha"],
        "slot": previous["slot"],
        "operation": "rollback",
        "metrics_warning": metrics_warning,
    }


def execute(request: Any, config: dict[str, Any]) -> dict[str, Any]:
    app, request = validate_request(request, config)
    lock = _lock(app)
    started = time.time()
    try:
        try:
            verification = request["operation"] in {"verify", "verify-stack"}
            single_backend_start = app.get("single_backend_maintenance") is True and request["operation"] in {"deploy-stack", "rollback-stack", "refresh-stack"}
            preflight(app, capacity_checks=not verification, memory_check=not verification and not single_backend_start)
            verify_release_origin(request, app)
            if request["operation"] == "deploy":
                return _deploy(request, app)
            if request["operation"] == "deploy-service":
                return _deploy_service(request, app)
            if request["operation"] == "rollback-service":
                return _rollback_service(request, app)
            if request["operation"] == "rollback":
                return _rollback(request, app)
            if request["operation"] == "validate-database":
                return _validate_database(request, app)
            if request["operation"] == "verify":
                return _verify_active(request, app)
            if request["operation"] == "unhealthy-test":
                return _unhealthy_test(request, app)
            if request["operation"] == "deploy-stack":
                return _deploy_stack(request, app)
            if request["operation"] == "rollback-stack":
                return _rollback_stack(request, app)
            if request["operation"] == "test-stack":
                return _test_stack_failure(request, app)
            if request["operation"] == "verify-stack":
                return _verify_stack_active(request, app)
            if request["operation"] == "refresh-stack":
                return _refresh_stack(request, app)
            raise DeploymentError("Coordinated application deployment is not yet enabled.")
        except Exception as exc:
            event = {
                "operation": request["operation"],
                "outcome": "failure",
                "commit_sha": request["commit_sha"],
                "image": request.get("image"),
                "migration_image": request.get("migration_image"),
                "workflow_run_id": request["release"]["run_id"],
                "actor": request["release"]["actor"],
                "error": str(exc)[:400],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            with contextlib.suppress(Exception):
                _append_audit(app, event)
            with contextlib.suppress(Exception):
                _write_metrics(
                    app,
                    _read_state(app["state_file"]),
                    "failure",
                    request["operation"],
                    time.time() - started,
                    request["release"]["run_id"],
                    request["commit_sha"],
                )
            raise
    finally:
        lock.close()


def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("ROMAN_PLATFORM_CONFIG", str(CONFIG_PATH)))
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError("Server deployment policy is unavailable or invalid.") from exc
    try:
        validate_registry(config)
    except RegistryError as exc:
        raise DeploymentError(f"Server application registry is invalid: {exc}") from exc
    return config


def read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise DeploymentError("Request is empty or exceeds the size limit.")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("Request is not valid UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise DeploymentError("Request must be a JSON object.")
    return value


def main() -> int:
    if sys.argv[1:] != ["--ssh"]:
        print("This command accepts only the restricted SSH request interface.", file=sys.stderr)
        return 2
    if os.environ.get("SSH_ORIGINAL_COMMAND", ""):
        print("Remote commands are not accepted.", file=sys.stderr)
        return 2
    try:
        result = execute(read_request(), load_config())
    except DeploymentError as exc:
        print(json.dumps({"outcome": "rejected", "error": str(exc)}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"outcome": "failed", "error": "Unexpected controller failure."}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


try:
    import fcntl
except ImportError:  # Allows request-policy tests to run on Windows.
    fcntl = None  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
