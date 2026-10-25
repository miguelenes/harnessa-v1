"""Canonical evidence and digest helpers for reviewed Harness² experiences."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def canonical_experience_evidence_refs(
    refs: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Keep the exact, bounded probe evidence shape in a stable order."""
    normalized = [
        {
            "digest": str(ref["digest"]),
            "kind": str(ref["kind"]),
            "label": str(ref["label"]),
        }
        for ref in refs
    ]
    return sorted(normalized, key=lambda ref: (ref["kind"], ref["label"], ref["digest"]))


def experience_candidate_digest(
    *,
    cue: str,
    lesson: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    repository_scope: str,
    domain_scope: str,
) -> str:
    payload = {
        "cue": cue,
        "lesson": lesson,
        "evidence_refs": canonical_experience_evidence_refs(evidence_refs),
        "repository_scope": repository_scope,
        "domain_scope": domain_scope,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
