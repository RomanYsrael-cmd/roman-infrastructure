"""Regression tests for the opt-in, version-agnostic migration planner."""
import copy
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_checksum_binding_accepts_historical_case_and_space_descriptions(self):
        report = {
            "schemaVersion": "34",
            "flywayVersion": "11.14.1",
            "schemaName": "roman_lms",
            "migrations": [
                {"version": "34", "state": "Success", "category": "Versioned",
                 "type": "SQL", "description": "prior migration",
                 "filepath": "/flyway/sql/V34__prior_migration.sql", "checksum": None},
                {"version": "35", "state": "Pending", "category": "Versioned",
                 "type": "SQL", "description": "file derivative rerun",
                 "filepath": "/flyway/sql/V35__file_derivative rerun.sql", "checksum": None},
            ],
        }
        history = [{
            "version": "34", "description": "prior migration", "type": "SQL",
            "script": "V34__prior_migration.sql", "checksum": 340, "success": True,
        }]
        enriched = bind_flyway_checksums(
            json.dumps(report), json.dumps(history),
            {"V34__prior_migration.sql": 340, "V35__file_derivative rerun.sql": 350},
        )
        self.assertEqual(
            check(json.loads(enriched), max_pending=1).pending,
            (("35", "V35__file_derivative rerun.sql", "file derivative rerun", 350),),
        )

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

    def test_migration_artifact_accepts_historical_descriptions_and_linked_script_sidecars(self):
        with tempfile.TemporaryDirectory(prefix="roman-flyway-historical-") as temp:
            directory = Path(temp)
            historical = {
                "V35__file_derivative rerun.sql": "SELECT 35;\n",
                "V9__phase1_refresh_tokensV8Rerun.sql": "SELECT 9;\n",
                "V87__module_content_ownership_indexes.sql": "SELECT 87;\n",
                "V135__versioned_paid_feature_registry.sql": "SELECT 135;\n",
                "V136__generalize_paid_feature_entitlement_keys.sql": "SELECT 136;\n",
            }
            for filename, contents in historical.items():
                (directory / filename).write_text(contents, encoding="utf-8")
            (directory / "V87__module_content_ownership_indexes.sql.conf").write_text(
                "executeInTransaction=true\n", encoding="utf-8"
            )

            checksums = migration_artifact_checksums(directory)

            self.assertEqual(set(checksums), set(historical))
            self.assertTrue(all(isinstance(checksum, int) for checksum in checksums.values()))
            self.assertNotIn("V87__module_content_ownership_indexes.sql.conf", checksums)

    def test_flyway_info_accepts_historical_descriptions_and_preserves_identity(self):
        report = {
            "schemaVersion": "34",
            "flywayVersion": "11.14.1",
            "schemaName": "roman_lms",
            "migrations": [
                {"version": "34", "state": "Success", "category": "Versioned",
                 "type": "SQL", "description": "previous migration",
                 "filepath": "/flyway/sql/V34__previous_migration.sql", "checksum": 234},
                {"version": "35", "state": "Pending", "category": "Versioned",
                 "type": "SQL", "description": "file derivative rerun",
                 "filepath": "/flyway/sql/V35__file_derivative rerun.sql", "checksum": 350},
            ],
        }
        plan = check(report)
        self.assertEqual(
            plan.pending,
            (("35", "V35__file_derivative rerun.sql", "file derivative rerun", 350),),
        )

        legacy = copy.deepcopy(report)
        legacy["schemaVersion"] = "8"
        legacy["migrations"] = [
            {"version": "8", "state": "Success", "category": "Versioned",
             "type": "SQL", "description": "phase1 authentication",
             "filepath": "/flyway/sql/V8__phase1_authentication.sql", "checksum": 80},
            {"version": "9", "state": "Pending", "category": "Versioned",
             "type": "SQL", "description": "phase1 refresh tokensV8Rerun",
             "filepath": "/flyway/sql/V9__phase1_refresh_tokensV8Rerun.sql", "checksum": 90},
        ]
        self.assertEqual(
            check(legacy).pending,
            (("9", "V9__phase1_refresh_tokensV8Rerun.sql", "phase1 refresh tokensV8Rerun", 90),),
        )

    def test_migration_artifact_rejects_duplicate_versions_and_invalid_identities(self):
        for filenames in (
            ("V134__first.sql", "V134__second.sql"),
            ("V134__first.sql", "V134_0__second.sql"),
            ("V01__bad.sql",),
            ("V1_02__bad_version.sql",),
            ("V1..2__bad_version.sql",),
            ("V134__Bad-Case.sql",),
            ("V134__bad.sql.txt",),
            ("V134__bad.sql.bak",),
        ):
            with tempfile.TemporaryDirectory(prefix="roman-flyway-identity-") as temp:
                directory = Path(temp)
                for filename in filenames:
                    (directory / filename).write_text("SELECT 1;\n", encoding="utf-8")
                with self.subTest(filenames=filenames), self.assertRaises(MigrationPlanError):
                    migration_artifact_checksums(directory)

    def test_migration_artifact_rejects_orphan_and_malformed_sidecars_and_unexpected_files(self):
        cases = (
            {"V87__module_content_ownership_indexes.sql.conf": "executeInTransaction=true\n"},
            {"V01__invalid.sql": "SELECT 1;\n", "V01__invalid.sql.conf": "executeInTransaction=true\n"},
            {"V1__migration.conf": "executeInTransaction=true\n"},
            {"README.md": "not a Flyway migration\n"},
            {"R__repeatable.sql": "SELECT 1;\n"},
        )
        for files in cases:
            with tempfile.TemporaryDirectory(prefix="roman-flyway-sidecar-") as temp:
                directory = Path(temp)
                for filename, contents in files.items():
                    (directory / filename).write_text(contents, encoding="utf-8")
                with self.subTest(files=files), self.assertRaises(MigrationPlanError):
                    migration_artifact_checksums(directory)

    def test_migration_artifact_rejects_symlinks_and_nested_directories(self):
        with tempfile.TemporaryDirectory(prefix="roman-flyway-links-") as temp:
            directory = Path(temp)
            target = directory / "V2__target.sql"
            target.write_text("SELECT 1;\n", encoding="utf-8")
            link = directory / "V1__linked.sql"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                link.write_text("SELECT 2;\n", encoding="utf-8")
                original_is_symlink = Path.is_symlink
                with patch.object(
                    Path,
                    "is_symlink",
                    lambda path: path.name == link.name or original_is_symlink(path),
                ):
                    with self.assertRaises(MigrationPlanError):
                        migration_artifact_checksums(directory)
            else:
                with self.assertRaises(MigrationPlanError):
                    migration_artifact_checksums(directory)

        with tempfile.TemporaryDirectory(prefix="roman-flyway-nested-") as temp:
            directory = Path(temp)
            (directory / "nested").mkdir()
            with self.assertRaises(MigrationPlanError):
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
            lambda row: row.update(filepath="/flyway/sql/V134__add_attendance_metadata.sql.conf"),
            lambda row: row.update(filepath="/flyway/sql/V134__add_attendance_metadata.txt"),
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

    def test_rejects_duplicate_successful_versions_and_traversing_migration_paths(self):
        report = example()
        report["migrations"].insert(1, {
            "version": "133.0", "state": "Success", "category": "Versioned",
            "type": "SQL", "description": "different description",
            "filepath": "/flyway/sql/V133_0__different_description.sql", "checksum": 232,
        })
        with self.assertRaises(MigrationPlanError):
            check(report)

        for path in (
            "/flyway/sql/../V134__add_attendance_metadata.sql",
            "/flyway/sql/subdir/V134__add_attendance_metadata.sql",
            "/flyway/sql\\V134__add_attendance_metadata.sql",
        ):
            report = example()
            report["migrations"][1]["filepath"] = path
            with self.subTest(path=path), self.assertRaises(MigrationPlanError):
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
        report["migrations"][2]["description"] = "new approved feature"
        self.assertNotEqual(first.fingerprint, check(report).fingerprint)

    def test_plan_fingerprint_is_stable_for_identical_historical_identity_and_checksums(self):
        report = {
            "schemaVersion": "34",
            "flywayVersion": "11.14.1",
            "schemaName": "roman_lms",
            "migrations": [
                {"version": "34", "state": "Success", "category": "Versioned",
                 "type": "SQL", "description": "prior migration",
                 "filepath": "/flyway/sql/V34__prior_migration.sql", "checksum": 340},
                {"version": "35", "state": "Pending", "category": "Versioned",
                 "type": "SQL", "description": "file derivative rerun",
                 "filepath": "/flyway/sql/V35__file_derivative rerun.sql", "checksum": 350},
            ],
        }
        first = check(report)
        self.assertEqual(first.fingerprint, check(copy.deepcopy(report)).fingerprint)
        changed = copy.deepcopy(report)
        changed["migrations"][1]["checksum"] += 1
        self.assertNotEqual(first.fingerprint, check(changed).fingerprint)

    def test_missing_pending_sql_checksum_and_modified_applied_checksum_fail_closed(self):
        report = example()
        report["migrations"] = report["migrations"][:2]
        report["migrations"][0]["checksum"] = None
        report["migrations"][1]["checksum"] = None
        history = [{
            "version": "133", "description": "existing migration", "type": "SQL",
            "script": "V133__existing_migration.sql", "checksum": 231, "success": True,
        }]
        with self.assertRaises(MigrationPlanError):
            bind_flyway_checksums(
                json.dumps(report), json.dumps(history),
                {"V133__existing_migration.sql": 231},
            )
        with self.assertRaises(MigrationPlanError):
            bind_flyway_checksums(
                json.dumps(report), json.dumps(history),
                {"V133__existing_migration.sql": 232,
                 "V134__add_attendance_metadata.sql": -321},
            )

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
