import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("phase6_start", ROOT / "deploy" / "staging" / "start_romanlms_phase6.py")
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)


class StagingStartupSafetyTests(unittest.TestCase):
    def test_requires_correct_identity_network_image_and_bind_mount(self):
        mounts = [{"Type": "bind", "Source": "/srv/databases/romanlms-phase6/postgres", "Destination": "/var/lib/postgresql/data", "RW": True}]
        fields = ["romanlms-phase6", "database", start.NETWORK, "approved-image", json.dumps(mounts), "no", "null", "false"]
        with mock.patch.object(start, "run", return_value="|".join(fields)), mock.patch.object(start.os.path, "realpath", return_value=mounts[0]["Source"]):
            self.assertFalse(start.verify_service("reserved-name", "database", mounts[0]["Source"], mounts[0]["Destination"], "approved-image"))
        fields[3] = "unapproved-image"
        with mock.patch.object(start, "run", return_value="|".join(fields)):
            with self.assertRaisesRegex(start.StartError, "identity"):
                start.verify_service("reserved-name", "database", mounts[0]["Source"], mounts[0]["Destination"], "approved-image")

    def test_rejects_storage_redirected_through_symlink(self):
        source = "/srv/databases/romanlms-phase6/postgres"
        mounts = [{"Type": "bind", "Source": source, "Destination": "/var/lib/postgresql/data", "RW": True}]
        fields = ["romanlms-phase6", "database", start.NETWORK, "approved-image", json.dumps(mounts), "no", "null", "true"]
        with mock.patch.object(start, "run", return_value="|".join(fields)), mock.patch.object(start.os.path, "realpath", return_value="/unapproved/path"):
            with self.assertRaisesRegex(start.StartError, "storage mount"):
                start.verify_service("reserved-name", "database", source, "/var/lib/postgresql/data", "approved-image")
