from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from harnessa.services.verification_evidence import (
    VerificationEvidenceInput,
    record_verification_evidence,
)


def _evidence(**changes) -> VerificationEvidenceInput:
    value = VerificationEvidenceInput(
        organization_id=uuid4(),
        project_id=uuid4(),
        repository_id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        attempt=1,
        fencing_token=1,
        source_record_id="fixture-record",
        source_revision="a" * 40,
        outcome="passed",
        summary="The approved criterion passed.",
        criteria=({"criterion_id": "R-1", "outcome": "passed", "summary": "Evidence recorded."},),
        verifier_id="fixture-verifier",
    )
    return replace(value, **changes)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source_record_id": " "}, "Invalid verifier source record ID"),
        ({"source_revision": " "}, "Invalid source revision"),
        ({"summary": " "}, "Verification summary must be 1–2000 characters"),
        (
            {"summary": "api_key=fixture-secret-value"},
            "Verification summary contains secret-like content",
        ),
        ({"verifier_id": " "}, "Invalid verifier identity"),
        ({"verifier_id": "v" * 121}, "Invalid verifier identity"),
        ({"source_record_id": "r" * 201}, "Invalid verifier source record ID"),
        ({"source_revision": "s" * 201}, "Invalid source revision"),
        ({"summary": "s" * 2001}, "Verification summary must be 1–2000 characters"),
        ({"criteria": ()}, "Verification criteria must contain 1–100 results"),
        ({"criteria": tuple({"criterion_id": f"R-{i}", "outcome": "passed", "summary": "ok"} for i in range(101))}, "Verification criteria must contain 1–100 results"),
        ({"criteria": ({"criterion_id": "R-1"},)}, "Invalid criterion result"),
        (
            {"criteria": ({"criterion_id": "R-1", "outcome": "other", "summary": "x"},)},
            "Invalid criterion result",
        ),
        ({"criteria": ({"criterion_id": "R-1", "outcome": "passed", "summary": "x" * 501},)}, "Invalid criterion result"),
        ({"criteria": ({"criterion_id": "R-1", "outcome": "passed", "summary": "token=fixture-secret"},)}, "Verification criteria contain secret-like content"),
        ({"artifact_digest": "not-a-digest"}, "Artifact digest must be a lowercase SHA-256"),
    ],
)
async def test_verification_evidence_rejects_invalid_untrusted_records(changes, message):
    with pytest.raises(HTTPException) as error:
        await record_verification_evidence(
            None,  # type: ignore[arg-type]  # Validation must finish before any DB lookup.
            _evidence(**changes),
            workspace_manager=None,  # type: ignore[arg-type]
        )
    assert error.value.detail == message
