import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "controller"))

from application_registry import RegistryError, validate_application, validate_registry


class ApplicationRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.examples = json.loads((ROOT / "examples" / "application-profiles.json").read_text(encoding="utf-8"))
        cls.platform = json.loads((ROOT / "platform" / "applications.json").read_text(encoding="utf-8"))

    def test_all_example_profiles_validate(self):
        self.assertEqual(
            set(self.examples["applications"]),
            {
                "sample-static-site", "sample-api", "sample-full-stack", "sample-multi-service",
                "sample-worker", "sample-scheduled-job", "sample-stateful-service",
                "sample-game-server", "sample-build-only",
            },
        )
        validate_registry(self.examples)

    def test_installed_registry_validates(self):
        validate_registry(self.platform)

    def test_rejects_build_only_application_that_can_be_deployed(self):
        app = copy.deepcopy(self.examples["applications"]["sample-build-only"])
        app["deployment_enabled"] = True
        with self.assertRaisesRegex(RegistryError, "cannot be deployed"):
            validate_application("sample-build-only", app)

    def test_rejects_build_only_artifact_path_traversal(self):
        app = copy.deepcopy(self.examples["applications"]["sample-build-only"])
        app["artifact_build"]["artifact_globs"] = ["../secrets/*"]
        with self.assertRaisesRegex(RegistryError, "safe registered toolchain"):
            validate_application("sample-build-only", app)

    def test_rejects_invalid_scheduled_job_calendar(self):
        app = copy.deepcopy(self.examples["applications"]["sample-scheduled-job"])
        app["runtime"]["systemd_calendar"] = "*-*-* 29:00:00 UTC"
        with self.assertRaisesRegex(RegistryError, "systemd UTC calendar"):
            validate_application("sample-scheduled-job", app)

    def test_rejects_route_file_outside_managed_nginx_directory(self):
        app = copy.deepcopy(self.examples["applications"]["sample-api"])
        app["route_file"] = "/tmp/application.conf"
        with self.assertRaisesRegex(RegistryError, "managed route"):
            validate_application("sample-api", app)

    def test_domain_routes_require_a_managed_loopback_site(self):
        app = copy.deepcopy(self.examples["applications"]["sample-static-site"])
        del app["nginx_site"]
        with self.assertRaisesRegex(RegistryError, "managed loopback Nginx site"):
            validate_application("sample-static-site", app)

    def test_rejects_duplicate_registered_nginx_listener_port(self):
        value = copy.deepcopy(self.examples)
        value["applications"]["sample-api"]["nginx_site"]["listen_port"] = 18082
        with self.assertRaisesRegex(RegistryError, "host port 18082/tcp"):
            validate_registry(value)

    def test_image_revision_mode_must_match_registered_source_checkout(self):
        app = copy.deepcopy(self.examples["applications"]["sample-api"])
        app["image_revision_mode"] = "platform"
        with self.assertRaisesRegex(RegistryError, "match the registered build source"):
            validate_application("sample-api", app)

    def test_rejects_cross_application_port_collision(self):
        value = copy.deepcopy(self.examples)
        value["applications"]["sample-api"]["slot_ports"]["blue"] = 20001
        with self.assertRaisesRegex(RegistryError, "host port 20001/tcp"):
            validate_registry(value)

    def test_rejects_duplicate_game_server_protocol_binding(self):
        value = copy.deepcopy(self.examples)
        ports = value["applications"]["sample-game-server"]["runtime"]["ports"]
        ports.append(copy.deepcopy(ports[0]))
        with self.assertRaisesRegex(RegistryError, "same host port"):
            validate_registry(value)

    def test_rejects_overlapping_domain_root_route(self):
        value = copy.deepcopy(self.examples)
        value["applications"]["sample-api"]["routes"][0] = {"domain": "static.example.test", "path": "/api"}
        with self.assertRaisesRegex(RegistryError, "domain route"):
            validate_registry(value)

    def test_rejects_an_application_without_explicit_operation_confirmation(self):
        app = copy.deepcopy(self.examples["applications"]["sample-api"])
        del app["confirmation_phrases"]["deploy"]
        with self.assertRaisesRegex(RegistryError, "confirmation phrase"):
            validate_application("sample-api", app)

    def test_accepts_root_registered_build_arguments_but_rejects_a_user_command(self):
        app = copy.deepcopy(self.examples["applications"]["sample-api"])
        app["build"]["operation_build_args"] = {"deploy": {"PUBLIC_MODE": "production"}}
        validate_application("sample-api", app)
        app["build"]["operation_build_args"]["deploy"]["RUN"] = "production\nRUN whoami"
        with self.assertRaisesRegex(RegistryError, "single-line literals"):
            validate_application("sample-api", app)


if __name__ == "__main__":
    unittest.main()
