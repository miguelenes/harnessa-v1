from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from harnessa.adapters.specification_workspace import SpecificationWorkspaceError
from harnessa.config import settings
from harnessa.services.verification_workflow import (
    VerificationDispatcher,
    _bind_verified_review_head,
    _bounded_git_diff,
    _compose_review_head,
    _lease_is_current,
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Harnessa test")
    _git(path, "config", "user.email", "test@harnessa.invalid")
    (path / "implementation.txt").write_text("implementation\n", encoding="utf-8")
    _git(path, "add", "implementation.txt")
    _git(path, "commit", "--quiet", "-m", "implementation")
    return path


class _DiffProcess:
    def __init__(self, output: bytes, returncode: int = 0, *, timeout: bool = False):
        self.output = output
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False

    async def communicate(self):
        if self.timeout:
            raise TimeoutError
        return self.output, b"ignored stderr"

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("process", "expected"),
    [
        (_DiffProcess(b"a small diff"), "a small diff"),
        (_DiffProcess(b"", returncode=1), "[diff unavailable: revision could not be compared]"),
        (
            _DiffProcess(b"x" * 1_000_001),
            "x" * 1_000_000 + "\n[diff truncated at 1 MB]",
        ),
    ],
)
async def test_bounded_git_diff_handles_output_and_git_failure(
    monkeypatch: pytest.MonkeyPatch,
    process: _DiffProcess,
    expected: str,
) -> None:
    async def fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "harnessa.services.verification_workflow.asyncio.create_subprocess_exec", fake_exec
    )
    assert await _bounded_git_diff(Path("/repo"), "base", "head", pathspec="spec.md") == expected


@pytest.mark.anyio
async def test_bounded_git_diff_kills_timed_out_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _DiffProcess(b"", timeout=True)

    async def fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "harnessa.services.verification_workflow.asyncio.create_subprocess_exec", fake_exec
    )
    assert await _bounded_git_diff(Path("/repo"), "base", "head") == (
        "[diff unavailable: git timed out]"
    )
    assert process.killed


@pytest.mark.anyio
async def test_review_head_contains_archive_diff_and_does_not_move_checkout_head(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    implementation_head = _git(repository, "rev-parse", "HEAD")
    specification_diff = (
        "diff --git a/openspec/changes/sample/spec.md b/openspec/changes/sample/spec.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/openspec/changes/sample/spec.md\n"
        "@@ -0,0 +1 @@\n"
        "+Proposed change\n"
    )
    archive_diff = (
        "diff --git a/openspec/changes/sample/spec.md b/openspec/changes/sample/spec.md\n"
        "deleted file mode 100644\n"
        "--- a/openspec/changes/sample/spec.md\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-Proposed change\n"
        "diff --git a/openspec/specs/sample.md b/openspec/specs/sample.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/openspec/specs/sample.md\n"
        "@@ -0,0 +1 @@\n"
        "+Archived specification\n"
    )

    review_head = await _compose_review_head(
        repository,
        implementation_head,
        specification_diff,
        archive_diff,
        "refs/harnessa/review-candidates/test-fence-1",
    )

    assert _git(repository, "show", f"{review_head}:openspec/specs/sample.md") == (
        "Archived specification"
    )
    assert "openspec/changes/sample/spec.md" not in _git(
        repository, "ls-tree", "-r", "--name-only", review_head
    )
    assert _git(repository, "rev-parse", f"{review_head}^") == implementation_head
    assert _git(repository, "rev-parse", "HEAD") == implementation_head
    assert (
        _git(repository, "rev-parse", "refs/harnessa/review-candidates/test-fence-1") == review_head
    )


@pytest.mark.anyio
async def test_review_head_without_diffs_reuses_implementation_revision(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    implementation_head = _git(repository, "rev-parse", "HEAD")
    review_head = await _compose_review_head(
        repository,
        implementation_head,
        "",
        "",
        "refs/harnessa/review-candidates/no-diff-fence-1",
    )
    assert review_head == implementation_head
    assert _git(repository, "rev-parse", "refs/harnessa/review-candidates/no-diff-fence-1") == (
        implementation_head
    )


@pytest.mark.anyio
async def test_review_head_rejects_an_unapplicable_archive_diff(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    implementation_head = _git(repository, "rev-parse", "HEAD")
    with pytest.raises(SpecificationWorkspaceError, match="OpenSpec archive could not be applied"):
        await _compose_review_head(
            repository,
            implementation_head,
            "",
            "not a git diff",
            "refs/harnessa/review-candidates/invalid-fence-1",
        )


def test_verification_lease_spans_the_shared_run_deadline() -> None:
    dispatcher = VerificationDispatcher(repository_sources={}, lease_seconds=1)
    assert dispatcher.lease_seconds >= settings.run_max_wall_seconds + 60


def test_expired_verification_lease_cannot_persist() -> None:
    now = datetime.now(UTC)
    lease = SimpleNamespace(
        owner_id="worker",
        fencing_token=3,
        expires_at=now - timedelta(seconds=1),
    )
    assert not _lease_is_current(lease, "worker", 3, now)
    lease.expires_at = now + timedelta(seconds=1)
    assert _lease_is_current(lease, "worker", 3, now)
    assert not _lease_is_current(lease, "other", 3, now)
    assert not _lease_is_current(lease, "worker", 4, now)


def test_unexecuted_verification_cannot_mark_review_head_verified() -> None:
    preview = {"proposed_final_review_head": "a" * 40}
    assert not _bind_verified_review_head(preview, "a" * 40, None)
    assert preview["final_head_verified"] is False
    assert not _bind_verified_review_head(preview, "a" * 40, "b" * 40)
    assert preview["final_head_verified"] is False
    assert _bind_verified_review_head(preview, "a" * 40, "a" * 40)
    assert preview["final_head_verified"] is True
