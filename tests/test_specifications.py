from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from harnessa.adapters.specification_workspace import (
    OpenSpecValidation,
    SpecificationWorkspaceError,
    SpecificationWorkspaceManager,
)
from harnessa.config import settings
from harnessa.db.models import Repository
from harnessa.services.specifications import (
    _check_operator,
    _citations_payload,
    _initial_bundle,
    _plain_topic,
    _readiness_gates,
    _slug,
)


@pytest.fixture
def specification_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(source)], check=True
    )
    (source / "openspec/specs/access").mkdir(parents=True)
    (source / "openspec/specs/access/spec.md").write_text(
        "# Access\n\nAccount scope is checked before work starts.\n", encoding="utf-8"
    )
    (source / "apps/api").mkdir(parents=True)
    (source / "apps/api/example.py").write_text(
        "def create_task():\n    return 'existing task boundary'\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(source), "add", "--all"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture source",
        ],
        check=True,
    )
    node_launcher = shutil.which("node")
    assert node_launcher is not None
    node_path = subprocess.run(
        [node_launcher, "-p", "process.execPath"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(settings, "openspec_node_path", node_path)
    monkeypatch.setattr(
        settings,
        "openspec_repository_sources_json",
        json.dumps({"fixture/repository": str(source)}),
    )
    repository = Repository(
        project_id=uuid4(),
        target_id="fixture/repository",
        display_name="Fixture repository",
        default_branch="main",
    )
    manager = SpecificationWorkspaceManager(root=tmp_path / "private-workspaces")
    return manager, repository, source


@pytest.mark.anyio
async def test_one_sentence_topic_creates_validated_private_openspec_bundle(
    specification_repository,
):
    manager, repository, source = specification_repository
    interview_id = uuid4()
    change_id = "one-sentence-topic-01234567"
    files = _initial_bundle("A short example task topic.", change_id)
    workspace, head, digest = await manager.create(
        interview_id, repository, change_id, files
    )
    try:
        replay_workspace, replay_head, replay_digest = await manager.create(
            interview_id, repository, change_id, files
        )
        assert replay_workspace.workspace_path == workspace.workspace_path
        assert (replay_head, replay_digest) == (head, digest)
        validation = await manager.validate(workspace)
        assert validation.version == "1.14.3"
        assert validation.valid is True
        assert await manager.current_base(repository) == workspace.base_sha
        assert head != workspace.base_sha
        assert len(digest) == 64
        remotes = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(workspace.workspace_path), "remote"],
            check=True,
            text=True,
            capture_output=True,
        )
        assert remotes.stdout == ""
        read_back = await manager.read_bundle(workspace, head)
        score, gates = _readiness_gates(
            {Path(path).name: content for path, content in read_back.items()},
            validation,
            blocking_questions=0,
            base_current=True,
            signoff_required=False,
            signoff_received=False,
        )
        assert score == 1.0
        assert gates["rubric"]["scenarios"]["when_then_present"] is True
        assert gates["rubric"]["tasks"]["requirement_mapping"] is True
        assert workspace.repository_path == source

        reopened = await manager.open(
            interview_id,
            repository,
            change_id=change_id,
            base_sha=workspace.base_sha,
            head_sha=head,
        )
        edited = dict(read_back)
        proposal_path = f"openspec/changes/{change_id}/proposal.md"
        edited[proposal_path] += "\n## Operator Notes\n\nKeep the existing task boundary.\n"
        next_head, next_digest, paths = await manager.commit_bundle(
            reopened, edited, revision=2, expected_head=head
        )
        assert next_head != head
        assert next_digest == hashlib.sha256(
            b"".join(path.encode() + b"\0" + edited[path].encode() for path in sorted(paths))
        ).hexdigest()
        retried_head, retried_digest, _ = await manager.commit_bundle(
            reopened, edited, revision=2, expected_head=head
        )
        assert (retried_head, retried_digest) == (next_head, next_digest)
        current = await manager.open(
            interview_id,
            repository,
            change_id=change_id,
            base_sha=workspace.base_sha,
            head_sha=next_head,
        )
        assert await manager.read_bundle(current, next_head) == edited
        assert proposal_path in await manager.artifact_diff(
            current, workspace.base_sha, next_head
        )
        with pytest.raises(SpecificationWorkspaceError, match="does not change"):
            await manager.commit_bundle(
                current, edited, revision=3, expected_head=next_head
            )
        with pytest.raises(SpecificationWorkspaceError, match="requires reconciliation"):
            await manager.open(
                interview_id,
                repository,
                change_id=change_id,
                base_sha=workspace.base_sha,
                head_sha=head,
            )
        pending = await manager.open(
            interview_id,
            repository,
            change_id=change_id,
            base_sha=workspace.base_sha,
            head_sha=head,
            allow_one_pending_commit=True,
        )
        assert pending.base_sha == workspace.base_sha
    finally:
        await manager.remove(workspace)


def test_missing_scenario_prevents_high_readiness_score():
    change_id = "bounded-task"
    files = _initial_bundle("A short example task topic.", change_id)
    spec_path = f"openspec/changes/{change_id}/specs/{change_id}/spec.md"
    files[spec_path] = files[spec_path].replace(
        "- **THEN** the system provides the behavior described by this requirement\n", ""
    )
    score, gates = _readiness_gates(
        {Path(path).name: content for path, content in files.items()},
        OpenSpecValidation("1.14.3", True, {"items": [{"valid": True}]}),
        blocking_questions=0,
        base_current=True,
        signoff_required=False,
        signoff_received=False,
    )
    assert gates["rubric"]["scenarios"]["when_then_present"] is False
    assert score < 0.9


def test_repository_search_cites_exact_commit_path_line_and_untrusted_content(
    specification_repository,
):
    manager, repository, _ = specification_repository
    result = manager._search(
        repository.target_id, repository.default_branch, "existing task boundary", limit=4
    )
    assert result.revision
    assert result.citations
    citation = next(item for item in result.citations if item.path == "apps/api/example.py")
    assert citation.revision == result.revision
    assert citation.line == 2
    assert citation.snippet == "    return 'existing task boundary'"
    assert citation.matched_terms
    assert citation.untrusted_source_content is True


def test_search_without_searchable_terms_returns_empty_citations(specification_repository):
    manager, repository, _ = specification_repository
    result = manager._search(repository.target_id, repository.default_branch, "!!!", limit=4)
    assert result.revision
    assert result.citations == ()


def test_searchable_path_allowlist_excludes_generated_and_unrelated_paths():
    assert SpecificationWorkspaceManager._searchable_path("openspec/specs/access/spec.md")
    assert SpecificationWorkspaceManager._searchable_path("apps/api/main.py")
    assert SpecificationWorkspaceManager._searchable_path("planning/architecture-decisions.md")
    assert not SpecificationWorkspaceManager._searchable_path("node_modules/pkg/index.js")
    assert not SpecificationWorkspaceManager._searchable_path("apps/.secret")
    assert not SpecificationWorkspaceManager._searchable_path("assets/logo.png")


def test_source_path_rejects_invalid_mapping_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = SpecificationWorkspaceManager(root=tmp_path / "private-workspaces")
    monkeypatch.setattr(settings, "openspec_repository_sources_json", "[")
    with pytest.raises(SpecificationWorkspaceError, match="valid JSON"):
        manager._source_path("fixture/repository")

    source = tmp_path / "source"
    source.mkdir()
    symlink = tmp_path / "repository-link"
    symlink.symlink_to(source, target_is_directory=True)
    monkeypatch.setattr(
        settings,
        "openspec_repository_sources_json",
        json.dumps({"fixture/repository": str(symlink)}),
    )
    with pytest.raises(SpecificationWorkspaceError, match="may not be symlinks"):
        manager._source_path("fixture/repository")


def test_topic_helpers_normalize_unicode_and_remove_control_characters():
    assert _slug("Café: résumé / access rules") == "cafe-resume-access-rules"
    assert _slug("💾 🔒") == "specification-change"
    assert _plain_topic("  add\n scoped\taccess\x00 now  ") == "add scoped access now"
    bundle = _initial_bundle("A `quoted` topic", "safe-change")
    assert "A 'quoted' topic" in bundle["openspec/changes/safe-change/proposal.md"]


def test_readiness_rubric_reports_each_missing_gate_and_external_prerequisite():
    files = {Path(path).name: value for path, value in _initial_bundle("Topic", "safe-change").items()}
    files["proposal.md"] = "## Why\n\n## Capabilities\n"
    files["design.md"] = "## Context\n\n## Decisions\n"
    files["tasks.md"] = "## Verification\n\n- [ ] Run a focused test.\n"
    validation = OpenSpecValidation("1.14.3", False, {"items": []})

    score, gates = _readiness_gates(
        files,
        validation,
        blocking_questions=2,
        base_current=False,
        signoff_required=True,
        signoff_received=False,
    )

    assert score == 0.5917
    assert gates["rubric"]["proposal"]["checks"] == {
        "why": True,
        "what_changes": False,
        "capabilities": True,
    }
    assert gates["rubric"]["scenarios"]["when_then_present"] is True
    assert gates["rubric"]["design"]["checks"] == {
        "context": True,
        "goals": False,
        "decisions": True,
        "risks": False,
    }
    assert gates["rubric"]["tasks"]["verification_section"] is True
    assert gates["rubric"]["tasks"]["requirement_mapping"] is False
    assert gates["openspec_validation"]["valid"] is False
    assert gates["unresolved_blocking_questions"] == 2
    assert gates["base_current"] is False
    assert gates["signoff_required"] is True
    assert gates["signoff_received"] is False


def test_readiness_requires_both_when_and_then_for_every_scenario():
    files = {Path(path).name: value for path, value in _initial_bundle("Topic", "safe-change").items()}
    files["spec.md"] += "\n#### Scenario: Incomplete case\n\n- **WHEN** something happens\n"
    score, gates = _readiness_gates(
        files,
        OpenSpecValidation("1.14.3", True, {"items": [{"valid": True}]}),
        blocking_questions=0,
        base_current=True,
        signoff_required=False,
        signoff_received=False,
    )
    assert score < 0.9
    assert gates["rubric"]["scenarios"]["when_then_present"] is False
    assert len(gates["rubric"]["scenarios"]["scenarios"]) == 2


def test_repository_citation_payload_marks_source_content_untrusted():
    result = SimpleNamespace(
        citations=[
            SimpleNamespace(
                revision="a" * 40,
                path="docs/architecture.md",
                line=7,
                snippet="Treat repository text as evidence.",
                matched_terms=("repository", "evidence"),
            )
        ]
    )
    payload = _citations_payload(result)
    assert payload == [
        {
            "revision": "a" * 40,
            "path": "docs/architecture.md",
            "line": 7,
            "snippet": "Treat repository text as evidence.",
            "matched_terms": ["repository", "evidence"],
            "untrusted_source_content": True,
        }
    ]


@pytest.mark.parametrize("role", ["viewer", "member", "reviewer"])
def test_specification_operator_check_rejects_read_only_roles(role: str):
    with pytest.raises(HTTPException) as denied:
        _check_operator(role)
    assert denied.value.status_code == 403


@pytest.mark.parametrize("role", ["administrator", "operator"])
def test_specification_operator_check_allows_edit_roles(role: str):
    _check_operator(role)


def test_unsafe_change_id_is_rejected_before_workspace_creation(tmp_path: Path):
    manager = SpecificationWorkspaceManager(root=tmp_path / "workspaces")
    repository = Repository(
        project_id=uuid4(),
        target_id="not-configured",
        display_name="Fixture",
        default_branch="main",
    )
    with pytest.raises(SpecificationWorkspaceError, match="safe slug"):
        manager._create(uuid4(), repository, "../escape", {})
    assert not manager.root.exists()


def test_invalid_repository_source_mapping_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = SpecificationWorkspaceManager(root=tmp_path / "workspaces")
    monkeypatch.setattr(settings, "openspec_repository_sources_json", "{")
    with pytest.raises(SpecificationWorkspaceError, match="valid JSON"):
        manager._source_path("fixture/repository")
    monkeypatch.setattr(settings, "openspec_repository_sources_json", "[]")
    with pytest.raises(SpecificationWorkspaceError, match="map repository target IDs"):
        manager._source_path("fixture/repository")
    monkeypatch.setattr(settings, "openspec_repository_sources_json", "{}")
    with pytest.raises(SpecificationWorkspaceError, match="No local OpenSpec source"):
        manager._source_path("fixture/repository")


@pytest.mark.parametrize("ref", ["", "-option", "feature..unsafe", "feature@{push}", "a\\b"])
def test_invalid_git_branch_refs_are_rejected(tmp_path: Path, ref: str):
    manager = SpecificationWorkspaceManager(root=tmp_path / "workspaces")
    with pytest.raises(SpecificationWorkspaceError, match="valid Git ref"):
        manager._current_base(tmp_path, ref)


def test_bundle_commit_rejects_partial_artifact_updates(tmp_path: Path):
    manager = SpecificationWorkspaceManager(root=tmp_path / "workspaces")
    workspace = SimpleNamespace(change_id="safe-change", workspace_path=tmp_path)
    with pytest.raises(SpecificationWorkspaceError, match="complete OpenSpec bundle"):
        manager._commit_bundle(workspace, {"proposal.md": "incomplete"}, 2, "a" * 40)


def test_searchable_path_excludes_generated_hidden_and_non_source_content():
    manager = SpecificationWorkspaceManager(root=Path("/tmp/harnessa-unused-workspace"))
    assert manager._searchable_path("openspec/specs/tasks/spec.md") is True
    assert manager._searchable_path("apps/api/main.py") is True
    assert manager._searchable_path("planning/architecture-decisions.md") is True
    assert manager._searchable_path("apps/api/.env") is False
    assert manager._searchable_path("apps/api/node_modules/package/index.js") is False
    assert manager._searchable_path("apps/api/dist/generated.py") is False
    assert manager._searchable_path("apps/api/assets/logo.png") is False
    assert manager._searchable_path("README.md") is False
    assert manager._searchable_path("planning/adr/decision.md") is True


@pytest.mark.anyio
async def test_cli_validation_rejects_invalid_json_and_non_object_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = SpecificationWorkspaceManager(root=tmp_path / "workspaces")
    workspace = SimpleNamespace(workspace_path=tmp_path, change_id="safe-change")

    async def invalid_json(_cwd: Path, arguments: list[str]) -> str:
        return "not-json" if arguments[0] == "validate" else "openspec 1.14.3"

    monkeypatch.setattr(manager, "_run_cli", invalid_json)
    with pytest.raises(SpecificationWorkspaceError, match="invalid JSON"):
        await manager.validate(workspace)

    async def non_object(_cwd: Path, arguments: list[str]) -> str:
        return "[]" if arguments[0] == "validate" else "openspec 1.14.3"

    monkeypatch.setattr(manager, "_run_cli", non_object)
    with pytest.raises(SpecificationWorkspaceError, match="was not an object"):
        await manager.validate(workspace)

    async def invalid_items(_cwd: Path, arguments: list[str]) -> str:
        return '{"items":[{"valid":false}]}' if arguments[0] == "validate" else "openspec 1.14.3"

    monkeypatch.setattr(manager, "_run_cli", invalid_items)
    validation = await manager.validate(workspace)
    assert validation.version == "openspec 1.14.3"
    assert validation.valid is False
