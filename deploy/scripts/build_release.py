#!/usr/bin/env python3
"""Build and publish the root-registered immutable images for a release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "controller"))
from application_registry import RegistryError, build_spec_sha256, validate_registry  # noqa: E402
from oidc_provenance import ProvenanceError, audience_for  # noqa: E402

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class BuildError(RuntimeError):
    pass


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise BuildError(f"Could not run build tool: {Path(args[0]).name}.") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()[-900:]
        raise BuildError(f"{Path(args[0]).name} failed: {detail}")
    return result


def _git_head(path: Path) -> str:
    result = _run(["git", "-C", str(path), "rev-parse", "HEAD"])
    return result.stdout.strip()


def _validate_identity(args: argparse.Namespace, app: dict[str, Any]) -> None:
    if not SHA.fullmatch(args.source_sha) or not SHA.fullmatch(args.platform_sha):
        raise BuildError("Full source and platform commit SHAs are required.")
    if args.repository != app["repository"]:
        raise BuildError("The application source repository is not registered.")
    if _git_head(args.source_root) != args.source_sha:
        raise BuildError("The checked out application source does not match the release commit.")
    if _git_head(args.platform_root) != args.platform_sha:
        raise BuildError("The checked out deployment platform does not match its pinned SHA.")


def _specs_for_operation(
    app: dict[str, Any], operation: str, component: str | None,
) -> list[tuple[str, str, dict[str, Any], str]]:
    build = app["build"]
    components = build["components"]
    profiles = app["deployment_profile"]
    if operation == "test-stack":
        if component not in components:
            raise BuildError("The requested stack test component is not registered.")
        selected = [(component, components[component], app["components"][component]["image_namespace"])]
    elif operation == "deploy-stack":
        selected = [(name, components[name], app["components"][name]["image_namespace"]) for name in sorted(components)]
    elif operation in {"deploy", "deploy-service", "unhealthy-test"}:
        if profiles not in {"static-web", "backend-api", "worker", "scheduled-job", "stateful-service", "game-server"}:
            raise BuildError("A single-image release does not match the registered profile.")
        selected = [("main", components["main"], app["image_namespace"])]
    elif operation == "validate-database":
        if "migrator" not in build or not isinstance(app.get("migration_image_namespace"), str):
            raise BuildError("A root-registered Flyway migrator build is required.")
        selected = [("migrator", build["migrator"], app["migration_image_namespace"])]
    else:
        raise BuildError("The selected operation does not publish a new image.")

    include_migrator = (
        operation == "deploy-stack"
        and isinstance(app.get("database"), dict)
        and app["database"].get("migration_adapter") == "flyway"
    )
    if include_migrator:
        if "migrator" not in build or not isinstance(app.get("migration_image_namespace"), str):
            raise BuildError("The database release has no root-registered Flyway migrator build.")
        selected.append(("migrator", build["migrator"], app["migration_image_namespace"]))
    return [(name, spec, namespace, name) for name, spec, namespace in selected]


def build_release(args: argparse.Namespace) -> dict[str, Any]:
    try:
        registry = json.loads(args.registry.read_text(encoding="utf-8"))
        validate_registry(registry)
    except (OSError, json.JSONDecodeError, RegistryError) as exc:
        raise BuildError(f"The root application registry is unavailable or invalid: {exc}") from exc
    app = registry["applications"].get(args.app_id)
    if not isinstance(app, dict) or app.get("provenance_mode") != "github-oidc":
        raise BuildError("The application is not registered for verified GitHub OIDC provenance.")
    _validate_identity(args, app)
    build_hash = build_spec_sha256(app["build"])
    specs = _specs_for_operation(app, args.operation, args.component)
    result_images: dict[str, str] = {}
    root_build_args = app["build"].get("operation_build_args", {}).get(args.operation, {})
    source_revision = args.source_sha if app.get("image_revision_mode", "platform") == "caller" else args.platform_sha
    package_repository_url = "https://github.com/" + app["image_package_repository"]
    source_repository_url = "https://github.com/" + app["image_source_repository"]
    temporary_root = Path(args.temp_dir or tempfile.gettempdir())
    temporary_root.mkdir(parents=True, exist_ok=True)

    for name, spec, namespace, _ in specs:
        checkout = args.platform_root if spec.get("source", "caller") == "platform" else args.source_root
        context = (checkout / spec["context"]).resolve()
        dockerfile = (checkout / spec["dockerfile"]).resolve()
        if (
            not context.is_dir()
            or not dockerfile.is_file()
            or context.is_symlink()
            or dockerfile.is_symlink()
            or not context.is_relative_to(checkout.resolve())
            or not dockerfile.is_relative_to(checkout.resolve())
        ):
            raise BuildError(f"Registered build paths for {name} are unavailable or escape their checkout.")
        tag = f"{namespace}:sha-{args.source_sha}-run-{args.run_id}-{args.run_attempt}-{name}"
        with tempfile.NamedTemporaryFile(prefix="roman-build-", suffix=".json", dir=temporary_root, delete=False) as stream:
            metadata_path = Path(stream.name)
        try:
            build_command = [
                "docker", "buildx", "build", "--push", "--pull",
                "--provenance=mode=max", "--sbom=true", "--metadata-file", str(metadata_path),
                "--platform", spec.get("platform", "linux/amd64"),
                "--file", str(dockerfile), "--tag", tag,
                "--label", f"org.opencontainers.image.source={package_repository_url}",
                "--label", f"org.opencontainers.image.revision={source_revision}",
                "--label", f"io.roman.platform.source-repository={source_repository_url}",
                "--label", f"io.roman.platform.source-revision={args.platform_sha}",
                "--label", f"io.roman.deployment.package-repository={package_repository_url}",
                "--label", f"io.roman.deployment.caller-revision={args.source_sha}",
            ]
            build_args = {
                "CALLER_REVISION": args.source_sha,
                "OCI_REVISION": source_revision,
                "PLATFORM_REF": args.platform_sha,
                "REVISION": source_revision,
                **spec.get("build_args", {}),
                **root_build_args,
            }
            for key, value in sorted(build_args.items()):
                build_command.extend(["--build-arg", f"{key}={value}"])
            build_command.append(str(context))
            _run(build_command)
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                digest = metadata["containerimage.digest"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise BuildError(f"BuildKit did not return a digest for {name}.") from exc
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise BuildError(f"BuildKit returned an invalid digest for {name}.")
            immutable = namespace + "@" + digest
            _run(["docker", "pull", immutable])
            repo_digests = _run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", immutable])
            try:
                stored_digests = json.loads(repo_digests.stdout.strip())
            except json.JSONDecodeError as exc:
                raise BuildError(f"The published {name} image could not be inspected.") from exc
            if not isinstance(stored_digests, list) or immutable not in stored_digests:
                raise BuildError(f"The registry did not retain {name} at the reported immutable digest.")
            labels_result = _run(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", immutable])
            try:
                labels = json.loads(labels_result.stdout.strip())
            except json.JSONDecodeError as exc:
                raise BuildError(f"The published {name} image labels could not be verified.") from exc
            expected_labels = {
                "org.opencontainers.image.source": package_repository_url,
                "org.opencontainers.image.revision": source_revision,
                "io.roman.platform.source-repository": source_repository_url,
                "io.roman.platform.source-revision": args.platform_sha,
                "io.roman.deployment.package-repository": package_repository_url,
                "io.roman.deployment.caller-revision": args.source_sha,
            }
            if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected_labels.items()):
                raise BuildError(f"The published {name} image provenance labels do not match this release.")
            result_images[name] = immutable
        finally:
            metadata_path.unlink(missing_ok=True)

    if args.operation == "test-stack":
        provenance_images = {args.component: result_images[args.component]}
    elif args.operation in {"validate-database"}:
        provenance_images = {"migrator": result_images["migrator"]}
    elif args.operation == "deploy-stack":
        provenance_images = dict(result_images)
    else:
        provenance_images = {"main": result_images["main"]}
    try:
        provenance_audience = audience_for(provenance_images, build_hash, args.source_sha)
    except ProvenanceError as exc:
        raise BuildError(f"Could not bind the provenance token to this build: {exc}") from exc

    if args.operation == "deploy-stack":
        output = {"images": {name: value for name, value in result_images.items() if name != "migrator"}}
        if "migrator" in result_images:
            output["migration_image"] = result_images["migrator"]
    elif args.operation == "test-stack":
        output = {"image": result_images[args.component]}
    elif args.operation == "validate-database":
        output = {"migration_image": result_images["migrator"]}
    else:
        output = {"image": result_images["main"]}
    return {
        **output,
        "build_spec_sha256": build_hash,
        "provenance_audience": provenance_audience,
        "images": output.get("images", {key: output[key] for key in ("image", "migration_image") if key in output}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--platform-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--component")
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_release(args)
    except BuildError as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 2
    if args.github_output:
        try:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
                for key in ("image", "migration_image", "images", "build_spec_sha256", "provenance_audience"):
                    if key in result:
                        value = json.dumps(result[key], sort_keys=True, separators=(",", ":")) if isinstance(result[key], dict) else str(result[key])
                        stream.write(f"{key}={value}\n")
        except OSError as exc:
            print("release build failed: GitHub Actions output file is unavailable", file=sys.stderr)
            return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
