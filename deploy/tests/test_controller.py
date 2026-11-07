import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "controller"))
sys.path.insert(0, str(ROOT / "deploy" / "monitoring"))
sys.path.insert(0, str(ROOT / "app" / "smoke"))

import roman_deploy as deploy
import provision_grafana_alerts as grafana_alerts
import server as smoke_server


CONFIG = json.loads((ROOT / "platform" / "applications.json").read_text(encoding="utf-8"))
SHA = "a" * 40
WORKFLOW_SHA = "b" * 40
DIGEST = "c" * 64
APP = dict(CONFIG["applications"]["platform-smoke"], allowed_platform_shas=[WORKFLOW_SHA])
CONFIG["applications"]["platform-smoke"] = APP


def request(**overrides):
    value = {
        "operation": "deploy",
        "app_id": "platform-smoke",
        "repository": "RomanYsrael-cmd/roman-lms",
        "branch": "main",
        "commit_sha": SHA,
        "workflow_sha": WORKFLOW_SHA,
        "platform_sha": WORKFLOW_SHA,
        "environment": "production",
        "strategy": "blue-green",
        "image": f"{APP['image_namespace']}@sha256:{DIGEST}",
        "github_token": "ghs_" + "x" * 32,
        "registry_token": "ghs_" + "y" * 32,
        "release": {"run_id": "12345", "run_attempt": "1", "actor": "RomanYsrael-cmd"},
    }
    value.update(overrides)
    return value


class RequestValidationTests(unittest.TestCase):
    def test_accepts_full_digest_and_complete_metadata(self):
        app, value = deploy.validate_request(request(), CONFIG)
        self.assertEqual(app["app_id"], "platform-smoke")
        self.assertEqual(value["commit_sha"], SHA)

    def test_rejects_non_allowlisted_repository(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "Repository is not authorized"):
            deploy.validate_request(request(repository="attacker/fork"), CONFIG)

    def test_rejects_non_production_branch(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "production branch"):
            deploy.validate_request(request(branch="feature/try"), CONFIG)

    def test_rejects_mutable_image_tag(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "immutable digest"):
            deploy.validate_request(request(image=APP["image_namespace"] + ":latest"), CONFIG)

    def test_rejects_wrong_image_namespace(self):
        image = f"ghcr.io/attacker/other@sha256:{DIGEST}"
        with self.assertRaisesRegex(deploy.DeploymentError, "allowlisted GHCR package"):
            deploy.validate_request(request(image=image), CONFIG)

    def test_rejects_arbitrary_commands_and_paths(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported fields"):
            deploy.validate_request(request(command="sh -c id", compose_file="/tmp/evil.yml"), CONFIG)

    def test_rejects_missing_release_metadata(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "approved release metadata"):
            deploy.validate_request(request(release={"run_id": "12345"}), CONFIG)

    def test_rejects_short_or_non_hex_commit(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "full 40-character"):
            deploy.validate_request(request(commit_sha="deadbeef"), CONFIG)

    def test_rejects_unapproved_reusable_workflow_commit(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "not on the server allowlist"):
            deploy.validate_request(request(platform_sha="d" * 40), CONFIG)

    def test_rollback_does_not_accept_image_override(self):
        value = request(operation="rollback")
        value["image"] = f"{APP['image_namespace']}@sha256:{DIGEST}"
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported fields"):
            deploy.validate_request(value, CONFIG)


class ForcedCommandLauncherTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sh"), "POSIX shell is required")
    def test_rejects_remote_commands_before_sudo(self):
        source = (ROOT / "deploy" / "controller" / "bin" / "roman-deploy-ssh").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            marker = temp / "sudo-called"
            fake_sudo = temp / "sudo"
            fake_sudo.write_text(f'#!/bin/sh\nprintf called > "{marker}"\nexit 99\n', encoding="utf-8")
            fake_sudo.chmod(0o755)
            launcher = temp / "launcher"
            launcher.write_text(source.replace("/usr/bin/sudo", str(fake_sudo)), encoding="utf-8")
            launcher.chmod(0o755)

            result = subprocess.run(
                ["sh", str(launcher)],
                env={**os.environ, "SSH_ORIGINAL_COMMAND": "id"},
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("Remote commands are not accepted", result.stderr)
            self.assertFalse(marker.exists(), "remote commands must be rejected before sudo")


class SmokeFixtureTests(unittest.TestCase):
    def test_unhealthy_mode_returns_503_and_explicit_status(self):
        with mock.patch.dict(os.environ, {"SMOKE_HEALTH_MODE": "unhealthy"}):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), smoke_server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/health", timeout=2)
                self.assertEqual(raised.exception.code, 503)
                try:
                    body = json.loads(raised.exception.read().decode("utf-8"))
                finally:
                    raised.exception.close()
                self.assertEqual(body["status"], "unhealthy")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)


class WorkflowOriginTests(unittest.TestCase):
    def setUp(self):
        self.app = dict(APP, allowed_platform_shas=[WORKFLOW_SHA])

    def _responses(self):
        return [
            {
                "event": "workflow_dispatch",
                "status": "in_progress",
                "head_branch": "main",
                "head_sha": WORKFLOW_SHA,
                "run_attempt": 1,
                "actor": {"login": "RomanYsrael-cmd"},
                "path": APP["workflow_path"],
                "referenced_workflows": [{
                    "path": APP["reusable_workflow_path"] + "@" + WORKFLOW_SHA,
                    "sha": WORKFLOW_SHA,
                }],
            },
            {"commit": {"sha": WORKFLOW_SHA}},
            {"commit": {"sha": SHA}},
            {"sha": SHA},
        ]

    def test_accepts_only_configured_in_progress_main_workflow(self):
        value = request()
        with mock.patch.object(deploy, "github_json", side_effect=self._responses()):
            deploy.verify_release_origin(value, self.app)

    def test_rejects_pull_request_workflow(self):
        value = request()
        responses = self._responses()
        responses[0]["event"] = "pull_request"
        with mock.patch.object(deploy, "github_json", side_effect=responses):
            with self.assertRaisesRegex(deploy.DeploymentError, "authorized in-progress"):
                deploy.verify_release_origin(value, self.app)

    def test_rejects_a_different_workflow_file(self):
        value = request()
        responses = self._responses()
        responses[0]["path"] = ".github/workflows/other.yml"
        with mock.patch.object(deploy, "github_json", side_effect=responses):
            with self.assertRaisesRegex(deploy.DeploymentError, "authorized in-progress"):
                deploy.verify_release_origin(value, self.app)

    def test_rejects_non_allowlisted_workflow_actor(self):
        value = request()
        responses = self._responses()
        responses[0]["actor"]["login"] = "unapproved-writer"
        value["release"]["actor"] = "unapproved-writer"
        with mock.patch.object(deploy, "github_json", side_effect=responses):
            with self.assertRaisesRegex(deploy.DeploymentError, "authorized in-progress"):
                deploy.verify_release_origin(value, self.app)


class ImageProvenanceTests(unittest.TestCase):
    def test_checks_image_source_release_and_platform_pin(self):
        labels = {
            "org.opencontainers.image.revision": SHA,
            "org.opencontainers.image.source": "https://github.com/RomanYsrael-cmd/roman-lms",
            "io.roman.platform.source-repository": "https://github.com/RomanYsrael-cmd/roman-infrastructure",
            "io.roman.platform.source-revision": WORKFLOW_SHA,
            "io.roman.deployment.package-repository": "https://github.com/RomanYsrael-cmd/roman-lms",
            "io.roman.deployment.caller-revision": SHA,
        }
        result = SimpleNamespace(stdout=json.dumps(labels), returncode=0, stderr="")
        with mock.patch.object(deploy, "run_command", return_value=result):
            deploy.verify_image_labels(
                "approved-image@sha256:" + DIGEST, SHA, SHA, WORKFLOW_SHA,
                APP["image_package_repository"], APP["image_source_repository"], {},
            )

    def test_rejects_wrong_image_revision(self):
        labels = {
            "org.opencontainers.image.revision": "d" * 40,
            "org.opencontainers.image.source": "https://github.com/RomanYsrael-cmd/roman-lms",
            "io.roman.platform.source-repository": "https://github.com/RomanYsrael-cmd/roman-infrastructure",
            "io.roman.platform.source-revision": WORKFLOW_SHA,
            "io.roman.deployment.package-repository": "https://github.com/RomanYsrael-cmd/roman-lms",
            "io.roman.deployment.caller-revision": SHA,
        }
        result = SimpleNamespace(stdout=json.dumps(labels), returncode=0, stderr="")
        with mock.patch.object(deploy, "run_command", return_value=result):
            with self.assertRaisesRegex(deploy.DeploymentError, "revision label"):
                deploy.verify_image_labels(
                    "approved-image@sha256:" + DIGEST, SHA, SHA, WORKFLOW_SHA,
                    APP["image_package_repository"], APP["image_source_repository"], {},
                )


class GateTests(unittest.TestCase):
    def _healthy_preflight(self):
        return (
            mock.patch.object(deploy.os.path, "ismount", return_value=True),
            mock.patch.object(deploy, "docker_root", return_value="/srv/apps/docker"),
            mock.patch.object(deploy.os.path, "isdir", return_value=True),
            mock.patch.object(deploy.shutil, "disk_usage", return_value=SimpleNamespace(free=12 * 1024**3)),
            mock.patch.object(deploy.os, "cpu_count", return_value=6),
            mock.patch.object(deploy, "available_memory_bytes", return_value=2 * 1024**3),
            mock.patch.object(deploy, "service_is_active", return_value=True),
        )

    def test_mount_gate_fails_closed(self):
        patches = self._healthy_preflight()
        with patches[0] as mount, patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            mount.return_value = False
            with self.assertRaisesRegex(deploy.DeploymentError, "Required storage mount"):
                deploy.preflight(APP)

    def test_disk_gate_rejects_low_space(self):
        patches = self._healthy_preflight()
        with patches[0], patches[1], patches[2], patches[3] as disk, patches[4], patches[5], patches[6]:
            disk.return_value = SimpleNamespace(free=1024)
            with self.assertRaisesRegex(deploy.DeploymentError, "Insufficient free space"):
                deploy.preflight(APP)

    def test_memory_gate_rejects_low_capacity(self):
        patches = self._healthy_preflight()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as memory, patches[6]:
            memory.return_value = 128 * 1024**2
            with self.assertRaisesRegex(deploy.DeploymentError, "Insufficient available memory"):
                deploy.preflight(APP)

    def test_docker_root_must_be_on_apps_drive(self):
        patches = self._healthy_preflight()
        with patches[0], mock.patch.object(deploy, "docker_root", return_value="/var/lib/docker"), patches[2], patches[3], patches[4], patches[5], patches[6]:
            with self.assertRaisesRegex(deploy.DeploymentError, "outside the approved applications SSD"):
                deploy.preflight(APP)

    def test_missing_mount_does_not_write_release_artifacts_to_os_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "deployments.jsonl"
            metrics_path = Path(tmp) / "roman-platform.prom"
            app = dict(APP, history_file=str(audit_path), metrics_file=str(metrics_path))
            with mock.patch.object(deploy.os.path, "ismount", return_value=False):
                with self.assertRaisesRegex(deploy.DeploymentError, "Required storage mount"):
                    deploy._append_audit(app, {"operation": "deploy"})
                with self.assertRaisesRegex(deploy.DeploymentError, "Required storage mount"):
                    deploy._write_metrics(app, {}, "failure", "deploy", 1, "12345", SHA)
            self.assertFalse(audit_path.exists())
            self.assertFalse(metrics_path.exists())

    @unittest.skipIf(deploy.fcntl is None, "Linux file locks are required")
    def test_conflicting_deployment_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = dict(APP, lock_dir=tmp)
            first = deploy._lock(app)
            try:
                with self.assertRaisesRegex(deploy.DeploymentError, "already running"):
                    deploy._lock(app)
            finally:
                first.close()


class RouteSafetyTests(unittest.TestCase):
    def test_failed_nginx_validation_restores_previous_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / "active.conf"
            original = "return 503;\n"
            route.write_text(original, encoding="utf-8")
            app = dict(APP, route_file=str(route))
            success = SimpleNamespace(returncode=0, stdout="", stderr="")
            calls = [deploy.DeploymentError("nginx rejected candidate"), success, success]
            with mock.patch.object(deploy, "run_command", side_effect=calls):
                with self.assertRaisesRegex(deploy.DeploymentError, "nginx rejected candidate"):
                    deploy.switch_route(app, 19081)
            self.assertEqual(route.read_text(encoding="utf-8"), original)


class MetricsTests(unittest.TestCase):
    def test_writes_latest_outcome_duration_and_active_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            metrics_path = Path(tmp) / "roman-platform.prom"
            app = dict(APP, metrics_file=str(metrics_path))
            state = {
                "active": {
                    "commit_sha": SHA,
                    "platform_sha": WORKFLOW_SHA,
                    "slot": "blue",
                }
            }

            with mock.patch.object(deploy.os.path, "ismount", return_value=True):
                deploy._write_metrics(app, state, "failure", "deploy", 2.75, "12345", SHA)

            metrics = metrics_path.read_text(encoding="utf-8")
            self.assertIn('roman_platform_last_deployment_info{app_id="platform-smoke",operation="deploy",outcome="failure",workflow_run_id="12345",request_sha="' + SHA + '"} 1', metrics)
            self.assertIn('roman_platform_last_deployment_duration_seconds{app_id="platform-smoke",operation="deploy",outcome="failure",workflow_run_id="12345",request_sha="' + SHA + '"} 2.750', metrics)
            self.assertIn(f'roman_platform_active_release_info{{app_id="platform-smoke",revision="{SHA}",slot="blue"}} 1', metrics)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(metrics_path.stat().st_mode), 0o644)

    def test_escapes_prometheus_label_values(self):
        self.assertEqual(deploy._prometheus_label('a"b\\c\nd'), 'a\\"b\\\\c\\nd')


class GrafanaAlertProvisioningTests(unittest.TestCase):
    def test_rules_use_existing_warning_route_and_deployment_metrics(self):
        rules = grafana_alerts.desired_rules("alerts-folder")
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(item["metadata"]["annotations"]["grafana.app/folder"] == "alerts-folder" for item in rules))
        self.assertTrue(all("grafana.app/folder" not in item["metadata"].get("labels", {}) for item in rules))
        self.assertTrue(all("grafana.com/group" not in item["metadata"].get("labels", {}) for item in rules))
        self.assertTrue(all("grafana.com/group-index" not in item["metadata"].get("labels", {}) for item in rules))
        self.assertTrue(all(item["spec"]["labels"]["severity"] == "warning" for item in rules))
        self.assertIn('outcome="failure"', rules[0]["spec"]["expressions"]["A"]["model"]["expr"])
        self.assertIn("bool 120", rules[1]["spec"]["expressions"]["A"]["model"]["expr"])
        self.assertTrue(all(item["spec"]["noDataState"] == "Ok" for item in rules))

    def test_upsert_skips_an_unchanged_rule_when_grafana_omits_zero_for(self):
        desired = grafana_alerts.desired_rules("alerts-folder")[0]
        current_spec = dict(desired["spec"])
        current_spec.pop("for")
        current = {
            "metadata": {"annotations": desired["metadata"]["annotations"]},
            "spec": current_spec,
        }
        with mock.patch.object(grafana_alerts, "api_request", return_value=current) as api:
            self.assertFalse(grafana_alerts._upsert_rule("unused", desired))
        self.assertEqual(api.call_count, 1)

    def test_upsert_refuses_a_rule_uid_owned_by_another_component(self):
        desired = grafana_alerts.desired_rules("alerts-folder")[0]
        current = {"spec": {"labels": {"component": "unrelated"}}}
        with mock.patch.object(grafana_alerts, "api_request", return_value=current):
            with self.assertRaisesRegex(grafana_alerts.ProvisioningError, "belongs to another component"):
                grafana_alerts._upsert_rule("unused", desired)


if __name__ == "__main__":
    unittest.main()
