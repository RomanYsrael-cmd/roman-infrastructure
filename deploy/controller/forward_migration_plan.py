"""Pure, release-scoped Flyway migration-plan validation.

This module does NOT run migrations or authorize deployment. A future controller
integration must bind the exact plan fingerprint to an immutable migrator image,
a verified source commit and a release attempt, then re-check it after backup
and writer quiescence before running Flyway.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

VERSION = re.compile(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))*\Z")
MIGRATION_FILE = re.compile(r"V(\d+(?:_\d+)*)__([a-z0-9_]+)\.sql\Z")
LEGACY_EXCEPTION_FIELDS = frozenset({
    "state", "category", "type", "version", "description", "filepath",
})


class MigrationPlanError(ValueError):
    """A pending migration plan cannot be safely authorized."""


@dataclass(frozen=True)
class ForwardMigrationPlan:
    source_version: str | None
    target_version: str | None
    pending: tuple[tuple[str, str, str, int], ...]
    fingerprint: str

    @property
    def needs_migration(self) -> bool:
        return bool(self.pending)


def _version_tuple(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise MigrationPlanError("Flyway migration version is not canonical numeric notation.")
    return tuple(int(part) for part in value.split("."))


def _validate_exceptions(value: list[dict[str, str]] | None) -> list[dict[str, str]]:
    exceptions = list(value or [])
    if any(
        not isinstance(item, dict)
        or set(item) != LEGACY_EXCEPTION_FIELDS
        or any(not isinstance(v, str) for v in item.values())
        for item in exceptions
    ):
        raise MigrationPlanError("Flyway history exception policy is invalid.")
    return exceptions


def validate_forward_plan(
    output: str,
    *,
    max_pending: int = 32,
    allowed_exceptions: list[dict[str, str]] | None = None,
    expected_flyway_version: str | None = None,
    expected_schema_name: str | None = None,
    allow_initial_schema: bool = False,
) -> ForwardMigrationPlan:
    """Validate ordered, forward-only SQL migrations from immutable Flyway info.

    No source or target version is hardcoded; the target is derived from
    validated pending SQL in the release image. Callers must independently
    verify image provenance, backup, database identity, compatibility and
    migration results. Rollbacks across versions are NOT authorized here.
    """
    if isinstance(max_pending, bool) or not isinstance(max_pending, int) or not 1 <= max_pending <= 100:
        raise MigrationPlanError("max_pending must be between 1 and 100.")
    try:
        report = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise MigrationPlanError("Flyway did not return valid migration JSON.") from exc
    if not isinstance(report, dict) or not isinstance(report.get("migrations"), list):
        raise MigrationPlanError("Flyway migration history is unavailable.")
    if not report["migrations"] and not allow_initial_schema:
        raise MigrationPlanError("Empty migration history requires a separately authorized bootstrap.")
    if expected_flyway_version and report.get("flywayVersion") != expected_flyway_version:
        raise MigrationPlanError("Flyway version does not match the verified image.")
    if expected_schema_name and report.get("schemaName") != expected_schema_name:
        raise MigrationPlanError("Flyway selected a different database schema.")
    if report.get("warnings") not in (None, []):
        raise MigrationPlanError("Flyway returned warnings requiring review.")

    raw_source = report.get("schemaVersion")
    if raw_source is None:
        if not allow_initial_schema:
            raise MigrationPlanError("Uninitialized database requires a separately authorized bootstrap.")
        source = None
        source_tuple = None
    else:
        source = str(raw_source)
        source_tuple = _version_tuple(source)

    exceptions = _validate_exceptions(allowed_exceptions)
    pending: list[tuple[str, str, str, int]] = []
    seen_versions: set[tuple[int, ...]] = set()
    highest = source_tuple
    success_count = 0
    for entry in report["migrations"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("state"), str):
            raise MigrationPlanError("Flyway history contains an invalid entry.")
        state = entry["state"]
        if state == "Success":
            success_count += 1
            version = entry.get("version")
            if version not in (None, ""):
                _version_tuple(str(version))
            continue
        if state != "Pending":
            exception = next(
                (rule for rule in exceptions if all(entry.get(k) == v for k, v in rule.items())),
                None,
            )
            if exception is None:
                raise MigrationPlanError("Flyway history includes an unapproved state.")
            exceptions.remove(exception)
            continue
        if entry.get("category") != "Versioned" or entry.get("type") != "SQL":
            raise MigrationPlanError("Only versioned SQL migrations can run automatically.")
        version = entry.get("version")
        parts = _version_tuple(version)
        filepath = entry.get("filepath")
        if not isinstance(filepath, str):
            raise MigrationPlanError("Pending SQL migration path is missing.")
        filename = filepath.removeprefix("/flyway/sql/")
        match = MIGRATION_FILE.fullmatch(filename)
        if not filepath.startswith("/flyway/sql/") or match is None or match.group(1).replace("_", ".") != version:
            raise MigrationPlanError("Pending migration file does not match the approved SQL directory and version.")
        description = entry.get("description")
        checksum = entry.get("checksum")
        if not isinstance(description, str) or not description.strip():
            raise MigrationPlanError("Pending migration description is missing.")
        if isinstance(checksum, bool) or not isinstance(checksum, int) or not -(2**31) <= checksum < 2**31:
            raise MigrationPlanError("Pending SQL checksum is unavailable or invalid.")
        if parts in seen_versions or (highest is not None and parts <= highest):
            raise MigrationPlanError("Pending versions must be unique and strictly increasing.")
        seen_versions.add(parts)
        highest = parts
        pending.append((version, filename, description, checksum))
        if len(pending) > max_pending:
            raise MigrationPlanError("Too many pending migrations for one release.")

    if exceptions:
        raise MigrationPlanError("Approved legacy history exceptions were not found.")
    if not allow_initial_schema and success_count == 0:
        raise MigrationPlanError("Migration history has no successful baseline.")
    if source is None and success_count:
        raise MigrationPlanError("Flyway source version is missing despite prior successful history.")
    target = pending[-1][0] if pending else source
    canonical = {
        "source_version": source,
        "target_version": target,
        "pending": pending,
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ForwardMigrationPlan(source, target, tuple(pending), fingerprint)
