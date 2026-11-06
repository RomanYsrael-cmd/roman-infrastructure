import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "controller"))

import roman_deploy as deploy


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
                "path": APP["workflow_path"] + "@refs/heads/main",
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
            "io.roman.platform.source-revision": WORKFLOW_SHA,
        }
        result = SimpleNamespace(stdout=json.dumps(labels), returncode=0, stderr="")
        with mock.patch.object(deploy, "run_command", return_value=result):
            deploy.verify_image_labels("approved-image@sha256:" + DIGEST, SHA, WORKFLOW_SHA, APP["repository"], {})

    def test_rejects_wrong_image_revision(self):
        labels = {
            "org.opencontainers.image.revision": "d" * 40,
            "org.opencontainers.image.source": "https://github.com/RomanYsrael-cmd/roman-lms",
            "io.roman.platform.source-revision": WORKFLOW_SHA,
        }
        result = SimpleNamespace(stdout=json.dumps(labels), returncode=0, stderr="")
        with mock.patch.object(deploy, "run_command", return_value=result):
            with self.assertRaisesRegex(deploy.DeploymentError, "revision label"):
                deploy.verify_image_labels("approved-image@sha256:" + DIGEST, SHA, WORKFLOW_SHA, APP["repository"], {})


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


if __name__ == "__main__":
    unittest.main()
