from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "controller"))
import oidc_provenance as provenance  # noqa: E402


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class OidcProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sha = "a" * 40
        self.platform_sha = "b" * 40
        self.build_hash = "c" * 64
        self.images = {
            "backend": "ghcr.io/example/backend@sha256:" + "d" * 64,
            "frontend": "ghcr.io/example/frontend@sha256:" + "e" * 64,
            "migrator": "ghcr.io/example/migrator@sha256:" + "f" * 64,
        }
        self.app = {
            "repository": "Example/full-stack",
            "workflow_repository": "Example/full-stack",
            "workflow_path": ".github/workflows/deploy.yml",
            "reusable_workflow_path": "RomanYsrael-cmd/roman-infrastructure/.github/workflows/deploy-application.yml",
            "production_branch": "main",
            "workflow_event": "workflow_dispatch",
        }
        self.request = {
            "commit_sha": self.sha,
            "workflow_sha": self.sha,
            "platform_sha": self.platform_sha,
            "release": {"run_id": "123", "run_attempt": "2", "actor": "Example", "mode": "manual"},
        }

    def test_image_audience_is_stable_and_binds_every_digest_and_source_identity(self) -> None:
        first = provenance.audience_for(self.images, self.build_hash, self.sha)
        self.assertEqual(first, provenance.audience_for(dict(reversed(list(self.images.items()))), self.build_hash, self.sha))
        self.assertNotEqual(first, provenance.audience_for({**self.images, "backend": "ghcr.io/example/backend@sha256:" + "9" * 64}, self.build_hash, self.sha))
        self.assertNotEqual(first, provenance.audience_for(self.images, "9" * 64, self.sha))
        self.assertNotEqual(first, provenance.audience_for(self.images, self.build_hash, "9" * 40))

    def test_rejects_mutable_or_non_ghcr_image_references(self) -> None:
        for value in ("ghcr.io/example/backend:latest", "docker.io/example/backend@sha256:" + "d" * 64):
            with self.subTest(image=value), self.assertRaises(provenance.ProvenanceError):
                provenance.audience_for({"main": value}, self.build_hash, self.sha)

    def test_github_jwt_requires_recognized_signature_before_claims_are_trusted(self) -> None:
        now = int(time.time())
        header = _b64(json.dumps({"alg": "RS256", "kid": "key-1"}, separators=(",", ":")).encode())
        payload = _b64(json.dumps({
            "iss": provenance.ISSUER,
            "aud": provenance.audience_for(self.images, self.build_hash, self.sha),
            "iat": now,
            "nbf": now,
            "exp": now + 300,
        }, separators=(",", ":")).encode())
        token = f"{header}.{payload}.{_b64(bytes(256))}"
        modulus = ((1 << 2047) + 0x12345).to_bytes(256, "big")
        jwks = {"keys": [{"kid": "key-1", "kty": "RSA", "alg": "RS256", "use": "sig", "n": _b64(modulus), "e": _b64((65537).to_bytes(3, "big"))}]}
        with mock.patch.object(provenance, "fetch_jwks", return_value=jwks):
            with self.assertRaisesRegex(provenance.ProvenanceError, "signature"):
                provenance.verify_github_token(token, provenance.audience_for(self.images, self.build_hash, self.sha), now=now)

    def test_release_claims_bind_caller_and_pinned_reusable_workflow(self) -> None:
        job_path = self.app["reusable_workflow_path"]
        claims = {
            "repository": self.app["workflow_repository"],
            "repository_owner": "Example",
            "ref": "refs/heads/main",
            "sha": self.sha,
            "run_id": "123",
            "run_attempt": "2",
            "event_name": "workflow_dispatch",
            "workflow_ref": "Example/full-stack/.github/workflows/deploy.yml@refs/heads/main",
            "actor": "Example",
            "job_workflow_ref": job_path + "@" + self.platform_sha,
            "job_workflow_sha": self.platform_sha,
        }
        with mock.patch.object(provenance, "verify_github_token", return_value=claims) as verify:
            result = provenance.verify_release_provenance("token", self.request, self.app, self.images, self.build_hash)
        self.assertEqual(result, claims)
        verify.assert_called_once_with(
            "token", provenance.audience_for(self.images, self.build_hash, self.sha)
        )

        with mock.patch.object(provenance, "verify_github_token", return_value={**claims, "job_workflow_sha": "9" * 40}):
            with self.assertRaisesRegex(provenance.ProvenanceError, "claims"):
                provenance.verify_release_provenance("token", self.request, self.app, self.images, self.build_hash)


if __name__ == "__main__":
    unittest.main()
