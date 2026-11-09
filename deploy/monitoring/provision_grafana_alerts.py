#!/usr/bin/env python3
"""Idempotently add deployment outcome rules to the existing Grafana stack."""

from __future__ import annotations

import base64
import copy
import json
import re
import sys
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


GRAFANA_URL = "http://127.0.0.1:3000"
GRAFANA_PASSWORD_FILE = Path("/etc/roman-platform/secrets/grafana_admin_password")
FOLDER_TITLE = "Roman Infrastructure Alerts"
CONTACT_POINT_NAME = "Roman Infrastructure Telegram"
DATASOURCE_UID = "prometheus"
NAMESPACE = "default"
APP_ID = "platform-smoke"
DASHBOARD_UID = "romanlms-phase6-private"
DASHBOARD_TAG = "roman-platform-managed"
PRODUCTION_FOLDER_TITLE = "RomanLMS Production"
PRODUCTION_FOLDER_UID = "romanlms-production"
PRODUCTION_DASHBOARD_UID = "romanlms-production-app"
PRODUCTION_DASHBOARD_TAG = "romanlms-production-managed"


class ProvisioningError(Exception):
    pass


def api_request(path: str, password: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    credentials = base64.b64encode(f"admin:{password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {credentials}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        GRAFANA_URL + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if method == "GET" and exc.code == 404:
            return None
        raise ProvisioningError(f"Grafana {method} {path} failed with HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProvisioningError(f"Grafana {method} {path} could not be reached ({type(exc).__name__}).") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ProvisioningError(f"Grafana {method} {path} returned invalid JSON.") from None


def _threshold_expression() -> dict[str, Any]:
    return {
        "queryType": "expression",
        "model": {
            "conditions": [{
                "evaluator": {"params": [0.5], "type": "gt"},
                "operator": {"type": "and"},
                "query": {"params": ["A"]},
                "reducer": {"params": [], "type": "last"},
                "type": "query",
            }],
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "expression": "A",
            "intervalMs": 1000,
            "maxDataPoints": 43200,
            "refId": "C",
            "type": "threshold",
        },
        "source": True,
    }


def _rule_resource(
    *,
    name: str,
    title: str,
    folder_uid: str,
    query: str,
    summary: str,
    description: str,
    for_duration: str = "0s",
    component: str = "deployment-controller",
    origin: str | None = None,
) -> dict[str, Any]:
    labels = {"component": component, "host": "roman-server", "severity": "warning"}
    if origin:
        labels["origin"] = origin
    return {
        "apiVersion": "rules.alerting.grafana.app/v1beta1",
        "kind": "AlertRule",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "annotations": {
                "grafana.app/folder": folder_uid,
            },
        },
        "spec": {
            "title": title,
            "trigger": {"interval": "30s"},
            "labels": labels,
            "annotations": {"summary": summary, "description": description},
            "for": for_duration,
            "noDataState": "Ok",
            "execErrState": "Alerting",
            "expressions": {
                "A": {
                    "relativeTimeRange": {"from": "5m0s", "to": "0s"},
                    "datasourceUID": DATASOURCE_UID,
                    "model": {
                        "editorMode": "code",
                        "expr": query,
                        "instant": True,
                        "intervalMs": 1000,
                        "legendFormat": "__auto",
                        "maxDataPoints": 43200,
                        "range": False,
                        "refId": "A",
                    },
                },
                "C": _threshold_expression(),
            },
        },
    }


def desired_rules(folder_uid: str, include_phase6: bool = False, include_production: bool = False) -> list[dict[str, Any]]:
    rules = [
        _rule_resource(
            name="roman-platform-deploy-failed",
            title="Roman Platform Deployment Failed",
            folder_uid=folder_uid,
            query='roman_platform_last_deployment_info{app_id="platform-smoke",outcome="failure"}',
            summary="Roman platform {{ $labels.operation }} failed",
            description=(
                "The smoke release request {{ $labels.workflow_run_id }} for commit "
                "{{ $labels.request_sha }} did not complete. The active route was retained. "
                "Review /srv/apps/roman-platform/logs/deployments.jsonl on roman-server."
            ),
        ),
        _rule_resource(
            name="roman-platform-deploy-succeeded",
            title="Roman Platform Deployment Succeeded",
            folder_uid=folder_uid,
            query=(
                '(time() - roman_platform_last_deployment_timestamp_seconds'
                '{app_id="platform-smoke",outcome="success"}) < bool 120'
            ),
            summary="Roman platform {{ $labels.operation }} succeeded",
            description=(
                "Smoke operation run {{ $labels.workflow_run_id }} for commit "
                "{{ $labels.request_sha }} passed its health gate. The short success "
                "notification resolves automatically after two minutes."
            ),
        ),
        _rule_resource(
            name="romanlms-phase6-deploy-failed",
            title="RomanLMS Private Deployment Failed",
            folder_uid=folder_uid,
            query='roman_platform_last_deployment_info{app_id="romanlms-phase6",outcome="failure"}',
            summary="RomanLMS private {{ $labels.operation }} failed",
            description=(
                "Private release run {{ $labels.workflow_run_id }} for commit "
                "{{ $labels.request_sha }} failed. Check the Phase 6 deployment audit and retained route."
            ),
        ),
        _rule_resource(
            name="romanlms-phase6-deploy-succeeded",
            title="RomanLMS Private Deployment Succeeded",
            folder_uid=folder_uid,
            query=(
                '(time() - roman_platform_last_deployment_timestamp_seconds'
                '{app_id="romanlms-phase6",outcome="success"}) < bool 120'
            ),
            summary="RomanLMS private {{ $labels.operation }} succeeded",
            description=(
                "Private release run {{ $labels.workflow_run_id }} for commit "
                "{{ $labels.request_sha }} passed its checks. This notification resolves after two minutes."
            ),
        ),
        _rule_resource(
            name="romanlms-phase6-component-down",
            title="RomanLMS Private Component Down",
            folder_uid=folder_uid,
            query='1 - romanlms_phase6_component_up',
            summary="RomanLMS private {{ $labels.app_component }} is unavailable",
            description="The private component health check failed. Inspect the active release and private route on roman-server.",
        ),
        _rule_resource(
            name="romanlms-phase6-dependency-down",
            title="RomanLMS Private Dependency Down",
            folder_uid=folder_uid,
            query='1 - romanlms_phase6_dependency_up',
            summary="RomanLMS private {{ $labels.dependency }} is unavailable",
            description="The isolated staging dependency health check failed. Inspect its container and storage mounts.",
        ),
        _rule_resource(
            name="romanlms-production-deploy-failed",
            title="RomanLMS Production Deployment Failed",
            folder_uid=folder_uid,
            query='roman_platform_last_deployment_info{app_id="romanlms-production",outcome="failure"}',
            summary="RomanLMS production {{ $labels.operation }} failed",
            description="Production release run {{ $labels.workflow_run_id }} for commit {{ $labels.request_sha }} failed. The previous active route was retained.",
            component="romanlms-production",
        ),
        _rule_resource(
            name="romanlms-production-deploy-succeeded",
            title="RomanLMS Production Deployment Succeeded",
            folder_uid=folder_uid,
            query='(time() - roman_platform_last_deployment_timestamp_seconds{app_id="romanlms-production",outcome="success"}) < bool 120',
            summary="RomanLMS production {{ $labels.operation }} succeeded",
            description="Production release run {{ $labels.workflow_run_id }} passed its release and readiness checks. This notification resolves after two minutes.",
            component="romanlms-production",
        ),
        _rule_resource(
            name="romanlms-production-component-down",
            title="RomanLMS Production Component Down",
            folder_uid=folder_uid,
            query="1 - romanlms_production_component_up",
            summary="RomanLMS production {{ $labels.app_component }} is unavailable",
            description="The production component health check failed. Inspect the active RomanLMS release and production gateway on roman-server.",
            component="romanlms-production",
            for_duration="2m",
        ),
        _rule_resource(
            name="romanlms-production-metrics-missing",
            title="RomanLMS Production Metrics Missing",
            folder_uid=folder_uid,
            query="1 - romanlms_production_metrics_up",
            summary="RomanLMS production application metrics are unavailable",
            description="The private production collector cannot read the active backend Prometheus endpoint.",
            component="romanlms-production",
            for_duration="2m",
        ),
        _rule_resource(
            name="romanlms-production-hikaricp-pressure",
            title="RomanLMS Hikari Pool Pressure",
            folder_uid=folder_uid,
            query=(
                "(romanlms_production_hikaricp_connections_pending > 0) or "
                "(romanlms_production_hikaricp_connections_active / "
                "clamp_min(romanlms_production_hikaricp_connections_max, 1) > 0.9)"
            ),
            summary="RomanLMS Hikari pool is near exhaustion",
            description="Connection requests are pending or more than 90% of the configured pool is active.",
            component="romanlms-production",
            for_duration="5m",
        ),
        _rule_resource(
            name="romanlms-production-http-5xx-rate",
            title="RomanLMS Application HTTP 5xx Rate High",
            folder_uid=folder_uid,
            query=(
                '(sum(rate(romanlms_production_http_responses_total{origin="application",status=~"5.."}[5m])) '
                '/ clamp_min(sum(rate(romanlms_production_http_responses_total{origin="application"}[5m])), 0.001)) > bool 0.05'
            ),
            summary="RomanLMS application 5xx rate exceeds 5%",
            description="Application-origin HTTP 5xx responses exceeded 5% of requests for ten minutes. Proxy and Cloudflare errors are not included in this application metric.",
            component="romanlms-production",
            origin="application",
            for_duration="10m",
        ),
        _rule_resource(
            name="romanlms-production-http-p95-latency",
            title="RomanLMS Application HTTP p95 Latency High",
            folder_uid=folder_uid,
            query=(
                "histogram_quantile(0.95, sum by (le) "
                "(rate(romanlms_production_http_server_requests_seconds_bucket[5m]))) > bool 1"
            ),
            summary="RomanLMS application HTTP p95 exceeds one second",
            description="Backend application response latency p95 stayed above one second for ten minutes.",
            component="romanlms-production",
            origin="application",
            for_duration="10m",
        ),
        _rule_resource(
            name="romanlms-production-slow-endpoints",
            title="RomanLMS Slow Endpoint Groups",
            folder_uid=folder_uid,
            query=(
                "sum by (endpoint_group, method) "
                "(rate(romanlms_production_http_requests_over_one_second_total[5m])) > bool 0.2"
            ),
            summary="RomanLMS {{ $labels.method }} {{ $labels.endpoint_group }} requests are slow",
            description="More than 0.2 requests per second in this bounded endpoint group exceeded one second for ten minutes.",
            component="romanlms-production",
            origin="application",
            for_duration="10m",
        ),
        _rule_resource(
            name="romanlms-production-jvm-heap-pressure",
            title="RomanLMS JVM Heap Pressure",
            folder_uid=folder_uid,
            query=(
                "(romanlms_production_jvm_memory_used_bytes{area=\"heap\"} / "
                "clamp_min(romanlms_production_jvm_heap_max_bytes, 1)) > bool 0.9"
            ),
            summary="RomanLMS JVM heap use is above 90%",
            description="Heap use stayed above 90% of the configured JVM heap for ten minutes.",
            component="romanlms-production",
            for_duration="10m",
        ),
        _rule_resource(
            name="romanlms-production-container-pid-pressure",
            title="RomanLMS Container PID Pressure",
            folder_uid=folder_uid,
            query="romanlms_production_container_pids_limit_ratio > bool 0.8",
            summary="RomanLMS {{ $labels.app_component }} is near its PID limit",
            description="The container is using more than 80% of its configured process limit for five minutes.",
            component="romanlms-production",
            for_duration="5m",
        ),
        _rule_resource(
            name="romanlms-production-container-restarts",
            title="RomanLMS Container Restarted",
            folder_uid=folder_uid,
            query="sum by (app_component) (increase(romanlms_production_container_restart_count[10m])) > bool 0",
            summary="RomanLMS {{ $labels.app_component }} container restarted",
            description="An active production application container restarted in the last ten minutes.",
            component="romanlms-production",
            for_duration="0s",
        ),
    ]
    selected = rules[:2]
    if include_phase6:
        selected.extend(rules[2:6])
    if include_production:
        selected.extend(rules[6:])
    return selected


def _verify_existing_telegram_route(password: str) -> str:
    contacts = api_request("/api/v1/provisioning/contact-points", password)
    if not isinstance(contacts, list) or not any(
        item.get("name") == CONTACT_POINT_NAME and item.get("type") == "telegram"
        for item in contacts
    ):
        raise ProvisioningError("The existing Telegram contact point was not found; no alert resources were changed.")

    policies = api_request("/api/v1/provisioning/policies", password)
    root = policies.get("policies", policies) if isinstance(policies, dict) else {}
    routes = root.get("routes", []) if isinstance(root, dict) else []
    if not any(
        route.get("receiver") == CONTACT_POINT_NAME
        and ["severity", "=", "warning"] in (route.get("object_matchers") or [])
        for route in routes
    ):
        raise ProvisioningError("The existing warning-to-Telegram route was not found; no alert resources were changed.")

    datasources = api_request("/api/datasources", password)
    if not isinstance(datasources, list) or not any(
        item.get("uid") == DATASOURCE_UID and item.get("type") == "prometheus"
        for item in datasources
    ):
        raise ProvisioningError("The existing Prometheus data source was not found; no alert resources were changed.")

    folders = api_request("/api/folders", password)
    folder = next((item for item in folders if item.get("title") == FOLDER_TITLE), None)
    if not folder or not folder.get("uid"):
        raise ProvisioningError("The existing Roman Infrastructure Alerts folder was not found.")
    return folder["uid"]


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_DURATION_MILLISECONDS = {
    "ms": Decimal(1),
    "s": Decimal(1_000),
    "m": Decimal(60_000),
    "h": Decimal(3_600_000),
    "d": Decimal(86_400_000),
    "w": Decimal(604_800_000),
    "y": Decimal(31_536_000_000),
}


def _duration_milliseconds(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    if not value:
        return Decimal(0)
    parts = list(_DURATION_PART.finditer(value))
    if not parts or "".join(part.group(0) for part in parts) != value:
        return None
    try:
        return sum(
            (Decimal(part.group(1)) * _DURATION_MILLISECONDS[part.group(2)] for part in parts),
            Decimal(0),
        )
    except InvalidOperation:
        return None


def _same_duration(actual: Any, expected: Any) -> bool:
    actual_text = actual or "0s"
    expected_text = expected or "0s"
    actual_milliseconds = _duration_milliseconds(actual_text)
    expected_milliseconds = _duration_milliseconds(expected_text)
    if actual_milliseconds is not None and expected_milliseconds is not None:
        return actual_milliseconds == expected_milliseconds
    return actual_text == expected_text


def _matches(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    metadata = current.get("metadata", {})
    spec = current.get("spec", {})
    wanted = desired["spec"]
    return (
        metadata.get("annotations", {}).get("grafana.app/folder")
        == desired["metadata"]["annotations"]["grafana.app/folder"]
        and spec.get("title") == wanted["title"]
        and spec.get("trigger") == wanted["trigger"]
        and spec.get("labels") == wanted["labels"]
        and spec.get("annotations") == wanted["annotations"]
        and _same_duration(spec.get("for"), wanted.get("for"))
        and spec.get("noDataState") == wanted["noDataState"]
        and spec.get("execErrState") == wanted["execErrState"]
        and spec.get("expressions", {}).get("A", {}).get("model", {}).get("expr")
        == wanted["expressions"]["A"]["model"]["expr"]
        and spec.get("expressions", {}).get("C", {}).get("model", {}).get("conditions")
        == wanted["expressions"]["C"]["model"]["conditions"]
    )


def _upsert_rule(password: str, desired: dict[str, Any]) -> bool:
    name = desired["metadata"]["name"]
    item_path = f"/apis/rules.alerting.grafana.app/v1beta1/namespaces/{NAMESPACE}/alertrules/{name}"
    collection_path = f"/apis/rules.alerting.grafana.app/v1beta1/namespaces/{NAMESPACE}/alertrules"
    current = api_request(item_path, password)
    if current is None:
        api_request(collection_path, password, method="POST", body=desired)
        current = api_request(item_path, password)
    else:
        labels = current.get("spec", {}).get("labels", {})
        if labels.get("component") not in {"deployment-controller", "romanlms-production"}:
            raise ProvisioningError(f"Grafana rule UID {name} already belongs to another component; it was not changed.")
        if _matches(current, desired):
            return False
        replacement = copy.deepcopy(desired)
        replacement["metadata"]["uid"] = current.get("metadata", {}).get("uid")
        replacement["metadata"]["resourceVersion"] = current.get("metadata", {}).get("resourceVersion")
        api_request(item_path, password, method="PUT", body=replacement)
        current = api_request(item_path, password)
    if not isinstance(current, dict) or not _matches(current, desired):
        raise ProvisioningError(f"Grafana did not confirm the requested alert rule {name}.")
    return True


def desired_dashboard() -> dict[str, Any]:
    panels = [
        ("Private components", "romanlms_phase6_component_up", "short"),
        ("Private dependencies", "romanlms_phase6_dependency_up", "short"),
        ("HTTP requests per second", "sum by (status_class) (rate(romanlms_phase6_http_requests_total[5m]))", "reqps"),
        ("Mean HTTP response time", "sum(rate(romanlms_phase6_http_duration_seconds_total[5m])) / clamp_min(sum(rate(romanlms_phase6_http_requests_observed_total[5m])), 0.001)", "s"),
        ("JVM memory", "romanlms_phase6_jvm_memory_used_bytes", "bytes"),
        ("Container memory", "romanlms_phase6_container_memory_bytes", "bytes"),
        ("Container CPU", "romanlms_phase6_container_cpu_percent", "percent"),
        ("Database connections", "romanlms_phase6_database_connections", "short"),
        ("Active release", 'roman_platform_active_release_info{app_id="romanlms-phase6"}', "short"),
        ("Database backup age", "roman_pgbackrest_last_backup_age_seconds", "s"),
        ("Storage available", "roman_monitoring_filesystem_available_bytes", "bytes"),
    ]
    return {
        "uid": DASHBOARD_UID,
        "title": "RomanLMS Phase 6 Private",
        "tags": [DASHBOARD_TAG],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": [
            {"id": index + 1, "type": "timeseries", "title": title,
             "gridPos": {"h": 8, "w": 12, "x": (index % 2) * 12, "y": (index // 2) * 8},
             "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
             "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
             "targets": [{"refId": "A", "expr": expression, "datasource": {"type": "prometheus", "uid": DATASOURCE_UID}}]}
            for index, (title, expression, unit) in enumerate(panels)
        ],
    }


def desired_production_dashboard() -> dict[str, Any]:
    panels = [
        ("Backend and frontend readiness", "romanlms_production_component_up", "short"),
        ("Metrics collector status", "romanlms_production_metrics_up", "short"),
        ("Hikari active, idle and pending", "romanlms_production_hikaricp_connections_active or romanlms_production_hikaricp_connections_idle or romanlms_production_hikaricp_connections_pending", "short"),
        ("Hikari maximum and minimum idle", "romanlms_production_hikaricp_connections_max or romanlms_production_hikaricp_connections_min", "short"),
        ("Hikari acquisition p95", "histogram_quantile(0.95, sum by (le) (rate(romanlms_production_hikaricp_connection_acquire_seconds_bucket[5m])))", "s"),
        ("Hikari usage and creation p95", "histogram_quantile(0.95, sum by (le) (rate(romanlms_production_hikaricp_connection_usage_seconds_bucket[5m]))) or histogram_quantile(0.95, sum by (le) (rate(romanlms_production_hikaricp_connection_creation_seconds_bucket[5m])))", "s"),
        ("Hikari connection timeouts", "rate(romanlms_production_hikaricp_connection_timeouts_total[5m])", "ops"),
        ("Application HTTP requests per second by status", "sum by (status) (rate(romanlms_production_http_responses_total{origin=\"application\"}[5m]))", "reqps"),
        ("Application 500, 502, 503 and 504 responses", "sum by (status) (rate(romanlms_production_http_responses_total{origin=\"application\",status=~\"500|502|503|504\"}[5m]))", "reqps"),
        ("Application HTTP 5xx ratio", "sum(rate(romanlms_production_http_responses_total{origin=\"application\",status=~\"5..\"}[5m])) / clamp_min(sum(rate(romanlms_production_http_responses_total{origin=\"application\"}[5m])), 0.001)", "percentunit"),
        ("Application HTTP p95 latency", "histogram_quantile(0.95, sum by (le) (rate(romanlms_production_http_server_requests_seconds_bucket[5m])))", "s"),
        ("Endpoint group HTTP p95 latency", "histogram_quantile(0.95, sum by (le, endpoint_group, method) (rate(romanlms_production_http_requests_by_endpoint_seconds_bucket[5m])))", "s"),
        ("Slow endpoint group requests per second", "sum by (endpoint_group, method) (rate(romanlms_production_http_requests_over_one_second_total[5m]))", "reqps"),
        ("JVM heap and non-heap memory", "romanlms_production_jvm_memory_used_bytes", "bytes"),
        ("JVM heap use percent", "100 * romanlms_production_jvm_memory_used_bytes{area=\"heap\"} / clamp_min(romanlms_production_jvm_heap_max_bytes, 1)", "percent"),
        ("JVM threads", "romanlms_production_jvm_threads_live or romanlms_production_jvm_threads_daemon or romanlms_production_jvm_threads_peak", "short"),
        ("JVM garbage collection pause rate", "rate(romanlms_production_jvm_gc_pause_seconds_total[5m])", "s"),
        ("Container CPU percent", "romanlms_production_container_cpu_percent", "percent"),
        ("Container memory use and limit", "romanlms_production_container_memory_bytes or romanlms_production_container_memory_limit_bytes", "bytes"),
        ("Container memory use percent", "100 * romanlms_production_container_memory_bytes / clamp_min(romanlms_production_container_memory_limit_bytes, 1)", "percent"),
        ("Container restarts", "increase(romanlms_production_container_restart_count[1h])", "short"),
        ("Container PID limit pressure", "romanlms_production_container_pids_limit_ratio", "percentunit"),
    ]
    return {
        "uid": PRODUCTION_DASHBOARD_UID,
        "title": "RomanLMS Production",
        "tags": [DASHBOARD_TAG, PRODUCTION_DASHBOARD_TAG],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": [
            {"id": index + 1, "type": "timeseries", "title": title,
             "gridPos": {"h": 8, "w": 12, "x": (index % 2) * 12, "y": (index // 2) * 8},
             "datasource": {"type": "prometheus", "uid": DATASOURCE_UID},
             "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
             "targets": [{"refId": "A", "expr": expression, "datasource": {"type": "prometheus", "uid": DATASOURCE_UID}}]}
            for index, (title, expression, unit) in enumerate(panels)
        ],
    }


def _upsert_dashboard_resource(
    password: str,
    folder_uid: str,
    desired: dict[str, Any],
    owner_tag: str,
    message: str,
) -> bool:
    dashboard_uid = desired["uid"]
    current = api_request(f"/api/dashboards/uid/{dashboard_uid}", password)
    if current is not None:
        dashboard = current.get("dashboard", {}) if isinstance(current, dict) else {}
        if owner_tag not in dashboard.get("tags", []):
            raise ProvisioningError(f"Dashboard UID {dashboard_uid} belongs to another dashboard; it was not changed.")
        keys = ("uid", "title", "tags", "timezone", "schemaVersion", "refresh", "time", "panels")
        if all(dashboard.get(key) == desired.get(key) for key in keys) and current.get("meta", {}).get("folderUid") == folder_uid:
            return False
        desired["id"] = dashboard.get("id")
        desired["version"] = dashboard.get("version", 0)
    result = api_request("/api/dashboards/db", password, method="POST", body={"dashboard": desired, "folderUid": folder_uid, "overwrite": True, "message": message})
    if not isinstance(result, dict) or result.get("uid") != dashboard_uid:
        raise ProvisioningError(f"Grafana did not confirm dashboard {dashboard_uid}.")
    return True


def _upsert_dashboard(password: str, folder_uid: str) -> bool:
    return _upsert_dashboard_resource(password, folder_uid, desired_dashboard(), DASHBOARD_TAG, "Managed RomanLMS private monitoring")


def _ensure_production_dashboard_folder(password: str) -> str:
    folders = api_request("/api/folders", password)
    if not isinstance(folders, list):
        raise ProvisioningError("Grafana folders could not be listed.")
    by_title = next((item for item in folders if item.get("title") == PRODUCTION_FOLDER_TITLE), None)
    by_uid = next((item for item in folders if item.get("uid") == PRODUCTION_FOLDER_UID), None)
    if by_title and by_title.get("uid") != PRODUCTION_FOLDER_UID:
        raise ProvisioningError("The RomanLMS Production folder title belongs to a different folder UID.")
    if by_uid and by_uid.get("title") != PRODUCTION_FOLDER_TITLE:
        raise ProvisioningError("The RomanLMS Production folder UID belongs to a different folder.")
    folder = by_title or by_uid
    if folder:
        return str(folder["uid"])
    created = api_request("/api/folders", password, method="POST", body={"uid": PRODUCTION_FOLDER_UID, "title": PRODUCTION_FOLDER_TITLE})
    if not isinstance(created, dict) or created.get("uid") != PRODUCTION_FOLDER_UID or created.get("title") != PRODUCTION_FOLDER_TITLE:
        raise ProvisioningError("Grafana did not confirm the RomanLMS Production folder.")
    return PRODUCTION_FOLDER_UID


def _upsert_production_dashboard(password: str, folder_uid: str) -> bool:
    return _upsert_dashboard_resource(
        password, folder_uid, desired_production_dashboard(), PRODUCTION_DASHBOARD_TAG,
        "Managed RomanLMS production application monitoring",
    )


def main() -> int:
    if any(argument not in {"--include-phase6", "--include-production"} for argument in sys.argv[1:]) or len(set(sys.argv[1:])) != len(sys.argv[1:]):
        print("Unsupported alert provisioning option.", file=sys.stderr)
        return 1
    include_phase6 = "--include-phase6" in sys.argv[1:]
    include_production = "--include-production" in sys.argv[1:]
    try:
        password = GRAFANA_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        print("Grafana administrator credential file is unavailable.", file=sys.stderr)
        return 1
    if not password:
        print("Grafana administrator credential file is empty.", file=sys.stderr)
        return 1

    try:
        health = api_request("/api/health", password)
        if not isinstance(health, dict) or health.get("database") != "ok":
            raise ProvisioningError("Grafana health check did not pass.")
        folder_uid = _verify_existing_telegram_route(password)
        changed = sum(_upsert_rule(password, item) for item in desired_rules(folder_uid, include_phase6, include_production))
        if include_phase6:
            changed += int(_upsert_dashboard(password, folder_uid))
        if include_production:
            production_folder_uid = _ensure_production_dashboard_folder(password)
            changed += int(_upsert_production_dashboard(password, production_folder_uid))
    except ProvisioningError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Grafana alert provisioning failed ({type(exc).__name__}).", file=sys.stderr)
        return 1

    print(f"Grafana deployment alert rules verified; changed={str(bool(changed)).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
