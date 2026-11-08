import hashlib
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("phase6_setup", ROOT / "deploy" / "staging" / "prepare_romanlms_phase6.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class StagingEnvironmentTests(unittest.TestCase):
    def test_runtime_disables_external_side_effects_and_trusts_only_the_private_proxy(self):
        runtime = setup.runtime_environment("synthetic-db-password", "synthetic-object-key", "synthetic-object-secret")

        self.assertEqual(runtime["ROMANLMS_METRICS_TRUSTED_PROXY_ADDRESSES"], "172.30.66.1")
        self.assertEqual(runtime["APP_RUNTIME_ENV"], "phase6-staging")
        self.assertEqual(runtime["ROMANLMS_MAIL_ENABLED"], "false")
        self.assertEqual(runtime["ROMANLMS_NOTIFICATIONS_FCM_ENABLED"], "false")
        self.assertEqual(runtime["ROMANLMS_SCHEDULING_ENABLED"], "true")
        self.assertEqual(runtime["SPRING_FLYWAY_ENABLED"], "false")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0, "requires an isolated Linux root test container")
class StagingSetupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.dump"
        self.source.write_bytes(b"synthetic backup")
        self.source.chmod(0o600)
        self.config = self.root / "config"
        self.config.mkdir(mode=0o700)
        self.patches = [
            mock.patch.object(setup, "SOURCE", self.source),
            mock.patch.object(setup, "SOURCE_SHA256", hashlib.sha256(b"synthetic backup").hexdigest()),
            mock.patch.object(setup, "DB_ROOT", self.root / "new-db"),
            mock.patch.object(setup, "OBJECT_ROOT", self.root / "new-objects"),
            mock.patch.object(setup, "CONFIG", self.config),
            mock.patch.object(setup.os.path, "ismount", return_value=True),
            mock.patch.object(setup.shutil, "disk_usage", return_value=type("Usage", (), {"free": 20 * 1024**3})()),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def fake_command(args, **kwargs):
        if args[:3] == ["ip", "-j", "-4"]:
            return subprocess.CompletedProcess(args, 0, b'[{"dst":"default"}]', b"")
        if args[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(args, 0, b"/srv/apps/docker\n", b"")
        if args[0] == "findmnt":
            return subprocess.CompletedProcess(args, 0, b"tmpfs\n", b"")
        if args[:2] in (["docker", "inspect"], ["docker", "network"]):
            return subprocess.CompletedProcess(args, 1, b"", b"")
        raise AssertionError("Unexpected external operation")

    def test_private_file_is_exclusive_and_root_only(self):
        path = self.config / "test.env"
        setup.write_private(path, {"DUMMY": "synthetic"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            setup.write_private(path, {"DUMMY": "replacement"})
        self.assertEqual(path.read_text(), "DUMMY=synthetic\n")

    def test_existing_destination_is_never_overwritten(self):
        (self.root / "new-db").mkdir()
        with mock.patch.object(setup, "command", side_effect=self.fake_command):
            with self.assertRaisesRegex(setup.SetupError, "already exists"):
                setup.ensure_fresh()

    def test_source_checksum_mismatch_stops_before_service_creation(self):
        self.source.write_bytes(b"changed")
        with mock.patch.object(setup, "command", side_effect=self.fake_command) as command:
            with self.assertRaisesRegex(setup.SetupError, "checksum"):
                setup.ensure_fresh()
        self.assertFalse(any(call.args[0][:2] == ["docker", "run"] for call in command.call_args_list))

    def test_conflicting_host_route_is_rejected(self):
        def conflicting(args, **kwargs):
            if args[:3] == ["ip", "-j", "-4"]:
                return subprocess.CompletedProcess(args, 0, b'[{"dst":"172.30.0.0/16"}]', b"")
            return self.fake_command(args, **kwargs)
        with mock.patch.object(setup, "command", side_effect=conflicting):
            with self.assertRaisesRegex(setup.SetupError, "conflicts"):
                setup.ensure_fresh()
