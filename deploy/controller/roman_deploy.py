#!/usr/bin/env python3
"""Restricted, allowlist-based deployment entry point for roman-server."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
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
    allowed = common | ({"image", "strategy", "registry_token"} if operation == "deploy" else set())
    if operation not in {"deploy", "rollback"} or set(request) - allowed:
        raise DeploymentError("Request contains unsupported fields or operation.")
    required = common | ({"image", "strategy", "registry_token"} if operation == "deploy" else set())
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

    if operation == "deploy":
        if request["strategy"] != app["strategy"]:
            raise DeploymentError("Deployment strategy does not match server policy.")
        image = _text(request["image"], "image digest reference", 300)
        image_namespace = re.escape(app["image_namespace"])
        if not re.fullmatch(image_namespace + r"@sha256:[0-9a-f]{64}", image):
            raise DeploymentError("Image must use the allowlisted GHCR package and immutable digest.")
        _secret(request["registry_token"], "registry token")
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
    marker = app.get("backup_marker")
    if marker:
        path = Path(marker)
        max_age = int(app["backup_max_age_hours"]) * 3600
        if not path.is_file() or time.time() - path.stat().st_mtime > max_age:
            raise DeploymentError("The application backup gate is not satisfied.")


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


def _remove_inactive_slot(app: dict[str, Any], slot: str, active: dict[str, Any] | None) -> None:
    containers = _active_containers(app, slot)
    if active and active.get("slot") == slot and active.get("container") in containers:
        raise DeploymentError("Refusing to remove the currently active release.")
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
        verify_image_labels(
            request["image"],
            request["platform_sha"],
            request["commit_sha"],
            request["platform_sha"],
            app["image_package_repository"],
            app["image_source_repository"],
            env,
        )

    previous = state.get("previous")
    if previous:
        _remove_inactive_slot(app, previous["slot"], active)
        state["previous"] = None
        require_storage_mounts(app)
        _atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    else:
        _remove_inactive_slot(app, candidate_slot, active)

    run_command([
        "docker", "run", "--detach",
        "--name", candidate_name,
        "--restart", "unless-stopped",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(int(app["pids_limit"])),
        "--memory", app["container_memory"],
        "--cpus", str(app["container_cpus"]),
        "--publish", f"127.0.0.1:{candidate_port}:{int(app['container_port'])}",
        "--label", f"roman.platform.app={app['app_id']}",
        "--label", f"roman.platform.slot={candidate_slot}",
        "--label", f"io.roman.deployment.caller-revision={request['commit_sha']}",
        request["image"],
    ], timeout=60)

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
    except Exception:
        if route_switched:
            _atomic_write(route_path, prior_route)
            run_command(["nginx", "-t"], timeout=20, check=False)
            run_command(["systemctl", "reload", "nginx"], timeout=20, check=False)
            if prior_state is None:
                state_path_obj.unlink(missing_ok=True)
            else:
                _atomic_write(state_path, prior_state)
        run_command(["docker", "rm", "-f", candidate_name], timeout=30, check=False)
        raise

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
    if previous.get("container") not in _active_containers(app, previous["slot"]):
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
            return _rollback(request, app)
        except Exception as exc:
            event = {
                "operation": request["operation"],
                "outcome": "failure",
                "commit_sha": request["commit_sha"],
                "image": request.get("image"),
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
