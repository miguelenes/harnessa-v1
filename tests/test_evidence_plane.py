import hashlib
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.evidence import EvidenceFixtureError, validate_harness2_fixture
from harnessa.api.evidence_routes import _cursor, _cursor_text, _record
from harnessa.contracts import (
    EvidenceValueStatus,
    Harness2ImportFixture,
)
from harnessa.main import app


def test_evidence_cursor_round_trips_and_rejects_invalid_values():
    timestamp = datetime(2026, 10, 23, tzinfo=UTC)
    identifier = uuid4()
    assert _cursor(_cursor_text(timestamp, identifier)) == (timestamp, identifier)
    with pytest.raises(HTTPException, match="Invalid evidence cursor"):
        _cursor("not-a-cursor")


def test_evidence_endpoint_requires_authenticated_project_membership():
    with TestClient(app) as client:
        response = client.get(f"/projects/{uuid4()}/evidence")
    assert response.status_code == 401


def test_evidence_record_preserves_unknowns_and_never_fabricates_cost():
    row = _record(
        source="litellm.gateway_ledger",
        source_id="gateway-record-1",
        kind="model_request",
        summary="usage=unknown; gateway_billed=unknown",
        recorded_at=datetime(2026, 10, 23, tzinfo=UTC),
        organization_id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
    )
    assert row.envelope.content_sha256.status == EvidenceValueStatus.UNKNOWN
    assert row.envelope.run_id.status == EvidenceValueStatus.UNKNOWN
    assert row.summary.endswith("gateway_billed=unknown")


def _fixture(content: bytes) -> Harness2ImportFixture:
    return Harness2ImportFixture(
        schema_version=1,
        source_revision="1cfe254e8d25b15f37048c3730134686abe60335",
        benchmark="lab",
        benchmark_task_id="task-17",
        executor="codex",
        recursion_mode="parallel",
        k=3,
        seed=17,
        result="passed",
        candidate_revision={"status": "known", "value": "candidate-a"},
        judge_revision={"status": "unknown"},
        artifacts=[
            {
                "path": "runs/lab/result.json",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "evidence_kind": "benchmark_result",
            }
        ],
    )


def test_harness2_fixture_checks_exact_file_set_and_content_digest():
    content = b'{"score": 0.75}'
    fixture = _fixture(content)
    assert validate_harness2_fixture(fixture, {"runs/lab/result.json": content}) == {
        "runs/lab/result.json": hashlib.sha256(content).hexdigest()
    }
    with pytest.raises(EvidenceFixtureError, match="checksum mismatch"):
        validate_harness2_fixture(fixture, {"runs/lab/result.json": b"changed"})
    with pytest.raises(EvidenceFixtureError, match="file set"):
        validate_harness2_fixture(fixture, {})


def test_harness2_fixture_rejects_traversal_and_trust_elevation():
    content = b"x"
    fixture = _fixture(content).model_dump()
    fixture["artifacts"][0]["path"] = "../outside.json"
    with pytest.raises(ValueError, match="relative"):
        Harness2ImportFixture.model_validate(fixture)
    fixture = _fixture(content).model_dump()
    fixture["trust"] = "trusted"
    with pytest.raises(ValueError, match="cannot be elevated"):
        Harness2ImportFixture.model_validate(fixture)


def test_artifact_reference_integrity_check_is_bounded_and_non_mutating():
    with TemporaryDirectory() as directory:
        store = LocalArtifactStore(Path(directory))
        content = b"artifact bytes"
        stored = store.put_bytes(content, media_type="text/plain")
        assert (
            store.verify_reference(
                stored.storage_key, expected_digest=stored.digest, expected_size=stored.size_bytes
            )
            == "verified"
        )
        assert (
            store.verify_reference(
                stored.storage_key,
                expected_digest=stored.digest,
                expected_size=stored.size_bytes,
                max_bytes=1,
            )
            == "over_limit"
        )
        assert (
            store.verify_reference(
                "sha256/../outside", expected_digest=stored.digest, expected_size=stored.size_bytes
            )
            == "invalid_reference"
        )
