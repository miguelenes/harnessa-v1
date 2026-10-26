from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from harnessa.db.harness import (
    _IMMUTABLE_EXPERIENCE_FIELDS,
    HarnessExperience,
    _reject_harness_experience_content_update,
)
from harnessa.domain.skill_evolution import (
    changed_skill_artifacts,
    compatibility_drift,
    compatibility_snapshot,
    duplicate_or_conflict,
    scopes_overlap,
    validate_provenance,
    validate_skill_artifact,
)
from sqlalchemy.orm import attributes


def _skill(content: str | None = None) -> str:
    return content or (
        "---\nname: test-skill\ndescription: A bounded example.\n"
        "license: MIT\nmetadata:\n  dependencies:\n    - python>=3.12\n"
        "allowed-tools:\n  - read_file\n---\n\nUse the approved read-only workflow.\n"
    )


def test_skill_artifact_is_pinned_to_supported_path_and_schema() -> None:
    artifact = validate_skill_artifact("skills/test-skill/SKILL.md", _skill())

    assert artifact.slug == "test-skill"
    assert artifact.license_id == "MIT"
    assert artifact.dependencies == ("python>=3.12",)
    assert artifact.declared_tools == ("read_file",)
    assert len(artifact.digest) == 64
    assert artifact.size_bytes == len(artifact.content.encode())

    with pytest.raises(ValueError, match="path"):
        validate_skill_artifact("scripts/harnessa_policy.py", _skill())
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_skill_artifact(
            "skills/test-skill/SKILL.md",
            _skill().replace("description: A bounded example.", "description: A bounded example.\npermissions: all"),
        )
    with pytest.raises(ValueError, match="match its path"):
        validate_skill_artifact(
            "skills/test-skill/SKILL.md", _skill().replace("name: test-skill", "name: other")
        )


def test_skill_schema_rejects_malformed_bounds_and_unknown_provenance() -> None:
    invalid = [
        "",
        "plain text",
        "---\nname: test-skill\n",
        "---\nname: [\n---\nbody\n",
        _skill().replace("Use the approved read-only workflow.", "   "),
        _skill().replace("license: MIT", 'license: ""'),
        _skill().replace("  dependencies:\n    - python>=3.12", "  extra: value"),
        _skill().replace("  dependencies:\n    - python>=3.12", "  dependencies: invalid"),
        _skill().replace("allowed-tools:\n  - read_file", "allowed-tools: read_file"),
    ]
    for content in invalid:
        with pytest.raises(ValueError):
            validate_skill_artifact("skills/test-skill/SKILL.md", content)
    with pytest.raises(ValueError, match="trust"):
        validate_provenance("imported", "repository", "trusted")
    assert not scopes_overlap("repository", "repo-a", "repo-a", "repository", "repo-b", "repo-b")
    assert duplicate_or_conflict(
        {"conflict_key": "other", "artifact_digest": "a"},
        conflict_key="lint",
        artifact_digest="a",
    ) is None


def test_skill_metadata_does_not_change_harnessa_authority() -> None:
    artifact = validate_skill_artifact("skills/test-skill/SKILL.md", _skill())

    # Declared tool labels are provenance only; callers must apply Harnessa's separate grants.
    assert artifact.declared_tools == ("read_file",)
    assert "permission" not in artifact.content


def test_candidate_skill_derives_only_from_changed_composed_skill_path() -> None:
    content = _skill()
    derived = changed_skill_artifacts({}, {"skills/test-skill/SKILL.md": content})

    assert len(derived) == 1
    assert derived[0].digest == validate_skill_artifact("skills/test-skill/SKILL.md", content).digest
    assert changed_skill_artifacts(
        {},
        {"skills/test-skill/SKILL.md": content},
        ignore=frozenset({("skills/test-skill/SKILL.md", derived[0].digest)}),
    ) == []
    assert changed_skill_artifacts({"skills/test-skill/SKILL.md": content}, {"skills/test-skill/SKILL.md": content}) == []
    assert changed_skill_artifacts({}, {"scripts/harnessa_policy.py": content}) == []


def test_skill_compatibility_and_duplicate_conflict_are_explicit() -> None:
    recorded = compatibility_snapshot(
        harness_revision="h2-a", policy_revision=3, model_capability="cap-a", repository="repo-a"
    )
    unchanged = compatibility_snapshot(
        harness_revision="h2-a", policy_revision=3, model_capability="cap-a", repository="repo-a"
    )
    changed = compatibility_snapshot(
        harness_revision="h2-a", policy_revision=4, model_capability="cap-a", repository="repo-a"
    )

    assert compatibility_drift(recorded, unchanged) == {}
    assert compatibility_drift(recorded, changed) == {
        "policy_revision": {"recorded": 3, "current": 4}
    }
    assert duplicate_or_conflict(
        {"conflict_key": "lint", "artifact_digest": "a"},
        conflict_key="lint",
        artifact_digest="a",
    ) == "duplicate"
    assert duplicate_or_conflict(
        {"conflict_key": "lint", "artifact_digest": "a"},
        conflict_key="lint",
        artifact_digest="b",
    ) == "conflict"


def test_skill_provenance_scopes_and_sources_are_allowlisted() -> None:
    for source in ("generated", "imported", "operator_authored"):
        validate_provenance(source, "repository", "untrusted")
    with pytest.raises(ValueError, match="source"):
        validate_provenance("model_self_report", "repository", "untrusted")
    with pytest.raises(ValueError, match="scope"):
        validate_provenance("imported", "global", "untrusted")


def test_scopes_overlap_only_when_artifacts_can_share_a_harness() -> None:
    assert scopes_overlap("organization", "org-a", "*", "repository", "repo-a", "repo-a")
    assert scopes_overlap("project", "project-a", "*", "task", "task-a", "repo-a")
    assert scopes_overlap("repository", "repo-a", "repo-a", "task", "task-a", "repo-a")
    assert not scopes_overlap("repository", "repo-a", "repo-a", "repository", "repo-b", "repo-b")
    assert not scopes_overlap("task", "task-a", "repo-a", "task", "task-b", "repo-a")
    assert not scopes_overlap("project", "project-a", "*", "project", "project-b", "*")


def test_skill_content_and_provenance_cannot_be_changed_after_insert() -> None:
    now = datetime.now(UTC)
    row = HarnessExperience(
        organization_id=uuid4(),
        project_id=uuid4(),
        source_task_id=None,
        source_experiment_id=None,
        source_run_id=None,
        source_evaluation_run_id=None,
        experience_key="test-skill",
        version=1,
        status="candidate",
        repository_scope="repo-a",
        domain_scope="repository",
        cue="test-skill",
        lesson="A bounded example.",
        evidence_refs=[],
        candidate_digest="a" * 64,
        conflict_key="test-skill",
        source_kind="operator_authored",
        author="operator",
        producer="harnessa-ui",
        scope_type="repository",
        scope_id="repo-a",
        license_id="MIT",
        dependencies=[],
        allowed_tools=[],
        trust_level="untrusted",
        artifact_path="skills/test-skill/SKILL.md",
        artifact_digest="b" * 64,
        artifact_storage_key="sha256/bb",
        artifact_size_bytes=20,
        confidence_rationale="Authored for deterministic lifecycle testing.",
        compatibility_snapshot={"schema_version": 1},
        expiry_at=now + timedelta(days=7),
        policy_revision=1,
    )
    for name in _IMMUTABLE_EXPERIENCE_FIELDS:
        attributes.set_committed_value(row, name, getattr(row, name))
    row.status = "stale"
    row.stale_reason = {"policy_revision": {"recorded": 1, "current": 2}}
    _reject_harness_experience_content_update(None, None, row)
    row.lesson = "rewritten content"
    with pytest.raises(ValueError, match="immutable"):
        _reject_harness_experience_content_update(None, None, row)
