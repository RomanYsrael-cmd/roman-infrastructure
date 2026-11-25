"""Release-scoped Flyway migration-plan validation used by the controller.

This module validates migration metadata and result history. The deployment
controller binds its plan fingerprint to the immutable migrator image, verified
source commit, database identity, backup, and release attempt, then rechecks the
plan after backup and writer quiescence before running Flyway.
"""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = re.compile(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))*\Z")
MIGRATION_FILE = re.compile(
    r"V(?P<version>\d+(?:[._]\d+)*)__"
    r"(?P<description>[A-Za-z0-9]+(?:[ _][A-Za-z0-9]+)*)\.sql\Z",
    re.ASCII,
)
SQL_CONF_SUFFIX = ".sql.conf"
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
    history: tuple[tuple[str, str, str, str, str, str, int | None], ...] = ()
    release_fingerprint: str | None = None

    @property
    def needs_migration(self) -> bool:
        return bool(self.pending)


@dataclass(frozen=True)
class MigrationSqlIdentity:
    filename: str
    version: str
    version_parts: tuple[int, ...]
    description: str


def _version_tuple(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise MigrationPlanError("Flyway migration version is not canonical numeric notation.")
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _parse_migration_filename(filename: str) -> MigrationSqlIdentity:
    """Parse a Flyway versioned SQL filename without rewriting its identity."""
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise MigrationPlanError("The Flyway SQL migration filename is unsafe.")
    match = MIGRATION_FILE.fullmatch(filename)
    if match is None:
        raise MigrationPlanError("A versioned SQL migration has an invalid Flyway identity.")
    version = match.group("version").replace("_", ".")
    version_parts = _version_tuple(version)
    description = re.sub(r"[ _]+", " ", match.group("description"))
    return MigrationSqlIdentity(filename, version, version_parts, description)


def _parse_migration_script_path(filepath: str) -> MigrationSqlIdentity:
    """Require Flyway's script path to be a direct child of the verified SQL root."""
    prefix = "/flyway/sql/"
    if not isinstance(filepath, str) or not filepath.startswith(prefix):
        raise MigrationPlanError("A versioned migration path is outside the verified SQL directory.")
    filename = filepath.removeprefix(prefix)
    if not filename or "/" in filename or "\\" in filename:
        raise MigrationPlanError("A versioned migration path is malformed or traverses directories.")
    return _parse_migration_filename(filename)


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


def flyway_sql_checksum(path: Path) -> int:
    """Calculate Flyway's UTF-8 SQL CRC32 with line ending and BOM normalization."""
    checksum = 0
    try:
        with path.open("r", encoding="utf-8", newline=None) as stream:
            for line_number, line in enumerate(stream):
                if line.endswith("\n"):
                    line = line[:-1]
                if line_number == 0 and line.startswith("\ufeff"):
                    line = line[1:]
                checksum = zlib.crc32(line.encode("utf-8"), checksum)
    except (OSError, UnicodeError) as exc:
        raise MigrationPlanError("A release SQL migration is unavailable or is not valid UTF-8.") from exc
    return checksum if checksum < 2**31 else checksum - 2**32


def migration_artifact_checksums(directory: Path) -> dict[str, int]:
    """Read versioned SQL identities and Flyway checksums from the exact migrator image."""
    if directory.is_symlink() or not directory.is_dir():
        raise MigrationPlanError("The verified SQL migration directory is unavailable or unsafe.")
    checksums: dict[str, int] = {}
    versions: set[tuple[int, ...]] = set()
    sidecars: set[str] = set()
    try:
        paths = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MigrationPlanError("The verified SQL migration directory cannot be read.") from exc
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise MigrationPlanError("Nested or linked SQL migration paths are unsupported.")
        if path.name.endswith(SQL_CONF_SUFFIX):
            sql_filename = path.name[:-len(".conf")]
            _parse_migration_filename(sql_filename)
            sidecars.add(sql_filename)
            continue
        if not path.name.startswith("V") or not path.name.endswith(".sql"):
            raise MigrationPlanError("The verified SQL migration directory contains an unsupported file.")
        identity = _parse_migration_filename(path.name)
        if identity.version_parts in versions:
            raise MigrationPlanError("The release artifact contains duplicate versioned SQL identities.")
        versions.add(identity.version_parts)
        checksums[path.name] = flyway_sql_checksum(path)
    missing_sql = sidecars - checksums.keys()
    if missing_sql:
        raise MigrationPlanError("A Flyway SQL configuration sidecar has no matching migration file.")
    return checksums


def bind_flyway_checksums(
    info_output: str,
    history_output: str,
    release_checksums: dict[str, int] | None = None,
) -> str:
    """Bind Flyway info identities to PostgreSQL history and exact release SQL checksums."""
    try:
        report = json.loads(info_output)
        database_history = json.loads(history_output)
    except (TypeError, ValueError) as exc:
        raise MigrationPlanError("Flyway info or database history is not valid JSON.") from exc
    if not isinstance(report, dict) or not isinstance(report.get("migrations"), list):
        raise MigrationPlanError("Flyway info has no migration list to bind.")
    if not isinstance(database_history, list):
        raise MigrationPlanError("The database returned malformed Flyway history.")
    if release_checksums is not None and (
        not isinstance(release_checksums, dict)
        or any(
            not isinstance(name, str)
            or isinstance(checksum, bool)
            or not isinstance(checksum, int)
            or not -(2**31) <= checksum < 2**31
            for name, checksum in release_checksums.items()
        )
    ):
        raise MigrationPlanError("The release migration checksum set is malformed.")

    def migration_key(version: Any, description: Any, migration_type: Any, script: Any) -> tuple[str, str, str, str]:
        if not isinstance(version, str) or not isinstance(description, str) or not isinstance(migration_type, str) or not isinstance(script, str):
            raise MigrationPlanError("The Flyway migration identity is malformed.")
        if not script or "/" in script or "\\" in script or script in {".", ".."}:
            raise MigrationPlanError("The Flyway migration script identity is unsafe.")
        identity = _parse_migration_filename(script)
        if identity.version != version or identity.description != description:
            raise MigrationPlanError("Flyway version or description does not match the SQL migration filename.")
        return version, description, migration_type, script

    applied: dict[tuple[str, str, str, str], int] = {}
    database_versioned_keys: set[tuple[str, str, str, str]] = set()
    for row in database_history:
        if not isinstance(row, dict):
            raise MigrationPlanError("The database returned malformed Flyway history.")
        version = row.get("version")
        if version is None:
            continue
        if not isinstance(version, str) or not version:
            raise MigrationPlanError("The database returned a malformed applied migration version.")
        if row.get("type") != "SQL":
            continue
        key = migration_key(version, row.get("description"), row.get("type"), row.get("script"))
        if key in database_versioned_keys:
            raise MigrationPlanError("The database contains duplicate applied versioned migration identities.")
        database_versioned_keys.add(key)
        success = row.get("success")
        if not isinstance(success, bool):
            raise MigrationPlanError("The database returned an invalid Flyway success state.")
        if success:
            checksum = row.get("checksum")
            if isinstance(checksum, bool) or not isinstance(checksum, int) or not -(2**31) <= checksum < 2**31:
                raise MigrationPlanError("Applied versioned migrations need database SQL checksums.")
            applied[key] = checksum

    info_versioned_keys: set[tuple[str, str, str, str]] = set()
    for entry in report["migrations"]:
        if not isinstance(entry, dict):
            raise MigrationPlanError("Flyway info contains a malformed migration identity.")
        category = entry.get("category")
        migration_type = entry.get("type")
        state = entry.get("state")
        version = entry.get("version", "")
        description = entry.get("description", "")
        filepath = entry.get("filepath", "")
        if category != "Versioned" or migration_type != "SQL" or not version:
            continue
        if not isinstance(filepath, str) or not filepath.startswith("/flyway/sql/"):
            raise MigrationPlanError("A versioned migration path is outside the verified SQL directory.")
        script = filepath.removeprefix("/flyway/sql/")
        key = migration_key(version, description, migration_type, script)
        if state == "Success":
            if key in info_versioned_keys:
                raise MigrationPlanError("Flyway info contains a duplicate applied migration identity.")
            info_versioned_keys.add(key)
            checksum = applied.pop(key, None)
            if checksum is None:
                raise MigrationPlanError("Flyway info and PostgreSQL migration history disagree.")
            reported_checksum = entry.get("checksum")
            if reported_checksum is not None and reported_checksum != checksum:
                raise MigrationPlanError("Flyway info and PostgreSQL migration checksums disagree.")
            if release_checksums is not None and release_checksums.get(script) != checksum:
                raise MigrationPlanError("An applied migration differs from the verified release SQL artifact.")
            entry["checksum"] = checksum
        elif state == "Pending":
            checksum = release_checksums.get(script) if release_checksums is not None else None
            if isinstance(checksum, bool) or not isinstance(checksum, int):
                raise MigrationPlanError("A pending migration checksum is unavailable from the verified release image.")
            reported_checksum = entry.get("checksum")
            if reported_checksum is not None and reported_checksum != checksum:
                raise MigrationPlanError("Flyway info and verified release migration checksums disagree.")
            entry["checksum"] = checksum

    if applied or info_versioned_keys != database_versioned_keys.intersection(info_versioned_keys):
        raise MigrationPlanError("Flyway info and PostgreSQL migration history disagree.")
    return json.dumps(report, sort_keys=True, separators=(",", ":"))


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
    history: list[tuple[str, str, str, str, str, str, int | None]] = []
    seen_versioned_versions: set[tuple[int, ...]] = set()
    highest = source_tuple
    success_count = 0
    for entry in report["migrations"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("state"), str):
            raise MigrationPlanError("Flyway history contains an invalid entry.")
        state = entry["state"]
        category = entry.get("category", "")
        migration_type = entry.get("type", "")
        raw_version = entry.get("version", "")
        description = entry.get("description", "")
        filepath = entry.get("filepath", "")
        checksum = entry.get("checksum")
        if any(not isinstance(value, str) for value in (category, migration_type, raw_version, description, filepath)):
            raise MigrationPlanError("Flyway history contains malformed migration metadata.")
        if checksum is not None and (
            isinstance(checksum, bool)
            or not isinstance(checksum, int)
            or not -(2**31) <= checksum < 2**31
        ):
            raise MigrationPlanError("Flyway history contains an invalid checksum.")
        history.append((state, category, migration_type, raw_version, description, filepath, checksum))
        if state == "Success":
            success_count += 1
            if category == "Versioned":
                if migration_type != "SQL" or checksum is None or not raw_version:
                    raise MigrationPlanError("Applied versioned migrations need a version, SQL file and checksum.")
                parts = _version_tuple(raw_version)
                identity = _parse_migration_script_path(filepath)
                if identity.version != raw_version or identity.description != description:
                    raise MigrationPlanError("Applied Flyway identity does not match its SQL migration filename.")
                if parts in seen_versioned_versions:
                    raise MigrationPlanError("Flyway history contains duplicate versioned migration versions.")
                seen_versioned_versions.add(parts)
                if source_tuple is not None and parts > source_tuple:
                    raise MigrationPlanError("Applied migration history is ahead of the observed schema version.")
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
        version = raw_version
        parts = _version_tuple(version)
        identity = _parse_migration_script_path(filepath)
        filename = identity.filename
        if identity.version != version:
            raise MigrationPlanError("Pending migration file does not match the approved SQL directory and version.")
        if not isinstance(description, str) or not description.strip() or identity.description != description:
            raise MigrationPlanError("Pending migration description is missing.")
        if isinstance(checksum, bool) or not isinstance(checksum, int) or not -(2**31) <= checksum < 2**31:
            raise MigrationPlanError("Pending SQL checksum is unavailable or invalid.")
        if parts in seen_versioned_versions or (highest is not None and parts <= highest):
            raise MigrationPlanError("Pending versions must be unique and strictly increasing.")
        seen_versioned_versions.add(parts)
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
        "history": history,
        "schema_name": report.get("schemaName"),
        "flyway_version": report.get("flywayVersion"),
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ForwardMigrationPlan(source, target, tuple(pending), fingerprint, tuple(history))


def validate_migration_result(
    output: str,
    planned: ForwardMigrationPlan,
    *,
    allowed_exceptions: list[dict[str, str]] | None = None,
    expected_flyway_version: str | None = None,
    expected_schema_name: str | None = None,
) -> ForwardMigrationPlan:
    """Validate a completed Flyway report against the exact planned release."""
    result = validate_forward_plan(
        output,
        allowed_exceptions=allowed_exceptions,
        expected_flyway_version=expected_flyway_version,
        expected_schema_name=expected_schema_name,
    )
    if result.pending:
        raise MigrationPlanError("Flyway left pending migrations after execution.")
    if planned.target_version is None or result.source_version != planned.target_version:
        raise MigrationPlanError("The applied database version differs from the validated migration target.")
    for version, filename, description, checksum in planned.pending:
        if not any(
            row[0] == "Success"
            and row[1] == "Versioned"
            and row[2] == "SQL"
            and row[3] == version
            and Path(row[5]).name == filename
            and row[4] == description
            and row[6] == checksum
            for row in result.history
        ):
            raise MigrationPlanError("An approved migration is missing or has a different postflight history row.")
    return result
