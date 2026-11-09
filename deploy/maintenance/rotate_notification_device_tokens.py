#!/usr/bin/env python3
"""Re-encrypt RomanLMS device tokens while holding a transaction through runtime refresh."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class RotationError(Exception):
    """A safe token-key rotation error without credential or token details."""


def decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RotationError("A token-encryption key is invalid.") from exc
    if len(key) != 32:
        raise RotationError("A token-encryption key must decode to 32 bytes.")
    return key


def decrypt_token(ciphertext: str, key: bytes, expected_hash: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise RotationError("A stored token hash is invalid; no rows were committed.")
    try:
        combined = base64.b64decode(ciphertext, validate=True)
        if len(combined) <= 12 + 16:
            raise ValueError("ciphertext is too short")
        plaintext = AESGCM(key).decrypt(combined[:12], combined[12:], None)
    except Exception as exc:
        raise RotationError("A stored token could not be authenticated; no rows were committed.") from exc
    if not plaintext or not hmac.compare_digest(hashlib.sha256(plaintext).hexdigest(), expected_hash):
        raise RotationError("A stored token did not match its verification hash; no rows were committed.")
    return plaintext


def encrypt_token(plaintext: bytes, key: bytes, iv: bytes | None = None) -> str:
    if not plaintext:
        raise RotationError("An empty notification token cannot be encrypted.")
    nonce = iv if iv is not None else os.urandom(12)
    if len(nonce) != 12:
        raise RotationError("The token-encryption nonce is invalid.")
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plaintext, None)).decode("ascii")


def _secure_text(path: Path, *, mode: int) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RotationError("A required protected key file is unavailable.") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or os.path.realpath(path) != str(path)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise RotationError("A protected key file has unsafe ownership or permissions.")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RotationError("A required protected key file is unreadable.") from exc


def _read_env(path: Path, *, secure: bool = False) -> dict[str, str]:
    if secure:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RotationError("A required protected environment file is unavailable.") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or os.path.realpath(path) != str(path)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RotationError("A protected environment file has unsafe ownership or permissions.")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RotationError("A required protected environment file is unavailable.") from exc
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise RotationError("A protected environment file has invalid syntax.")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or "\n" in value or "\r" in value:
            raise RotationError("A protected environment file has invalid syntax.")
        values[name] = value
    return values


def _psql_environment(database_env_path: Path) -> dict[str, str]:
    values = _read_env(database_env_path, secure=True)
    database_url = values.get("DB_URL", "")
    parsed = urlsplit(database_url.removeprefix("jdbc:"))
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 5432
        or parsed.path != "/romanlms_prod"
        or not values.get("DB_USERNAME")
        or not values.get("DB_PASSWORD")
    ):
        raise RotationError("The configured production database is not the allowlisted local database.")
    return os.environ | {
        "PGHOST": "127.0.0.1",
        "PGPORT": "5432",
        "PGDATABASE": "romanlms_prod",
        "PGUSER": values["DB_USERNAME"],
        "PGPASSWORD": values["DB_PASSWORD"],
    }


def _read_until(process: subprocess.Popen[str], marker: str) -> list[str]:
    output: list[str] = []
    while True:
        line = process.stdout.readline() if process.stdout else ""
        if not line:
            raise RotationError("The production database rotation transaction stopped unexpectedly.")
        value = line.rstrip("\r\n")
        if value == marker:
            return output
        output.append(value)
        if len(output) > 100_000:
            raise RotationError("The notification-token record count exceeded the safe rotation limit.")


def _send_sql(process: subprocess.Popen[str], sql: str) -> None:
    if process.stdin is None:
        raise RotationError("The production database rotation transaction is unavailable.")
    process.stdin.write(sql + "\n")
    process.stdin.flush()


def _rollback(process: subprocess.Popen[str]) -> None:
    if process.stdin is None or process.poll() is not None:
        return
    try:
        _send_sql(process, "ROLLBACK;")
        _send_sql(process, r"\q")
        process.stdin.close()
        process.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def rotate(
    database_env_path: Path,
    previous_runtime_env_path: Path,
    new_key_path: Path,
    *,
    control_stream=None,
    output_stream=None,
) -> int:
    control = control_stream or sys.stdin
    output = output_stream or sys.stdout
    old_values = _read_env(previous_runtime_env_path, secure=True)
    old_key = decode_key(old_values.get("NOTIFICATION_DEVICE_TOKEN_ENCRYPTION_KEY", ""))
    new_key = decode_key(_secure_text(new_key_path, mode=0o600))
    if hmac.compare_digest(old_key, new_key):
        raise RotationError("The replacement token-encryption key must differ from the current key.")

    process = subprocess.Popen(
        ["psql", "-X", "-q", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=_psql_environment(database_env_path),
    )
    decided = False
    try:
        _send_sql(process, "BEGIN;")
        _send_sql(process, "LOCK TABLE roman_lms.notification_device_tokens IN SHARE ROW EXCLUSIVE MODE;")
        _send_sql(process, "SELECT id::text, token_hash, token_ciphertext FROM roman_lms.notification_device_tokens ORDER BY id;")
        _send_sql(process, r"\echo __ROMANLMS_TOKEN_ROWS_END__")
        rows = _read_until(process, "__ROMANLMS_TOKEN_ROWS_END__")
        prepared = 0
        for row in rows:
            fields = row.split("|", 2)
            if len(fields) != 3:
                raise RotationError("The production token table returned an invalid row; no rows were committed.")
            token_id, token_hash, ciphertext = fields
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", token_id):
                raise RotationError("A production token row has an invalid identifier; no rows were committed.")
            plaintext = decrypt_token(ciphertext, old_key, token_hash)
            replacement = encrypt_token(plaintext, new_key)
            _send_sql(
                process,
                "UPDATE roman_lms.notification_device_tokens "
                f"SET token_ciphertext = '{replacement}' "
                f"WHERE id = '{token_id}' AND token_hash = '{token_hash}' RETURNING id;",
            )
            _send_sql(process, r"\echo __ROMANLMS_TOKEN_ROW_UPDATED__")
            updated = _read_until(process, "__ROMANLMS_TOKEN_ROW_UPDATED__")
            if updated != [token_id]:
                raise RotationError("A production token row changed during rotation; no rows were committed.")
            prepared += 1
        _send_sql(process, "SELECT count(*) FROM roman_lms.notification_device_tokens;")
        _send_sql(process, r"\echo __ROMANLMS_TOKEN_COUNT_END__")
        count_rows = _read_until(process, "__ROMANLMS_TOKEN_COUNT_END__")
        if count_rows != [str(prepared)]:
            raise RotationError("The production token count changed during rotation; no rows were committed.")

        print(f"prepared_rows={prepared}; enter COMMIT after the runtime refresh succeeds, or ROLLBACK if it fails.", file=output, flush=True)
        decision = control.readline().strip().upper()
        if decision not in {"COMMIT", "ROLLBACK"}:
            raise RotationError("Rotation was not committed; the database transaction is being rolled back.")
        _send_sql(process, decision + ";")
        marker = "__ROMANLMS_TOKEN_COMMITTED__" if decision == "COMMIT" else "__ROMANLMS_TOKEN_ROLLED_BACK__"
        _send_sql(process, rf"\echo {marker}")
        _read_until(process, marker)
        _send_sql(process, r"\q")
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=15)
        if process.returncode != 0:
            raise RotationError("The production database rotation did not finish cleanly.")
        decided = True
        try:
            new_key_path.unlink(missing_ok=True)
        except OSError:
            raise RotationError("The rotation finished but its temporary key file could not be removed.") from None
        print(f"outcome={decision.lower()} rows={prepared}", file=output, flush=True)
        return 0 if decision == "COMMIT" else 1
    finally:
        if not decided:
            _rollback(process)


def self_test() -> int:
    key = bytes(range(32))
    token = b"romanlms-fcm-key-rotation-self-test"
    expected_hash = hashlib.sha256(token).hexdigest()
    encrypted = encrypt_token(token, key, bytes(range(12)))
    if not hmac.compare_digest(decrypt_token(encrypted, key, expected_hash), token):
        raise RotationError("Token encryption self-test failed.")
    try:
        decrypt_token(encrypted, bytes(reversed(range(32))), expected_hash)
    except RotationError:
        pass
    else:
        raise RotationError("Token encryption self-test accepted the wrong key.")
    print("self_test=passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--database-env", type=Path, default=Path("/etc/roman-platform/romanlms-production/database.env"))
    parser.add_argument("--previous-runtime-env", type=Path, default=Path("/etc/roman-platform/romanlms-production/runtime.env.backup-before-token-rotation"))
    parser.add_argument("--new-key-file", type=Path, default=Path("/etc/roman-platform/romanlms-production/notification-token-key.next"))
    arguments = parser.parse_args()
    if arguments.self_test:
        return self_test()
    try:
        return rotate(arguments.database_env, arguments.previous_runtime_env, arguments.new_key_file)
    except (RotationError, OSError, subprocess.SubprocessError) as exc:
        print(f"rotation_error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
