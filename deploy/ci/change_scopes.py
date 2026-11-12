#!/usr/bin/env python3
"""Classify changed infrastructure paths into focused CI checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


SCOPES = (
    "controller",
    "configuration",
    "ansible",
    "monitoring",
    "cloudflare",
    "smoke",
    "workflow",
    "ci_policy",
    "docs",
)


def classify_paths(paths: list[str]) -> dict[str, object]:
    result: dict[str, object] = {scope: False for scope in SCOPES}
    unknown: list[str] = []
    for raw_path in paths:
        path = raw_path.replace("\\", "/").lstrip("/")
        if path == "*":
            result.update({scope: True for scope in SCOPES if scope != "docs"})
            continue
        if path.startswith(".github/workflows/"):
            result["workflow"] = True
        elif path.startswith("deploy/ci/") or path.startswith("deploy/tests/test_ci_"):
            result["ci_policy"] = True
        elif path in {"deploy/tests/test_controller.py", "deploy/tests/test_romanlms_ci_gate.py"} or path.startswith("deploy/controller/"):
            result["controller"] = True
        elif path.startswith("deploy/ansible/"):
            result["ansible"] = True
        elif path.startswith("deploy/monitoring/") or path == "deploy/tests/test_phase6_monitoring.py":
            result["monitoring"] = True
        elif path.startswith("deploy/cloudflare/"):
            result["cloudflare"] = True
        elif path.startswith("app/smoke/"):
            result["smoke"] = True
        elif path == "platform/applications.json" or path.startswith("platform/"):
            result["configuration"] = True
        elif path.startswith("docs/") or path in {"README.md", "LICENSE", "CHANGELOG.md"}:
            result["docs"] = True
        else:
            unknown.append(path)
    result["unknown_paths"] = unknown
    return result


def changed_paths(base: str, head: str) -> list[str]:
    if not base or set(base) == {"0"}:
        return ["*"]
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--base", default=os.environ.get("BASE_SHA", ""))
    parser.add_argument("--head", default=os.environ.get("GITHUB_SHA", "HEAD"))
    arguments = parser.parse_args()
    result = classify_paths(changed_paths(arguments.base, arguments.head))
    unknown = result.pop("unknown_paths")
    output_lines = [f"{key}={str(value).lower()}" for key, value in result.items()]
    output_lines.append("unknown_paths=" + json.dumps(unknown, separators=(",", ":")))
    if arguments.github_output:
        with arguments.github_output.open("a", encoding="utf-8") as output:
            output.write("\n".join(output_lines) + "\n")
    for line in output_lines:
        print(line)
    if unknown:
        print("Each changed path must map to a focused validation scope.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
