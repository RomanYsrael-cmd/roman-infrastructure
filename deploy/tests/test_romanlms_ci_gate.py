from __future__ import annotations

import unittest
from copy import deepcopy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "controller"))
from romanlms_ci_gate import CiGateError, verify_ci_release


SOURCE_SHA = "a" * 40
LONG_SHA = "b" * 40
TREE_SHA = "c" * 40
WORKFLOW_BLOB_SHA = "d" * 40
PARENT_SHA = "e" * 40
WORKFLOW_ID = 123
LONG_RUN_ID = 41
SHORT_RUN_ID = 52


def _run(*, run_id: int, sha: str, branch: str, profile: str, event: str, created: str, updated: str, conclusion: str = "success") -> dict:
    return {
        "id": run_id,
        "run_attempt": 1,
        "workflow_id": WORKFLOW_ID,
        "path": ".github/workflows/ci.yml",
        "head_sha": sha,
        "head_branch": branch,
        "head_repository": {"full_name": "RomanYsrael-cmd/roman-lms"},
        "actor": {"login": "RomanYsrael-cmd"},
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "display_title": f"RomanLMS CI - {profile} - {sha}",
        "created_at": created,
        "updated_at": updated,
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.short_runs = [
            _run(
                run_id=SHORT_RUN_ID,
                sha=SOURCE_SHA,
                branch="main",
                profile="SHORT",
                event="push",
                created="2026-11-12T11:02:00Z",
                updated="2026-11-12T11:10:00Z",
            )
        ]
        self.long_runs = [
            _run(
                run_id=LONG_RUN_ID,
                sha=LONG_SHA,
                branch="feat/auto-release-smoke",
                profile="LONG",
                event="pull_request",
                created="2026-11-12T10:00:00Z",
                updated="2026-11-12T10:40:00Z",
            )
        ]
        self.main_tree = TREE_SHA
        self.candidate_tree = TREE_SHA
        self.changed_files = [{"filename": "frontend/index.html", "status": "modified"}]
        self.merged_at = "2026-11-12T11:00:00Z"
        self.pull_requests = [
            {
                "number": 17,
                "state": "closed",
                "merged_at": self.merged_at,
                "merge_commit_sha": SOURCE_SHA,
                "user": {"login": "RomanYsrael-cmd"},
                "base": {
                    "ref": "main",
                    "repo": {"full_name": "RomanYsrael-cmd/roman-lms"},
                },
                "head": {
                    "sha": LONG_SHA,
                    "ref": "feat/auto-release-smoke",
                    "repo": {"full_name": "RomanYsrael-cmd/roman-lms"},
                },
            }
        ]

    def __call__(self, path: str):
        normalized = path.lstrip("/")
        if normalized == "actions/workflows/ci.yml":
            return {"id": WORKFLOW_ID, "path": ".github/workflows/ci.yml", "state": "active"}
        if normalized == "branches/main":
            return {"commit": {"sha": SOURCE_SHA}}
        if normalized.startswith("contents/.github/workflows/ci.yml?"):
            return {"type": "file", "sha": WORKFLOW_BLOB_SHA}
        if normalized == f"commits/{SOURCE_SHA}":
            return {"sha": SOURCE_SHA, "commit": {"tree": {"sha": self.main_tree}}, "parents": [{"sha": PARENT_SHA}]}
        if normalized == f"commits/{LONG_SHA}":
            return {"sha": LONG_SHA, "commit": {"tree": {"sha": self.candidate_tree}}}
        if normalized.startswith(f"commits/{SOURCE_SHA}/pulls?"):
            return deepcopy(self.pull_requests)
        if normalized.startswith(f"actions/workflows/{WORKFLOW_ID}/runs?"):
            if "branch=main" in normalized:
                return {"total_count": len(self.short_runs), "workflow_runs": deepcopy(self.short_runs)}
            return {"total_count": len(self.long_runs), "workflow_runs": deepcopy(self.long_runs)}
        if normalized.startswith(f"actions/runs/{LONG_RUN_ID}/attempts/1/jobs?") or normalized.startswith(f"actions/runs/{SHORT_RUN_ID}/attempts/1/jobs?"):
            return {
                "total_count": 1,
                "jobs": [{"name": "CI / required", "status": "completed", "conclusion": "success"}],
            }
        if normalized.startswith(f"compare/{PARENT_SHA}...{SOURCE_SHA}"):
            return {"files": deepcopy(self.changed_files)}
        raise AssertionError(f"Unexpected GitHub API path: {path}")


def _verify(api: FakeGitHub, *, short_run_id: int | None = None, short_run_attempt: int | None = None):
    return verify_ci_release(
        api,
        source_sha=SOURCE_SHA,
        expected_workflow_blob_sha=WORKFLOW_BLOB_SHA,
        short_run_id=short_run_id,
        short_run_attempt=short_run_attempt,
    )


class RomanLmsCiReleaseGateTests(unittest.TestCase):
    def test_accepts_exact_short_long_tree_and_production_change(self):
        proof = _verify(FakeGitHub(), short_run_id=SHORT_RUN_ID, short_run_attempt=1)
        self.assertEqual(proof["sourceSha"], SOURCE_SHA)
        self.assertEqual(proof["longRunId"], LONG_RUN_ID)
        self.assertEqual(proof["shortRunId"], SHORT_RUN_ID)
        self.assertEqual(proof["pullRequestNumber"], 17)
        self.assertTrue(proof["productionChange"])

    def test_control_only_changes_do_not_request_production_images(self):
        api = FakeGitHub()
        api.changed_files = [{"filename": ".github/workflows/ci.yml", "status": "modified"}]
        proof = _verify(api)
        self.assertFalse(proof["productionChange"])

    def test_changed_ci_workflow_blob_must_match_root_approved_identity(self):
        original = FakeGitHub.__call__

        class WrongWorkflow(FakeGitHub):
            def __call__(self, path: str):
                if path.lstrip("/").startswith("contents/.github/workflows/ci.yml?"):
                    return {"type": "file", "sha": "f" * 40}
                return original(self, path)

        with self.assertRaisesRegex(CiGateError, "root-approved workflow identity"):
            _verify(WrongWorkflow())

    def test_different_candidate_tree_is_rejected(self):
        api = FakeGitHub()
        api.candidate_tree = "f" * 40
        with self.assertRaisesRegex(CiGateError, "differs from the exact main release tree"):
            _verify(api)

    def test_latest_failed_short_run_blocks_even_if_an_older_one_passed(self):
        api = FakeGitHub()
        api.short_runs.append(
            _run(
                run_id=SHORT_RUN_ID + 1,
                sha=SOURCE_SHA,
                branch="main",
                profile="SHORT",
                event="push",
                created="2026-11-12T11:11:00Z",
                updated="2026-11-12T11:13:00Z",
                conclusion="failure",
            )
        )
        with self.assertRaisesRegex(CiGateError, "did not complete successfully"):
            _verify(api)

    def test_triggered_run_must_be_the_latest_exact_short_attempt(self):
        with self.assertRaisesRegex(CiGateError, "not the latest exact-SHA main SHORT run"):
            _verify(FakeGitHub(), short_run_id=SHORT_RUN_ID - 1, short_run_attempt=1)

    def test_direct_main_commit_without_a_merged_owner_pr_is_blocked(self):
        api = FakeGitHub()
        api.pull_requests = []
        with self.assertRaisesRegex(CiGateError, "no merged owner-authored same-repository PR"):
            _verify(api)

    def test_short_must_start_after_merge(self):
        api = FakeGitHub()
        api.short_runs[0]["created_at"] = "2026-11-12T10:59:00Z"
        with self.assertRaisesRegex(CiGateError, "began before the pull request was merged"):
            _verify(api)

    def test_long_must_finish_before_merge(self):
        api = FakeGitHub()
        api.long_runs[0]["updated_at"] = "2026-11-12T11:01:00Z"
        with self.assertRaisesRegex(CiGateError, "did not pass before the pull request was merged"):
            _verify(api)


if __name__ == "__main__":
    unittest.main(verbosity=2)
