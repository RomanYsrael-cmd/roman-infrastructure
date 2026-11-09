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
SAFE_ENDPOINT_GROUPS = {
    "admin", "assignments", "attendance", "auth", "courses", "enrollments", "files",
    "grades", "instructor", "messages", "mobile", "modules", "notifications", "offerings",
    "platform", "quizzes", "students", "tenant", "users",
}


def _label(labels: str, name: str) -> str | None:
    match = re.search(r'(?:^|,)' + re.escape(name) + r'="((?:[^"\\]|\\.)*)"(?:,|$)', labels)
    return match.group(1) if match else None


def _endpoint_group(uri: str | None) -> str:
    if not uri:
        return "other"
    path = uri.split("?", 1)[0].strip("/").split("/")
    if path and path[0] == "lms":
        path = path[1:]
    if path and path[0] == "api":
        path = path[1:]
        segment = path[0].lower() if path else "other"
        return segment if segment in SAFE_ENDPOINT_GROUPS else "api_other"
    if path and path[0] == "actuator":
        return "health" if len(path) > 1 and path[1] == "health" else "actuator"
    return "root" if not path or path == [""] else "other"


def _method_group(labels: str) -> str:
    method = _label(labels, "method")
    allowed = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
    return method if method in allowed else "OTHER"


def _bucket(labels: str) -> str | None:
    raw = _label(labels, "le")
    if raw in {"+Inf", "Inf", "inf"}:
        return "+Inf"
    try:
        value = float(raw) if raw is not None else math.nan
    except ValueError:
        return None
    return format(value, ".6g") if math.isfinite(value) and value >= 0 else None


def probe(port: int, path: str, expected: bytes, limit: int = 8192) -> tuple[int, float]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            body = response.read(limit)
            return int(response.status == 200 and expected in body), time.monotonic() - started
    except (OSError, urllib.error.URLError, TimeoutError):
        return 0, time.monotonic() - started


def aggregate_metrics(text: str) -> dict[str, Any]:
    result = {f"http_{code}xx": 0.0 for code in range(1, 6)}
    hikari_seen: set[str] = set()
    result.update({
        "http_other": 0.0, "http_count": 0.0, "http_duration": 0.0, "http_codes": {},
        "http_status": {}, "http_endpoint_counts": {}, "http_endpoint_duration": {},
        "http_buckets": {}, "http_endpoint_buckets": {},
        "heap_used": 0.0, "nonheap_used": 0.0, "heap_max": 0.0, "cpu": 0.0,
        "db_connections": 0.0, "hikari_active": 0.0, "hikari_idle": 0.0,
        "hikari_pending": 0.0, "hikari_max": 0.0, "hikari_min": 0.0,
        "hikari_timeouts": 0.0, "hikari_available": 0.0,
        "hikari_timers": {name: {"count": 0.0, "sum": 0.0, "max": 0.0, "buckets": {}} for name in ("acquire", "usage", "creation")},
        "jvm_threads_live": 0.0, "jvm_threads_daemon": 0.0, "jvm_threads_peak": 0.0,
        "jvm_gc_count": 0.0, "jvm_gc_duration": 0.0,
    })
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
            status = _label(labels, "status")
            status_class = status[0] + "xx" if status and re.fullmatch(r"[1-5][0-9]{2}", status) else "other"
            result["http_" + status_class] += value
            result["http_count"] += value
            result["http_status"][status_class] = result["http_status"].get(status_class, 0.0) + value
            if status and re.fullmatch(r"[1-5][0-9]{2}", status):
                result["http_codes"][status] = result["http_codes"].get(status, 0.0) + value
            endpoint = _endpoint_group(_label(labels, "uri"))
            method = _method_group(labels)
            key = (endpoint, method)
            result["http_endpoint_counts"][key] = result["http_endpoint_counts"].get(key, 0.0) + value
        elif name == "http_server_requests_seconds_sum":
            result["http_duration"] += value
            endpoint = _endpoint_group(_label(labels, "uri"))
            method = _method_group(labels)
            key = (endpoint, method)
            result["http_endpoint_duration"][key] = result["http_endpoint_duration"].get(key, 0.0) + value
        elif name == "http_server_requests_seconds_bucket":
            bound = _bucket(labels)
            if bound is not None:
                result["http_buckets"][bound] = result["http_buckets"].get(bound, 0.0) + value
                key = (_endpoint_group(_label(labels, "uri")), _method_group(labels), bound)
                result["http_endpoint_buckets"][key] = result["http_endpoint_buckets"].get(key, 0.0) + value
        elif name == "jvm_memory_used_bytes":
            area = _label(labels, "area")
            if area in {"heap", "nonheap"}:
                result[area + "_used"] += value
        elif name == "jvm_memory_max_bytes" and _label(labels, "area") == "heap":
            result["heap_max"] += value
        elif name == "process_cpu_usage":
            result["cpu"] = value
        elif name == "hikaricp_connections_active":
            result["db_connections"] += value
            result["hikari_active"] += value
            hikari_seen.add("active")
        elif name == "hikaricp_connections_idle":
            result["hikari_idle"] += value
            hikari_seen.add("idle")
        elif name == "hikaricp_connections_pending":
            result["hikari_pending"] += value
            hikari_seen.add("pending")
        elif name == "hikaricp_connections_max":
            result["hikari_max"] += value
            hikari_seen.add("max")
        elif name == "hikaricp_connections_min":
            result["hikari_min"] += value
            hikari_seen.add("min")
        elif name == "hikaricp_connections_timeout_total":
            result["hikari_timeouts"] += value
        elif name.startswith("hikaricp_connections_"):
            timer_match = re.fullmatch(r"hikaricp_connections_(acquire|usage|creation)_seconds_(count|sum|max|bucket)", name)
            if timer_match:
                timer_name, statistic = timer_match.groups()
                timer = result["hikari_timers"][timer_name]
                if statistic == "bucket":
                    bound = _bucket(labels)
                    if bound is not None:
                        timer["buckets"][bound] = timer["buckets"].get(bound, 0.0) + value
                elif statistic == "max":
                    timer[statistic] = max(timer[statistic], value)
                else:
                    timer[statistic] += value
        elif name == "jvm_threads_live_threads":
            result["jvm_threads_live"] = max(result["jvm_threads_live"], value)
        elif name == "jvm_threads_daemon_threads":
            result["jvm_threads_daemon"] = max(result["jvm_threads_daemon"], value)
        elif name == "jvm_threads_peak_threads":
            result["jvm_threads_peak"] = max(result["jvm_threads_peak"], value)
        elif name == "jvm_gc_pause_seconds_count":
            result["jvm_gc_count"] += value
        elif name == "jvm_gc_pause_seconds_sum":
            result["jvm_gc_duration"] += value
    result["hikari_available"] = float({"active", "idle", "pending", "max", "min"}.issubset(hikari_seen))
    return result


def docker_health(name: str) -> int:
    match = re.fullmatch(r"roman-platform-romanlms-production-(backend|frontend)-(?:blue|green)-[0-9a-f]{12}", name)
    if not match:
        return 0
    expected_app = f"romanlms-production-{match.group(1)}"
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{index .Config.Labels \"roman.platform.app\"}}", name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return int(result.returncode == 0 and result.stdout.strip() == f"host|healthy|{expected_app}")
    except (OSError, subprocess.TimeoutExpired):
        return 0


def docker_stats(containers: dict[str, str]) -> dict[str, dict[str, float]]:
    try:
        process = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers.values()], capture_output=True, text=True, timeout=20, check=False)
        if process.returncode:
            return {}
        reverse = {value: key for key, value in containers.items()}
        units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
        values: dict[str, dict[str, float]] = {}
        for line in process.stdout.splitlines():
            item = json.loads(line)
            component = reverse.get(str(item.get("Name", "")).lstrip("/"))
            memory = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", str(item.get("MemUsage", "")).split("/")[0])
            if component and memory and memory.group(2) in units:
                pids = float(str(item.get("PIDs", "0")).strip())
                values[component] = {
                    "memory": float(memory.group(1)) * units[memory.group(2)],
                    "cpu": float(str(item.get("CPUPerc", "0%")).rstrip("%")),
                    "pids": pids if math.isfinite(pids) and pids >= 0 else 0.0,
                }
        return values
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}


def docker_resource_limits(containers: dict[str, str]) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = {}
    for component, name in containers.items():
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.HostConfig.Memory}}|{{.HostConfig.PidsLimit}}|{{.RestartCount}}", name],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode:
                continue
            memory, pids, restarts = (int(part) for part in result.stdout.strip().split("|"))
            values[component] = {
                "memory_limit": float(max(0, memory)),
                "pids_limit": float(max(0, pids)),
                "restart_count": float(max(0, restarts)),
            }
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            continue
    return values


def application_metric_lines(aggregated: dict[str, Any]) -> list[str]:
    lines = [
        "# TYPE romanlms_production_hikaricp_metrics_available gauge",
        f"romanlms_production_hikaricp_metrics_available {aggregated['hikari_available']}",
        "# TYPE romanlms_production_hikaricp_connections_active gauge",
        f"romanlms_production_hikaricp_connections_active {aggregated['hikari_active']}",
        "# TYPE romanlms_production_hikaricp_connections_idle gauge",
        f"romanlms_production_hikaricp_connections_idle {aggregated['hikari_idle']}",
        "# TYPE romanlms_production_hikaricp_connections_pending gauge",
        f"romanlms_production_hikaricp_connections_pending {aggregated['hikari_pending']}",
        "# TYPE romanlms_production_hikaricp_connections_max gauge",
        f"romanlms_production_hikaricp_connections_max {aggregated['hikari_max']}",
        "# TYPE romanlms_production_hikaricp_connections_min gauge",
        f"romanlms_production_hikaricp_connections_min {aggregated['hikari_min']}",
        "# TYPE romanlms_production_hikaricp_connection_timeouts_total counter",
        f"romanlms_production_hikaricp_connection_timeouts_total {aggregated['hikari_timeouts']}",
        "# TYPE romanlms_production_database_connections gauge",
        f"romanlms_production_database_connections {aggregated['db_connections']}",
        "# TYPE romanlms_production_http_requests_observed_total counter",
        f"romanlms_production_http_requests_observed_total {aggregated['http_count']}",
        "# TYPE romanlms_production_http_duration_seconds_total counter",
        f"romanlms_production_http_duration_seconds_total {aggregated['http_duration']}",
        "# TYPE romanlms_production_http_requests_total counter",
    ]
    for key in ("1xx", "2xx", "3xx", "4xx", "5xx", "other"):
        lines.append(f'romanlms_production_http_requests_total{{status_class="{key}"}} {aggregated["http_" + key]}')
    lines.append("# TYPE romanlms_production_http_responses_total counter")
    for status, count in sorted(aggregated["http_codes"].items()):
        lines.append(f'romanlms_production_http_responses_total{{origin="application",status="{status}"}} {count}')
    lines.append("# TYPE romanlms_production_http_server_requests_seconds histogram")
    for bound, value in sorted(aggregated["http_buckets"].items(), key=lambda item: float("inf") if item[0] == "+Inf" else float(item[0])):
        lines.append(f'romanlms_production_http_server_requests_seconds_bucket{{le="{bound}"}} {value}')
    lines.extend([
        f'romanlms_production_http_server_requests_seconds_count {aggregated["http_count"]}',
        f'romanlms_production_http_server_requests_seconds_sum {aggregated["http_duration"]}',
        "# TYPE romanlms_production_http_requests_by_endpoint_total counter",
    ])
    for (endpoint, method), count in sorted(aggregated["http_endpoint_counts"].items()):
        lines.append(f'romanlms_production_http_requests_by_endpoint_total{{endpoint_group="{endpoint}",method="{method}"}} {count}')
    lines.append("# TYPE romanlms_production_http_requests_by_endpoint_seconds histogram")
    for (endpoint, method, bound), value in sorted(
        aggregated["http_endpoint_buckets"].items(),
        key=lambda item: (item[0][0], item[0][1], float("inf") if item[0][2] == "+Inf" else float(item[0][2])),
    ):
        lines.append(f'romanlms_production_http_requests_by_endpoint_seconds_bucket{{endpoint_group="{endpoint}",method="{method}",le="{bound}"}} {value}')
    for (endpoint, method), count in sorted(aggregated["http_endpoint_counts"].items()):
        lines.append(f'romanlms_production_http_requests_by_endpoint_seconds_count{{endpoint_group="{endpoint}",method="{method}"}} {count}')
        duration = aggregated["http_endpoint_duration"].get((endpoint, method), 0.0)
        lines.append(f'romanlms_production_http_requests_by_endpoint_seconds_sum{{endpoint_group="{endpoint}",method="{method}"}} {duration}')
    lines.append("# TYPE romanlms_production_http_requests_over_one_second_total counter")
    for (endpoint, method), count in sorted(aggregated["http_endpoint_counts"].items()):
        one_second = aggregated["http_endpoint_buckets"].get((endpoint, method, "1"))
        if one_second is not None:
            lines.append(f'romanlms_production_http_requests_over_one_second_total{{endpoint_group="{endpoint}",method="{method}"}} {max(0.0, count - one_second)}')
    for name, timer in aggregated["hikari_timers"].items():
        prefix = f"romanlms_production_hikaricp_connection_{name}"
        lines.extend([
            f"# TYPE {prefix}_seconds histogram",
            f"{prefix}_seconds_count {timer['count']}",
            f"{prefix}_seconds_sum {timer['sum']}",
            f"# TYPE {prefix}_max_seconds gauge",
            f"{prefix}_max_seconds {timer['max']}",
        ])
        for bound, value in sorted(timer["buckets"].items(), key=lambda item: float("inf") if item[0] == "+Inf" else float(item[0])):
            lines.append(f'{prefix}_seconds_bucket{{le="{bound}"}} {value}')
    lines.extend([
        "# TYPE romanlms_production_jvm_threads_live gauge",
        f"romanlms_production_jvm_threads_live {aggregated['jvm_threads_live']}",
        "# TYPE romanlms_production_jvm_threads_daemon gauge",
        f"romanlms_production_jvm_threads_daemon {aggregated['jvm_threads_daemon']}",
        "# TYPE romanlms_production_jvm_threads_peak gauge",
        f"romanlms_production_jvm_threads_peak {aggregated['jvm_threads_peak']}",
        "# TYPE romanlms_production_jvm_gc_pause_events_total counter",
        f"romanlms_production_jvm_gc_pause_events_total {aggregated['jvm_gc_count']}",
        "# TYPE romanlms_production_jvm_gc_pause_seconds_total counter",
        f"romanlms_production_jvm_gc_pause_seconds_total {aggregated['jvm_gc_duration']}",
    ])
    return lines


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
        ])
        lines.extend(application_metric_lines(aggregated))
    stats = docker_stats(containers)
    limits = docker_resource_limits(containers)
    for component in containers:
        if component in stats:
            values = stats[component]
            lines.append(f'romanlms_production_container_memory_bytes{{app_component="{component}"}} {values["memory"]}')
            lines.append(f'romanlms_production_container_cpu_percent{{app_component="{component}"}} {values["cpu"]}')
            lines.append(f'romanlms_production_container_pids_used{{app_component="{component}"}} {values["pids"]}')
        if component in limits:
            values = limits[component]
            lines.append(f'romanlms_production_container_memory_limit_bytes{{app_component="{component}"}} {values["memory_limit"]}')
            lines.append(f'romanlms_production_container_pids_limit{{app_component="{component}"}} {values["pids_limit"]}')
            lines.append(f'romanlms_production_container_restart_count{{app_component="{component}"}} {values["restart_count"]}')
            ratio = 0.0
            if component in stats and values["pids_limit"] > 0:
                ratio = stats[component]["pids"] / values["pids_limit"]
            lines.append(f'romanlms_production_container_pids_limit_ratio{{app_component="{component}"}} {ratio}')
    write_metrics(lines)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("RomanLMS production metric collection failed.", file=sys.stderr)
        raise SystemExit(1)
