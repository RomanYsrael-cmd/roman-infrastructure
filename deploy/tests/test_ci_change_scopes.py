import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "ci"))

from change_scopes import classify_paths


class ChangeScopeTests(unittest.TestCase):
    def test_controller_change_selects_policy_checks_only(self):
        result = classify_paths([
            "deploy/controller/roman_deploy.py",
            "deploy/controller/romanlms_ci_gate.py",
            "deploy/tests/test_controller.py",
        ])
        self.assertTrue(result["controller"])
        self.assertFalse(result["smoke"])
        self.assertFalse(result["ansible"])
        self.assertEqual(result["unknown_paths"], [])

    def test_infrastructure_components_map_to_their_focused_checks(self):
        result = classify_paths([
            "deploy/ansible/install.yml",
            "deploy/monitoring/collect_romanlms_production.py",
            "deploy/cloudflare/romanlms-production-proxy/worker.mjs",
            "app/smoke/server.py",
            "platform/applications.json",
        ])
        for scope in ("ansible", "monitoring", "cloudflare", "smoke", "configuration"):
            self.assertTrue(result[scope], scope)
        self.assertEqual(result["unknown_paths"], [])

    def test_docs_are_known_and_unknown_paths_fail_closed(self):
        self.assertEqual(classify_paths(["docs/deployment.md"])["unknown_paths"], [])
        self.assertEqual(classify_paths(["unmapped/new-component.py"])["unknown_paths"], ["unmapped/new-component.py"])

    def test_full_history_fallback_enables_all_validation_scopes(self):
        result = classify_paths(["*"])
        self.assertTrue(all(result[scope] for scope in (
            "controller", "configuration", "ansible", "monitoring", "cloudflare", "smoke", "workflow", "ci_policy",
        )))
        self.assertEqual(result["unknown_paths"], [])


if __name__ == "__main__":
    unittest.main()
