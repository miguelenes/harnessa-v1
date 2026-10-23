"""Deterministic validation for minimized external evidence manifests."""

from __future__ import annotations

import hashlib

from harnessa.contracts import Harness2ImportFixture


class EvidenceFixtureError(ValueError):
    """Raised when fixture bytes do not match their declared provenance."""


def validate_harness2_fixture(
    fixture: Harness2ImportFixture, files: dict[str, bytes]
) -> dict[str, str]:
    """Check exact file set, sizes, and hashes without trusting imported metadata."""
    expected = {item.path: item for item in fixture.artifacts}
    if set(files) != set(expected):
        raise EvidenceFixtureError("Harness² fixture file set differs from its manifest")
    digests: dict[str, str] = {}
    for path, content in files.items():
        item = expected[path]
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != item.size_bytes or digest != item.sha256:
            raise EvidenceFixtureError(f"Harness² fixture checksum mismatch: {path}")
        digests[path] = digest
    return digests
