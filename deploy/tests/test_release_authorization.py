from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
from verify_release_request import AuthorizationError, authorize  # noqa: E402


class ReleaseAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = ROOT / "examples" / "application-profiles.json"
        cls.app = json.loads(cls.registry.read_text(encoding="utf-8"))["applications"]["sample-static-site"]
        cls.source_sha = "a" * 40
        cls.platform_sha = cls.app["allowed_platform_shas"][0]
        cls.args = Namespace(
            registry=cls.registry,
            app_id="sample-static-site",
            operation="deploy",
            component="",
            repository=cls.app["repository"],
            branch="main",
            source_sha=cls.source_sha,
            workflow_sha=cls.source_sha,
            platform_sha=cls.platform_sha,
            event="workflow_dispatch",
            actor=cls.app["authorized_actors"][0],
            run_id="42",
            run_attempt="1",
            release_mode="manual",
            trigger_run_id="",
            trigger_run_attempt="",
            confirmation=cls.app["confirmation_phrases"]["deploy"],
        )

    def _responses(self, *, branch_sha: str | None = None, conclusion: str = "success"):
        app = self.app
        return {
            "actions/runs/42": {
                "status": "in_progress",
                "path": app["workflow_path"],
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": self.source_sha,
                "run_attempt": 1,
                "actor": {"login": app["authorized_actors"][0]},
                "referenced_workflows": [{
                    "path": app["reusable_workflow_path"] + "@" + self.platform_sha,
                    "sha": self.platform_sha,
                }],
            },
            "branches/main": {"commit": {"sha": branch_sha or self.source_sha}},
            "actions/workflows/ci.yml/runs?head_sha=" + self.source_sha + "&event=push&per_page=100": {
                "workflow_runs": [{
                    "id": 10,
                    "run_attempt": 1,
                    "head_sha": self.source_sha,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": conclusion,
                    "actor": {"login": app["authorized_actors"][0]},
                }],
            },
        }

    def _authorize(self, args: Namespace | None = None, *, branch_sha: str | None = None, conclusion: str = "success"):
        responses = self._responses(branch_sha=branch_sha, conclusion=conclusion)
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-with-sufficient-length"}), \
             mock.patch("verify_release_request._api", side_effect=lambda _repo, path, _token: responses[path]):
            return authorize(args or self.args)

    def test_authorizes_current_owner_workflow_exact_source_ci_and_pinned_platform(self) -> None:
        value = self._authorize()
        self.assertEqual(value["app_id"], "sample-static-site")
        self.assertEqual(value["source_sha"], self.source_sha)
        self.assertEqual(value["environment"], "production")

    def test_rejects_stale_source_commit(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "stale"):
            self._authorize(branch_sha="b" * 40)

    def test_rejects_failed_exact_commit_ci(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "latest required CI"):
            self._authorize(conclusion="failure")

    def test_rejects_wrong_confirmation_before_github_api_calls(self) -> None:
        args = Namespace(**vars(self.args))
        args.confirmation = "DEPLOY SOMETHING ELSE"
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-with-sufficient-length"}), \
             mock.patch("verify_release_request._api") as api:
            with self.assertRaisesRegex(AuthorizationError, "confirmation"):
                authorize(args)
        api.assert_not_called()

    def test_rejects_workflow_that_did_not_call_the_registered_sha(self) -> None:
        responses = self._responses()
        responses["actions/runs/42"]["referenced_workflows"] = []
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-with-sufficient-length"}), \
             mock.patch("verify_release_request._api", side_effect=lambda _repo, path, _token: responses[path]):
            with self.assertRaisesRegex(AuthorizationError, "workflow run is not authorized"):
                authorize(self.args)

    def test_rejects_pinned_workflow_sha_missing_from_root_allowlist(self) -> None:
        args = Namespace(**vars(self.args))
        args.platform_sha = "f" * 40
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-with-sufficient-length"}), \
             mock.patch("verify_release_request._api") as api:
            with self.assertRaisesRegex(AuthorizationError, "root application allowlist"):
                authorize(args)
        api.assert_not_called()

    def test_authorizer_runs_standalone_from_the_repository_root(self) -> None:
        script = ROOT / "deploy" / "scripts" / "verify_release_request.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Check a reusable GitHub deployment request", result.stdout)


class BuildOnlyReleaseAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ROOT / "examples" / "application-profiles.json"
        self.app = json.loads(self.registry.read_text(encoding="utf-8"))["applications"]["sample-build-only"]
        self.source_sha = "a" * 40
        self.platform_sha = self.app["allowed_platform_shas"][0]
        self.args = Namespace(
            registry=self.registry,
            app_id="sample-build-only",
            operation="build-artifact",
            component="",
            repository=self.app["repository"],
            branch="main",
            source_sha=self.source_sha,
            workflow_sha=self.source_sha,
            platform_sha=self.platform_sha,
            event="workflow_dispatch",
            actor=self.app["authorized_actors"][0],
            run_id="42",
            run_attempt="1",
            release_mode="manual",
            trigger_run_id="",
            trigger_run_attempt="",
            confirmation=self.app["confirmation_phrases"]["build-artifact"],
        )

    def test_authorizes_pinned_build_only_workflow_and_reports_registered_toolchain(self) -> None:
        responses = {
            "actions/runs/42": {
                "status": "in_progress",
                "path": self.app["workflow_path"],
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": self.source_sha,
                "run_attempt": 1,
                "actor": {"login": self.args.actor},
                "referenced_workflows": [{
                    "path": self.app["reusable_workflow_path"] + "@" + self.platform_sha,
                    "sha": self.platform_sha,
                }],
            },
            "branches/main": {"commit": {"sha": self.source_sha}},
            "actions/workflows/ci.yml/runs?head_sha=" + self.source_sha + "&event=push&per_page=100": {
                "workflow_runs": [{
                    "id": 19,
                    "run_attempt": 1,
                    "head_sha": self.source_sha,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "actor": {"login": self.args.actor},
                }],
            },
        }
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-with-sufficient-length"}), \
             mock.patch("verify_release_request._api", side_effect=lambda _repo, path, _token: responses[path]):
            result = authorize(self.args)
        self.assertEqual(result["operation"], "build-artifact")
        self.assertEqual(result["build_system"], "gradle-android")

    def test_build_only_profiles_cannot_request_host_deployments(self) -> None:
        args = Namespace(**vars(self.args))
        args.operation = "deploy"
        args.confirmation = "BUILD AND PACKAGE APPLICATION"
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-with-sufficient-length"}), \
             mock.patch("verify_release_request._api") as api:
            with self.assertRaisesRegex(AuthorizationError, "cannot be used for server deployment"):
                authorize(args)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
