#!/usr/bin/env python3
"""Idempotently add deployment outcome rules to the existing Grafana stack."""

from __future__ import annotations

import base64
import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


GRAFANA_URL = "http://127.0.0.1:3000"
GRAFANA_PASSWORD_FILE = Path("/etc/roman-platform/secrets/grafana_admin_password")
FOLDER_TITLE = "Roman Infrastructure Alerts"
CONTACT_POINT_NAME = "Roman Infrastructure Telegram"
DATASOURCE_UID = "prometheus"
NAMESPACE = "default"
APP_ID = "platform-smoke"


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
) -> dict[str, Any]:
    return {
        "apiVersion": "rules.alerting.grafana.app/v1beta1",
        "kind": "AlertRule",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                "grafana.app/folder": folder_uid,
            },
        },
        "spec": {
            "title": title,
            "trigger": {"interval": "30s"},
            "labels": {
                "component": "deployment-controller",
                "host": "roman-server",
                "severity": "warning",
            },
            "annotations": {"summary": summary, "description": description},
            "for": "0s",
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


def desired_rules(folder_uid: str) -> list[dict[str, Any]]:
    return [
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
    ]


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


def _matches(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    metadata = current.get("metadata", {})
    spec = current.get("spec", {})
    wanted = desired["spec"]
    return (
        metadata.get("labels", {}).get("grafana.app/folder")
        == desired["metadata"]["labels"]["grafana.app/folder"]
        and spec.get("title") == wanted["title"]
        and spec.get("trigger") == wanted["trigger"]
        and spec.get("labels") == wanted["labels"]
        and spec.get("annotations") == wanted["annotations"]
        and spec.get("for") == wanted["for"]
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
        if labels.get("component") != "deployment-controller":
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


def main() -> int:
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
        changed = sum(_upsert_rule(password, item) for item in desired_rules(folder_uid))
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
