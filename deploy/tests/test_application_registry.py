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

    def test_schema_compatibility_evidence_is_exact_and_fail_closed(self):
        app = copy.deepcopy(self.platform["applications"]["romanlms-production"])
        validate_application("romanlms-production", app)
        mutations = (
            ("missing content hash", lambda item: item["applied_migration"].pop("sha256")),
            ("unreviewed effect", lambda item: item.update(effect="additive-schema-change")),
            ("unallowlisted workflow", lambda item: item.update(target_platform_sha="f" * 40)),
            ("pending migration overlaps observed schema", lambda item: item["pending_migrations"][0].update(version="153", script="V153__gds_runtime_minimum_table_and_sequence_grants.sql")),
            ("target mismatch", lambda item: item["pending_migrations"][0].update(version="155", script="V155__unexpected_change.sql")),
            ("mutable retained image", lambda item: item["retained_release"]["components"].update(backend="ghcr.io/owner/backend:latest")),
        )
        for label, mutate in mutations:
            value = copy.deepcopy(app)
            mutate(value["schema_compatibility_evidence"][0])
            with self.subTest(mutation=label):
                with self.assertRaises(RegistryError):
                    validate_application("romanlms-production", value)

    def test_schedulease_environments_are_isolated(self):
        apps = self.platform["applications"]
        staging = apps["schedulease-staging"]
        production = apps["schedulease-production"]
        lms = apps["romanlms-production"]
        for field in (
            "container_network", "allowed_database_urls", "migration_database_username",
            "state_file", "history_file", "route_file", "metrics_file",
        ):
            with self.subTest(field=field):
                self.assertNotEqual(staging[field], production[field])
                self.assertNotEqual(production[field], lms[field])
        self.assertTrue(set(staging["secret_refs"]).isdisjoint(production["secret_refs"]))
        lms_secrets = {
            lms["database_env_file"], lms["migration_database_env_file"],
            lms["components"]["backend"]["container_env_file"],
        }
        self.assertTrue(set(production["secret_refs"]).isdisjoint(lms_secrets))
        self.assertTrue(set(staging["secret_refs"]).isdisjoint(lms_secrets))
        for app in (staging, production):
            with self.subTest(app=app["app_id"]):
                self.assertEqual(app["container_network_mode"], "bridge")
                self.assertFalse(app["container_publish"])
                self.assertEqual(set(app["components"]), {"backend"})
                self.assertTrue(app["components"]["backend"]["sse_enabled"])
                self.assertTrue(app["single_backend_maintenance"])
                self.assertEqual(app["storage_root"], f"/srv/apps/{app['app_id']}")
                self.assertEqual(app["required_mounts"], ["/srv/apps", "/srv/databases"])
                self.assertEqual(app["docker_root_prefix"], "/srv/apps")
                self.assertGreaterEqual(app["minimum_free_gib"], 10)
                self.assertGreaterEqual(app["minimum_cpu_cores"], 2)
                self.assertEqual(app["database"]["backup_adapter"], "verified-dump")
                self.assertEqual(app["schema_migration"]["rollback_after_migration"], "forward-only")

    def test_control_plane_uses_registered_maintenance_migrations_and_least_privilege_sql(self):
        app = self.platform["applications"]["romanlms-control-plane"]
        self.assertTrue(app["single_backend_maintenance"])
        self.assertFalse(app["automatic_release_enabled"])
        self.assertEqual(app["automatic_operations"], [])
        backend = app["components"]["backend"]
        self.assertEqual(backend["route_prefix"], "/api/")
        self.assertEqual(backend["health_path"], "/actuator/health")
        self.assertEqual(backend["public_health_path"], "/api/actuator/health")
        self.assertEqual(backend["route_health_url"], "http://127.0.0.1:18094/api/actuator/health")
        frontend = app["components"]["frontend"]
        self.assertIn(
            "/var/cache/nginx:rw,nosuid,nodev,noexec,size=32m,uid=101,gid=101,mode=0777",
            frontend["container_tmpfs"],
        )

        policy = (ROOT / "deploy" / "scripts" / "romanlms-control-plane" / "database-roles.sql").read_text(encoding="utf-8")
        for statement in (
            "REVOKE pg_read_all_data FROM roman_control_backup;",
            "ALTER ROLE roman_control_migrator NOINHERIT;",
            "WITH INHERIT FALSE, SET TRUE",
            "GRANT CREATE ON DATABASE roman_control_prod TO roman_control_owner;",
            "ALTER DEFAULT PRIVILEGES FOR ROLE roman_control_owner IN SCHEMA roman_control",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO roman_control_runtime",
            "GRANT SELECT ON TABLES TO roman_control_backup, roman_control_migrator",
            "REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA roman_control FROM PUBLIC",
            "GRANT EXECUTE ON FUNCTIONS TO roman_control_runtime",
            "REVOKE ALL PRIVILEGES ON SCHEMA roman_control FROM roman_control_observer;",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, policy)
        self.assertNotIn("GRANT pg_read_all_data TO roman_control_backup", policy)

    def test_control_plane_delivery_worker_is_enabled_in_both_slots_and_refreshable(self):
        app = self.platform["applications"]["romanlms-control-plane"]
        self.assertTrue(app["runtime_refresh_enabled"])
        self.assertEqual(
            app["confirmation_phrases"]["refresh-stack"],
            "REFRESH ROMANLMS CONTROL PLANE RUNTIME CONFIGURATION",
        )
        for slot in ("blue", "green"):
            with self.subTest(slot=slot):
                self.assertEqual(
                    app["components"]["backend"]["container_environment_by_slot"][slot]["CONTROL_PLANE_WORKER_ENABLED"],
                    "true",
                )

    def test_gds_entitlement_enforcement_is_enabled_only_for_gds_in_both_slots(self):
        app = self.platform["applications"]["romanlms-production"]
        enforcement_keys = {
            slot: app["components"]["backend"]["container_environment_by_slot"][slot][
                "ROMANLMS_FEATURE_ENFORCEMENT_ENABLED_KEYS"
            ]
            for slot in ("blue", "green")
        }
        self.assertEqual(enforcement_keys, {"blue": "GDS_SIMULATOR", "green": "GDS_SIMULATOR"})

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
