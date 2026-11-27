"""Typed, root-owned application registry validation for Roman Infrastructure."""
from __future__ import annotations

import ipaddress
import json
import re
import hashlib
from typing import Any


APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROFILES = frozenset({
    "static-web", "backend-api", "full-stack", "multi-container",
    "worker", "scheduled-job", "stateful-service", "game-server", "build-only",
})
HTTP_PROFILES = frozenset({"static-web", "backend-api"})
STACK_PROFILES = frozenset({"full-stack", "multi-container"})
MIGRATION_SCRIPT = re.compile(r"V(?P<version>[0-9]+(?:[._][0-9]+)*)__([A-Za-z0-9]+(?:[ _][A-Za-z0-9]+)*)\.sql\Z")


def _version_parts(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*", value):
        return None
    return tuple(int(part) for part in value.split("."))


def _validate_schema_compatibility_evidence(app: dict[str, Any]) -> None:
    """Validate exact, root-owned exceptions for reviewed forward schema drift."""
    entries = app.get("schema_compatibility_evidence", [])
    if not isinstance(entries, list) or len(entries) > 16:
        raise RegistryError("schema compatibility evidence must be a bounded list")
    database = app.get("database")
    if entries and (not isinstance(database, dict) or database.get("migration_adapter") != "flyway"):
        raise RegistryError("schema compatibility evidence requires the Flyway adapter")
    seen_ids: set[str] = set()
    for entry in entries:
        fields = {
            "id", "recorded_schema_version", "observed_schema_version", "target_schema_version",
            "target_commit_sha", "target_platform_sha", "effect", "applied_migration", "pending_migrations", "retained_release",
        }
        if not isinstance(entry, dict) or set(entry) != fields:
            raise RegistryError("schema compatibility evidence fields are incomplete or unsupported")
        evidence_id = entry.get("id")
        if not isinstance(evidence_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", evidence_id) or evidence_id in seen_ids:
            raise RegistryError("schema compatibility evidence ID is invalid or duplicated")
        seen_ids.add(evidence_id)
        recorded = _version_parts(entry.get("recorded_schema_version"))
        observed = _version_parts(entry.get("observed_schema_version"))
        target = _version_parts(entry.get("target_schema_version"))
        if recorded is None or observed is None or target is None or not recorded < observed < target:
            raise RegistryError("schema compatibility versions must be canonical and strictly increasing")
        if entry.get("effect") != "grant-only":
            raise RegistryError("only explicitly reviewed grant-only schema compatibility is supported")
        target_commit = entry.get("target_commit_sha")
        if not isinstance(target_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", target_commit):
            raise RegistryError("schema compatibility target commit must be a full Git SHA")
        target_platform = entry.get("target_platform_sha")
        if not isinstance(target_platform, str) or target_platform not in app.get("allowed_platform_shas", []):
            raise RegistryError("schema compatibility target workflow must be on the root allowlist")

        def validate_migration(item: Any, expected_version: str | None = None) -> tuple[int, ...]:
            migration_fields = {"version", "script", "checksum", "sha256"}
            if not isinstance(item, dict) or set(item) != migration_fields:
                raise RegistryError("schema compatibility migration evidence is incomplete")
            version = item.get("version")
            version_tuple = _version_parts(version)
            script = item.get("script")
            match = MIGRATION_SCRIPT.fullmatch(script) if isinstance(script, str) else None
            if (
                version_tuple is None
                or match is None
                or match.group("version").replace("_", ".") != version
                or (expected_version is not None and version != expected_version)
                or isinstance(item.get("checksum"), bool)
                or not isinstance(item.get("checksum"), int)
                or not -(2**31) <= item["checksum"] < 2**31
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            ):
                raise RegistryError("schema compatibility migration identity or checksum is invalid")
            return version_tuple

        applied = entry.get("applied_migration")
        if validate_migration(applied, entry["observed_schema_version"]) != observed:
            raise RegistryError("schema compatibility applied migration must identify the observed schema")
        pending = entry.get("pending_migrations")
        if not isinstance(pending, list) or not pending or len(pending) > 32:
            raise RegistryError("schema compatibility requires a bounded exact pending-migration plan")
        pending_versions = [validate_migration(item) for item in pending]
        if (
            pending_versions[0] <= observed
            or any(left >= right for left, right in zip(pending_versions, pending_versions[1:]))
            or pending_versions[-1] != target
        ):
            raise RegistryError("schema compatibility pending migrations must be ordered through the exact target")

        retained = entry.get("retained_release")
        retained_fields = {"commit_sha", "platform_sha", "migration_image", "components"}
        if not isinstance(retained, dict) or set(retained) != retained_fields:
            raise RegistryError("schema compatibility retained release identity is incomplete")
        if (
            not isinstance(retained.get("commit_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", retained["commit_sha"])
            or retained.get("platform_sha") not in app.get("allowed_platform_shas", [])
            or not isinstance(retained.get("platform_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", retained["platform_sha"])
        ):
            raise RegistryError("schema compatibility retained release commit is invalid")
        migration_namespace = app.get("migration_image_namespace")
        if (
            not isinstance(migration_namespace, str)
            or not isinstance(retained.get("migration_image"), str)
            or not re.fullmatch(re.escape(migration_namespace) + r"@sha256:[0-9a-f]{64}", retained["migration_image"])
        ):
            raise RegistryError("schema compatibility retained migration image must use its immutable registered digest")
        components = retained.get("components")
        if not isinstance(components, dict) or set(components) != set(app.get("components", {})):
            raise RegistryError("schema compatibility retained components do not match the application")
        for name, component in app["components"].items():
            namespace = component.get("image_namespace") if isinstance(component, dict) else None
            image = components.get(name)
            if not isinstance(namespace, str) or not isinstance(image, str) or not re.fullmatch(re.escape(namespace) + r"@sha256:[0-9a-f]{64}", image):
                raise RegistryError("schema compatibility retained component image must use its immutable registered digest")


class RegistryError(ValueError):
    """Raised when a server-side application policy is ambiguous or unsafe."""


def build_spec_sha256(build: Any) -> str:
    try:
        payload = json.dumps(build, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RegistryError("build specification cannot be serialized safely") from exc
    return hashlib.sha256(payload).hexdigest()


def _safe_build_path(value: Any, name: str) -> None:
    if value == "." and name == "build context":
        return
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", value)
    ):
        raise RegistryError(f"{name} must be a safe relative repository path")


def _validate_build_spec(app: dict[str, Any], profile: str) -> None:
    mode = app.get("provenance_mode", "legacy-labels")
    if mode not in {"legacy-labels", "github-oidc"}:
        raise RegistryError("image provenance mode is unsupported")
    build = app.get("build")
    if build is None:
        if mode == "github-oidc":
            raise RegistryError("OIDC-provenance deployments require a root-registered build specification")
        return
    if not isinstance(build, dict) or set(build) - {"components", "migrator", "operation_build_args"}:
        raise RegistryError("build specification has unsupported fields")
    components = build.get("components")
    if not isinstance(components, dict) or not components:
        raise RegistryError("build specification requires named image components")
    expected_names = set(app.get("components", {})) if profile in STACK_PROFILES else {"main"}
    if set(components) != expected_names:
        raise RegistryError("build component names must match the registered deployment profile")
    for name, spec in {**components, **({"migrator": build["migrator"]} if "migrator" in build else {})}.items():
        if name == "migrator" and "migrator" not in build:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) or not isinstance(spec, dict):
            raise RegistryError("build component name or record is invalid")
        if set(spec) - {"context", "dockerfile", "platform", "build_args", "source"}:
            raise RegistryError("build component contains unsupported options")
        if spec.get("source", "caller") not in {"caller", "platform"}:
            raise RegistryError("build component source must be the caller or pinned platform checkout")
        _safe_build_path(spec.get("context"), "build context")
        _safe_build_path(spec.get("dockerfile"), "Dockerfile path")
        if spec.get("platform", "linux/amd64") not in {"linux/amd64", "linux/arm64"}:
            raise RegistryError("build platform is unsupported")
        build_args = spec.get("build_args", {})
        if not isinstance(build_args, dict) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key)
            or not isinstance(value, str)
            or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            for key, value in build_args.items()
        ):
            raise RegistryError("build arguments must be root-controlled single-line literals")
    operation_args = build.get("operation_build_args", {})
    if not isinstance(operation_args, dict) or set(operation_args) - {
        "deploy", "deploy-service", "unhealthy-test", "deploy-stack", "test-stack", "validate-database",
    }:
        raise RegistryError("build operation arguments contain an unsupported deployment operation")
    for values in operation_args.values():
        if not isinstance(values, dict) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key)
            or not isinstance(value, str)
            or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            for key, value in values.items()
        ):
            raise RegistryError("operation build arguments must be root-controlled single-line literals")
    if "migrator" in build and (
        not isinstance(app.get("migration_image_namespace"), str)
        or not isinstance(app.get("database"), dict)
        or app["database"].get("migration_adapter") != "flyway"
    ):
        raise RegistryError("a migrator build requires a registered Flyway image package")
    if (
        profile in STACK_PROFILES
        and isinstance(app.get("database"), dict)
        and app["database"].get("migration_adapter") == "flyway"
        and "migrator" not in build
    ):
        raise RegistryError("database-backed stack releases must build their registered Flyway migrator")
    if mode == "github-oidc" and not isinstance(app.get("image_source_repository"), str):
        raise RegistryError("OIDC-provenance images require a registered source repository")


def _positive_number(value: Any, name: str, *, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value > maximum:
        raise RegistryError(f"{name} must be a positive value within the configured limit")


def _port(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise RegistryError(f"{name} must be a valid TCP or UDP port")
    return value


def _memory_mb(value: Any, name: str) -> float:
    if not isinstance(value, str):
        raise RegistryError(f"{name} must be an explicit Docker memory limit")
    match = re.fullmatch(r"([1-9][0-9]*)(m|g)", value.lower())
    if not match:
        raise RegistryError(f"{name} must use a positive m or g memory limit")
    amount = int(match.group(1))
    return float(amount if match.group(2) == "m" else amount * 1024)


def validate_application(app_id: str, app: Any) -> dict[str, Any]:
    if not APP_ID.fullmatch(app_id) or not isinstance(app, dict):
        raise RegistryError("application identifier or record is invalid")
    if app.get("app_id", app_id) != app_id:
        raise RegistryError("application record ID does not match its registry key")
    if not isinstance(app.get("repository"), str) or not REPOSITORY.fullmatch(app["repository"]):
        raise RegistryError("application repository must use owner/name notation")
    workflow_repository = app.get("workflow_repository", app.get("repository"))
    if not isinstance(workflow_repository, str) or not REPOSITORY.fullmatch(workflow_repository):
        raise RegistryError("workflow repository must use owner/name notation")
    workflow = app.get("workflow_path")
    if (
        not isinstance(workflow, str)
        or not workflow.startswith(".github/workflows/")
        or ".." in workflow.split("/")
        or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow)
    ):
        raise RegistryError("application workflow path is invalid")
    if not isinstance(app.get("production_branch"), str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", app["production_branch"]):
        raise RegistryError("application production branch is invalid")
    if not isinstance(app.get("environment"), str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", app["environment"]):
        raise RegistryError("application deployment environment is invalid")
    profile = app.get("deployment_profile")
    if profile not in PROFILES:
        raise RegistryError("application deployment profile is unsupported")
    if profile != "build-only" and app.get("image_revision_mode") not in {"caller", "platform"}:
        raise RegistryError("image revision mode must explicitly bind the image to the caller or pinned platform commit")
    if app.get("workflow_event", "workflow_dispatch") not in {"workflow_dispatch", "push", "workflow_run"}:
        raise RegistryError("application release event is unsupported")
    automatic_enabled = app.get("automatic_release_enabled", False)
    automatic_operations = app.get("automatic_operations", [])
    if (
        not isinstance(automatic_enabled, bool)
        or not isinstance(automatic_operations, list)
        or any(
            not isinstance(operation, str)
        or operation not in {"deploy", "deploy-service", "deploy-stack", "build-artifact"}
            for operation in automatic_operations
        )
        or len(set(automatic_operations)) != len(automatic_operations)
        or (automatic_enabled and (app.get("automatic_workflow_event") != "workflow_run" or not automatic_operations))
        or (not automatic_enabled and automatic_operations)
    ):
        raise RegistryError("automatic release policy must explicitly allow supported operations")
    automatic_phrase = app.get("automatic_confirmation_phrase")
    if automatic_enabled and (
        not isinstance(automatic_phrase, str)
        or not re.fullmatch(r"[A-Z0-9][A-Z0-9 -]{5,119}", automatic_phrase)
    ):
        raise RegistryError("automatic releases require a root-registered confirmation phrase")
    if app.get("ci_authorization", "generic") not in {"generic", "romanlms-production"}:
        raise RegistryError("application CI authorization adapter is unsupported")
    confirmation_phrases = app.get("confirmation_phrases", {})
    if not isinstance(confirmation_phrases, dict) or any(
        operation not in {"deploy", "deploy-service", "deploy-stack", "rollback", "rollback-service", "rollback-stack", "refresh-stack", "test-stack", "unhealthy-test", "validate-database", "verify", "verify-stack", "build-artifact"}
        or not isinstance(phrase, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ]{7,119}", phrase)
        for operation, phrase in confirmation_phrases.items()
    ):
        raise RegistryError("application operation confirmation phrases must be explicit safe literals")
    if profile == "build-only":
        actors = app.get("authorized_actors")
        platform_shas = app.get("allowed_platform_shas")
        reusable_workflow = app.get("reusable_workflow_path")
        ci_workflow = app.get("required_ci_workflow_file")
        artifact_build = app.get("artifact_build")
        if (
            not isinstance(actors, list)
            or not actors
            or any(not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor) for actor in actors)
            or not isinstance(platform_shas, list)
            or not platform_shas
            or any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in platform_shas)
            or len(set(platform_shas)) != len(platform_shas)
            or not isinstance(reusable_workflow, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/build-application\.ya?ml", reusable_workflow)
            or not isinstance(ci_workflow, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", ci_workflow)
            or not isinstance(artifact_build, dict)
            or set(artifact_build) != {"system", "project_path", "artifact_globs"}
            or artifact_build.get("system") not in {"gradle-android", "gradle-jvm", "maven-jvm", "npm", "python"}
            or not isinstance(artifact_build.get("project_path"), str)
            or artifact_build["project_path"] != "." and (
                "\\" in artifact_build["project_path"]
                or any(part in {"", ".", ".."} for part in artifact_build["project_path"].split("/"))
                or not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", artifact_build["project_path"])
            )
            or not isinstance(artifact_build.get("artifact_globs"), list)
            or not artifact_build["artifact_globs"]
            or any(
                not isinstance(pattern, str)
                or not pattern
                or pattern.startswith("/")
                or "\\" in pattern
                or any(part in {"", ".", ".."} for part in pattern.split("/"))
                or not re.fullmatch(r"[A-Za-z0-9_.*?\[\]/-]{1,240}", pattern)
                for pattern in artifact_build["artifact_globs"]
            )
            or len(set(artifact_build["artifact_globs"])) != len(artifact_build["artifact_globs"])
            or app.get("image_package_repository") is not None
            or app.get("secret_refs", [])
        ):
            raise RegistryError("build-only applications require a safe registered toolchain, artifact paths, CI and reusable workflow")
    else:
        actors = app.get("authorized_actors")
        platform_shas = app.get("allowed_platform_shas")
        if (
            not isinstance(actors, list)
            or not actors
            or any(not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor) for actor in actors)
            or not isinstance(platform_shas, list)
            or not platform_shas
            or any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in platform_shas)
            or len(set(platform_shas)) != len(platform_shas)
            or not isinstance(app.get("reusable_workflow_path"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", app["reusable_workflow_path"])
            or not isinstance(app.get("required_ci_workflow_file"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", app["required_ci_workflow_file"])
            or app.get("image_package_repository") != app.get("repository")
            or not isinstance(app.get("image_source_repository"), str)
            or not REPOSITORY.fullmatch(app["image_source_repository"])
        ):
            raise RegistryError("application release authorization or image provenance policy is incomplete")
    strategy = app.get("strategy")
    strategy_by_profile = {
        "static-web": {"blue-green"},
        "backend-api": {"blue-green", "rolling", "recreate"},
        "full-stack": {"blue-green", "maintenance"},
        "multi-container": {"blue-green", "rolling", "recreate", "maintenance"},
        "worker": {"rolling", "recreate"},
        "scheduled-job": {"scheduled"},
        "stateful-service": {"recreate", "maintenance"},
        "game-server": {"recreate", "maintenance"},
        "build-only": {"build-only"},
    }
    if strategy not in strategy_by_profile[profile]:
        raise RegistryError("deployment strategy does not match the application profile")
    if profile == "build-only":
        if app.get("deployment_enabled", False) is not False:
            raise RegistryError("build-only applications cannot be deployed to the server")
        if "build-artifact" not in confirmation_phrases:
            raise RegistryError("build-only applications require a registered artifact release confirmation")
        return app

    required_confirmations = (
        {"deploy"} if profile in HTTP_PROFILES else
        {"deploy-stack", "rollback-stack"} if profile in STACK_PROFILES else
        {"deploy-service", "rollback-service"}
    )
    if app.get("stack_test_enabled") is True:
        required_confirmations.add("test-stack")
    if not required_confirmations.issubset(confirmation_phrases):
        raise RegistryError("each deployed application operation requires a registered confirmation phrase")

    _validate_build_spec(app, profile)
    build_spec = app.get("build")
    if isinstance(build_spec, dict) and isinstance(build_spec.get("components"), dict):
        expected_build_source = "platform" if app["image_revision_mode"] == "platform" else "caller"
        if any(spec.get("source", "caller") != expected_build_source for spec in build_spec["components"].values()):
            raise RegistryError("image revision mode must match the registered build source")

    namespace = app.get("image_namespace")
    if profile not in STACK_PROFILES and (
        not isinstance(namespace, str)
        or not re.fullmatch(r"ghcr\.io/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", namespace)
    ):
        raise RegistryError("application image namespace must be a GHCR package")
    resources = app.get("resources")
    if not isinstance(resources, dict) or set(resources) != {"cpu_cores", "memory_mb", "pids_limit"}:
        raise RegistryError("application resources must declare CPU, memory, and process limits")
    _positive_number(resources["cpu_cores"], "CPU limit", maximum=12)
    _positive_number(resources["memory_mb"], "memory limit", maximum=8192)
    if isinstance(resources["pids_limit"], bool) or not isinstance(resources["pids_limit"], int) or not 16 <= resources["pids_limit"] <= 4096:
        raise RegistryError("process limit must be between 16 and 4096")

    if profile in HTTP_PROFILES:
        if (
            not isinstance(app.get("slot_ports"), dict)
            or set(app["slot_ports"]) != {"blue", "green"}
            or not isinstance(app.get("health_path"), str)
            or not app["health_path"].startswith("/")
            or not isinstance(app.get("route_file"), str)
            or not re.fullmatch(r"/etc/roman-platform/nginx/active-[a-z0-9-]+\.conf", app["route_file"])
            or not isinstance(app.get("expected_health_text"), str)
            or not app["expected_health_text"]
            or not isinstance(app.get("container_memory"), str)
            or not isinstance(app.get("container_cpus"), (int, float))
            or isinstance(app.get("container_cpus"), bool)
            or app.get("container_cpus", 0) <= 0
            or not isinstance(app.get("pids_limit"), int)
            or isinstance(app.get("pids_limit"), bool)
        ):
            raise RegistryError("HTTP profiles require blue/green ports, a health path, and a managed route")
        _port(app["container_port"], "container_port")
        if _memory_mb(app["container_memory"], "container memory") > resources["memory_mb"] or app["container_cpus"] > resources["cpu_cores"] or app["pids_limit"] > resources["pids_limit"]:
            raise RegistryError("HTTP container limits cannot exceed the application resource budget")
    elif profile in STACK_PROFILES:
        components = app.get("components")
        if (
            not isinstance(components, dict)
            or not components
            or not isinstance(app.get("route_file"), str)
            or not re.fullmatch(r"/etc/roman-platform/nginx/active-[a-z0-9-]+\.conf", app["route_file"])
        ):
            raise RegistryError("stack profiles require named components and a managed route")
        for name, component in components.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) or not isinstance(component, dict):
                raise RegistryError("stack component names and policies must be valid")
            if not isinstance(component.get("image_namespace"), str) or not re.fullmatch(r"ghcr\.io/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", component["image_namespace"]):
                raise RegistryError("every stack component needs an approved image namespace")
            _port(component.get("container_port"), "component container_port")
            if (
                not isinstance(component.get("container_memory"), str)
                or isinstance(component.get("container_cpus"), bool)
                or not isinstance(component.get("container_cpus"), (int, float))
                or component["container_cpus"] <= 0
                or isinstance(component.get("pids_limit"), bool)
                or not isinstance(component.get("pids_limit"), int)
                or component["pids_limit"] < 16
            ):
                raise RegistryError("each stack component needs explicit CPU, memory, and process limits")
        component_cpu = sum(float(item["container_cpus"]) for item in components.values())
        component_memory = sum(_memory_mb(item["container_memory"], "component memory") for item in components.values())
        component_pids = sum(item["pids_limit"] for item in components.values())
        if component_cpu > resources["cpu_cores"] or component_memory > resources["memory_mb"] or component_pids > resources["pids_limit"]:
            raise RegistryError("stack component limits exceed the application resource budget")
        if (
            not isinstance(app.get("container_network"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,47}", app["container_network"])
            or app.get("container_publish") is not False
            or isinstance(app.get("private_listener_port"), bool)
            or not isinstance(app.get("private_listener_port"), int)
            or not 1024 <= app["private_listener_port"] <= 65535
        ):
            raise RegistryError("stack profiles require a private named network and a managed loopback listener")
        mode = app.get("container_network_mode", "bridge")
        if mode == "bridge":
            try:
                network = ipaddress.ip_network(app["network_subnet"], strict=True)
            except (KeyError, ValueError, TypeError) as exc:
                raise RegistryError("stack profiles require a private IPv4 network subnet") from exc
            if not isinstance(network, ipaddress.IPv4Network) or not network.is_private:
                raise RegistryError("stack network subnet must be private IPv4")
        elif mode == "host":
            if app.get("allow_host_network") is not True or app.get("container_network") != "host":
                raise RegistryError("host networking requires an explicit application policy")
        else:
            raise RegistryError("stack network mode is unsupported")
        prefixes: set[str] = set()
        has_root_route = False
        for name, component in components.items():
            prefix = component.get("route_prefix")
            if not isinstance(prefix, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", prefix) or prefix in prefixes:
                raise RegistryError("stack components need unique safe route prefixes")
            prefixes.add(prefix)
            has_root_route = has_root_route or prefix == "/"
            if (
                not isinstance(component.get("health_path"), str)
                or not component["health_path"].startswith("/")
                or not isinstance(component.get("expected_health_text"), str)
                or not component["expected_health_text"]
                or not isinstance(component.get("readiness_timeout_seconds"), int)
                or isinstance(component.get("readiness_timeout_seconds"), bool)
                or not 10 <= component["readiness_timeout_seconds"] <= 900
            ):
                raise RegistryError("stack components require bounded HTTP health checks")
            expected_health_url = rf"http://127\.0\.0\.1:{app['private_listener_port']}/[^\s]*"
            if not isinstance(component.get("route_health_url"), str) or not re.fullmatch(expected_health_url, component["route_health_url"]):
                raise RegistryError("stack component route health must use the managed loopback listener")
            slots = component.get("slot_ips")
            if mode == "bridge":
                expected_slots = {"blue", "green", "test"} if app.get("stack_test_enabled") else {"blue", "green"}
                if not isinstance(slots, dict) or set(slots) != expected_slots:
                    raise RegistryError("bridge stack components need fixed blue/green addresses")
                for address in slots.values():
                    try:
                        parsed = ipaddress.IPv4Address(address)
                    except (ipaddress.AddressValueError, TypeError) as exc:
                        raise RegistryError("stack component address is invalid") from exc
                    if parsed not in network or parsed in {network.network_address, network.broadcast_address}:
                        raise RegistryError("stack component address is outside its private subnet")
            else:
                slot_ports = component.get("slot_ports")
                expected_slots = {"blue", "green", "test"} if app.get("stack_test_enabled") else {"blue", "green"}
                if not isinstance(slot_ports, dict) or set(slot_ports) != expected_slots:
                    raise RegistryError("host-network stack components need fixed loopback ports")
                for port in slot_ports.values():
                    _port(port, "host-network slot port")
        if not has_root_route:
            raise RegistryError("a stack must register a root route")
    elif profile == "worker":
        runtime = app.get("runtime")
        if (
            not isinstance(runtime, dict)
            or runtime.get("health") != "docker-healthcheck"
            or runtime.get("network") != f"roman-platform-{app_id}"
            or not isinstance(runtime.get("uid"), int)
            or isinstance(runtime.get("uid"), bool)
            or not 1 <= runtime["uid"] <= 65535
            or not isinstance(runtime.get("gid"), int)
            or isinstance(runtime.get("gid"), bool)
            or not 1 <= runtime["gid"] <= 65535
        ):
            raise RegistryError("workers require an isolated network and image health check")
        if strategy == "rolling" and runtime.get("concurrency_safe") is not True:
            raise RegistryError("rolling worker releases require an explicit duplicate-processing safety declaration")
    elif profile == "scheduled-job":
        runtime = app.get("runtime")
        calendar = runtime.get("systemd_calendar") if isinstance(runtime, dict) else None
        if not isinstance(calendar, str) or not re.fullmatch(r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:\.\.(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))? )?\*-\*-\* (?:[01][0-9]|2[0-3]):[0-5][0-9]:00 UTC", calendar):
            raise RegistryError("scheduled jobs require a validated systemd UTC calendar")
        if runtime.get("network") != f"roman-platform-{app_id}":
            raise RegistryError("scheduled jobs require an isolated network")
        if (
            not isinstance(runtime.get("uid"), int)
            or isinstance(runtime.get("uid"), bool)
            or not 1 <= runtime["uid"] <= 65535
            or not isinstance(runtime.get("gid"), int)
            or isinstance(runtime.get("gid"), bool)
            or not 1 <= runtime["gid"] <= 65535
        ):
            raise RegistryError("scheduled jobs require a non-root runtime identity")
    elif profile in {"stateful-service", "game-server"}:
        runtime = app.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("health") != "docker-healthcheck":
            raise RegistryError("stateful services and game servers require an image health check")
        storage = runtime.get("storage_root")
        if not isinstance(storage, str) or storage != f"/srv/apps/{app_id}" or ".." in storage.split("/"):
            raise RegistryError("persistent storage must be isolated under the app ID")
        backup = runtime.get("backup_adapter")
        if backup != "verified-volume-archive":
            raise RegistryError("stateful workloads require an approved backup adapter")
        if not re.fullmatch(rf"roman-platform-{re.escape(app_id)}", runtime.get("network", "")):
            raise RegistryError("persistent workloads require an app-isolated Docker network")
        if (
            not isinstance(runtime.get("uid"), int)
            or isinstance(runtime.get("uid"), bool)
            or not 1 <= runtime["uid"] <= 65535
            or not isinstance(runtime.get("gid"), int)
            or isinstance(runtime.get("gid"), bool)
            or not 1 <= runtime["gid"] <= 65535
        ):
            raise RegistryError("persistent workloads require a non-root runtime identity")
        storage_container_path = runtime.get("storage_container_path", "/data")
        if not isinstance(storage_container_path, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/?)*", storage_container_path) or ".." in storage_container_path.split("/"):
            raise RegistryError("persistent data must mount at a safe absolute container path")
        ports = runtime.get("ports", [])
        if profile == "game-server" and not ports:
            raise RegistryError("game servers must declare at least one TCP or UDP port")
        for item in ports:
            if not isinstance(item, dict) or set(item) != {"host_port", "container_port", "protocol", "bind_address"}:
                raise RegistryError("game-server port declaration is invalid")
            _port(item["host_port"], "host port")
            _port(item["container_port"], "container port")
            if item["protocol"] not in {"tcp", "udp"} or item["bind_address"] not in {"0.0.0.0", "127.0.0.1"}:
                raise RegistryError("game-server protocol or bind address is invalid")

    database = app.get("database")
    if database is not None:
        if not isinstance(database, dict) or database.get("engine") not in {"postgresql"}:
            raise RegistryError("the configured database engine has no installed adapter")
        if database.get("migration_adapter") not in {None, "flyway"}:
            raise RegistryError("the configured database migration adapter is unsupported")
        if database.get("migration_adapter") == "flyway" and database.get("backup_adapter") not in {"pgbackrest", "verified-dump"}:
            raise RegistryError("Flyway migrations require an approved database backup adapter")
    _validate_schema_compatibility_evidence(app)
    secrets = app.get("secret_refs", [])
    if not isinstance(secrets, list) or any(
        not isinstance(ref, str)
        or not ref.startswith(f"/etc/roman-platform/{app_id}/")
        or ".." in ref.split("/")
        for ref in secrets
    ):
        raise RegistryError("secrets must be root-managed app-scoped references")
    for ref in secrets:
        if any(part in {".", ".."} for part in ref.split("/")):
            raise RegistryError("secret references cannot traverse directories")
    runtime = app.get("runtime", {})
    if isinstance(runtime, dict):
        env_file = runtime.get("env_file")
        if env_file is not None and env_file not in secrets:
            raise RegistryError("runtime environment files must be declared in the app-scoped secret references")
        environment = runtime.get("environment", {})
        if not isinstance(environment, dict) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or not isinstance(value, str)
            or len(value) > 1024
            or not re.fullmatch(r"[A-Za-z0-9_./:@+-]*", value)
            for key, value in environment.items()
        ):
            raise RegistryError("runtime environment values must be non-secret, single-token literals")
        timeout = runtime.get("readiness_timeout_seconds", 180)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 900:
            raise RegistryError("service readiness timeout must be between 10 and 900 seconds")
    routes = app.get("routes")
    nginx_site = app.get("nginx_site")
    if routes is not None:
        if profile not in HTTP_PROFILES | STACK_PROFILES or not isinstance(routes, list) or not routes:
            raise RegistryError("only HTTP and stack applications can register non-empty domain routes")
        expected_site_file = f"/etc/nginx/conf.d/roman-platform-app-{app_id}.conf"
        if (
            not isinstance(nginx_site, dict)
            or set(nginx_site) != {"site_file", "listen_address", "listen_port"}
            or nginx_site.get("site_file") != expected_site_file
            or nginx_site.get("listen_address") != "127.0.0.1"
        ):
            raise RegistryError("registered domains require a managed loopback Nginx site")
        _port(nginx_site.get("listen_port"), "managed Nginx listener port")
        if profile in STACK_PROFILES and any(
            isinstance(route, dict) and route.get("path") != "/" for route in routes
        ):
            raise RegistryError("stack domain routes must delegate paths to the registered component route map")
    elif nginx_site is not None:
        raise RegistryError("a managed Nginx site requires registered domain routes")
    return app


def validate_registry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise RegistryError("application registry schema version 2 is required")
    apps = value.get("applications")
    if not isinstance(apps, dict) or not apps:
        raise RegistryError("application registry is empty or malformed")
    ports: dict[tuple[int, str], str] = {}
    routes: dict[tuple[str, str], str] = {}
    subnets: list[tuple[ipaddress.IPv4Network, str]] = []
    networks: dict[str, str] = {}
    for app_id, app in apps.items():
        validate_application(app_id, app)
        if app.get("container_network_mode", "bridge") == "bridge" and isinstance(app.get("network_subnet"), str):
            subnet = ipaddress.ip_network(app["network_subnet"], strict=True)
            for existing_subnet, owner in subnets:
                if subnet.overlaps(existing_subnet):
                    raise RegistryError(f"Docker subnet {subnet} overlaps {owner}'s {existing_subnet}")
            subnets.append((subnet, app_id))
        network_name = app.get("container_network")
        if isinstance(network_name, str) and network_name != "host":
            if network_name in networks and networks[network_name] != app_id:
                raise RegistryError(f"Docker network {network_name} is shared by {networks[network_name]} and {app_id}")
            networks[network_name] = app_id
        declared_ports: list[tuple[int, str]] = []
        for key in ("slot_ports",):
            mapping = app.get(key)
            if isinstance(mapping, dict):
                declared_ports.extend((_port(port, key), "tcp") for port in mapping.values())
        components = app.get("components", {})
        if isinstance(components, dict):
            for component in components.values():
                mapping = component.get("slot_ports") if isinstance(component, dict) else None
                if isinstance(mapping, dict):
                    declared_ports.extend((_port(port, "component slot port"), "tcp") for port in mapping.values())
        runtime = app.get("runtime", {})
        if isinstance(runtime, dict):
            declared_ports.extend(
                (_port(item.get("host_port"), "host port"), item.get("protocol", "tcp"))
                for item in runtime.get("ports", [])
                if isinstance(item, dict)
            )
        nginx_site = app.get("nginx_site")
        if isinstance(nginx_site, dict):
            declared_ports.append((_port(nginx_site.get("listen_port"), "managed Nginx listener port"), "tcp"))
        if len(declared_ports) != len(set(declared_ports)):
            raise RegistryError(f"application {app_id} declares the same host port more than once")
        for port, protocol in declared_ports:
            if protocol not in {"tcp", "udp"}:
                raise RegistryError("host port protocol must be TCP or UDP")
            key = (port, protocol)
            if key in ports and ports[key] != app_id:
                raise RegistryError(f"host port {port}/{protocol} is shared by {ports[key]} and {app_id}")
            ports[key] = app_id
        for route in app.get("routes", []):
            if not isinstance(route, dict) or set(route) != {"domain", "path"}:
                raise RegistryError("route declarations need a domain and path")
            domain, path = route["domain"], route["path"]
            if (
                not isinstance(domain, str)
                or not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", domain)
                or not isinstance(path, str)
                or not re.fullmatch(r"/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*/?)?", path)
                or ".." in path.split("/")
            ):
                raise RegistryError("route domain or path is invalid")
            key = (domain.lower(), path)
            conflicting_owner = next(
                (
                    owner
                    for (existing_domain, existing_path), owner in routes.items()
                    if existing_domain == key[0]
                    and (
                        existing_path == key[1]
                        or existing_path == "/"
                        or key[1] == "/"
                        or existing_path.rstrip("/").startswith(key[1].rstrip("/") + "/")
                        or key[1].rstrip("/").startswith(existing_path.rstrip("/") + "/")
                    )
                ),
                None,
            )
            if conflicting_owner is not None:
                raise RegistryError(f"domain route {domain}{path} overlaps a route registered to {conflicting_owner}")
            if key in routes and routes[key] != app_id:
                raise RegistryError(f"domain route {domain}{path} is already registered to {routes[key]}")
            routes[key] = app_id
    return value
