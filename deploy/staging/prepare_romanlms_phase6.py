#!/usr/bin/env python3
"""Create a new isolated Phase 6 database and object store; never touches production."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NETWORK = "romanlms-phase6-staging"
SUBNET = "172.30.66.0/24"
DB_NAME = "romanlms-phase6-db"
OBJECT_NAME = "romanlms-phase6-object"
DB_URL = "jdbc:postgresql://romanlms-phase6-db:5432/romanlms_phase6"
SOURCE = Path("/run/roman-platform/romanlms-phase6-source.dump")
SOURCE_SHA256 = "b9a1d013332c7692d11b84c2503e51195b8e6ec36e71a5fa223640fa9a617890"
CONFIG = Path("/etc/roman-platform/romanlms-phase6")
DB_ROOT = Path("/srv/databases/romanlms-phase6")
OBJECT_ROOT = Path("/srv/data/romanlms-phase6")
MARKER = Path("/srv/data/romanlms-phase6/restore-ready.json")
POSTGRES_IMAGE = "postgres@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395"
OBJECT_IMAGE = "chrislusf/seaweedfs@sha256:57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59"


class SetupError(Exception):
    pass


def command(args: list[str], *, input_bytes: bytes | None = None, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(args, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"Required operation failed: {Path(args[0]).name}.") from exc
    if check and result.returncode:
        # Never emit child output: database tools may include connection details.
        raise SetupError(f"Required operation failed: {Path(args[0]).name} (exit {result.returncode}).")
    return result


def write_private(path: Path, values: dict[str, str]) -> None:
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def ensure_fresh() -> None:
    if os.geteuid() != 0:
        raise SetupError("Run this setup as root through the existing administrative mechanism.")
    for mount in ("/srv/apps", "/srv/databases", "/srv/data"):
        if not os.path.ismount(mount):
            raise SetupError(f"Required storage mount is unavailable: {mount}.")
        if shutil.disk_usage(mount).free < 10 * 1024**3:
            raise SetupError(f"Insufficient free staging capacity on {mount}.")
    memory = Path("/proc/meminfo").read_text(encoding="ascii")
    available = next((int(line.split()[1]) for line in memory.splitlines() if line.startswith("MemAvailable:")), 0)
    if available < 3 * 1024 * 1024:
        raise SetupError("Insufficient available memory for isolated staging dependencies.")
    routes = json.loads(command(["ip", "-j", "-4", "route", "show", "table", "all"], timeout=15).stdout)
    staging_network = ipaddress.ip_network(SUBNET)
    for route in routes:
        destination = route.get("dst", "default")
        if destination == "default":
            continue
        try:
            existing = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        if existing.overlaps(staging_network):
            raise SetupError("The proposed staging subnet conflicts with an existing host route.")
    docker_root = command(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=20).stdout.decode().strip()
    if not os.path.realpath(docker_root).startswith("/srv/apps/"):
        raise SetupError("Docker storage is not on the applications mount.")
    if command(["findmnt", "-n", "-o", "FSTYPE", "--target", "/run"], timeout=10).stdout.decode().strip() != "tmpfs":
        raise SetupError("The temporary restore location is not memory-backed.")
    try:
        info = SOURCE.lstat()
    except OSError as exc:
        raise SetupError("The root-controlled source dump is unavailable.") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise SetupError("The source dump must be a private root-owned regular file.")
    digest = hashlib.sha256()
    with SOURCE.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != SOURCE_SHA256:
        raise SetupError("The source dump checksum does not match the reviewed backup.")
    for path in (DB_ROOT, OBJECT_ROOT):
        if path.exists() or path.is_symlink():
            raise SetupError(f"The staging destination already exists: {path}; inspect it before retrying.")
    for path in (CONFIG / "database.env", CONFIG / "runtime.env", CONFIG / "object.env", CONFIG / "postgres.env"):
        if path.exists() or path.is_symlink():
            raise SetupError("Staging credentials already exist; inspect the prior setup before retrying.")
    for name in (DB_NAME, OBJECT_NAME):
        if command(["docker", "inspect", name], check=False).returncode == 0:
            raise SetupError("A staging service with the reserved name already exists.")
    if command(["docker", "network", "inspect", NETWORK], check=False).returncode == 0:
        raise SetupError("The staging network already exists; inspect it before retrying.")


def wait_healthy(name: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = command(["docker", "inspect", "--format", "{{.State.Health.Status}}", name], timeout=15, check=False)
        if result.returncode == 0 and result.stdout.decode().strip() == "healthy":
            return
        time.sleep(2)
    raise SetupError(f"Staging service did not become healthy: {name}.")


def main() -> int:
    os.umask(0o077)
    ensure_fresh()
    CONFIG.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CONFIG.is_symlink() or CONFIG.stat().st_uid != 0 or stat.S_IMODE(CONFIG.stat().st_mode) & 0o077:
        raise SetupError("Staging configuration directory must be private and root-owned.")
    DB_ROOT.mkdir(mode=0o700)
    OBJECT_ROOT.mkdir(mode=0o700)
    database_dir = DB_ROOT / "postgres"
    object_dir = OBJECT_ROOT / "objects"
    database_dir.mkdir(mode=0o700)
    object_dir.mkdir(mode=0o700)
    os.chown(database_dir, 70, 70)
    os.chown(object_dir, 10001, 10001)

    owner_password = secrets.token_urlsafe(48)
    app_password = secrets.token_urlsafe(48)
    object_access = "phase6" + secrets.token_hex(12)
    object_secret = secrets.token_urlsafe(48)
    write_private(CONFIG / "postgres.env", {"POSTGRES_USER": "phase6_owner", "POSTGRES_PASSWORD": owner_password, "POSTGRES_DB": "romanlms_phase6", "PGDATA": "/var/lib/postgresql/data/pgdata"})
    write_private(CONFIG / "database.env", {"DB_URL": DB_URL, "DB_USERNAME": "phase6_app", "DB_PASSWORD": app_password})
    write_private(CONFIG / "object.env", {"AWS_ACCESS_KEY_ID": object_access, "AWS_SECRET_ACCESS_KEY": object_secret, "S3_BUCKET": "romanlms-phase6"})
    write_private(CONFIG / "runtime.env", {
        "DB_URL": DB_URL, "DB_USERNAME": "phase6_app", "DB_PASSWORD": app_password,
        "APP_RUNTIME_ENV": "phase6-staging", "APP_RUNTIME_FORBIDDEN_DB_URL_SUBSTRINGS": "romanlms_prod,neon.tech",
        "ROMANLMS_STORAGE_PREFIX": "phase6/", "ROMANLMS_MAIL_ENABLED": "false", "ROMANLMS_NOTIFICATIONS_FCM_ENABLED": "false",
        "ROMANLMS_SCHEDULING_ENABLED": "false", "SPRING_FLYWAY_ENABLED": "false", "SERVER_ADDRESS": "0.0.0.0", "PORT": "8080",
        "DB_POOL_MAX_SIZE": "5", "DB_POOL_MIN_IDLE": "0", "HOME": "/tmp", "REQUEST_MAPPING_LOG_LEVEL": "INFO", "S3_ENDPOINT": "http://romanlms-phase6-object:8333",
        "S3_BUCKET": "romanlms-phase6", "S3_ACCESS_KEY_ID": object_access, "S3_SECRET_ACCESS_KEY": object_secret,
        "S3_REGION": "us-east-1", "S3_PATH_STYLE_ACCESS_ENABLED": "true", "JWT_SECRET": secrets.token_urlsafe(64),
        "REFRESH_TOKEN_PEPPER": secrets.token_urlsafe(48), "AUTH_EMAIL_TOKEN_PEPPER": secrets.token_urlsafe(48),
        "NOTIFICATION_DEVICE_TOKEN_ENCRYPTION_KEY": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "JAVA_TOOL_OPTIONS": "-Xms128m -Xmx640m -XX:MaxMetaspaceSize=256m -XX:+ExitOnOutOfMemoryError",
    })
    command(["docker", "network", "create", "--internal", "--subnet", SUBNET, NETWORK], timeout=30)
    for image in (POSTGRES_IMAGE, OBJECT_IMAGE):
        command(["docker", "pull", image], timeout=900)
    command(["docker", "run", "-d", "--name", DB_NAME, "--label", "roman.platform.staging=romanlms-phase6", "--label", "roman.platform.role=database", "--network", NETWORK, "--ip", "172.30.66.10", "--restart", "no", "--user", "70:70", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "1g", "--cpus", "1", "--pids-limit", "128", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--tmpfs", "/var/run/postgresql:rw,size=16m,uid=70,gid=70,mode=0770", "--mount", f"type=bind,src={database_dir},dst=/var/lib/postgresql/data", "--env-file", str(CONFIG / "postgres.env"), "--health-cmd", "pg_isready -U phase6_owner -d romanlms_phase6", "--health-interval", "10s", "--health-timeout", "5s", "--health-retries", "12", POSTGRES_IMAGE], timeout=60)
    wait_healthy(DB_NAME)
    with SOURCE.open("rb") as stream:
        try:
            result = subprocess.run(["docker", "exec", "-i", DB_NAME, "pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "-U", "phase6_owner", "-d", "romanlms_phase6"], stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SetupError("The isolated database restore did not complete.") from exc
    if result.returncode:
        raise SetupError("The isolated database restore failed; the source backup was preserved.")
    sql = (
        f"CREATE ROLE phase6_app LOGIN PASSWORD '{app_password}';\n"
        "GRANT CONNECT ON DATABASE romanlms_phase6 TO phase6_app;\n"
        "GRANT USAGE ON SCHEMA roman_lms TO phase6_app;\n"
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA roman_lms TO phase6_app;\n"
        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA roman_lms TO phase6_app;\n"
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA roman_lms TO phase6_app;\n"
    )
    command(["docker", "exec", "-i", DB_NAME, "psql", "-v", "ON_ERROR_STOP=1", "-U", "phase6_owner", "-d", "romanlms_phase6"], input_bytes=sql.encode(), timeout=120)
    result = command(["docker", "exec", DB_NAME, "psql", "-At", "-U", "phase6_owner", "-d", "romanlms_phase6", "-c", "SELECT version || ':' || success::text FROM roman_lms.flyway_schema_history WHERE version IS NOT NULL ORDER BY installed_rank DESC LIMIT 1"], timeout=30)
    if result.stdout.decode().strip() != "132:true":
        raise SetupError("The isolated restore does not have the reviewed V132 schema state.")
    command(["docker", "run", "-d", "--name", OBJECT_NAME, "--label", "roman.platform.staging=romanlms-phase6", "--label", "roman.platform.role=object", "--network", NETWORK, "--ip", "172.30.66.11", "--restart", "no", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m", "--cpus", "0.5", "--pids-limit", "128", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--mount", f"type=bind,src={object_dir},dst=/data", "--env-file", str(CONFIG / "object.env"), "--health-cmd", "test $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8333/) = 403", "--health-interval", "10s", "--health-timeout", "5s", "--health-retries", "12", OBJECT_IMAGE, "mini", "-dir=/data", "-ip=172.30.66.11", "-ip.bind=0.0.0.0", "-webdav=false", "-admin.ui=false"], timeout=60)
    wait_healthy(OBJECT_NAME)
    marker = {"source_sha256": SOURCE_SHA256, "source_snapshot": "2026-11-06_162051", "schema_version": "132", "restored_at": datetime.now(timezone.utc).isoformat()}
    fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(marker, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    command(["systemctl", "enable", "--now", "romanlms-phase6.service"], timeout=300)
    print("Private Phase 6 staging restore and dependencies are ready; production was not modified.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
