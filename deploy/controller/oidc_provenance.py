"""Verify GitHub Actions OIDC evidence bound to immutable image digests."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any


ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
AUDIENCE_PREFIX = "roman-platform-v1:"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ProvenanceError(ValueError):
    """Raised when a GitHub-issued build identity cannot be verified."""


def canonical_build_digest(images: dict[str, str], build_spec_sha256: str, source_sha: str) -> str:
    if not isinstance(images, dict) or not images or not SHA_RE.fullmatch(source_sha):
        raise ProvenanceError("image identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", build_spec_sha256):
        raise ProvenanceError("build specification identity is invalid")
    for name, image in images.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
            raise ProvenanceError("image component identity is invalid")
        if not isinstance(image, str) or not re.fullmatch(r"ghcr\.io/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}", image):
            raise ProvenanceError("image digest reference is invalid")
    payload = json.dumps(
        {"build_spec_sha256": build_spec_sha256, "images": images, "source_sha": source_sha},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def audience_for(images: dict[str, str], build_spec_sha256: str, source_sha: str) -> str:
    return AUDIENCE_PREFIX + canonical_build_digest(images, build_spec_sha256, source_sha)


def _decode_segment(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ProvenanceError("token encoding is invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise ProvenanceError("token encoding is invalid") from exc


def _json_segment(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode_segment(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("token JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise ProvenanceError("token JSON is invalid")
    return decoded


def fetch_jwks() -> dict[str, Any]:
    request = urllib.request.Request(
        JWKS_URL,
        headers={"Accept": "application/json", "User-Agent": "roman-platform-provenance-verifier"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(256 * 1024))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ProvenanceError("GitHub OIDC signing keys are unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise ProvenanceError("GitHub OIDC signing keys are invalid")
    return payload


def _verify_signature(signing_input: bytes, signature: bytes, jwk: dict[str, Any]) -> None:
    if jwk.get("kty") != "RSA" or jwk.get("alg") != "RS256" or jwk.get("use") not in {None, "sig"}:
        raise ProvenanceError("token signing key type is unsupported")
    try:
        modulus = int.from_bytes(_decode_segment(jwk["n"]), "big")
        exponent = int.from_bytes(_decode_segment(jwk["e"]), "big")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvenanceError("token signing key is invalid") from exc
    if modulus.bit_length() < 2048 or exponent < 3 or exponent % 2 == 0:
        raise ProvenanceError("token signing key is too weak")
    width = (modulus.bit_length() + 7) // 8
    signature_value = int.from_bytes(signature, "big")
    if len(signature) != width or signature_value >= modulus:
        raise ProvenanceError("token signature is invalid")
    encoded_message = pow(signature_value, exponent, modulus).to_bytes(width, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    padding_size = width - len(digest_info) - 3
    if padding_size < 8:
        raise ProvenanceError("token signature is invalid")
    expected = b"\x00\x01" + b"\xff" * padding_size + b"\x00" + digest_info
    if not hmac.compare_digest(encoded_message, expected):
        raise ProvenanceError("token signature is invalid")


def verify_github_token(token: str, audience: str, *, now: int | None = None) -> dict[str, Any]:
    if not isinstance(token, str) or len(token) > 12_000 or token.count(".") != 2:
        raise ProvenanceError("GitHub OIDC token is missing or malformed")
    header_segment, claims_segment, signature_segment = token.split(".")
    header = _json_segment(header_segment)
    claims = _json_segment(claims_segment)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ProvenanceError("GitHub OIDC token signing metadata is invalid")
    jwks = fetch_jwks()
    keys = [item for item in jwks["keys"] if isinstance(item, dict) and item.get("kid") == header["kid"]]
    if len(keys) != 1:
        raise ProvenanceError("GitHub OIDC signing key is not recognized")
    _verify_signature(
        f"{header_segment}.{claims_segment}".encode("ascii"),
        _decode_segment(signature_segment),
        keys[0],
    )
    current = int(time.time()) if now is None else now
    issued = claims.get("iat")
    not_before = claims.get("nbf", issued)
    expiry = claims.get("exp")
    if (
        claims.get("iss") != ISSUER
        or claims.get("aud") != audience
        or isinstance(issued, bool)
        or not isinstance(issued, int)
        or isinstance(not_before, bool)
        or not isinstance(not_before, int)
        or isinstance(expiry, bool)
        or not isinstance(expiry, int)
        or issued > current + 30
        or not_before > current + 30
        or expiry <= current
        or expiry <= issued
        or expiry - issued > 900
    ):
        raise ProvenanceError("GitHub OIDC token issuer, audience, or lifetime is invalid")
    return claims


def verify_release_provenance(
    token: str,
    request: dict[str, Any],
    app: dict[str, Any],
    images: dict[str, str],
    build_spec_sha256: str,
) -> dict[str, Any]:
    release = request.get("release")
    if not isinstance(release, dict):
        raise ProvenanceError("release identity is unavailable")
    audience = audience_for(images, build_spec_sha256, request.get("commit_sha", ""))
    claims = verify_github_token(token, audience)
    mode = release.get("mode", "manual")
    event = app.get("automatic_workflow_event") if mode == "automatic" else app.get("workflow_event", "workflow_dispatch")
    branch = app.get("production_branch")
    workflow_ref = f"{app['workflow_repository']}/{app['workflow_path']}@refs/heads/{branch}"
    job_workflow_ref = claims.get("job_workflow_ref")
    job_path = app.get("reusable_workflow_path", "").split("@", 1)[0]
    expected = {
        "repository": app["workflow_repository"],
        "ref": f"refs/heads/{branch}",
        "sha": request.get("workflow_sha"),
        "run_id": release.get("run_id"),
        "run_attempt": release.get("run_attempt"),
        "event_name": event,
        "workflow_ref": workflow_ref,
        "actor": release.get("actor"),
        "job_workflow_sha": request.get("platform_sha"),
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise ProvenanceError("GitHub OIDC claims do not match the authorized release run")
    if (
        not isinstance(job_workflow_ref, str)
        or not job_workflow_ref.startswith(job_path + "@")
        or claims.get("repository_owner") != app["workflow_repository"].split("/", 1)[0]
    ):
        raise ProvenanceError("GitHub OIDC reusable workflow identity is not authorized")
    return claims
