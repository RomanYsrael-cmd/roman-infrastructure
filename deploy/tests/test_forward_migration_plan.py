"""Regression tests for the opt-in, version-agnostic migration planner."""
import copy
import json
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "controller"))
from forward_migration_plan import (
    MigrationPlanError,
    bind_flyway_checksums,
    migration_artifact_checksums,
    validate_forward_plan,
    validate_migration_result,
)


def example():
    return {
        "schemaVersion": "133",
        "flywayVersion": "11.14.1",
        "schemaName": "roman_lms",
        "migrations": [
            {"version": "133", "state": "Success", "category": "Versioned",
             "type": "SQL", "description": "existing migration",
             "filepath": "/flyway/sql/V133__existing_migration.sql", "checksum": 231},
            {"version": "134", "state": "Pending", "category": "Versioned",
             "type": "SQL", "description": "add attendance metadata",
             "filepath": "/flyway/sql/V134__add_attendance_metadata.sql", "checksum": -321},
            {"version": "135", "state": "Pending", "category": "Versioned",
             "type": "SQL", "description": "add simulator tables",
             "filepath": "/flyway/sql/V135__add_simulator_tables.sql", "checksum": 7342},
            {"version": "136", "state": "Pending", "category": "Versioned",
             "type": "SQL", "description": "add grades index",
             "filepath": "/flyway/sql/V136__add_grades_index.sql", "checksum": 4309},
        ],
    }


def check(report, **kwargs):
    return validate_forward_plan(
        json.dumps(report),
        expected_flyway_version="11.14.1",
        expected_schema_name="roman_lms",
        **kwargs,
    )


class ForwardMigrationPlanTests(unittest.TestCase):
    def test_flyway_info_checksums_are_bound_to_database_history_and_release_sql(self):
        report = example()
        report["migrations"] = report["migrations"][:2]
        report["migrations"][0]["checksum"] = None
        report["migrations"][1]["checksum"] = None
        history = [{
            "version": "133",
            "description": "existing migration",
            "type": "SQL",
            "script": "V133__existing_migration.sql",
            "checksum": 231,
            "success": True,
        }]
        enriched = bind_flyway_checksums(
            json.dumps(report),
            json.dumps(history),
            {"V133__existing_migration.sql": 231, "V134__add_attendance_metadata.sql": -321},
        )
        plan = check(json.loads(enriched), max_pending=1)
        self.assertEqual(plan.pending, (("134", "V134__add_attendance_metadata.sql", "add attendance metadata", -321),))
        self.assertIn(("Success", "Versioned", "SQL", "133", "existing migration", "/flyway/sql/V133__existing_migration.sql", 231), plan.history)

    def test_checksum_binding_rejects_history_drift_and_missing_release_migrations(self):
        report = example()
        report["migrations"] = report["migrations"][:2]
        report["migrations"][0]["checksum"] = None
        report["migrations"][1]["checksum"] = None
        history = [{
            "version": "133", "description": "existing migration", "type": "SQL",
            "script": "V133__existing_migration.sql", "checksum": 231, "success": True,
        }]
        cases = (
            ({"V133__existing_migration.sql": 232, "V134__add_attendance_metadata.sql": -321}, history),
            ({"V133__existing_migration.sql": 231}, history),
            ({"V133__existing_migration.sql": 231, "V134__add_attendance_metadata.sql": -321}, []),
        )
        for checksums, database_history in cases:
            with self.subTest(checksums=checksums, database_history=database_history), self.assertRaises(MigrationPlanError):
                bind_flyway_checksums(json.dumps(report), json.dumps(database_history), checksums)

    def test_migration_artifact_checksum_reader_is_line_ending_and_bom_stable(self):
        with tempfile.TemporaryDirectory(prefix="roman-flyway-checksum-") as temp:
            directory = Path(temp)
            (directory / "V134__add_attendance_metadata.sql").write_bytes(
                "\ufeffCREATE TABLE attendance (id int);\r\nINSERT INTO attendance VALUES (1);\r\n".encode("utf-8")
            )
            checksum = migration_artifact_checksums(directory)["V134__add_attendance_metadata.sql"]
            (directory / "V135__add_other_metadata.sql").write_text("CREATE TABLE other (id int);\n", encoding="utf-8")
            checksums = migration_artifact_checksums(directory)
            self.assertEqual(checksums["V134__add_attendance_metadata.sql"], checksum)
            self.assertEqual(set(checksums), {"V134__add_attendance_metadata.sql", "V135__add_other_metadata.sql"})

    def test_migration_artifact_rejects_duplicate_and_unsafe_versions(self):
        for filenames in (
            ("V134__first.sql", "V134__second.sql"),
            ("V01__bad.sql",),
            ("V134__BadCase.sql",),
        ):
            with tempfile.TemporaryDirectory(prefix="roman-flyway-identity-") as temp:
                directory = Path(temp)
                for filename in filenames:
                    (directory / filename).write_text("SELECT 1;\n", encoding="utf-8")
                with self.subTest(filenames=filenames), self.assertRaises(MigrationPlanError):
                    migration_artifact_checksums(directory)

    def test_multiple_pending_versions_without_manifest_version_pin(self):
        plan = check(example())
        self.assertTrue(plan.needs_migration)
        self.assertEqual(plan.source_version, "133")
        self.assertEqual(plan.target_version, "136")
        self.assertEqual([row[0] for row in plan.pending], ["134", "135", "136"])
        self.assertEqual(len(plan.fingerprint), 64)
        self.assertEqual(plan, check(example()))

    def test_no_pending_does_not_trigger_database_migration(self):
        report = example()
        report["migrations"] = report["migrations"][:1]
        plan = check(report)
        self.assertFalse(plan.needs_migration)
        self.assertEqual(plan.source_version, plan.target_version)

    def test_rejects_injected_sql_file_and_non_versioned_migration(self):
        for mutate in (
            lambda row: row.update(filepath="/tmp/V134__add_attendance_metadata.sql"),
            lambda row: row.update(filepath="/flyway/sql/V135__wrong.sql"),
            lambda row: row.update(category="Repeatable"),
            lambda row: row.update(type="JDBC"),
            lambda row: row.update(checksum=None),
            lambda row: row.update(checksum=True),
            lambda row: row.update(description=" "),
        ):
            report = example()
            mutate(report["migrations"][1])
            with self.subTest(mutate=mutate), self.assertRaises(MigrationPlanError):
                check(report)

    def test_rejects_duplicate_or_backward_migrations(self):
        for version in ("133", "134", "132"):
            report = example()
            report["migrations"][2]["version"] = version
            report["migrations"][2]["filepath"] = f"/flyway/sql/V{version}__some_file.sql"
            with self.subTest(version=version), self.assertRaises(MigrationPlanError):
                check(report)
        report = example()
        report["migrations"][1], report["migrations"][2] = report["migrations"][2], report["migrations"][1]
        with self.assertRaises(MigrationPlanError):
            check(report)

    def test_rejects_unapproved_history_and_warnings(self):
        for state in ("Failed", "Missing", "Future", "Above Target", "Outdated", "Ignored"):
            report = example()
            report["migrations"].append({"state": state, "version": "100"})
            with self.subTest(state=state), self.assertRaises(MigrationPlanError):
                check(report)
        report = example()
        report["warnings"] = ["checksum changed"]
        with self.assertRaises(MigrationPlanError):
            check(report)

    def test_rejects_excessive_pending_and_wrong_schema_identity(self):
        with self.assertRaises(MigrationPlanError):
            check(example(), max_pending=2)
        with self.assertRaises(MigrationPlanError):
            check(example(), max_pending=0)
        report = example()
        report["schemaName"] = "other_schema"
        with self.assertRaises(MigrationPlanError):
            check(report)
        report = example()
        report["flywayVersion"] = "11.99.0"
        with self.assertRaises(MigrationPlanError):
            check(report)

    def test_plan_fingerprint_changes_with_checksum_or_script(self):
        first = check(example())
        report = example()
        report["migrations"][2]["checksum"] = 999
        self.assertNotEqual(first.fingerprint, check(report).fingerprint)

    def test_plan_fingerprint_binds_applied_history_checksums(self):
        first = check(example())
        report = example()
        report["migrations"][0]["checksum"] += 1
        self.assertNotEqual(first.fingerprint, check(report).fingerprint)
        report = example()
        report["migrations"][2]["filepath"] = "/flyway/sql/V135__new_approved_feature.sql"
        self.assertNotEqual(first.fingerprint, check(report).fingerprint)

    def test_legacy_exception_requires_exact_matching_history_row(self):
        exception = {
            "state": "Deleted", "category": "Repeatable", "type": "SQL",
            "version": "", "description": "seed dev data", "filepath": "",
        }
        report = example()
        report["migrations"].insert(1, copy.deepcopy(exception))
        check(report, allowed_exceptions=[exception])
        with self.assertRaises(MigrationPlanError):
            check(report)
        with self.assertRaises(MigrationPlanError):
            check(example(), allowed_exceptions=[exception])

    def test_empty_database_requires_explicit_bootstrap(self):
        report = {"schemaVersion": None, "migrations": [
            {"state": "Pending", "category": "Versioned", "type": "SQL",
             "version": "1", "description": "initial tables",
             "filepath": "/flyway/sql/V1__initial_tables.sql", "checksum": 12},
        ]}
        with self.assertRaises(MigrationPlanError):
            validate_forward_plan(json.dumps(report))
        plan = validate_forward_plan(json.dumps(report), allow_initial_schema=True)
        self.assertIsNone(plan.source_version)
        self.assertEqual(plan.target_version, "1")

    def test_applied_versioned_history_requires_checksum_and_cannot_be_ahead(self):
        report = example()
        report["migrations"][0]["checksum"] = None
        with self.assertRaises(MigrationPlanError):
            check(report)
        report = example()
        report["migrations"][0]["version"] = "134"
        with self.assertRaises(MigrationPlanError):
            check(report)

    def test_postflight_requires_all_planned_checksums_and_no_remaining_pending(self):
        planned = check(example())
        report = example()
        report["schemaVersion"] = "136"
        report["migrations"] = [
            {"version": "133", "state": "Success", "category": "Versioned",
             "type": "SQL", "description": "existing migration",
             "filepath": "/flyway/sql/V133__existing_migration.sql", "checksum": 231},
            *[
                {"version": version, "state": "Success", "category": "Versioned",
                 "type": "SQL", "description": description,
                 "filepath": f"/flyway/sql/{filename}", "checksum": checksum}
                for version, filename, description, checksum in planned.pending
            ],
        ]
        validated = validate_migration_result(
            json.dumps(report), planned,
            expected_flyway_version="11.14.1",
            expected_schema_name="roman_lms",
        )
        self.assertEqual(validated.source_version, "136")
        self.assertFalse(validated.needs_migration)

        report["migrations"][-1]["checksum"] += 1
        with self.assertRaises(MigrationPlanError):
            validate_migration_result(json.dumps(report), planned)
        report["migrations"][-1]["state"] = "Pending"
        with self.assertRaises(MigrationPlanError):
            validate_migration_result(json.dumps(report), planned)


if __name__ == "__main__":
    unittest.main()
