#!/usr/bin/env python3
"""Collect bounded, data-free health metrics for the RomanLMS production release."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CONFIG = Path("/etc/roman-platform/applications.json")
STATE = Path("/srv/apps/romanlms-production/state/romanlms-production.json")
OUTPUT = Path("/srv/apps/monitoring/textfile_collector/romanlms_production.prom")
METRIC = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+([-+0-9.eE]+)(?:\s+[0-9]+)?$')


def probe(port: int, path: str, expected: bytes, limit: int = 8192) -> tuple[int, float]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            body = response.read(limit)
            return int(response.status == 200 and expected in body), time.monotonic() - started
    except (OSError, urllib.error.URLError, TimeoutError):
        return 0, time.monotonic() - started


def aggregate_metrics(text: str) -> dict[str, float]:
    result = {f"http_{code}xx": 0.0 for code in range(1, 6)}
    result.update({"http_other": 0.0, "http_count": 0.0, "http_duration": 0.0, "heap_used": 0.0, "nonheap_used": 0.0, "heap_max": 0.0, "cpu": 0.0, "db_connections": 0.0})
    for line in text.splitlines():
        match = METRIC.fullmatch(line.strip())
        if not match:
            continue
        name, labels, raw = match.groups()
        try:
            value = float(raw)
        except ValueError:
            continue
        if not math.isfinite(value) or value < 0:
            continue
        labels = labels or ""
        if name == "http_server_requests_seconds_count":
            status = re.search(r'(?:^|,)status="([1-5])[0-9]{2}"(?:,|$)', labels)
            result["http_" + (status.group(1) + "xx" if status else "other")] += value
            result["http_count"] += value
        elif name == "http_server_requests_seconds_sum":
            result["http_duration"] += value
        elif name == "jvm_memory_used_bytes":
            area = re.search(r'(?:^|,)area="(heap|nonheap)"(?:,|$)', labels)
            if area:
                result[area.group(1) + "_used"] += value
        elif name == "jvm_memory_max_bytes" and re.search(r'(?:^|,)area="heap"(?:,|$)', labels):
            result["heap_max"] += value
        elif name == "process_cpu_usage":
            result["cpu"] = value
        elif name == "hikaricp_connections_active":
            result["db_connections"] += value
    return result


def docker_health(name: str) -> int:
    if not re.fullmatch(r"roman-platform-romanlms-production-(?:backend|frontend)-(?:blue|green)-[0-9a-f]{12}", name):
        return 0
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{index .Config.Labels \"roman.platform.app\"}}", name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return int(result.returncode == 0 and result.stdout.strip() == "host|healthy|romanlms-production")
    except (OSError, subprocess.TimeoutExpired):
        return 0


def docker_stats(containers: dict[str, str]) -> dict[str, tuple[float, float]]:
    try:
        process = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers.values()], capture_output=True, text=True, timeout=20, check=False)
        if process.returncode:
            return {}
        reverse = {value: key for key, value in containers.items()}
        units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
        values: dict[str, tuple[float, float]] = {}
        for line in process.stdout.splitlines():
            item = json.loads(line)
            component = reverse.get(str(item.get("Name", "")).lstrip("/"))
            memory = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", str(item.get("MemUsage", "")).split("/")[0])
            if component and memory and memory.group(2) in units:
                values[component] = (float(memory.group(1)) * units[memory.group(2)], float(str(item.get("CPUPerc", "0%")).rstrip("%")))
        return values
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}


def load_active() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        app = config["applications"]["romanlms-production"]
        state = json.loads(STATE.read_text(encoding="utf-8"))
        active = state["active"]
        components = active["components"]
        for name in ("backend", "frontend"):
            record = components[name]
            slot = record["slot"]
            container = record["container"]
            if slot not in {"blue", "green"} or container != f"roman-platform-romanlms-production-{name}-{slot}-{record['commit_sha'][:12]}":
                raise ValueError("release identity mismatch")
            port = app["components"][name]["slot_ports"][slot]
            if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
                raise ValueError("invalid approved component port")
            record["port"] = port
        return active, components
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, {}


def write_metrics(lines: list[str]) -> None:
    OUTPUT.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".romanlms-production-", dir=OUTPUT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, OUTPUT)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main() -> int:
    if not os.path.ismount("/srv/apps") or not CONFIG.is_file():
        return 1
    _, components = load_active()
    up: dict[str, int] = {"backend": 0, "frontend": 0}
    duration: dict[str, float] = {"backend": 0.0, "frontend": 0.0}
    containers: dict[str, str] = {}
    aggregated = aggregate_metrics("")
    metrics_up = 0
    if components:
        backend = components["backend"]
        frontend = components["frontend"]
        backend_http, duration["backend"] = probe(backend["port"], "/lms/actuator/health", b'"status":"UP"')
        frontend_http, duration["frontend"] = probe(frontend["port"], "/", b"<!doctype html>")
        up["backend"] = backend_http * docker_health(backend["container"])
        up["frontend"] = frontend_http * docker_health(frontend["container"])
        containers = {name: components[name]["container"] for name in ("backend", "frontend")}
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{backend['port']}/lms/actuator/prometheus", timeout=10) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise OSError("metrics payload limit exceeded")
                aggregated = aggregate_metrics(payload.decode("utf-8", errors="replace"))
            metrics_up = 1
        except (OSError, urllib.error.URLError, TimeoutError):
            metrics_up = 0

    lines = [
        f'romanlms_production_component_up{{app_component="backend"}} {up["backend"]}',
        f'romanlms_production_component_up{{app_component="frontend"}} {up["frontend"]}',
        f'romanlms_production_probe_seconds{{app_component="backend"}} {duration["backend"]:.6f}',
        f'romanlms_production_probe_seconds{{app_component="frontend"}} {duration["frontend"]:.6f}',
        f"romanlms_production_metrics_up {metrics_up}",
    ]
    if metrics_up:
        lines.extend([
            f'romanlms_production_jvm_memory_used_bytes{{area="heap"}} {aggregated["heap_used"]}',
            f'romanlms_production_jvm_memory_used_bytes{{area="nonheap"}} {aggregated["nonheap_used"]}',
            f"romanlms_production_jvm_heap_max_bytes {aggregated['heap_max']}",
            f"romanlms_production_process_cpu_ratio {aggregated['cpu']}",
            f"romanlms_production_database_connections {aggregated['db_connections']}",
            "# TYPE romanlms_production_http_requests_observed_total counter",
            f"romanlms_production_http_requests_observed_total {aggregated['http_count']}",
            f"romanlms_production_http_duration_seconds_total {aggregated['http_duration']}",
            "# TYPE romanlms_production_http_requests_total counter",
        ])
        for key in ("1xx", "2xx", "3xx", "4xx", "5xx", "other"):
            lines.append(f'romanlms_production_http_requests_total{{status_class="{key}"}} {aggregated["http_" + key]}')
    for component, (memory, cpu) in docker_stats(containers).items():
        lines.append(f'romanlms_production_container_memory_bytes{{app_component="{component}"}} {memory}')
        lines.append(f'romanlms_production_container_cpu_percent{{app_component="{component}"}} {cpu}')
    write_metrics(lines)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("RomanLMS production metric collection failed.", file=sys.stderr)
        raise SystemExit(1)
