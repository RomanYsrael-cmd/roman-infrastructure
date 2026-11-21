#!/usr/bin/env python3
"""Run a registered build-only toolchain and create a checksummed release artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "controller"))
from application_registry import RegistryError, validate_registry  # noqa: E402

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024


class BuildArtifactError(RuntimeError):
    """A release artifact could not be built or verified."""


def _git_head(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildArtifactError("The checked out source revision could not be verified.") from exc
    if result.returncode != 0:
        raise BuildArtifactError("The checked out source revision could not be verified.")
    return result.stdout.strip()


def _commands_for(system: str, project: Path) -> list[list[str]]:
    if system in {"gradle-android", "gradle-jvm"}:
        wrapper = project / "gradlew"
        if not wrapper.is_file() or wrapper.is_symlink():
            raise BuildArtifactError("Gradle builds require a regular repository Gradle wrapper.")
        tasks = ["test", "assembleDebug"] if system == "gradle-android" else ["test", "build"]
        return [["./gradlew", "--no-daemon", "--console=plain", *tasks]]
    if system == "maven-jvm":
        wrapper = project / "mvnw"
        if not wrapper.is_file() or wrapper.is_symlink():
            raise BuildArtifactError("Maven builds require a regular repository Maven wrapper.")
        return [["./mvnw", "--batch-mode", "--no-transfer-progress", "verify"]]
    if system == "npm":
        if not (project / "package-lock.json").is_file():
            raise BuildArtifactError("Node builds require a committed package-lock.json.")
        return [["npm", "ci"], ["npm", "test", "--if-present"], ["npm", "run", "build", "--if-present"]]
    if system == "python":
        if not (project / "pyproject.toml").is_file():
            raise BuildArtifactError("Python package builds require a pyproject.toml.")
        return [
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "build", "pytest"],
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "."],
            [sys.executable, "-m", "pytest"],
            [sys.executable, "-m", "build", "--no-isolation"],
        ]
    raise BuildArtifactError("The registered build-only toolchain is unsupported.")


def _collect_artifacts(project: Path, patterns: list[str]) -> list[Path]:
    matches: dict[str, Path] = {}
    for pattern in patterns:
        try:
            found = project.glob(pattern)
            for candidate in found:
                resolved = candidate.resolve(strict=True)
                if candidate.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(project.resolve()):
                    raise BuildArtifactError("A release output is not a regular file inside the registered project directory.")
                relative = resolved.relative_to(project.resolve()).as_posix()
                matches[relative] = resolved
        except (OSError, ValueError) as exc:
            raise BuildArtifactError("A registered artifact path could not be inspected safely.") from exc
    if not matches:
        raise BuildArtifactError("The registered build completed without producing any configured release artifacts.")
    return [matches[key] for key in sorted(matches)]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_artifact(args: argparse.Namespace) -> dict[str, Any]:
    try:
        registry = json.loads(args.registry.read_text(encoding="utf-8"))
        validate_registry(registry)
    except (OSError, json.JSONDecodeError, RegistryError) as exc:
        raise BuildArtifactError("The checked out application registry is invalid.") from exc
    app = registry["applications"].get(args.app_id)
    if not isinstance(app, dict) or app.get("deployment_profile") != "build-only":
        raise BuildArtifactError("The application is not registered for build-only release artifacts.")
    if (
        not isinstance(args.source_sha, str)
        or len(args.source_sha) != 40
        or any(character not in "0123456789abcdef" for character in args.source_sha)
        or not isinstance(args.platform_sha, str)
        or args.platform_sha not in app["allowed_platform_shas"]
    ):
        raise BuildArtifactError("The source or pinned platform revision is invalid for this registration.")
    if _git_head(args.source_root) != args.source_sha or _git_head(args.platform_root) != args.platform_sha:
        raise BuildArtifactError("The checked out source does not match the authorized immutable revisions.")

    spec = app["artifact_build"]
    project = (args.source_root / spec["project_path"]).resolve()
    source_root = args.source_root.resolve()
    if not project.is_dir() or project.is_symlink() or not project.is_relative_to(source_root):
        raise BuildArtifactError("The registered project path is unavailable or escapes the application checkout.")
    output = args.output.resolve()
    if args.output.is_symlink() or output.is_relative_to(source_root) or output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise BuildArtifactError("The artifact output directory must be outside the checkout and empty.")
    output.mkdir(parents=True, exist_ok=True)

    build_env = os.environ.copy()
    build_env.update({"SOURCE_DATE_EPOCH": "0", "TZ": "UTC", "LC_ALL": "C"})
    for command in _commands_for(spec["system"], project):
        try:
            result = subprocess.run(command, cwd=project, env=build_env, check=False, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildArtifactError(f"Registered {Path(command[0]).name} command could not complete.") from exc
        if result.returncode != 0:
            raise BuildArtifactError(f"Registered {Path(command[0]).name} command failed with exit code {result.returncode}.")

    files = _collect_artifacts(project, spec["artifact_globs"])
    if sum(path.stat().st_size for path in files) > MAX_ARTIFACT_BYTES:
        raise BuildArtifactError("The registered release artifacts exceed the 2 GiB upload limit.")
    artifact_root = output / "artifacts"
    artifact_root.mkdir()
    checksums: list[str] = []
    artifact_rows: list[dict[str, Any]] = []
    for source in files:
        relative = source.relative_to(project).as_posix()
        destination = artifact_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        digest = _sha256_file(destination)
        size = destination.stat().st_size
        artifact_rows.append({"path": relative, "sha256": digest, "size_bytes": size})
        checksums.append(f"{digest}  artifacts/{relative}")

    manifest = {
        "application_id": args.app_id,
        "repository": app["repository"],
        "source_sha": args.source_sha,
        "platform_sha": args.platform_sha,
        "build_system": spec["system"],
        "artifacts": artifact_rows,
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
    )
    (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    archive = args.archive.resolve()
    if args.archive.is_symlink() or archive.is_relative_to(source_root) or archive.is_relative_to(output):
        raise BuildArtifactError("The release archive must be outside the application checkout.")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                relative = path.relative_to(output).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | (path.stat().st_mode & 0o777)) << 16
                with path.open("rb") as source, bundle.open(info, "w") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
    return {**manifest, "archive": str(archive), "archive_sha256": _sha256_file(archive)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--platform-sha", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_artifact(args)
    except BuildArtifactError as exc:
        print(f"build-only release failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
