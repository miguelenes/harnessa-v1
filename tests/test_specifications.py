from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from harnessa.adapters.specification_workspace import (
    OpenSpecValidation,
    SpecificationWorkspaceManager,
)
from harnessa.config import settings
from harnessa.db.models import Repository
from harnessa.services.specifications import _initial_bundle, _readiness_gates


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
