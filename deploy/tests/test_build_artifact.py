from __future__ import annotations

import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
import build_artifact  # noqa: E402
from build_artifact import BuildArtifactError  # noqa: E402


class BuildArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = ROOT / "examples" / "application-profiles.json"
        cls.app = json.loads(cls.registry.read_text(encoding="utf-8"))["applications"]["sample-build-only"]

    def test_runs_registered_toolchain_and_publishes_source_bound_checksummed_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="roman-build-artifact-") as temp:
            work = Path(temp)
            source, platform = work / "source", work / "platform"
            source.mkdir()
            platform.mkdir()
            artifact_directory = source / "app" / "build" / "outputs" / "apk" / "debug"
            artifact_directory.mkdir(parents=True)
            output, archive = work / "output", work / "release.zip"

            def fake_run(command, *, cwd, env, check, timeout):
                self.assertEqual(command, ["registered-test-and-build"])
                self.assertEqual(cwd, source)
                self.assertEqual(env["SOURCE_DATE_EPOCH"], "0")
                (artifact_directory / "debug.apk").write_bytes(b"immutable android artifact")
                return mock.Mock(returncode=0)

            args = Namespace(
                registry=self.registry,
                app_id="sample-build-only",
                source_sha="a" * 40,
                platform_sha=self.app["allowed_platform_shas"][0],
                source_root=source,
                platform_root=platform,
                output=output,
                archive=archive,
            )
            with mock.patch.object(build_artifact, "_git_head", side_effect=[args.source_sha, args.platform_sha]), \
                 mock.patch.object(build_artifact, "_commands_for", return_value=[["registered-test-and-build"]]), \
                 mock.patch.object(build_artifact.subprocess, "run", side_effect=fake_run):
                result = build_artifact.build_artifact(args)

            self.assertEqual(result["source_sha"], args.source_sha)
            self.assertEqual(result["build_system"], "gradle-android")
            self.assertEqual(result["artifacts"][0]["path"], "app/build/outputs/apk/debug/debug.apk")
            self.assertEqual(len(result["archive_sha256"]), 64)
            self.assertTrue(archive.is_file())
            self.assertTrue((output / "SHA256SUMS").is_file())

    def test_rejects_checkout_revision_mismatch_before_running_build_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="roman-build-mismatch-") as temp:
            work = Path(temp)
            source, platform = work / "source", work / "platform"
            source.mkdir()
            platform.mkdir()
            args = Namespace(
                registry=self.registry,
                app_id="sample-build-only",
                source_sha="a" * 40,
                platform_sha=self.app["allowed_platform_shas"][0],
                source_root=source,
                platform_root=platform,
                output=work / "output",
                archive=work / "release.zip",
            )
            with mock.patch.object(build_artifact, "_git_head", return_value="b" * 40), \
                 mock.patch.object(build_artifact, "_commands_for") as commands:
                with self.assertRaisesRegex(BuildArtifactError, "immutable revisions"):
                    build_artifact.build_artifact(args)
            commands.assert_not_called()

    def test_rejects_artifact_symlink_that_escapes_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="roman-build-symlink-") as temp:
            root = Path(temp)
            project = root / "project"
            outside = root / "outside.bin"
            project.mkdir()
            outside.write_bytes(b"outside")
            try:
                (project / "escape.bin").symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks are unavailable on this runner: {exc}")
            with self.assertRaisesRegex(BuildArtifactError, "regular file inside"):
                build_artifact._collect_artifacts(project, ["*.bin"])

    def test_registered_toolchains_require_lockfiles_and_wrappers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="roman-build-toolchains-") as temp:
            project = Path(temp)
            with self.assertRaisesRegex(BuildArtifactError, "Gradle wrapper"):
                build_artifact._commands_for("gradle-jvm", project)
            (project / "package-lock.json").write_text("{}", encoding="utf-8")
            self.assertEqual(len(build_artifact._commands_for("npm", project)), 3)
            with self.assertRaisesRegex(BuildArtifactError, "pyproject.toml"):
                build_artifact._commands_for("python", project)


if __name__ == "__main__":
    unittest.main()
