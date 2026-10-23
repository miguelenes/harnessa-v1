"""Pinned Harness² compatibility boundary and immutable product manifest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from harness2.core import tree as upstream_tree  # type: ignore[import-untyped]
from harness2.substrate.opencode import OPENCODE_SURFACE  # type: ignore[import-untyped]

from harnessa.adapters.artifacts import LocalArtifactStore, StoredArtifact

UPSTREAM_REVISION = "1cfe254e8d25b15f37048c3730134686abe60335"
UPSTREAM_LICENSE = "Apache-2.0"
MANIFEST_SCHEMA_VERSION = 1
MAX_COMPONENT_FILES = 500
MAX_COMPONENT_BYTES = 2_000_000
_IMMUTABLE_POLICY_FILES = frozenset(
    {
        "harnessa_policy.py",
        "host_policy.json",
        "evaluator_policy.json",
        "promotion_policy.json",
        "sandbox_policy.py",
    }
)


@dataclass(frozen=True)
class HarnessComponent:
    category: str
    upstream_surface: str
    product_paths: tuple[str, ...]
    editable: bool = True


# These categories are the upstream semantic surface. The product path names are
# deliberately mapped here instead of being assumed from historical attachments.
COMPONENTS: tuple[HarnessComponent, ...] = (
    HarnessComponent("system_prompt", "system prompt", ("systemprompt.md",)),
    HarnessComponent("guardrails", "guardrails", ("guardrails.md",)),
    HarnessComponent("memory", "memory", ("LongTermMEMORY.md",)),
    HarnessComponent("tool_guidance", "tool guidance", ("tool_descriptions/tool_guidance.md",)),
    HarnessComponent("subagents", "subagents", ("sub_agents/*/AGENT.md",)),
    HarnessComponent("skills", "skills", ("skills/*/SKILL.md",)),
    HarnessComponent("scripts", "scripts", ("scripts/*.py",)),
    HarnessComponent("plugins", "plugins", ("plugin/*.ts",)),
)


@dataclass(frozen=True)
class HarnessManifest:
    schema_version: int
    upstream_revision: str
    upstream_license: str
    adapter: str
    components: tuple[dict[str, Any], ...]
    immutable_policy: dict[str, Any]


def is_immutable_policy_path(path: str) -> bool:
    return any(part.casefold() in _IMMUTABLE_POLICY_FILES for part in path.split("/"))


def product_manifest(*, policy_revision: str, policy_digest: str) -> HarnessManifest:
    """Return editable upstream guidance plus a separate, immutable host policy reference."""
    return HarnessManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        upstream_revision=UPSTREAM_REVISION,
        upstream_license=UPSTREAM_LICENSE,
        adapter="harnessa-opencode-v1",
        components=tuple(
            {
                "category": component.category,
                "upstream_surface": component.upstream_surface,
                "paths": list(component.product_paths),
                "editable": component.editable,
            }
            for component in COMPONENTS
        ),
        immutable_policy={"revision": policy_revision, "sha256": policy_digest},
    )


def canonical_tree_bytes(tree: dict[str, str]) -> bytes:
    validate_product_tree(tree)
    return json.dumps(
        {"schema_version": MANIFEST_SCHEMA_VERSION, "components": tree},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def tree_digest(tree: dict[str, str]) -> str:
    return hashlib.sha256(canonical_tree_bytes(tree)).hexdigest()


def validate_product_tree(tree: dict[str, str]) -> dict[str, Any]:
    """Apply upstream's Opencode surface validation and product-specific host boundaries."""
    if len(tree) > MAX_COMPONENT_FILES:
        raise ValueError(f"Harness component tree exceeds {MAX_COMPONENT_FILES} files")
    total = 0
    normalized: dict[str, str] = {}
    for raw_path, contents in tree.items():
        path = upstream_tree.normalize_rel(raw_path)
        if (
            path != raw_path
            or not upstream_tree.is_allowed(OPENCODE_SURFACE, path)
            or is_immutable_policy_path(path)
        ):
            raise ValueError(f"Harness path is outside the editable upstream surface: {raw_path}")
        if not isinstance(contents, str):
            raise TypeError(f"Harness component must contain UTF-8 text: {raw_path}")
        total += len(contents.encode("utf-8"))
        normalized[path] = contents
    if total > MAX_COMPONENT_BYTES:
        raise ValueError(f"Harness component tree exceeds {MAX_COMPONENT_BYTES} bytes")
    return cast(
        dict[str, Any],
        upstream_tree.validate(
            OPENCODE_SURFACE,
            normalized,
            allow_subagents=False,
            reserved_scripts=upstream_tree.RESERVED_SCRIPTS | {"harnessa_policy.py"},
        ),
    )


def store_tree(store: LocalArtifactStore, tree: dict[str, str]) -> StoredArtifact:
    payload = canonical_tree_bytes(tree)
    return store.put_bytes(payload, media_type="application/vnd.harnessa.harness-tree+json")


def load_tree(
    store: LocalArtifactStore, *, storage_key: str, digest: str, size_bytes: int
) -> dict[str, str]:
    state = store.verify_reference(
        storage_key,
        expected_digest=digest,
        expected_size=size_bytes,
        max_bytes=MAX_COMPONENT_BYTES + 64_000,
    )
    if state != "verified":
        raise ValueError(f"Harness artifact is not verified: {state}")
    path = store.root / storage_key
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Harness artifact schema version is unsupported")
    components = doc.get("components")
    if not isinstance(components, dict) or any(
        not isinstance(path, str) or not isinstance(contents, str)
        for path, contents in components.items()
    ):
        raise ValueError("Harness artifact component map is invalid")
    validate_product_tree(components)
    return components


def component_diff(base: dict[str, str], candidate: dict[str, str]) -> dict[str, Any]:
    validate_product_tree(base)
    validate_product_tree(candidate)
    return cast(dict[str, Any], upstream_tree.diff_summary(OPENCODE_SURFACE, base, candidate))
