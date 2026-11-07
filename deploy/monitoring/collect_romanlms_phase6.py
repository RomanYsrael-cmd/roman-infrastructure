#!/usr/bin/env python3
"""Collect bounded private RomanLMS metrics without exporting request labels or data."""
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

STATE = Path("/srv/apps/romanlms-phase6/state/romanlms-phase6.json")
OUTPUT = Path("/srv/apps/monitoring/textfile_collector/romanlms_phase6.prom")
BASE = "http://127.0.0.1:18081"
SAMPLE = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+([-+0-9.eE]+)(?:\s+[0-9]+)?$')


def aggregate_metrics(text: str) -> dict[str, float]:
    result = {f"http_{code}xx": 0.0 for code in range(1, 6)}
    result.update({"http_other": 0.0, "http_count": 0.0, "http_duration": 0.0, "heap_used": 0.0, "nonheap_used": 0.0, "heap_max": 0.0, "cpu": 0.0, "db_connections": 0.0})
    for line in text.splitlines():
        match = SAMPLE.fullmatch(line.strip())
        if not match:
            continue
        name, labels, raw_value = match.groups()
        try:
            value = float(raw_value)
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


def probe(path: str, expected: bytes) -> tuple[int, float]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as response:
            return int(response.status == 200 and expected in response.read(8192)), time.monotonic() - started
    except (OSError, urllib.error.URLError, TimeoutError):
        return 0, time.monotonic() - started


def docker_health(name: str) -> int:
    try:
        result = subprocess.run(["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", name], capture_output=True, text=True, timeout=10, check=False)
        return int(result.returncode == 0 and result.stdout.strip() == "healthy")
    except (OSError, subprocess.TimeoutExpired):
        return 0


def docker_stats(containers: dict[str, str]) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    if not containers:
        return result
    try:
        process = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers.values()], capture_output=True, text=True, timeout=20, check=False)
        if process.returncode:
            return result
        reverse = {value: key for key, value in containers.items()}
        units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
        for line in process.stdout.splitlines():
            item = json.loads(line)
            component = reverse.get(str(item.get("Name", "")).lstrip("/"))
            memory = re.fullmatch(r'\s*([0-9.]+)\s*([A-Za-z]+)\s*', str(item.get("MemUsage", "")).split("/")[0])
            if component and memory and memory.group(2) in units:
                result[component] = (float(memory.group(1)) * units[memory.group(2)], float(str(item.get("CPUPerc", "0%")).rstrip("%")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}
    return result


def main() -> int:
    if not STATE.exists():
        return 0
    if not os.path.ismount("/srv/apps"):
        return 1
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        active = state["active"]
        records = active["components"]
        containers = {name: records[name]["container"] for name in ("backend", "frontend")}
        if any(not re.fullmatch(r"roman-platform-romanlms-phase6-(?:backend|frontend)-(?:blue|green)-[0-9a-f]{12}", value) for value in containers.values()):
            return 1
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return 1
    backend, backend_time = probe("/lms/actuator/health", b'"status":"UP"')
    frontend, frontend_time = probe("/", b"<!doctype html>")
    try:
        with urllib.request.urlopen(BASE + "/lms/actuator/prometheus", timeout=10) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise OSError("Application metrics exceeded the collection limit")
            raw = payload.decode("utf-8", errors="replace")
        aggregated = aggregate_metrics(raw)
        metrics_up = 1
    except (OSError, urllib.error.URLError, TimeoutError):
        aggregated = aggregate_metrics("")
        metrics_up = 0
    lines = [
        f'romanlms_phase6_component_up{{app_component="backend"}} {backend}',
        f'romanlms_phase6_component_up{{app_component="frontend"}} {frontend}',
        f'romanlms_phase6_probe_seconds{{app_component="backend"}} {backend_time:.6f}',
        f'romanlms_phase6_probe_seconds{{app_component="frontend"}} {frontend_time:.6f}',
        f'romanlms_phase6_dependency_up{{dependency="database"}} {docker_health("romanlms-phase6-db")}',
        f'romanlms_phase6_dependency_up{{dependency="object"}} {docker_health("romanlms-phase6-object")}',
        f"romanlms_phase6_metrics_up {metrics_up}",
    ]
    if metrics_up:
        lines.extend([
            f'romanlms_phase6_jvm_memory_used_bytes{{area="heap"}} {aggregated["heap_used"]}',
            f'romanlms_phase6_jvm_memory_used_bytes{{area="nonheap"}} {aggregated["nonheap_used"]}',
            f'romanlms_phase6_jvm_heap_max_bytes {aggregated["heap_max"]}',
            f'romanlms_phase6_process_cpu_ratio {aggregated["cpu"]}',
            f'romanlms_phase6_database_connections {aggregated["db_connections"]}',
            "# TYPE romanlms_phase6_http_requests_observed_total counter",
            f'romanlms_phase6_http_requests_observed_total {aggregated["http_count"]}',
            "# TYPE romanlms_phase6_http_duration_seconds_total counter",
            f'romanlms_phase6_http_duration_seconds_total {aggregated["http_duration"]}',
            "# TYPE romanlms_phase6_http_requests_total counter",
        ])
        for key in ("1xx", "2xx", "3xx", "4xx", "5xx", "other"):
            lines.append(f'romanlms_phase6_http_requests_total{{status_class="{key}"}} {aggregated["http_" + key]}')
    for component, (memory, cpu) in docker_stats(containers).items():
        lines.append(f'romanlms_phase6_container_memory_bytes{{app_component="{component}"}} {memory}')
        lines.append(f'romanlms_phase6_container_cpu_percent{{app_component="{component}"}} {cpu}')
    OUTPUT.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".romanlms-phase6-", dir=OUTPUT.parent)
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
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Private RomanLMS metric collection failed.", file=sys.stderr)
        raise SystemExit(1)
