#!/usr/bin/env python3
"""Restricted, allowlist-based deployment entry point for roman-server."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_PATH = Path("/etc/roman-platform/applications.json")
MAX_REQUEST_BYTES = 32 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class DeploymentError(Exception):
    """A safe, user-facing deployment rejection or operational failure."""


def _text(value: Any, name: str, limit: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value or "\r" in value:
        raise DeploymentError(f"Invalid {name}.")
    return value


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
        "unhealthy-test": {"image", "registry_token"},
        "validate-database": {"migration_image", "registry_token"},
        "deploy-stack": {"images", "strategy", "migration_image", "registry_token"},
        "rollback-stack": {"migration_image", "registry_token"},
        "test-stack": {"image", "component", "registry_token"},
        "verify-stack": set(),
        "verify": set(),
        "rollback": set(),
    }
    if operation not in operation_fields:
        raise DeploymentError("Request contains unsupported fields or operation.")
    allowed = common | operation_fields[operation]
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
    if not isinstance(release, dict) or set(release) != {"run_id", "run_attempt", "actor"}:
        raise DeploymentError("Request is missing approved release metadata.")
    for key in ("run_id", "run_attempt"):
        if not isinstance(release[key], str) or not release[key].isdigit() or len(release[key]) > 20:
            raise DeploymentError("Invalid workflow run metadata.")
    _text(release["actor"], "workflow actor", 100)
    _secret(request["github_token"], "GitHub token")

    if operation in {"deploy", "unhealthy-test"}:
        if operation == "unhealthy-test" and not app.get("unhealthy_test_enabled"):
            raise DeploymentError("Unhealthy-candidate testing is not enabled for this application.")
        if operation == "deploy" and request.get("strategy") != app["strategy"]:
            raise DeploymentError("Deployment strategy does not match server policy.")
        image = _text(request["image"], "image digest reference", 300)
        image_namespace = re.escape(app["image_namespace"])
        if not re.fullmatch(image_namespace + r"@sha256:[0-9a-f]{64}", image):
            raise DeploymentError("Image must use the allowlisted GHCR package and immutable digest.")
        _secret(request["registry_token"], "registry token")
    elif operation in {"validate-database", "deploy-stack", "rollback-stack"}:
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
    run_id = request["release"]["run_id"]
    run = github_json(
        app["workflow_repository"],
        f"actions/runs/{run_id}",
        request["github_token"],
    )
    expected_path = app["workflow_path"]
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
        run.get("event") != app["workflow_event"]
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
    if request["operation"] in {"deploy-stack", "rollback-stack", "test-stack", "verify-stack"}:
        verify_required_ci(request, app)


def verify_required_ci(request: dict[str, Any], app: dict[str, Any]) -> None:
    workflow = app.get("required_ci_workflow_file")
    if not isinstance(workflow, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow):
        raise DeploymentError("The required CI workflow is not configured.")
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


def preflight(app: dict[str, Any]) -> None:
    require_storage_mounts(app)
    root = os.path.realpath(docker_root())
    expected_root = os.path.realpath(app["docker_root_prefix"])
    if root != expected_root and not root.startswith(expected_root.rstrip(os.sep) + os.sep):
        raise DeploymentError("Docker storage is outside the approved applications SSD.")
    storage_root = app["storage_root"]
    if not os.path.isdir(storage_root):
        raise DeploymentError("Approved application storage directory is missing.")
    free_bytes = shutil.disk_usage(storage_root).free
    if free_bytes < int(app["minimum_free_gib"]) * 1024**3:
        raise DeploymentError("Insufficient free space on the applications SSD.")
    if os.cpu_count() is None or os.cpu_count() < int(app["minimum_cpu_cores"]):
        raise DeploymentError("Insufficient CPU capacity for the configured deployment strategy.")
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
            if not isinstance(evidence, dict) or evidence.get("source_sha256") != app["backup_marker_expected_source_sha256"] or evidence.get("schema_version") != str(app["expected_schema_version"]):
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
        args.extend(["--env-file", str(_secure_env_file(env_file))])
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
        raise DeploymentError("Image caller-revision label does not match the authorized RomanLMS commit.")


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
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = type(exc).__name__
        time.sleep(1)
    raise DeploymentError(f"Readiness check failed: {last_error}.")


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
            if (
                not isinstance(public_health_path, str)
                or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", public_health_path)
                or not public_health_path.startswith(prefix)
                or public_health_path in health_paths
            ):
                raise DeploymentError("The application public health route is invalid.")
            health_paths.add(public_health_path)
            blocks.append(
                f"location = {public_health_path} {{\n"
                f"    proxy_pass http://{address}:{port}{public_health_path};\n"
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


def _verify_flyway_info(output: str, expected_version: str, allowed_exceptions: list[dict[str, str]] | None = None, *, flyway_version: str | None = None, schema_name: str | None = None) -> None:
    try:
        info = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DeploymentError("Flyway did not return a valid database compatibility report.") from exc
    if not isinstance(info, dict) or str(info.get("schemaVersion")) != expected_version:
        raise DeploymentError("Database schema version does not match the approved release.")
    if flyway_version and info.get("flywayVersion") != flyway_version:
        raise DeploymentError("Flyway version does not match the approved release.")
    if schema_name and info.get("schemaName") != schema_name:
        raise DeploymentError("Flyway selected an unapproved database schema.")
    if info.get("warnings") not in (None, []):
        raise DeploymentError("Flyway reported warnings requiring review.")
    migrations = info.get("migrations")
    if not isinstance(migrations, list) or not migrations:
        raise DeploymentError("Flyway migration history is unavailable.")
    remaining = list(allowed_exceptions or [])
    fields = {"state", "category", "type", "version", "description", "filepath"}
    if any(not isinstance(rule, dict) or set(rule) != fields or any(not isinstance(value, str) for value in rule.values()) for rule in remaining):
        raise DeploymentError("The approved migration history exceptions are invalid.")
    for item in migrations:
        if not isinstance(item, dict) or not isinstance(item.get("state"), str):
            raise DeploymentError("Flyway migration history is invalid.")
        if item["state"] != "Success":
            match = next((rule for rule in remaining if all(item.get(key) == value for key, value in rule.items())), None)
            if match is None:
                raise DeploymentError("Database migration history is not compatible with the approved release.")
            remaining.remove(match)
    if remaining:
        raise DeploymentError("Approved migration history exceptions are missing.")


def _validate_database(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    env_file = _secure_env_file(app.get("database_env_file"))
    env_values = _env_file_values(env_file)
    required = {"DB_URL", "DB_USERNAME", "DB_PASSWORD"}
    if required - set(env_values) or any(not env_values.get(key, "").strip() for key in required):
        raise DeploymentError("The staging database environment is incomplete.")
    if not re.fullmatch(r"jdbc:postgresql://[^\s/]+/[^\s]+", env_values["DB_URL"]):
        raise DeploymentError("The staging database connection URL is invalid.")
    allowed_urls = app.get("allowed_database_urls")
    if request.get("operation") in {"deploy-stack", "rollback-stack"} and (not isinstance(allowed_urls, list) or not allowed_urls):
        raise DeploymentError("The isolated database destination is not allowlisted.")
    if allowed_urls is not None and (not isinstance(allowed_urls, list) or env_values["DB_URL"] not in allowed_urls):
        raise DeploymentError("The database destination is not allowlisted.")
    image = request["migration_image"]
    started = time.time()
    with registry_session(app, request) as env:
        run_command(["docker", "pull", image], env=env, timeout=600)
        _verify_image(image, request, app, env)
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
            "--env", f"EXPECTED_TARGET_VERSION={app['expected_schema_version']}",
            "--env", f"FLYWAY_CLI_VERSION={app['flyway_cli_version']}",
            image,
            "validate",
        ]
        run_command(
            args,
            env=env,
            timeout=int(app.get("migration_timeout_seconds", 180)),
            redactions=(env_values["DB_URL"], env_values["DB_USERNAME"], env_values["DB_PASSWORD"]),
        )
        info_args = args[:-1] + ["info", "-outputType=json"]
        result = run_command(
            info_args,
            env=env,
            timeout=int(app.get("migration_timeout_seconds", 180)),
            redactions=(env_values["DB_URL"], env_values["DB_USERNAME"], env_values["DB_PASSWORD"]),
        )
        _verify_flyway_info(result.stdout, str(app["expected_schema_version"]), app.get("flyway_history_exceptions"), flyway_version=app["flyway_cli_version"], schema_name=app.get("flyway_schema"))
    duration = round(time.time() - started, 3)
    timestamp = datetime.now(timezone.utc).isoformat()
    _append_audit(app, {
        "operation": "validate-database",
        "outcome": "success",
        "commit_sha": request["commit_sha"],
        "migration_image": image,
        "expected_schema_version": str(app["expected_schema_version"]),
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
            "validate-database",
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
        "expected_schema_version": str(app["expected_schema_version"]),
        "migration_image": image,
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
    component_app = {**app, **component, "app_id": f"{app['app_id']}-{name}"}
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
            result = run_command(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", record["container"]], timeout=15, check=False)
            try:
                networks = json.loads(result.stdout)
                actual_ip = networks[app["container_network"]]["IPAddress"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise DeploymentError("The application component network could not be verified.") from exc
            if result.returncode != 0 or actual_ip != component_app["slot_ips"][slot] or set(networks) != {app["container_network"]}:
                raise DeploymentError("The application component is not on its approved private address.")
            port = int(component_app["container_port"])
        _wait_http(
            f"http://{actual_ip}:{port}{component_app['health_path']}",
            component_app["expected_health_text"], int(component_app["readiness_timeout_seconds"]),
        )


def _stack_route_healthy(app: dict[str, Any]) -> None:
    for name in sorted(app["components"]):
        component = _stack_component_app(app, name)
        _wait_http(component["route_health_url"], component["expected_health_text"], int(component["readiness_timeout_seconds"]))


def _verify_stack_active(request: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    _stack_validate_network(app)
    state = _read_state(app["state_file"])
    active = state.get("active")
    if not isinstance(active, dict):
        raise DeploymentError("There is no active application release to verify.")
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
    _validate_database(request, app)
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
    _append_audit(app, {"operation": "deploy-stack", "outcome": "started", "commit_sha": request["commit_sha"], "images": request["images"], "platform_sha": request["platform_sha"], "slot": slot, "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": datetime.now(timezone.utc).isoformat()})
    if state.get("previous"):
        # The candidate reuses the retained slot. Clear the pointer before removing
        # anything so an interrupted cleanup cannot advertise a nonexistent rollback.
        state["previous"] = None
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    route_path = Path(app["route_file"])
    prior_route = route_path.read_text(encoding="utf-8")
    prior_state = state_path.read_text(encoding="utf-8") if state_path.exists() else None
    started_names: list[str] = []
    route_switched = False
    records: dict[str, Any] = {}
    try:
        for name in sorted(app["components"]):
            component_app = _stack_component_app(app, name)
            active_component = (active.get("components") or {}).get(name) if active else None
            _remove_inactive_slot(component_app, slot, active_component)
            container = _container_name(component_app, slot, request["commit_sha"])
            # A Docker client timeout can occur after the daemon created the container.
            # Record its fixed name first so cleanup also covers that case.
            started_names.append(container)
            run_command(_container_run_args(component_app, request["images"][name], container, slot, int(component_app["container_port"]), request["commit_sha"]), timeout=60)
            records[name] = {"slot": slot, "commit_sha": request["commit_sha"], "container": container, "image": request["images"][name]}
        release = {"app_id": app["app_id"], "slot": slot, "commit_sha": request["commit_sha"], "platform_sha": request["platform_sha"], "schema_version": str(app["expected_schema_version"]), "components": records, "deployed_at": datetime.now(timezone.utc).isoformat()}
        _stack_release_healthy(app, release)
        verify_release_origin(request, app)
        _switch_stack_route(app, slot)
        route_switched = True
        new_state = {"active": release, "previous": active, "updated_at": release["deployed_at"]}
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
        _append_audit(app, {"operation": "deploy-stack", "outcome": "success", "commit_sha": request["commit_sha"], "images": request["images"], "platform_sha": request["platform_sha"], "slot": slot, "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": release["deployed_at"]})
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
        raise
    metrics_warning = False
    try:
        _write_metrics(app, new_state, "success", "deploy-stack", time.time() - started, request["release"]["run_id"], request["commit_sha"])
    except OSError:
        metrics_warning = True
    return {"outcome": "success", "app_id": app["app_id"], "slot": slot, "commit_sha": request["commit_sha"], "images": request["images"], "metrics_warning": metrics_warning}


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
    _validate_database(request, app)
    state_path = Path(app["state_file"])
    state = _read_state(str(state_path))
    active, previous = state.get("active"), state.get("previous")
    if not active or not previous or previous.get("schema_version") != str(app["expected_schema_version"]) or active.get("slot") == previous.get("slot"):
        raise DeploymentError("No retained application release matches the current schema policy.")
    _stack_validate_network(app)
    _append_audit(app, {"operation": "rollback-stack", "outcome": "started", "from_commit_sha": active["commit_sha"], "to_commit_sha": previous["commit_sha"], "workflow_run_id": request["release"]["run_id"], "actor": request["release"]["actor"], "timestamp": datetime.now(timezone.utc).isoformat()})
    route_path = Path(app["route_file"])
    prior_route = route_path.read_text(encoding="utf-8")
    prior_state = state_path.read_text(encoding="utf-8")
    switched = False
    started_names: list[str] = []
    try:
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
            preflight(app)
            verify_release_origin(request, app)
            if request["operation"] == "deploy":
                return _deploy(request, app)
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
    if not isinstance(config, dict) or not isinstance(config.get("applications"), dict):
        raise DeploymentError("Server deployment policy is invalid.")
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
