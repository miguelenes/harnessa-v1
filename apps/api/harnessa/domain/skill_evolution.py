"""Validation and compatibility helpers for immutable Harness² skill artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]

SKILL_PATH = re.compile(r"^skills/([a-z0-9]+(?:-[a-z0-9]+)*)/SKILL\.md$")
MAX_SKILL_BYTES = 64_000
ALLOWED_SCOPES = frozenset({"task", "repository", "project", "organization"})
ALLOWED_SOURCES = frozenset({"generated", "imported", "operator_authored"})
ALLOWED_TRUST = frozenset({"untrusted", "reviewed"})
FRONTMATTER_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)


@dataclass(frozen=True)
class SkillArtifact:
    path: str
    slug: str
    name: str
    description: str
    license_id: str
    dependencies: tuple[str, ...]
    declared_tools: tuple[str, ...]
    content: str
    digest: str
    size_bytes: int


def validate_skill_artifact(path: str, content: str) -> SkillArtifact:
    """Validate the product's sole editable skill file shape without granting tools."""
    match = SKILL_PATH.fullmatch(path)
    if match is None:
        raise ValueError("Skill artifact path must be skills/<lowercase-slug>/SKILL.md")
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > MAX_SKILL_BYTES:
        raise ValueError(f"Skill artifact must be between 1 and {MAX_SKILL_BYTES} bytes")
    if not content.startswith("---\n"):
        raise ValueError("Skill artifact requires YAML frontmatter")
    frontmatter, separator, body = content[4:].partition("\n---\n")
    if not separator:
        raise ValueError("Skill artifact frontmatter is not closed")
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise ValueError("Skill artifact frontmatter is invalid YAML") from exc
    if not isinstance(metadata, dict) or set(metadata) - FRONTMATTER_FIELDS:
        raise ValueError("Skill artifact frontmatter contains unsupported fields")
    slug = match.group(1)
    name = metadata.get("name")
    description = metadata.get("description")
    if name != slug or not isinstance(description, str) or not description.strip():
        raise ValueError("Skill name must match its path and have a description")
    if len(description) > 1000 or len(body.strip()) < 1:
        raise ValueError("Skill description or body is outside its allowed bounds")
    license_id = metadata.get("license", "UNKNOWN")
    if not isinstance(license_id, str) or not license_id.strip() or len(license_id) > 120:
        raise ValueError("Skill license must be a bounded identifier or UNKNOWN")
    raw_dependencies = metadata.get("metadata", {})
    if not isinstance(raw_dependencies, dict) or set(raw_dependencies) - {"dependencies"}:
        raise ValueError("Skill metadata may contain only declared dependencies")
    dependencies = raw_dependencies.get("dependencies", [])
    tools = metadata.get("allowed-tools", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and 0 < len(item) <= 200 for item in dependencies
    ):
        raise ValueError("Skill dependencies must be bounded strings")
    if not isinstance(tools, list) or not all(
        isinstance(item, str) and 0 < len(item) <= 120 for item in tools
    ):
        raise ValueError("Skill allowed-tools must be bounded declared strings")
    return SkillArtifact(
        path=path,
        slug=slug,
        name=name,
        description=description.strip(),
        license_id=license_id.strip(),
        dependencies=tuple(sorted(set(dependencies))),
        declared_tools=tuple(sorted(set(tools))),
        content=content,
        digest=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
    )


def compatibility_snapshot(**revisions: str | int | None) -> dict[str, Any]:
    """Return a deterministic snapshot for stale detection; values remain explicit."""
    normalized = {key: revisions[key] for key in sorted(revisions)}
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return {"schema_version": 1, **normalized, "digest": hashlib.sha256(payload.encode()).hexdigest()}


def compatibility_drift(
    recorded: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Describe changed or missing compatibility inputs, never coercing unknowns."""
    fields = set(recorded) | set(current)
    fields.discard("digest")
    return {
        key: {"recorded": recorded.get(key), "current": current.get(key)}
        for key in sorted(fields)
        if recorded.get(key) != current.get(key)
    }


def validate_provenance(source_kind: str, scope_type: str, trust_level: str) -> None:
    if source_kind not in ALLOWED_SOURCES:
        raise ValueError("Unsupported skill provenance source")
    if scope_type not in ALLOWED_SCOPES:
        raise ValueError("Unsupported skill scope")
    if trust_level not in ALLOWED_TRUST:
        raise ValueError("Unsupported skill trust level")


def scopes_overlap(
    left_type: str,
    left_id: str,
    left_repository: str,
    right_type: str,
    right_id: str,
    right_repository: str,
) -> bool:
    """Whether two scoped artifacts could be loaded into the same task harness."""
    # Callers compare rows within one organization and resolve the project/repository
    # context before invoking this helper.
    if left_type == "organization" or right_type == "organization":
        return True
    if left_type == "project" or right_type == "project":
        if left_type == right_type == "project":
            return left_id == right_id
        return True
    if left_repository == "*" or right_repository == "*":
        return True
    if left_repository != right_repository:
        return False
    if left_type == right_type == "repository":
        return left_id == right_id
    if left_type == right_type == "task":
        return left_id == right_id
    return {left_type, right_type} == {"task", "repository"}


def duplicate_or_conflict(
    existing: Mapping[str, Any], *, conflict_key: str, artifact_digest: str
) -> str | None:
    if existing.get("conflict_key") != conflict_key:
        return None
    if existing.get("artifact_digest") == artifact_digest:
        return "duplicate"
    return "conflict"


def changed_skill_artifacts(
    base_tree: Mapping[str, str],
    candidate_tree: Mapping[str, str],
    *,
    ignore: frozenset[tuple[str, str]] = frozenset(),
) -> list[SkillArtifact]:
    """Extract only added or modified, schema-valid skill files from a composed tree."""
    result: list[SkillArtifact] = []
    for path, content in sorted(candidate_tree.items()):
        if path.startswith("skills/") and base_tree.get(path) != content:
            artifact = validate_skill_artifact(path, content)
            if (artifact.path, artifact.digest) not in ignore:
                result.append(artifact)
    return result
