from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from harnessa.adapters.verification import (
    CommandEvidence,
    CriterionEvidence,
    PodmanCommandExecutor,
    VerificationCommand,
    VerificationCriterion,
    VerificationManifest,
    VerificationPolicyError,
    VerificationRunner,
    VerificationUnavailable,
    _aggregate,
    _redact,
)
from harnessa.adapters.verification import (
    _git as _materialize_git,
)

_IMAGE = "localhost/harnessa-verifier@sha256:" + "a" * 64


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Verifier Fixture")
    _git(repository, "config", "user.email", "verifier@example.invalid")
    (repository / "answer.txt").write_text("approved revision\n", encoding="utf-8")
    _git(repository, "add", "answer.txt")
    _git(repository, "commit", "--quiet", "-m", "fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


def _manifest(*, manual: bool = True) -> VerificationManifest:
    return VerificationManifest(
        revision=4,
        criteria=(
            VerificationCriterion(
                id="R-1",
                requirement="The source contains the expected answer.",
                scenario="Read answer.txt and compare it to the expected value.",
            ),
            VerificationCriterion(
                id="R-2",
                requirement="The result needs visual review.",
                scenario="Review the rendered result.",
                manual_review=manual,
            ),
        ),
        commands=(
            VerificationCommand(
                id="check-answer",
                argv=("/usr/local/bin/check-answer",),
                image=_IMAGE,
                criteria=("R-1",),
            ),
        ),
    )


class _FakeExecutor:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.workspaces: list[Path] = []
        self.seen_contents: list[str] = []
        self.fixture_contents: list[str] = []

    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
        self.workspaces.append(workspace)
        visible = (workspace / "answer.txt").read_text(encoding="utf-8")
        self.seen_contents.append(visible)
        fixture = workspace.parent / "protected-fixtures" / "expected" / "answer.txt"
        if fixture.exists():
            self.fixture_contents.append(fixture.read_text(encoding="utf-8"))
        outcome = self.outcomes.pop(0)
        # A verifier command can mutate only its private, per-command checkout.
        (workspace / "answer.txt").write_text("verifier mutation\n", encoding="utf-8")
        for artifact_path in command.artifact_paths:
            artifact = workspace / artifact_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("captured report\n", encoding="utf-8")
        return CommandEvidence(
            command_id=command.id,
            image=command.image,
            exit_code=0 if outcome == "passed" else 2 if outcome == "failed" else None,
            timed_out=False,
            stdout=visible,
            stderr="",
            duration_ms=3,
            outcome=outcome,  # type: ignore[arg-type]
        )


class _ProtectedAnswerExecutor:
    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
        expected = (workspace.parent / "protected-fixtures" / "expected" / "answer.txt").read_text(
            encoding="utf-8"
        )
        actual = (workspace / "answer.txt").read_text(encoding="utf-8")
        passed = actual == expected
        return CommandEvidence(
            command_id=command.id,
            image=command.image,
            exit_code=0 if passed else 1,
            timed_out=False,
            stdout="answer matched protected fixture"
            if passed
            else "answer did not match protected fixture",
            stderr="",
            duration_ms=1,
            outcome="passed" if passed else "failed",
        )


class _UnavailableExecutor:
    async def validate(self, manifest: VerificationManifest) -> None:
        raise VerificationUnavailable("digest-pinned image is unavailable")

    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
        raise AssertionError("An unavailable verifier must not execute commands")


class _PassingExecutorWithoutArtifact:
    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
        return CommandEvidence(
            command_id=command.id,
            image=command.image,
            exit_code=0,
            timed_out=False,
            stdout="passed",
            stderr="",
            duration_ms=1,
            outcome="passed",
        )


class _BinaryArtifactExecutor:
    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
        artifact = workspace / command.artifact_paths[0]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"\x00\xffbinary\x00")
        return CommandEvidence(
            command_id=command.id,
            image=command.image,
            exit_code=0,
            timed_out=False,
            stdout="passed",
            stderr="",
            duration_ms=1,
            outcome="passed",
        )


class _SymlinkArtifactExecutor:
    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
        artifact = workspace / command.artifact_paths[0]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.symlink_to("/etc/passwd")
        return CommandEvidence(
            command_id=command.id,
            image=command.image,
            exit_code=0,
            timed_out=False,
            stdout="passed",
            stderr="",
            duration_ms=1,
            outcome="passed",
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"image": "localhost/image:latest"},
        {"argv": ("-x",)},
        {"argv": ("/bin/check", "")},
        {"artifact_paths": ("../outside",)},
        {"artifact_paths": (".git/config",)},
        {"artifact_paths": ("same.txt", "same.txt")},
    ],
)
def test_manifest_rejects_unsafe_command_inputs(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VerificationCommand.model_validate(
            {**VerificationCommand(id="check", argv=("/bin/check",), image=_IMAGE, criteria=("R-1",)).model_dump(), **updates}
        )


def test_manifest_rejects_unresolved_duplicate_and_uncovered_criteria() -> None:
    base = _manifest()
    with pytest.raises(ValueError, match="unique"):
        VerificationManifest(
            revision=1,
            criteria=(base.criteria[0], base.criteria[0]),
            commands=base.commands,
        )
    with pytest.raises(ValueError, match="reference declared"):
        VerificationManifest(
            revision=1,
            criteria=base.criteria,
            commands=(VerificationCommand.model_validate({**base.commands[0].model_dump(), "criteria": ("missing",)}),),
        )
    with pytest.raises(ValueError, match="covered"):
        VerificationManifest(
            revision=1,
            criteria=(base.criteria[0].model_copy(update={"manual_review": False}),),
            commands=(VerificationCommand.model_validate({**base.commands[0].model_dump(), "required": False}),),
        )
    with pytest.raises(ValueError, match="command IDs must be unique"):
        VerificationManifest(
            revision=1,
            criteria=(base.criteria[0],),
            commands=(base.commands[0], base.commands[0]),
        )


def test_verification_limit_and_aggregate_validation() -> None:
    with pytest.raises(VerificationPolicyError, match="limits must be positive"):
        VerificationRunner(_FakeExecutor(["passed"]), max_total_seconds=0)
    assert _aggregate((CriterionEvidence("R-1", "passed", "ok", ("check",)),)) == "passed"


@pytest.mark.anyio
async def test_verification_rejects_bad_source_and_protected_fixtures(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    runner = VerificationRunner(_FakeExecutor(["passed"]))
    with pytest.raises(VerificationPolicyError, match="full source commit SHA"):
        await runner.run(_manifest(), repository_path=repository, source_revision="HEAD")
    with pytest.raises(VerificationPolicyError, match="repository is unavailable"):
        await runner.run(_manifest(), repository_path=tmp_path / "missing", source_revision=revision)
    file_path = tmp_path / "not-a-repository"
    file_path.write_text("not a repository", encoding="utf-8")
    with pytest.raises(VerificationPolicyError, match="repository is unavailable"):
        await runner.run(_manifest(), repository_path=file_path, source_revision=revision)
    for fixtures in ({"../escape": b"x"}, {".git/config": b"x"}, {"ok": "text"}):
        with pytest.raises(VerificationPolicyError):
            await runner.run(
                _manifest(), repository_path=repository, source_revision=revision,
                protected_fixtures=fixtures,  # type: ignore[arg-type]
            )
    with pytest.raises(VerificationPolicyError, match="exceeds its approved limit"):
        await runner.run(
            _manifest(), repository_path=repository, source_revision=revision,
            protected_fixtures={"too-large.bin": b"x" * 2_000_001},
        )


@pytest.mark.anyio
async def test_unavailable_executor_and_missing_artifact_never_pass(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    unavailable = await VerificationRunner(_UnavailableExecutor()).run(
        _manifest(), repository_path=repository, source_revision=revision
    )
    assert unavailable.outcome == "unverified"
    assert unavailable.commands[0].outcome == "unverified"

    manifest = _manifest().model_copy(
        update={"commands": (_manifest().commands[0].model_copy(
            update={"artifact_paths": ("missing/report.txt",)}
        ),)}
    )
    result = await VerificationRunner(_PassingExecutorWithoutArtifact()).run(
        manifest, repository_path=repository, source_revision=revision
    )
    assert result.outcome == "unverified"
    assert result.commands[0].artifacts[0]["status"] == "missing"


@pytest.mark.anyio
async def test_artifact_symlinks_and_shared_output_limits_are_enforced(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    manifest = _manifest().model_copy(
        update={"commands": (_manifest().commands[0].model_copy(
            update={"artifact_paths": ("report.txt",)}
        ),)}
    )
    with pytest.raises(VerificationPolicyError, match="symlink"):
        await VerificationRunner(_SymlinkArtifactExecutor()).run(
            manifest, repository_path=repository, source_revision=revision
        )

    class LargeOutputExecutor:
        async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
            return CommandEvidence(
                command_id=command.id, image=command.image, exit_code=0, timed_out=False,
                stdout="x" * 2048, stderr="", duration_ms=1, outcome="passed",
            )

    result = await VerificationRunner(LargeOutputExecutor(), max_output_bytes=1024).run(
        _manifest(), repository_path=repository, source_revision=revision
    )
    assert result.commands[0].stdout.startswith("[output omitted")


@pytest.mark.anyio
async def test_podman_command_shape_and_missing_executable_are_safe(tmp_path: Path, monkeypatch) -> None:
    repository, _revision = _repository(tmp_path)
    executor = PodmanCommandExecutor(executable="podman-missing-for-test")
    argv = executor.command(_manifest().commands[0], repository)
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--entrypoint" in argv
    assert "HOME=/tmp" in argv
    monkeypatch.setattr("harnessa.adapters.verification.shutil.which", lambda _name: None)
    evidence = await executor.execute(_manifest().commands[0], repository)
    assert evidence.outcome == "unverified"
    unsupported_path = tmp_path / "workspace,with-comma"
    unsupported_path.mkdir()
    with pytest.raises(VerificationPolicyError, match="unsupported characters"):
        executor.command(_manifest().commands[0], unsupported_path)


@pytest.mark.anyio
async def test_podman_readiness_rejects_missing_or_non_rootless_runtime(monkeypatch) -> None:
    import json

    from harnessa.adapters import verification

    executor = PodmanCommandExecutor(executable="podman-fixture")
    monkeypatch.setattr(verification.shutil, "which", lambda _name: None)
    with pytest.raises(VerificationUnavailable, match="Podman is unavailable"):
        await executor.validate(_manifest())

    class InfoProcess:
        returncode = 0

        async def communicate(self):
            return json.dumps({"host": {"security": {"rootless": False}}}).encode(), b""

    monkeypatch.setattr(verification.shutil, "which", lambda _name: "/usr/bin/podman")

    async def fake_exec(*args, **kwargs):
        return InfoProcess()

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(VerificationUnavailable, match="requires rootless Podman"):
        await executor.validate(_manifest())

    class MissingImageProcess(InfoProcess):
        async def communicate(self):
            return json.dumps({"host": {"security": {"rootless": True}}}).encode(), b""

        async def wait(self):
            return 1

    async def fake_missing_image(*args, **kwargs):
        return MissingImageProcess()

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fake_missing_image)
    with pytest.raises(VerificationUnavailable, match="image is not present locally"):
        await executor.validate(_manifest())

    async def fail_start(*args, **kwargs):
        raise OSError("fixture subprocess failure")

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fail_start)
    with pytest.raises(VerificationUnavailable, match="readiness could not be verified"):
        await executor.validate(_manifest())

    class InvalidInfoProcess:
        returncode = 0

        async def communicate(self):
            return b"not-json", b""

    async def invalid_info(*args, **kwargs):
        return InvalidInfoProcess()

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", invalid_info)
    with pytest.raises(VerificationUnavailable, match="requires rootless Podman"):
        await executor.validate(_manifest())


def test_verifier_output_is_redacted_before_it_is_returned() -> None:
    result = _redact(b"api_key=fixture-secret-value")
    assert "fixture-secret-value" not in result
    assert "redacted" in result


@pytest.mark.anyio
async def test_git_materialization_rejects_failed_commands() -> None:
    with pytest.raises(VerificationPolicyError, match="Could not materialize"):
        await _materialize_git("--not-a-valid-git-subcommand")


@pytest.mark.anyio
async def test_verification_runs_each_command_from_a_clean_exact_revision(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    executor = _FakeExecutor(["passed"])

    result = await VerificationRunner(executor).run(
        _manifest(manual=True), repository_path=repository, source_revision=revision
    )

    assert result.source_revision == revision
    assert result.manifest_revision == 4
    assert result.criteria[0].outcome == "passed"
    assert result.criteria[1].outcome == "manual_review_required"
    assert result.outcome == "manual_review_required"
    assert not executor.workspaces[0].exists()
    assert executor.workspaces[0].name == "check-answer"
    assert executor.seen_contents == ["approved revision\n"]
    assert len(result.content_hash) == 64


@pytest.mark.anyio
async def test_all_required_criteria_can_aggregate_to_passed(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    base = _manifest()
    manifest = VerificationManifest(
        revision=base.revision,
        criteria=(base.criteria[0],),
        commands=base.commands,
    )
    result = await VerificationRunner(_FakeExecutor(["passed"])).run(
        manifest, repository_path=repository, source_revision=revision
    )
    assert result.criteria[0].outcome == "passed"
    assert result.outcome == "passed"


@pytest.mark.anyio
async def test_protected_fixture_is_outside_the_agent_revision_and_clean_checkout(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    executor = _FakeExecutor(["passed"])

    await VerificationRunner(executor).run(
        _manifest(),
        repository_path=repository,
        source_revision=revision,
        protected_fixtures={"expected/answer.txt": b"private expected value\n"},
    )

    assert executor.fixture_contents == ["private expected value\n"]
    assert not (executor.workspaces[0] / "protected-fixtures").exists()
    assert not (repository / "protected-fixtures").exists()


@pytest.mark.anyio
async def test_verification_captures_declared_artifact_bytes_and_digest(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    manifest = _manifest().model_copy(
        update={
            "commands": (
                _manifest()
                .commands[0]
                .model_copy(update={"artifact_paths": ("reports/result.txt",)}),
            )
        }
    )

    result = await VerificationRunner(_FakeExecutor(["passed"])).run(
        manifest, repository_path=repository, source_revision=revision
    )

    artifact = result.commands[0].artifacts[0]
    assert artifact["path"] == "reports/result.txt"
    assert artifact["size_bytes"] == len(b"captured report\n")
    assert artifact["sha256"]
    assert artifact["content_base64"] == "Y2FwdHVyZWQgcmVwb3J0Cg=="


@pytest.mark.anyio
async def test_broken_implementation_fails_then_a_new_repair_revision_passes(
    tmp_path: Path,
) -> None:
    repository, _base = _repository(tmp_path)
    (repository / "answer.txt").write_text("broken answer\n", encoding="utf-8")
    _git(repository, "add", "answer.txt")
    _git(repository, "commit", "--quiet", "-m", "deliberately broken implementation")
    broken_revision = _git(repository, "rev-parse", "HEAD")
    manifest = _manifest()
    protected = {"expected/answer.txt": b"private expected value\n"}

    broken = await VerificationRunner(_ProtectedAnswerExecutor()).run(
        manifest,
        repository_path=repository,
        source_revision=broken_revision,
        protected_fixtures=protected,
    )
    assert broken.outcome == "failed"
    assert broken.source_revision == broken_revision

    (repository / "answer.txt").write_text("private expected value\n", encoding="utf-8")
    _git(repository, "add", "answer.txt")
    _git(repository, "commit", "--quiet", "-m", "bounded repair")
    repaired_revision = _git(repository, "rev-parse", "HEAD")
    repaired = await VerificationRunner(_ProtectedAnswerExecutor()).run(
        manifest,
        repository_path=repository,
        source_revision=repaired_revision,
        protected_fixtures=protected,
    )

    assert repaired_revision != broken_revision
    assert repaired.outcome == "manual_review_required"
    assert repaired.criteria[0].outcome == "passed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command_outcome", "expected"),
    [("failed", "failed"), ("unverified", "unverified"), ("passed", "passed")],
)
async def test_matrix_outcome_comes_from_command_evidence(
    tmp_path: Path, command_outcome: str, expected: str
) -> None:
    repository, revision = _repository(tmp_path)
    result = await VerificationRunner(_FakeExecutor([command_outcome])).run(
        _manifest(), repository_path=repository, source_revision=revision
    )

    assert result.criteria[0].outcome == expected
    if expected != "passed":
        assert result.outcome == expected


@pytest.mark.anyio
async def test_unavailable_runtime_keeps_required_evidence_unverified(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    result = await VerificationRunner(_UnavailableExecutor()).run(
        _manifest(), repository_path=repository, source_revision=revision
    )

    assert result.outcome == "unverified"
    assert result.commands[0].exit_code is None
    assert "image is unavailable" in result.commands[0].stderr


@pytest.mark.anyio
async def test_missing_declared_artifact_cannot_pass_a_required_criterion(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    manifest = _manifest().model_copy(
        update={
            "commands": (
                _manifest()
                .commands[0]
                .model_copy(update={"artifact_paths": ("reports/missing.txt",)}),
            )
        }
    )

    result = await VerificationRunner(_PassingExecutorWithoutArtifact()).run(
        manifest, repository_path=repository, source_revision=revision
    )

    assert result.commands[0].artifacts == ({"path": "reports/missing.txt", "status": "missing"},)
    assert result.criteria[0].outcome == "unverified"


@pytest.mark.anyio
async def test_shared_output_limit_omits_oversized_command_output(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)

    class LargeOutputExecutor:
        async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
            return CommandEvidence(
                command_id=command.id,
                image=command.image,
                exit_code=0,
                timed_out=False,
                stdout="x" * 1500,
                stderr="",
                duration_ms=1,
                outcome="passed",
            )

    result = await VerificationRunner(LargeOutputExecutor(), max_output_bytes=1024).run(
        _manifest(), repository_path=repository, source_revision=revision
    )

    assert result.commands[0].outcome == "passed"
    assert result.commands[0].stdout == "[output omitted: shared evidence limit exceeded]"


@pytest.mark.anyio
async def test_verifier_rejects_non_revision_and_missing_repository(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    runner = VerificationRunner(_FakeExecutor(["passed"]))
    with pytest.raises(VerificationPolicyError, match="full source commit SHA"):
        await runner.run(_manifest(), repository_path=repository, source_revision="short")
    with pytest.raises(VerificationPolicyError, match="repository is unavailable"):
        await runner.run(_manifest(), repository_path=tmp_path / "absent", source_revision=revision)


@pytest.mark.anyio
async def test_verifier_rejects_unsafe_and_oversized_protected_fixtures(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    runner = VerificationRunner(_FakeExecutor(["passed"]))
    with pytest.raises(VerificationPolicyError, match="fixture path or content"):
        await runner.run(
            _manifest(),
            repository_path=repository,
            source_revision=revision,
            protected_fixtures={"../escape": b"secret"},
        )
    with pytest.raises(VerificationPolicyError, match="fixture set exceeds"):
        await runner.run(
            _manifest(),
            repository_path=repository,
            source_revision=revision,
            protected_fixtures={"large.bin": b"x" * 2_000_001},
        )


@pytest.mark.anyio
async def test_binary_artifact_is_hashed_but_redacted(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    manifest = _manifest().model_copy(
        update={
            "commands": (
                _manifest().commands[0].model_copy(update={"artifact_paths": ("report.bin",)}),
            )
        }
    )

    result = await VerificationRunner(_BinaryArtifactExecutor()).run(
        manifest, repository_path=repository, source_revision=revision
    )

    artifact = result.commands[0].artifacts[0]
    assert artifact["sha256"]
    assert artifact["redacted"] is True
    assert artifact["content_base64"] == ""


@pytest.mark.anyio
async def test_verifier_rejects_symlinked_artifacts(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    manifest = _manifest().model_copy(
        update={
            "commands": (
                _manifest().commands[0].model_copy(update={"artifact_paths": ("report.txt",)}),
            )
        }
    )

    with pytest.raises(VerificationPolicyError, match="may not be a symlink"):
        await VerificationRunner(_SymlinkArtifactExecutor()).run(
            manifest, repository_path=repository, source_revision=revision
        )


def test_manifest_rejects_unpinned_images_shells_and_unmapped_criteria() -> None:
    with pytest.raises(ValueError, match="sha256 digest"):
        VerificationCommand.model_validate(
            {"id": "run", "argv": ["pytest"], "image": "latest", "criteria": ["R-1"]}
        )
    with pytest.raises(ValueError, match="entrypoint"):
        VerificationCommand(id="shell", argv=("-c", "echo unsafe"), image=_IMAGE, criteria=("R-1",))
    with pytest.raises(ValueError, match="covered"):
        VerificationManifest(
            revision=1,
            criteria=(VerificationCriterion(id="R-1", requirement="req", scenario="when/then"),),
            commands=(
                VerificationCommand(
                    id="check", argv=("check",), image=_IMAGE, criteria=("R-1",), required=False
                ),
            ),
        )


def test_podman_command_is_networkless_and_does_not_use_a_shell(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    command = VerificationCommand(
        id="check",
        argv=("/usr/local/bin/pytest", "-q", "tests"),
        image=_IMAGE,
        criteria=("R-1",),
    )
    argv = PodmanCommandExecutor(executable="podman").command(command, workspace)

    assert "--network=none" in argv
    assert argv[argv.index("--env") + 1] == "HOME=/tmp"
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--entrypoint" in argv
    assert argv[argv.index(_IMAGE) + 1 :] == list(command.argv[1:])
    assert "sh" not in argv


def test_protected_fixtures_mount_read_only_outside_the_checkout(tmp_path: Path) -> None:
    workspace = tmp_path / "check-answer"
    workspace.mkdir()
    fixture_root = tmp_path / "protected-fixtures"
    fixture_root.mkdir()
    command = VerificationCommand(
        id="check",
        argv=("/usr/local/bin/check-answer",),
        image=_IMAGE,
        criteria=("R-1",),
    )

    argv = PodmanCommandExecutor(executable="podman").command(command, workspace)

    fixture_mount = argv[
        argv.index("--volume", argv.index(f"{workspace}:/workspace:rw,rprivate,Z")) + 1
    ]
    assert fixture_mount == f"{fixture_root}:/verification-fixtures:ro,rprivate,Z"
    assert not (workspace / "protected-fixtures").exists()
