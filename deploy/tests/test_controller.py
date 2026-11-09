import contextlib
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
import collect_romanlms_production as production_metrics
import server as smoke_server


CONFIG = json.loads((ROOT / "platform" / "applications.json").read_text(encoding="utf-8"))
SHA = "a" * 40
WORKFLOW_SHA = "b" * 40
DIGEST = "c" * 64
APP = dict(CONFIG["applications"]["platform-smoke"], allowed_platform_shas=[WORKFLOW_SHA])
CONFIG["applications"]["platform-smoke"] = APP


class WorkflowContractTests(unittest.TestCase):
    def test_server_requires_latest_exact_commit_ci_to_pass(self):
        app = dict(APP, required_ci_workflow_file="ci.yml")
        good = {"id": 10, "run_attempt": 1, "head_sha": SHA, "head_branch": "main", "event": "push", "status": "completed", "conclusion": "success", "actor": {"login": "RomanYsrael-cmd"}}
        with mock.patch.object(deploy, "github_json", return_value={"workflow_runs": [good]}):
            deploy.verify_required_ci(request(), app)
        failed = {**good, "id": 11, "conclusion": "failure"}
        with mock.patch.object(deploy, "github_json", return_value={"workflow_runs": [failed, good]}):
            with self.assertRaisesRegex(deploy.DeploymentError, "latest required CI"):
                deploy.verify_required_ci(request(), app)
        with mock.patch.object(deploy, "github_json", return_value={"workflow_runs": [{**good, "actor": {"login": "other"}}]}):
            with self.assertRaisesRegex(deploy.DeploymentError, "authorized actor"):
                deploy.verify_required_ci(request(), app)
    def test_private_release_requires_owner_manual_main_and_successful_ci(self):
        workflow = (ROOT / ".github" / "workflows" / "romanlms-private-deploy.yml").read_text(encoding="utf-8")
        for required in ("github.actor == 'RomanYsrael-cmd'", "github.event_name == 'workflow_dispatch'",
                         "github.ref == 'refs/heads/main'", "DEPLOY PRIVATE ROMANLMS",
                         "ROLLBACK PRIVATE ROMANLMS", "actions/workflows/ci.yml/runs?head_sha=",
                         "$latest.conclusion -ne 'success'", "$currentRun.referenced_workflows",
                         "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"):
            self.assertIn(required, workflow)

    def test_private_release_publishes_with_buildkit_and_verifies_immutable_digest(self):
        workflow = (ROOT / ".github" / "workflows" / "romanlms-private-deploy.yml").read_text(encoding="utf-8")
        self.assertIn("'buildx','build','--push','--metadata-file'", workflow)
        self.assertIn("$publishMetadata.'containerimage.digest'", workflow)
        self.assertIn("& docker pull $digestReference", workflow)
        self.assertIn("$repoDigests -notcontains $digestReference", workflow)
        self.assertNotIn("'--load'", workflow)
        self.assertNotIn("& docker push $tag", workflow)

    def test_production_profile_is_manual_owner_only_and_server_policy_scoped(self):
        workflow = (ROOT / ".github" / "workflows" / "romanlms-private-deploy.yml").read_text(encoding="utf-8")
        for required in (
            "'romanlms-production' = @{ Workflow='.github/workflows/romanlms-production-deploy.yml'; Environment='production'; ImagePrefix='romanlms-prod' }",
            "github.actor == 'RomanYsrael-cmd'",
            "github.event_name == 'workflow_dispatch'",
            "github.ref == 'refs/heads/main'",
            "app_id=$env:APP_ID",
            "environment=$env:DEPLOYMENT_ENVIRONMENT",
            "currentRun.path -ne $profile.Workflow",
        ):
            self.assertIn(required, workflow)
        self.assertIn("inputs.app_id == 'romanlms-production'", workflow)
        self.assertIn("inputs.confirmation == 'DEPLOY ROMANLMS PRODUCTION'", workflow)
        self.assertIn("inputs.confirmation == 'ROLLBACK ROMANLMS PRODUCTION'", workflow)

    def test_expected_unhealthy_rejection_resets_powershell_exit_code(self):
        workflow = (ROOT / ".github" / "workflows" / "romanlms-smoke-deploy.yml").read_text(encoding="utf-8")
        confirmation = "Write-Host 'Server rejected the unhealthy candidate at the readiness gate.'"
        self.assertIn(confirmation, workflow)
        self.assertIn(confirmation + "\n              $global:LASTEXITCODE = 0", workflow)


class RetainedReleaseContainerTests(unittest.TestCase):
    def test_matches_the_retained_container_name_and_labels(self):
        name = deploy._container_name(APP, "blue", SHA)
        release = {"slot": "blue", "commit_sha": SHA, "container": name}
        result = subprocess.CompletedProcess(
            args=["docker", "inspect"],
            returncode=0,
            stdout=f"/{name}|platform-smoke|blue\n",
            stderr="",
        )
        with mock.patch.object(deploy, "run_command", return_value=result) as run:
            self.assertTrue(deploy._release_container_available(APP, release))
        self.assertEqual(run.call_args.args[0][-1], name)

    def test_rejects_wrong_container_identity_without_running_docker(self):
        release = {"slot": "blue", "commit_sha": SHA, "container": "--help"}
        with mock.patch.object(deploy, "run_command") as run:
            self.assertFalse(deploy._release_container_available(APP, release))
        run.assert_not_called()

    def test_rejects_container_with_wrong_slot_label(self):
        name = deploy._container_name(APP, "blue", SHA)
        release = {"slot": "blue", "commit_sha": SHA, "container": name}
        result = subprocess.CompletedProcess(
            args=["docker", "inspect"],
            returncode=0,
            stdout=f"/{name}|platform-smoke|green\n",
            stderr="",
        )
        with mock.patch.object(deploy, "run_command", return_value=result):
            self.assertFalse(deploy._release_container_available(APP, release))


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

    def test_phase6_database_validation_accepts_only_the_allowlisted_migrator(self):
        app = dict(
            APP,
            app_id="romanlms-phase6-backend",
            environment="phase6-staging",
            image_namespace="ghcr.io/romanysrael-cmd/romanlms-phase6-backend",
            migration_image_namespace="ghcr.io/romanysrael-cmd/romanlms-phase6-migrator",
            image_revision_mode="caller",
        )
        config = {"applications": {app["app_id"]: app}}
        value = request(
            operation="validate-database",
            app_id=app["app_id"],
            environment="phase6-staging",
            migration_image=f"{app['migration_image_namespace']}@sha256:{DIGEST}",
        )
        value.pop("image")
        value.pop("strategy")
        value["registry_token"] = "ghs_" + "z" * 32
        selected, accepted = deploy.validate_request(value, config)
        self.assertEqual(selected["app_id"], "romanlms-phase6-backend")
        self.assertEqual(accepted["migration_image"], f"{app['migration_image_namespace']}@sha256:{DIGEST}")

    def test_phase6_database_validation_rejects_an_unapproved_migrator_package(self):
        app = dict(
            APP,
            app_id="romanlms-phase6-backend",
            environment="phase6-staging",
            image_namespace="ghcr.io/romanysrael-cmd/romanlms-phase6-backend",
            migration_image_namespace="ghcr.io/romanysrael-cmd/romanlms-phase6-migrator",
        )
        config = {"applications": {app["app_id"]: app}}
        value = request(
            operation="validate-database",
            app_id=app["app_id"],
            environment="phase6-staging",
            migration_image=f"ghcr.io/attacker/migrator@sha256:{DIGEST}",
        )
        value.pop("image")
        value.pop("strategy")
        value["registry_token"] = "ghs_" + "z" * 32
        with self.assertRaisesRegex(deploy.DeploymentError, "allowlisted GHCR package"):
            deploy.validate_request(value, config)

    def test_unhealthy_test_requires_an_explicit_server_policy(self):
        value = request(operation="unhealthy-test")
        value.pop("strategy")
        with self.assertRaisesRegex(deploy.DeploymentError, "not enabled"):
            deploy.validate_request(value, CONFIG)


class RomanLmsContainerPolicyTests(unittest.TestCase):
    def test_stack_request_requires_each_allowlisted_immutable_image(self):
        app = dict(APP, migration_image_namespace="ghcr.io/owner/migrator", components={
            "backend": {"image_namespace": "ghcr.io/owner/backend"},
            "frontend": {"image_namespace": "ghcr.io/owner/frontend"},
        })
        value = request(operation="deploy-stack", migration_image=f"ghcr.io/owner/migrator@sha256:{DIGEST}", images={
            "backend": f"ghcr.io/owner/backend@sha256:{DIGEST}",
            "frontend": f"ghcr.io/owner/frontend@sha256:{DIGEST}",
        })
        value.pop("image")
        config = {"applications": {"platform-smoke": app}}
        deploy.validate_request(value, config)
        for bad_images in ({"backend": value["images"]["backend"]}, {**value["images"], "frontend": "ghcr.io/owner/frontend:latest"}):
            with self.subTest(images=bad_images), self.assertRaises(deploy.DeploymentError):
                deploy.validate_request({**value, "images": bad_images}, config)

    def test_failure_test_requires_explicit_component_and_allowlisted_digest(self):
        app = dict(APP, stack_test_enabled=True, components={"backend": {"image_namespace": "ghcr.io/owner/backend"}})
        value = request(operation="test-stack", component="backend", image=f"ghcr.io/owner/backend@sha256:{DIGEST}")
        value.pop("strategy")
        config = {"applications": {"platform-smoke": app}}
        deploy.validate_request(value, config)
        for change in ({"component": "frontend"}, {"image": "ghcr.io/other/backend:latest"}):
            with self.subTest(change=change), self.assertRaises(deploy.DeploymentError):
                deploy.validate_request({**value, **change}, config)

    def test_stack_route_renders_both_components_for_one_slot(self):
        components = {
            "backend": {"route_prefix": "/lms/", "slot_ports": {"blue": 18182, "green": 18184}},
            "frontend": {"route_prefix": "/", "slot_ports": {"blue": 18181, "green": 18183}},
        }
        rendered = deploy._stack_route_content(components, "green")
        self.assertIn("location ^~ /lms/", rendered)
        self.assertIn("127.0.0.1:18184", rendered)
        self.assertIn("location /", rendered)
        self.assertIn("127.0.0.1:18183", rendered)
        self.assertNotIn("127.0.0.1:18182", rendered)
        self.assertIn("proxy_set_header Upgrade $http_upgrade", rendered)

    def test_stack_route_rejects_unsafe_or_ambiguous_routes(self):
        for prefix in ("/lms/;return 200;", "/missing-trailing", "/../../"):
            with self.subTest(prefix=prefix), self.assertRaises(deploy.DeploymentError):
                deploy._stack_route_content({"backend": {"route_prefix": prefix, "slot_ports": {"blue": 18182}}}, "blue")
        duplicated = {name: {"route_prefix": "/", "slot_ports": {"blue": port}} for name, port in (("one", 18181), ("two", 18182))}
        with self.assertRaises(deploy.DeploymentError):
            deploy._stack_route_content(duplicated, "blue")

    def test_stack_route_uses_fixed_internal_addresses(self):
        components = {
            "backend": {"route_prefix": "/lms/", "container_port": 8080, "slot_ips": {"blue": "172.30.66.21"}},
            "frontend": {"route_prefix": "/", "container_port": 3000, "slot_ips": {"blue": "172.30.66.31"}},
        }
        rendered = deploy._stack_route_content(components, "blue")
        self.assertIn("proxy_pass http://172.30.66.21:8080;", rendered)
        self.assertIn("proxy_pass http://172.30.66.31:3000;", rendered)

    def test_flyway_info_requires_exact_version_and_no_pending_migrations(self):
        valid = {"schemaVersion": "132", "migrations": [{"version": "132", "state": "Success"}]}
        deploy._verify_flyway_info(json.dumps(valid), "132")
        invalid = [
            {"schemaVersion": "131", "migrations": [{"state": "Success"}]},
            {"schemaVersion": "132", "migrations": [{"state": "Pending"}]},
            {"schemaVersion": "132", "migrations": [{"state": "Failed"}]},
            {"schemaVersion": "132", "migrations": []},
        ]
        for report in invalid:
            with self.subTest(report=report), self.assertRaises(deploy.DeploymentError):
                deploy._verify_flyway_info(json.dumps(report), "132")
        with self.assertRaises(deploy.DeploymentError):
            deploy._verify_flyway_info("not json", "132")

    def test_flyway_info_requires_exact_approved_legacy_history(self):
        exceptions = [
            {"state": state, "category": "Repeatable", "type": "SQL", "version": "", "description": "seed dev data", "filepath": ""}
            for state in ("Deleted", "Missing")
        ]
        info = {
            "schemaVersion": "132",
            "flywayVersion": "11.14.1",
            "schemaName": "roman_lms",
            "migrations": [{"state": "Success"}, *exceptions],
        }
        deploy._verify_flyway_info(json.dumps(info), "132", exceptions, flyway_version="11.14.1", schema_name="roman_lms")
        with self.assertRaises(deploy.DeploymentError):
            deploy._verify_flyway_info(json.dumps(info), "132")
        with self.assertRaises(deploy.DeploymentError):
            deploy._verify_flyway_info(json.dumps(info), "132", exceptions[:1])
        mismatched = {**info, "migrations": [{"state": "Success"}, {**exceptions[0], "description": "unexpected migration"}, exceptions[1]]}
        with self.assertRaises(deploy.DeploymentError):
            deploy._verify_flyway_info(json.dumps(mismatched), "132", exceptions, flyway_version="11.14.1", schema_name="roman_lms")

    def test_database_preflight_rejects_missing_or_invalid_connection_before_registry_access(self):
        cases = [
            {"DB_URL": "jdbc:postgresql://db/stage", "DB_USERNAME": "", "DB_PASSWORD": "secret"},
            {"DB_URL": "not-a-jdbc-url", "DB_USERNAME": "stage", "DB_PASSWORD": "secret"},
            {"DB_URL": "jdbc:postgresql://db/stage", "DB_USERNAME": "stage"},
        ]
        for values in cases:
            with self.subTest(values=values), \
                 mock.patch.object(deploy, "_secure_env_file", return_value=Path("/private/env")), \
                 mock.patch.object(deploy, "_env_file_values", return_value=values), \
                 mock.patch.object(deploy, "registry_session") as registry:
                with self.assertRaises(deploy.DeploymentError):
                    deploy._validate_database({}, {"database_env_file": "/private/env"})
                registry.assert_not_called()

    def test_missing_environment_file_is_rejected(self):
        with mock.patch.object(Path, "lstat", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(deploy.DeploymentError, "unavailable"):
                deploy._secure_env_file("/private/missing.env")

    def test_container_options_use_private_network_and_root_controlled_slot_values(self):
        app = dict(
            APP,
            app_id="romanlms-phase6-backend",
            container_network="romanlms-phase6-staging",
            container_env_file="/etc/roman-platform/romanlms-phase6/runtime.env",
            container_environment_by_slot={"blue": {"PORT": "8080", "SERVER_ADDRESS": "0.0.0.0"}},
            container_tmpfs=["/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777"],
        )
        with mock.patch.object(deploy, "_secure_env_file", return_value=Path(app["container_env_file"])):
            args = deploy._container_run_args(app, "registry/app@sha256:" + DIGEST, "roman-test", "blue", 18182, SHA)
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "romanlms-phase6-staging")
        self.assertIn("127.0.0.1:18182:8080", args)
        self.assertIn("--env-file", args)
        self.assertIn("PORT=8080", args)
        self.assertNotIn("host", args)

    def test_app_image_revision_tracks_caller_source_while_smoke_stays_pinned_to_platform(self):
        with mock.patch.object(deploy, "verify_image_labels") as verify:
            deploy._verify_image("image", {"commit_sha": SHA, "platform_sha": WORKFLOW_SHA}, {**APP, "image_revision_mode": "caller"}, {})
        self.assertEqual(verify.call_args.args[1], SHA)
        with mock.patch.object(deploy, "verify_image_labels") as verify:
            deploy._verify_image("image", {"commit_sha": SHA, "platform_sha": WORKFLOW_SHA}, APP, {})
        self.assertEqual(verify.call_args.args[1], WORKFLOW_SHA)


class StackDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.route = root / "route.conf"
        self.route.write_text("return 503;\n", encoding="utf-8")
        self.state = root / "state.json"
        self.app = dict(APP, app_id="test-stack", state_file=str(self.state), route_file=str(self.route),
                        container_network="private-stack", network_subnet="172.30.66.0/24", private_listener_port=18081, container_publish=False, expected_schema_version="132", components={
            "backend": {"route_prefix": "/lms/", "container_port": 8080, "slot_ips": {"blue": "172.30.66.21", "green": "172.30.66.22"}, "route_health_url": "http://127.0.0.1:18081/lms/actuator/health"},
            "frontend": {"route_prefix": "/", "container_port": 3000, "slot_ips": {"blue": "172.30.66.31", "green": "172.30.66.32"}, "route_health_url": "http://127.0.0.1:18081/"},
        })
        self.request = request(operation="deploy-stack", images={"backend": "image-b", "frontend": "image-f"}, migration_image="migration")

    def test_deploy_starts_both_components_before_one_route_switch(self):
        commands = []
        def run(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        def switch(app, slot):
            self.assertEqual(sum(args[:2] == ["docker", "run"] for args in commands), 2)
            self.route.write_text("switched " + slot, encoding="utf-8")
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), \
             mock.patch.object(deploy, "run_command", side_effect=run), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "_remove_inactive_slot"), mock.patch.object(deploy, "_stack_release_healthy"), \
             mock.patch.object(deploy, "verify_release_origin"), mock.patch.object(deploy, "_switch_stack_route", side_effect=switch), \
             mock.patch.object(deploy, "require_storage_mounts"), mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_write_metrics"):
            result = deploy._deploy_stack(self.request, self.app)
        self.assertEqual(result["slot"], "blue")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(set(state["active"]["components"]), {"backend", "frontend"})
        self.assertEqual(self.route.read_text(encoding="utf-8"), "switched blue")
        candidate_commands = [args for args in commands if args[:2] == ["docker", "run"]]
        self.assertTrue(all("--publish" not in args and "--ip" in args for args in candidate_commands))

    def test_single_backend_maintenance_stops_old_backend_before_starting_candidate(self):
        self.app["single_backend_maintenance"] = True
        old_sha = "d" * 40
        active_components = {}
        for name in ("backend", "frontend"):
            component = deploy._stack_component_app(self.app, name)
            active_components[name] = {
                "slot": "green", "commit_sha": old_sha,
                "container": deploy._container_name(component, "green", old_sha),
                "image": "retained-image",
            }
        self.state.write_text(json.dumps({"active": {
            "app_id": self.app["app_id"], "slot": "green", "commit_sha": old_sha,
            "schema_version": "132", "components": active_components,
        }, "previous": None}), encoding="utf-8")
        commands = []
        statuses = iter(("true", "false"))

        def run(args, **kwargs):
            commands.append(args)
            if args[:3] == ["docker", "inspect", "--format"]:
                return subprocess.CompletedProcess(args, 0, next(statuses), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        def switch(app, slot):
            self.route.write_text("switched " + slot, encoding="utf-8")

        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), \
             mock.patch.object(deploy, "run_command", side_effect=run), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "_remove_inactive_slot"), mock.patch.object(deploy, "_release_container_available", return_value=True), \
             mock.patch.object(deploy, "_stack_release_healthy"), mock.patch.object(deploy, "verify_release_origin"), \
             mock.patch.object(deploy, "_switch_stack_route", side_effect=switch), mock.patch.object(deploy, "require_storage_mounts"), \
             mock.patch.object(deploy, "available_memory_bytes", return_value=4 * 1024**3), \
             mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_write_metrics"):
            result = deploy._deploy_stack(self.request, self.app)

        self.assertEqual(result["outcome"], "success")
        frontend_start = next(i for i, args in enumerate(commands) if args[:2] == ["docker", "run"] and "frontend" in args[args.index("--name") + 1])
        backend_stop = next(i for i, args in enumerate(commands) if args[:2] == ["docker", "stop"])
        backend_start = next(i for i, args in enumerate(commands) if args[:2] == ["docker", "run"] and "backend" in args[args.index("--name") + 1])
        self.assertLess(frontend_start, backend_stop)
        self.assertLess(backend_stop, backend_start)

    def test_single_backend_maintenance_removes_candidate_before_restoring_retained_backend(self):
        self.app["single_backend_maintenance"] = True
        old_sha = "d" * 40
        active_components = {}
        for name in ("backend", "frontend"):
            component = deploy._stack_component_app(self.app, name)
            active_components[name] = {
                "slot": "green", "commit_sha": old_sha,
                "container": deploy._container_name(component, "green", old_sha),
                "image": "retained-image",
            }
        active = {"app_id": self.app["app_id"], "slot": "green", "commit_sha": old_sha,
                  "schema_version": "132", "components": active_components}
        self.state.write_text(json.dumps({"active": active, "previous": None}), encoding="utf-8")
        commands = []
        states = iter(("true", "false", "false"))

        def run(args, **kwargs):
            commands.append(args)
            if args[:3] == ["docker", "inspect", "--format"]:
                return subprocess.CompletedProcess(args, 0, next(states), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        failure = deploy.DeploymentError("Readiness check failed: candidate did not start.")
        health = [None, failure, None]
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), \
             mock.patch.object(deploy, "run_command", side_effect=run), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "_remove_inactive_slot"), mock.patch.object(deploy, "_release_container_available", return_value=True), \
             mock.patch.object(deploy, "available_memory_bytes", return_value=4 * 1024**3), mock.patch.object(deploy, "_stack_release_healthy", side_effect=health), \
             mock.patch.object(deploy, "_stack_route_healthy"), mock.patch.object(deploy, "verify_release_origin"), \
             mock.patch.object(deploy, "_switch_stack_route") as switch, mock.patch.object(deploy, "require_storage_mounts"), \
             mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_write_metrics"):
            with self.assertRaisesRegex(deploy.DeploymentError, "candidate did not start"):
                deploy._deploy_stack(self.request, self.app)

        switch.assert_not_called()
        latest_remove = max(i for i, args in enumerate(commands) if args[:3] == ["docker", "rm", "-f"])
        restore_backend = next(i for i, args in enumerate(commands) if args[:2] == ["docker", "start"] and active_components["backend"]["container"] in args)
        self.assertLess(latest_remove, restore_backend)

    def test_maintenance_rollback_restores_active_backend_when_capacity_gate_fails(self):
        app = dict(self.app, single_backend_maintenance=True, minimum_available_memory_mb=3072)
        active = {"slot": "green", "commit_sha": "b" * 40, "schema_version": "132"}
        previous = {"slot": "blue", "commit_sha": "a" * 40, "schema_version": "132"}
        self.state.write_text(json.dumps({"active": active, "previous": previous}), encoding="utf-8")
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "_stop_stack_backend"), \
             mock.patch.object(deploy, "available_memory_bytes", return_value=1024 * 1024**2), \
             mock.patch.object(deploy, "_start_retained_stack") as start_previous, \
             mock.patch.object(deploy, "_restart_stack_backend") as restore_active, mock.patch.object(deploy, "require_storage_mounts"), \
             mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "Insufficient available memory"):
                deploy._rollback_stack(self.request, app)
        start_previous.assert_not_called()
        restore_active.assert_called_once_with(app, active)
        switch.assert_not_called()

    def test_maintenance_rollback_stops_active_backend_before_starting_retained_release(self):
        app = dict(self.app, single_backend_maintenance=True)
        active = {"slot": "green", "commit_sha": "b" * 40, "schema_version": "132"}
        previous = {"slot": "blue", "commit_sha": "a" * 40, "schema_version": "132"}
        self.state.write_text(json.dumps({"active": active, "previous": previous}), encoding="utf-8")
        events = []
        def stop_backend(*args):
            events.append("stop-active-backend")
        def start_previous(*args):
            events.append("start-retained-release")
            return []
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "_stop_stack_backend", side_effect=stop_backend), \
             mock.patch.object(deploy, "_start_retained_stack", side_effect=start_previous), mock.patch.object(deploy, "verify_release_origin"), \
             mock.patch.object(deploy, "available_memory_bytes", return_value=4 * 1024**3), mock.patch.object(deploy, "_switch_stack_route"), mock.patch.object(deploy, "require_storage_mounts"), \
             mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_write_metrics"):
            result = deploy._rollback_stack(self.request, app)
        self.assertEqual(result["slot"], "blue")
        self.assertEqual(events, ["stop-active-backend", "start-retained-release"])

    def test_unhealthy_active_release_can_be_replaced_after_candidate_is_healthy(self):
        self.app["allow_unhealthy_active_replacement"] = True
        active = {"slot": "blue", "commit_sha": "b" * 40, "components": {}}
        self.state.write_text(json.dumps({"active": active, "previous": None}), encoding="utf-8")
        commands = []
        audit = []

        def run(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        def switch(app, slot):
            self.assertEqual(sum(args[:2] == ["docker", "run"] for args in commands), 2)
            self.route.write_text("switched " + slot, encoding="utf-8")

        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), \
             mock.patch.object(deploy, "run_command", side_effect=run), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "_remove_inactive_slot"), \
             mock.patch.object(deploy, "_stack_release_healthy", side_effect=[deploy.DeploymentError("Readiness check failed: TimeoutError."), None]), \
             mock.patch.object(deploy, "verify_release_origin"), mock.patch.object(deploy, "_switch_stack_route", side_effect=switch), \
             mock.patch.object(deploy, "require_storage_mounts"), mock.patch.object(deploy, "_append_audit", side_effect=lambda app, event: audit.append(event)), \
             mock.patch.object(deploy, "_write_metrics"):
            result = deploy._deploy_stack(self.request, self.app)

        self.assertEqual(result["slot"], "green")
        self.assertEqual(self.route.read_text(encoding="utf-8"), "switched green")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["active"]["commit_sha"], SHA)
        self.assertEqual(state["previous"], active)
        self.assertEqual(audit[0]["outcome"], "active-readiness-failed")

    def test_unhealthy_active_replacement_does_not_hide_candidate_readiness_failure(self):
        self.app["allow_unhealthy_active_replacement"] = True
        active = {"slot": "blue", "commit_sha": "b" * 40, "components": {}}
        self.state.write_text(json.dumps({"active": active, "previous": None}), encoding="utf-8")
        commands = []

        def run(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), \
             mock.patch.object(deploy, "run_command", side_effect=run), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "_remove_inactive_slot"), \
             mock.patch.object(deploy, "_stack_release_healthy", side_effect=[deploy.DeploymentError("Readiness check failed: TimeoutError."), deploy.DeploymentError("Readiness check failed: HTTP 503 or unexpected response.")]), \
             mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "Readiness check failed: HTTP 503"):
                deploy._deploy_stack(self.request, self.app)

        switch.assert_not_called()
        self.assertEqual(self.route.read_text(encoding="utf-8"), "return 503;\n")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["active"], active)
        self.assertEqual(sum(args[:3] == ["docker", "rm", "-f"] for args in commands), 2)

    def test_unhealthy_active_replacement_does_not_hide_release_state_errors(self):
        self.app["allow_unhealthy_active_replacement"] = True
        active = {"slot": "blue", "commit_sha": "b" * 40, "components": {}}
        self.state.write_text(json.dumps({"active": active, "previous": None}), encoding="utf-8")
        with mock.patch.object(deploy, "_validate_database"), \
             mock.patch.object(deploy, "_stack_release_healthy", side_effect=deploy.DeploymentError("The retained application release state is invalid.")), \
             mock.patch.object(deploy, "registry_session") as registry, mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "state is invalid"):
                deploy._deploy_stack(self.request, self.app)
        registry.assert_not_called()
        switch.assert_not_called()

    def test_unhealthy_active_replacement_requires_explicit_server_policy(self):
        active = {"slot": "blue", "commit_sha": "b" * 40, "components": {}}
        self.state.write_text(json.dumps({"active": active, "previous": None}), encoding="utf-8")
        with mock.patch.object(deploy, "_validate_database"), \
             mock.patch.object(deploy, "_stack_release_healthy", side_effect=deploy.DeploymentError("Readiness check failed: TimeoutError.")), \
             mock.patch.object(deploy, "registry_session") as registry, mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "Readiness check failed"):
                deploy._deploy_stack(self.request, self.app)
        registry.assert_not_called()
        switch.assert_not_called()

    def test_second_component_failure_removes_started_candidate_and_keeps_route(self):
        commands = []
        def run(args, **kwargs):
            commands.append(args)
            if args[:2] == ["docker", "run"] and sum(c[:2] == ["docker", "run"] for c in commands) == 2:
                raise deploy.DeploymentError("candidate failed")
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), \
             mock.patch.object(deploy, "run_command", side_effect=run), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "_remove_inactive_slot"), mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "candidate failed"):
                deploy._deploy_stack(self.request, self.app)
        switch.assert_not_called()
        self.assertEqual(self.route.read_text(encoding="utf-8"), "return 503;\n")
        self.assertEqual(sum(args[:3] == ["docker", "rm", "-f"] for args in commands), 2)

    def test_failed_migration_preflight_never_pulls_or_switches(self):
        with mock.patch.object(deploy, "_validate_database", side_effect=deploy.DeploymentError("migration preflight")), \
             mock.patch.object(deploy, "registry_session") as registry, mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "migration preflight"):
                deploy._deploy_stack(self.request, self.app)
        registry.assert_not_called()
        switch.assert_not_called()

    def test_failure_test_preserves_active_pair_and_records_expected_rejection(self):
        self.app["stack_test_enabled"] = True
        self.app["components"]["backend"]["slot_ips"]["test"] = "172.30.66.41"
        self.app["components"]["frontend"]["slot_ips"]["test"] = "172.30.66.42"
        self.app["components"]["backend"]["unhealthy_test_environment"] = {"DB_URL": "jdbc:postgresql://invalid.example/test"}
        self.app["components"]["backend"]["unhealthy_test_timeout_seconds"] = 1
        active = {"slot": "blue", "commit_sha": SHA}
        self.state.write_text(json.dumps({"active": active}), encoding="utf-8")
        value = request(operation="test-stack", component="backend", image="image-b")
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if args[:2] == ["docker", "inspect"] else 0, "", "")
        with mock.patch.object(deploy, "_stack_release_healthy") as health, mock.patch.object(deploy, "_stack_route_healthy") as route, \
             mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "run_command", side_effect=run) as command, mock.patch.object(deploy, "_wait_http", side_effect=deploy.DeploymentError("unhealthy")), \
             mock.patch.object(deploy, "_append_audit") as audit, mock.patch.object(deploy, "_write_metrics") as metrics:
            result = deploy._test_stack_failure(value, self.app)
        self.assertEqual(result["outcome"], "expected-rejection")
        self.assertEqual(health.call_count, 2)
        self.assertEqual(route.call_count, 2)
        self.assertTrue(any(call.args[0][:3] == ["docker", "rm", "-f"] for call in command.call_args_list))
        self.assertEqual(audit.call_args.args[1]["outcome"], "failure")
        self.assertEqual(metrics.call_args.args[2], "failure")

    def test_unhealthy_component_removes_both_candidates_without_switching(self):
        commands = []
        def run(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "registry_session", return_value=contextlib.nullcontext({})), \
             mock.patch.object(deploy, "run_command", side_effect=run), mock.patch.object(deploy, "_verify_image"), \
             mock.patch.object(deploy, "_remove_inactive_slot"), mock.patch.object(deploy, "_stack_release_healthy", side_effect=deploy.DeploymentError("unhealthy")), \
             mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "unhealthy"):
                deploy._deploy_stack(self.request, self.app)
        switch.assert_not_called()
        self.assertEqual(sum(args[:3] == ["docker", "rm", "-f"] for args in commands), 2)
        self.assertEqual(self.route.read_text(encoding="utf-8"), "return 503;\n")

    def test_removal_refuses_active_slot_even_when_docker_returns_ids(self):
        with mock.patch.object(deploy, "_active_containers") as containers:
            with self.assertRaisesRegex(deploy.DeploymentError, "active release"):
                deploy._remove_inactive_slot(APP, "blue", {"slot": "blue", "container": "a-name"})
        containers.assert_not_called()

    def test_rejects_component_address_collision_and_publication(self):
        self.app["components"]["frontend"]["slot_ips"]["blue"] = "172.30.66.21"
        with self.assertRaisesRegex(deploy.DeploymentError, "address policy"):
            deploy._stack_validate_network(self.app)
        self.app["components"]["frontend"]["slot_ips"]["blue"] = "172.30.66.31"
        self.app["components"]["frontend"]["container_publish"] = True
        with self.assertRaisesRegex(deploy.DeploymentError, "without published ports"):
            deploy._stack_validate_network(self.app)

    def test_production_host_network_requires_loopback_per_slot_without_published_ports(self):
        app = dict(self.app, container_network="host", container_network_mode="host", allow_host_network=True,
                   components={
                       "backend": {"route_prefix": "/lms/", "container_port": 8080,
                                   "slot_ports": {"blue": 18100, "green": 18102},
                                   "host_network_bind_environment_variable": "SERVER_ADDRESS",
                                   "host_network_port_environment_variable": "SERVER_PORT",
                                   "container_environment_by_slot": {
                                       "blue": {"SERVER_ADDRESS": "127.0.0.1", "SERVER_PORT": "18100"},
                                       "green": {"SERVER_ADDRESS": "127.0.0.1", "SERVER_PORT": "18102"},
                                   },
                                   "route_health_url": "http://127.0.0.1:18081/lms/actuator/health"},
                       "frontend": {"route_prefix": "/", "container_port": 3000,
                                    "slot_ports": {"blue": 18101, "green": 18103},
                                    "host_network_bind_environment_variable": "FRONTEND_BIND_ADDRESS",
                                    "host_network_port_environment_variable": "FRONTEND_PORT",
                                    "container_environment_by_slot": {
                                        "blue": {"FRONTEND_BIND_ADDRESS": "127.0.0.1", "FRONTEND_PORT": "18101"},
                                        "green": {"FRONTEND_BIND_ADDRESS": "127.0.0.1", "FRONTEND_PORT": "18103"},
                                    },
                                    "route_health_url": "http://127.0.0.1:18081/"},
                   })
        deploy._stack_validate_network(app)
        backend = deploy._stack_component_app(app, "backend")
        with mock.patch.object(deploy, "_secure_env_file", return_value=Path("/private/runtime.env")):
            args = deploy._container_run_args(backend, "registry/app@sha256:" + DIGEST, "roman-test", "green", 8080, SHA)
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "host")
        self.assertNotIn("--publish", args)
        self.assertIn("SERVER_ADDRESS=127.0.0.1", args)
        self.assertIn("SERVER_PORT=18102", args)

    def test_production_host_network_rejects_non_loopback_bind_and_port_mismatch(self):
        app = dict(self.app, container_network="host", container_network_mode="host", allow_host_network=True,
                   components={
                       "backend": {"route_prefix": "/lms/", "container_port": 8080, "slot_ports": {"blue": 18100, "green": 18102},
                                   "host_network_bind_environment_variable": "SERVER_ADDRESS",
                                   "host_network_port_environment_variable": "SERVER_PORT",
                                   "container_environment_by_slot": {
                                       "blue": {"SERVER_ADDRESS": "0.0.0.0", "SERVER_PORT": "18100"},
                                       "green": {"SERVER_ADDRESS": "127.0.0.1", "SERVER_PORT": "18103"},
                                   },
                                   "route_health_url": "http://127.0.0.1:18081/lms/actuator/health"},
                   })
        with self.assertRaisesRegex(deploy.DeploymentError, "loopback"):
            deploy._stack_component_app(app, "backend")

    def test_host_network_route_preserves_forwarded_https_and_uses_loopback_ports(self):
        components = {
            "backend": {"route_prefix": "/lms/", "slot_ports": {"blue": 18100},
                        "forwarded_proto_variable": "$http_x_forwarded_proto",
                        "public_health_path": "/lms/actuator/health", "sse_enabled": True},
            "frontend": {"route_prefix": "/", "slot_ports": {"blue": 18101}},
        }
        rendered = deploy._stack_route_content(components, "blue")
        self.assertIn("proxy_pass http://127.0.0.1:18100;", rendered)
        self.assertIn("location = /lms/actuator/health", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:18100/lms/actuator/health;", rendered)
        self.assertIn("proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;", rendered)
        self.assertIn("proxy_buffering off;", rendered)
        self.assertIn("proxy_request_buffering off;", rendered)

    def test_rollback_switches_both_components_to_the_retained_release(self):
        active = {"slot": "green", "commit_sha": "b" * 40, "schema_version": "132"}
        previous = {"slot": "blue", "commit_sha": "a" * 40, "schema_version": "132"}
        self.state.write_text(json.dumps({"active": active, "previous": previous}), encoding="utf-8")
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "_start_retained_stack", return_value=[]) as health, \
             mock.patch.object(deploy, "verify_release_origin"), mock.patch.object(deploy, "_switch_stack_route") as switch, \
             mock.patch.object(deploy, "require_storage_mounts"), mock.patch.object(deploy, "_append_audit"), mock.patch.object(deploy, "_write_metrics"):
            result = deploy._rollback_stack(self.request, self.app)
        health.assert_called_once_with(self.app, previous)
        switch.assert_called_once_with(self.app, "blue")
        self.assertEqual(result["slot"], "blue")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["active"], previous)
        self.assertEqual(state["previous"], active)

    def test_rollback_rejects_a_different_schema_version(self):
        self.state.write_text(json.dumps({"active": {"slot": "green"}, "previous": {"slot": "blue", "schema_version": "131"}}), encoding="utf-8")
        with mock.patch.object(deploy, "_validate_database"), mock.patch.object(deploy, "_switch_stack_route") as switch:
            with self.assertRaisesRegex(deploy.DeploymentError, "schema policy"):
                deploy._rollback_stack(self.request, self.app)
        switch.assert_not_called()

    def test_failed_component_route_health_restores_the_whole_prior_route(self):
        with mock.patch.object(deploy, "run_command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
             mock.patch.object(deploy, "_wait_http", side_effect=[None, deploy.DeploymentError("unhealthy")]):
            with self.assertRaisesRegex(deploy.DeploymentError, "unhealthy"):
                deploy._switch_stack_route(self.app, "blue")
        self.assertEqual(self.route.read_text(encoding="utf-8"), "return 503;\n")
        self.assertEqual(sum(call.args[0] == ["systemctl", "reload", "nginx"] for call in run.call_args_list), 2)


class RomanLmsProductionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.app = CONFIG["applications"]["romanlms-production"]

    def test_production_policy_isolated_from_staging_and_uses_only_the_restored_database(self):
        self.assertEqual(self.app["environment"], "production")
        self.assertTrue(self.app["allow_unhealthy_active_replacement"])
        self.assertEqual(self.app["allowed_platform_shas"], ["f14001afa20e54681826394dd06a485dafbdcdac"])
        self.assertEqual(self.app["allowed_database_urls"], ["jdbc:postgresql://127.0.0.1:5432/romanlms_prod"])
        self.assertEqual(self.app["expected_schema_version"], "132")
        self.assertEqual(
            self.app["flyway_history_exceptions"],
            [
                {"state": state, "category": "Repeatable", "type": "SQL", "version": "", "description": "seed dev data", "filepath": ""}
                for state in ("Deleted", "Missing")
            ],
        )
        self.assertFalse(self.app["container_publish"])

    def test_production_pool_and_single_backend_policy_are_explicit_and_required(self):
        deploy._validate_romanlms_capacity_policy(self.app)
        self.assertTrue(self.app["single_backend_maintenance"])
        for slot in ("blue", "green"):
            environment = self.app["components"]["backend"]["container_environment_by_slot"][slot]
            self.assertEqual(environment["DB_POOL_MAX_SIZE"], "50")
            self.assertEqual(environment["DB_POOL_MIN_IDLE"], "2")
        bad_app = json.loads(json.dumps(self.app))
        bad_app["components"]["backend"]["container_environment_by_slot"]["green"]["DB_POOL_MAX_SIZE"] = "10"
        with self.assertRaisesRegex(deploy.DeploymentError, "maximum 50"):
            deploy._validate_romanlms_capacity_policy(bad_app)

    def test_verify_skips_candidate_capacity_gate_but_keeps_release_validation(self):
        value = {
            "operation": "verify-stack", "app_id": self.app["app_id"],
            "repository": self.app["repository"], "branch": self.app["production_branch"],
            "commit_sha": SHA, "workflow_sha": SHA,
            "platform_sha": self.app["allowed_platform_shas"][0], "environment": self.app["environment"],
            "release": {"run_id": "12345", "run_attempt": "1", "actor": "RomanYsrael-cmd"},
            "github_token": "ghs_" + "x" * 32,
        }
        lock = mock.Mock()
        with mock.patch.object(deploy, "_lock", return_value=lock), mock.patch.object(deploy, "preflight") as preflight, \
             mock.patch.object(deploy, "verify_release_origin"), mock.patch.object(deploy, "_verify_stack_active", return_value={"outcome": "healthy"}):
            result = deploy.execute(value, CONFIG)
        self.assertEqual(result["outcome"], "healthy")
        preflight.assert_called_once_with(self.app, capacity_checks=False, memory_check=False)
        lock.close.assert_called_once_with()
        self.assertEqual(self.app["container_network_mode"], "host")
        deploy._stack_validate_network(self.app)

    def test_production_component_ports_are_loopback_only_and_route_in_one_release_file(self):
        for name, bind_variable, port_variable in (
            ("backend", "SERVER_ADDRESS", "PORT"),
            ("frontend", "FRONTEND_BIND_ADDRESS", "FRONTEND_PORT"),
        ):
            component = deploy._stack_component_app(self.app, name)
            self.assertEqual(component["host_network_bind_environment_variable"], bind_variable)
            self.assertEqual(component["host_network_port_environment_variable"], port_variable)
            self.assertTrue(all(env[bind_variable] == "127.0.0.1" for env in component["container_environment_by_slot"].values()))
            with mock.patch.object(deploy, "_secure_env_file", return_value=Path("/private/runtime.env")):
                args = deploy._container_run_args(component, "registry/app@sha256:" + DIGEST, "roman-test", "blue", component["container_port"], SHA)
            self.assertEqual(args[args.index("--network") + 1], "host")
            self.assertNotIn("--publish", args)
        for slot, backend_port, frontend_port in (("blue", 18100, 18101), ("green", 18102, 18103)):
            route = deploy._stack_route_content(self.app["components"], slot)
            self.assertIn(f"proxy_pass http://127.0.0.1:{backend_port};", route)
            self.assertIn(f"proxy_pass http://127.0.0.1:{frontend_port};", route)
            self.assertIn("location = /lms/actuator/health", route)
        gateway = (ROOT / "deploy" / "ansible" / "templates" / "romanlms-production.conf.j2").read_text(encoding="utf-8")
        self.assertIn("listen 127.0.0.1:8081;", gateway)
        self.assertIn("location ^~ /lms/actuator/", gateway)
        self.assertIn("return 404;", gateway)


class Phase6PolicyTests(unittest.TestCase):
    def test_private_policy_keeps_network_database_and_publication_closed(self):
        app = CONFIG["applications"]["romanlms-phase6"]
        self.assertFalse(app.get("allow_unhealthy_active_replacement", False))
        deploy._stack_validate_network(app)
        self.assertFalse(app["container_publish"])
        self.assertEqual(app["container_restart_policy"], "no")
        self.assertEqual(
            app["allowed_database_urls"],
            [
                "jdbc:postgresql://romanlms-phase6-db:5432/romanlms_phase6",
                "jdbc:postgresql://romanlms-phase6-db:5432/romanlms_phase6_test",
            ],
        )
        self.assertEqual(app["private_listener_port"], 18081)
        self.assertEqual(set(app["components"]), {"backend", "frontend"})
        self.assertTrue(all(len(value) == 40 for value in app["allowed_platform_shas"]))
        for slot in ("blue", "green"):
            rendered = deploy._stack_route_content(app["components"], slot)
            self.assertIn(app["components"]["backend"]["slot_ips"][slot], rendered)
            self.assertIn(app["components"]["frontend"]["slot_ips"][slot], rendered)


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


class ReadinessCheckTests(unittest.TestCase):
    def test_http_error_reports_the_candidate_status_code(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:19082/health",
            503,
            "Service Unavailable",
            {},
            None,
        )
        with mock.patch.object(deploy.urllib.request, "urlopen", side_effect=error):
            with mock.patch.object(deploy.time, "monotonic", side_effect=[0, 0, 2]):
                with mock.patch.object(deploy.time, "sleep"):
                    with self.assertRaisesRegex(
                        deploy.DeploymentError,
                        "Readiness check failed: HTTP 503 or unexpected response",
                    ):
                        deploy._wait_http(
                            "http://127.0.0.1:19082/health",
                            '"status":"ok"',
                            1,
                        )


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

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0, "requires Linux root-owned test files")
    def test_restore_marker_must_match_approved_backup_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "restore.json"
            marker.write_text(json.dumps({"source_sha256": "a" * 64, "schema_version": "132"}), encoding="utf-8")
            marker.chmod(0o600)
            app = dict(APP, backup_marker=str(marker), backup_max_age_hours=24,
                       backup_marker_expected_source_sha256="a" * 64, expected_schema_version="132")
            patches = self._healthy_preflight()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                deploy.preflight(app)
                marker.write_text(json.dumps({"source_sha256": "b" * 64, "schema_version": "132"}), encoding="utf-8")
                with self.assertRaisesRegex(deploy.DeploymentError, "does not match"):
                    deploy.preflight(app)

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
    def test_private_dashboard_includes_release_health_and_resource_queries(self):
        dashboard = grafana_alerts.desired_dashboard()
        self.assertIn(grafana_alerts.DASHBOARD_TAG, dashboard["tags"])
        queries = " ".join(panel["targets"][0]["expr"] for panel in dashboard["panels"])
        for metric in ("romanlms_phase6_component_up", "romanlms_phase6_http_requests_total", "romanlms_phase6_jvm_memory_used_bytes", "roman_platform_active_release_info", "roman_pgbackrest_last_backup_age_seconds"):
            self.assertIn(metric, queries)

    def test_dashboard_refuses_to_overwrite_an_unmanaged_uid(self):
        with mock.patch.object(grafana_alerts, "api_request", return_value={"dashboard": {"tags": []}}) as api:
            with self.assertRaisesRegex(grafana_alerts.ProvisioningError, "belongs to another dashboard"):
                grafana_alerts._upsert_dashboard("unused", "folder")
        self.assertEqual(api.call_count, 1)
    def test_rules_use_existing_warning_route_and_deployment_metrics(self):
        self.assertEqual(len(grafana_alerts.desired_rules("alerts-folder")), 2)
        rules = grafana_alerts.desired_rules("alerts-folder", include_phase6=True)
        self.assertEqual(len(rules), 6)
        self.assertTrue(all(item["metadata"]["annotations"]["grafana.app/folder"] == "alerts-folder" for item in rules))
        self.assertTrue(all("grafana.app/folder" not in item["metadata"].get("labels", {}) for item in rules))
        self.assertTrue(all("grafana.com/group" not in item["metadata"].get("labels", {}) for item in rules))
        self.assertTrue(all("grafana.com/group-index" not in item["metadata"].get("labels", {}) for item in rules))
        self.assertTrue(all(item["spec"]["labels"]["severity"] == "warning" for item in rules))
        self.assertIn('outcome="failure"', rules[0]["spec"]["expressions"]["A"]["model"]["expr"])
        self.assertIn("bool 120", rules[1]["spec"]["expressions"]["A"]["model"]["expr"])
        self.assertIn('app_id="romanlms-phase6"', rules[2]["spec"]["expressions"]["A"]["model"]["expr"])
        self.assertIn('app_id="romanlms-phase6"', rules[3]["spec"]["expressions"]["A"]["model"]["expr"])
        self.assertTrue(all(item["spec"]["noDataState"] == "Ok" for item in rules))

    def test_production_rules_are_opt_in_and_use_production_only_metrics(self):
        rules = grafana_alerts.desired_rules("alerts-folder", include_production=True)
        self.assertEqual(len(rules), 13)
        production_rules = rules[2:]
        self.assertTrue(all("romanlms-production" in item["metadata"]["name"] for item in production_rules))
        self.assertTrue(any("romanlms_production_component_up" in item["spec"]["expressions"]["A"]["model"]["expr"] for item in production_rules))
        self.assertTrue(any("romanlms_production_hikaricp_connections_pending" in item["spec"]["expressions"]["A"]["model"]["expr"] for item in production_rules))
        self.assertTrue(any("status=~\"5..\"" in item["spec"]["expressions"]["A"]["model"]["expr"] for item in production_rules))
        self.assertTrue(all(item["spec"]["labels"]["component"] == "romanlms-production" for item in production_rules))

    def test_production_dashboard_covers_pool_latency_http_runtime_and_container_pressure(self):
        dashboard = grafana_alerts.desired_production_dashboard()
        self.assertEqual(dashboard["uid"], "romanlms-production-app")
        self.assertEqual(dashboard["title"], "RomanLMS Production")
        panel_source = "\n".join(panel["targets"][0]["expr"] for panel in dashboard["panels"])
        for metric in (
            "romanlms_production_hikaricp_connections_max",
            "romanlms_production_http_responses_total",
            "romanlms_production_http_requests_by_endpoint_seconds_bucket",
            "romanlms_production_jvm_threads_live",
            "romanlms_production_container_pids_limit_ratio",
            "romanlms_production_container_restart_count",
        ):
            self.assertIn(metric, panel_source)


class ProductionMetricsTests(unittest.TestCase):
    def test_metrics_aggregation_discards_request_labels_and_only_returns_bounded_totals(self):
        aggregated = production_metrics.aggregate_metrics(
            'http_server_requests_seconds_count{status="200",uri="/lms/api/user?email=synthetic"} 3\n'
            'jvm_memory_used_bytes{area="heap"} 1024\n'
            'hikaricp_connections_active{pool="app"} 2\n'
        )
        self.assertEqual(aggregated["http_2xx"], 3)
        self.assertEqual(aggregated["heap_used"], 1024)
        self.assertEqual(aggregated["db_connections"], 2)
        self.assertNotIn("synthetic", repr(aggregated))

    def test_exports_bounded_hikari_http_and_runtime_metrics_without_raw_request_labels(self):
        text = "\n".join([
            'hikaricp_connections_active{pool="db"} 3',
            'hikaricp_connections_idle{pool="db"} 2',
            'hikaricp_connections_pending{pool="db"} 1',
            'hikaricp_connections_max{pool="db"} 50',
            'hikaricp_connections_min{pool="db"} 2',
            'hikaricp_connections_timeout_total{pool="db"} 4',
            'hikaricp_connections_acquire_seconds_count{pool="db"} 10',
            'hikaricp_connections_acquire_seconds_sum{pool="db"} 0.5',
            'hikaricp_connections_acquire_seconds_max{pool="db"} 0.2',
            'hikaricp_connections_acquire_seconds_bucket{pool="db",le="0.1"} 8',
            'hikaricp_connections_acquire_seconds_bucket{pool="db",le="+Inf"} 10',
            'http_server_requests_seconds_count{method="GET",status="200",uri="/lms/api/offerings/123?email=private"} 3',
            'http_server_requests_seconds_count{method="POST",status="500",uri="/lms/api/offerings/123?email=private"} 1',
            'http_server_requests_seconds_sum{method="GET",uri="/lms/api/offerings/123?email=private"} 0.7',
            'http_server_requests_seconds_sum{method="POST",uri="/lms/api/offerings/123?email=private"} 1.2',
            'http_server_requests_seconds_bucket{method="GET",status="200",uri="/lms/api/offerings/123?email=private",le="1"} 3',
            'http_server_requests_seconds_bucket{method="GET",status="200",uri="/lms/api/offerings/123?email=private",le="+Inf"} 3',
            'http_server_requests_seconds_bucket{method="POST",status="500",uri="/lms/api/offerings/123?email=private",le="1"} 0',
            'http_server_requests_seconds_bucket{method="POST",status="500",uri="/lms/api/offerings/123?email=private",le="+Inf"} 1',
            'jvm_threads_live_threads 28',
            'jvm_threads_daemon_threads 18',
            'jvm_threads_peak_threads 32',
            'jvm_gc_pause_seconds_count{action="end of minor GC"} 9',
            'jvm_gc_pause_seconds_sum{action="end of minor GC"} 0.4',
        ])
        aggregated = production_metrics.aggregate_metrics(text)
        self.assertEqual(aggregated["hikari_max"], 50)
        self.assertEqual(aggregated["hikari_min"], 2)
        self.assertEqual(aggregated["hikari_active"], 3)
        self.assertEqual(aggregated["hikari_idle"], 2)
        self.assertEqual(aggregated["hikari_pending"], 1)
        self.assertEqual(aggregated["hikari_timeouts"], 4)
        self.assertEqual(aggregated["hikari_timers"]["acquire"]["count"], 10)
        self.assertEqual(aggregated["http_codes"]["500"], 1)
        self.assertEqual(aggregated["jvm_threads_live"], 28)
        rendered = "\n".join(production_metrics.application_metric_lines(aggregated))
        self.assertIn("romanlms_production_hikaricp_connections_max 50", rendered)
        self.assertIn('endpoint_group="offerings",method="GET"', rendered)
        self.assertIn('status="500"', rendered)
        self.assertNotIn("private", rendered)
        self.assertNotIn("123", rendered)

    def test_docker_stats_reports_process_use_and_limits(self):
        container = "roman-platform-romanlms-production-backend-green-" + SHA[:12]
        stats = json.dumps({"Name": container, "MemUsage": "128MiB / 1024MiB", "CPUPerc": "1.5%", "PIDs": "12"})
        with mock.patch.object(production_metrics.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stats, "")):
            values = production_metrics.docker_stats({"backend": container})
        self.assertEqual(values["backend"]["pids"], 12)
        self.assertEqual(values["backend"]["cpu"], 1.5)
        inspect = subprocess.CompletedProcess([], 0, "1073741824|256|2\n", "")
        with mock.patch.object(production_metrics.subprocess, "run", return_value=inspect):
            limits = production_metrics.docker_resource_limits({"backend": container})
        self.assertEqual(limits["backend"]["memory_limit"], 1073741824)
        self.assertEqual(limits["backend"]["pids_limit"], 256)
        self.assertEqual(limits["backend"]["restart_count"], 2)

    def test_docker_health_requires_the_exact_production_host_network_identity(self):
        for component in ("backend", "frontend"):
            name = f"roman-platform-romanlms-production-{component}-blue-" + SHA[:12]
            result = subprocess.CompletedProcess([], 0, f"host|healthy|romanlms-production-{component}\n", "")
            with mock.patch.object(production_metrics.subprocess, "run", return_value=result):
                self.assertEqual(production_metrics.docker_health(name), 1)

            wrong_label = subprocess.CompletedProcess([], 0, "host|healthy|romanlms-production\n", "")
            with mock.patch.object(production_metrics.subprocess, "run", return_value=wrong_label):
                self.assertEqual(production_metrics.docker_health(name), 0)

            wrong_network = subprocess.CompletedProcess([], 0, f"bridge|healthy|romanlms-production-{component}\n", "")
            with mock.patch.object(production_metrics.subprocess, "run", return_value=wrong_network):
                self.assertEqual(production_metrics.docker_health(name), 0)

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
