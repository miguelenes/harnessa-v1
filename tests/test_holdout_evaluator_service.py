from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.holdout_evaluator import (
    EvaluatorConfig,
    _load_cases,
    _safe_child,
    _verify_cas_artifact,
    create_app,
)


def _write_source_archive(
    path: Path, content: bytes = b"Synthetic acceptance fixture.\n"
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("README.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(task_id: str, project_id: str, patch: bytes | None) -> dict[str, object]:
    artifacts: list[dict[str, object]] = []
    if patch is not None:
        digest = hashlib.sha256(patch).hexdigest()
        artifacts.append({
            "phase": "composed",
            "deliverable": True,
            "artifacts": [{
                "sha256": digest,
                "size_bytes": len(patch),
                "media_type": "application/vnd.git-patch",
            }],
        })
    manifest: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "project_id": project_id,
        "base_sha": "a" * 40,
        "artifacts": artifacts,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["digest"] = hashlib.sha256(canonical).hexdigest()
    return manifest


async def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://holdout.local"
    )


@pytest.mark.anyio
async def test_private_holdout_passes_patch_without_returning_verifier_feedback(
    tmp_path: Path,
) -> None:
    task_id, project_id = str(uuid4()), str(uuid4())
    base_dir = tmp_path / "private"
    archive_path = base_dir / "bases" / "base.tar"
    archive_digest = _write_source_archive(archive_path)
    verifier = base_dir / "verifiers" / "readme-safety-v1.py"
    verifier.parent.mkdir(parents=True)
    verifier.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "assert (Path(sys.argv[1]) / 'README.md').read_text() == "
        "'Synthetic acceptance fixture.\\nThe data is synthetic.\\n'\n",
        encoding="utf-8",
    )
    suite = {
        "schema_version": 1,
        "dataset_id": "m13-acceptance-holdout",
        "dataset_revision": "1",
        "dataset_license": "MIT",
        "cases": [{
            "task_id": task_id,
            "project_id": project_id,
            "base_sha": "a" * 40,
            "source_archive": "bases/base.tar",
            "source_archive_sha256": archive_digest,
            "verifier_id": "readme-safety-v1",
        }],
    }
    suite_path = base_dir / "suite.json"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")
    artifact_root = tmp_path / "cas"
    patch = (
        b"diff --git a/README.md b/README.md\n"
        b"--- a/README.md\n+++ b/README.md\n"
        b"@@ -1 +1,2 @@\n Synthetic acceptance fixture.\n"
        b"+The data is synthetic.\n"
    )
    LocalArtifactStore(artifact_root).put_bytes(
        patch, media_type="application/vnd.git-patch"
    )
    config = EvaluatorConfig("private-token", suite_path, base_dir, artifact_root)
    app = create_app(config)

    async with await _client(app) as client:
        response = await client.post(
            "/evaluate",
            json=_manifest(task_id, project_id, patch),
            headers={"Authorization": "Bearer private-token"},
        )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "passed"
    assert receipt["manifest_digest"] == _manifest(task_id, project_id, patch)["digest"]
    assert receipt["judge_revision"].startswith("private:m13-acceptance-holdout@1:")
    assert "synthetic" not in json.dumps(receipt).lower()


@pytest.mark.anyio
async def test_evaluator_fails_closed_for_missing_patch_and_wrong_identity(tmp_path: Path) -> None:
    task_id, project_id = str(uuid4()), str(uuid4())
    base_dir = tmp_path / "private"
    archive_digest = _write_source_archive(base_dir / "bases" / "base.tar")
    verifier = base_dir / "verifiers" / "case-v1.py"
    verifier.parent.mkdir(parents=True)
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    suite_path = base_dir / "suite.json"
    suite_path.write_text(json.dumps({
        "schema_version": 1,
        "dataset_id": "m13-acceptance-holdout",
        "dataset_revision": "1",
        "dataset_license": "MIT",
        "cases": [{
            "task_id": task_id,
            "project_id": project_id,
            "base_sha": "a" * 40,
            "source_archive": "bases/base.tar",
            "source_archive_sha256": archive_digest,
            "verifier_id": "case-v1",
        }],
    }), encoding="utf-8")
    app = create_app(EvaluatorConfig("token", suite_path, base_dir, tmp_path / "cas"))

    async with await _client(app) as client:
        missing_patch = await client.post(
            "/evaluate",
            json=_manifest(task_id, project_id, None),
            headers={"Authorization": "Bearer token"},
        )
        wrong_identity = await client.post(
            "/evaluate",
            json=_manifest(task_id, str(uuid4()), None),
            headers={"Authorization": "Bearer token"},
        )

    assert missing_patch.status_code == 200
    assert missing_patch.json()["status"] == "failed"
    assert wrong_identity.status_code == 422
    assert "private verifier" not in wrong_identity.text


@pytest.mark.anyio
async def test_evaluator_rejects_bad_token_and_tampered_manifest(tmp_path: Path) -> None:
    task_id, project_id = str(uuid4()), str(uuid4())
    base_dir = tmp_path / "private"
    archive_digest = _write_source_archive(base_dir / "bases" / "base.tar")
    verifier = base_dir / "verifiers" / "case-v1.py"
    verifier.parent.mkdir(parents=True)
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    suite_path = base_dir / "suite.json"
    suite_path.write_text(json.dumps({
        "schema_version": 1,
        "dataset_id": "m13-acceptance-holdout",
        "dataset_revision": "1",
        "dataset_license": "MIT",
        "cases": [{
            "task_id": task_id,
            "project_id": project_id,
            "base_sha": "a" * 40,
            "source_archive": "bases/base.tar",
            "source_archive_sha256": archive_digest,
            "verifier_id": "case-v1",
        }],
    }), encoding="utf-8")
    app = create_app(EvaluatorConfig("token", suite_path, base_dir, tmp_path / "cas"))
    manifest = _manifest(task_id, project_id, None)
    manifest["base_sha"] = "b" * 40

    async with await _client(app) as client:
        unauthorized = await client.post("/evaluate", json=manifest)
        tampered = await client.post(
            "/evaluate", json=manifest, headers={"Authorization": "Bearer token"}
        )

    assert unauthorized.status_code == 401
    assert tampered.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    "asset_state",
    [
        "missing_archive",
        "missing_verifier",
        "archive_checksum_mismatch",
        "invalid_verifier_syntax",
        "malformed_archive",
        "unsafe_archive_path",
        "file_directory_conflict",
        "directory_file_conflict",
    ],
)
async def test_readiness_requires_valid_case_assets(tmp_path: Path, asset_state: str) -> None:
    task_id, project_id = str(uuid4()), str(uuid4())
    base_dir = tmp_path / "private"
    archive = base_dir / "bases" / "base.tar"
    archive_digest = _write_source_archive(archive)
    verifier = base_dir / "verifiers" / "case-v1.py"
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    if asset_state == "missing_archive":
        archive.unlink()
    elif asset_state == "missing_verifier":
        verifier.unlink()
    elif asset_state == "archive_checksum_mismatch":
        archive.write_bytes(b"changed archive")
    elif asset_state == "invalid_verifier_syntax":
        verifier.write_text("def broken(:\n", encoding="utf-8")
    elif asset_state == "malformed_archive":
        archive.write_bytes(b"not a tar archive")
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    elif asset_state == "unsafe_archive_path":
        content = b"unsafe path fixture\n"
        with tarfile.open(archive, "w") as bundle:
            member = tarfile.TarInfo("../escape.txt")
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    elif asset_state in {"file_directory_conflict", "directory_file_conflict"}:
        members = [("file", "foo"), ("directory", "foo/bar")]
        if asset_state == "directory_file_conflict":
            members.reverse()
        with tarfile.open(archive, "w") as bundle:
            for kind, name in members:
                member = tarfile.TarInfo(name)
                if kind == "directory":
                    member.type = tarfile.DIRTYPE
                    member.size = 0
                    bundle.addfile(member)
                else:
                    content = b"file ancestor conflict\n"
                    member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    suite_path = base_dir / "suite.json"
    suite_path.write_text(json.dumps({
        "schema_version": 1,
        "dataset_id": "m13-acceptance-holdout",
        "dataset_revision": "1",
        "dataset_license": "MIT",
        "cases": [{
            "task_id": task_id,
            "project_id": project_id,
            "base_sha": "a" * 40,
            "source_archive": "bases/base.tar",
            "source_archive_sha256": archive_digest,
            "verifier_id": "case-v1",
        }],
    }), encoding="utf-8")
    artifact_root = tmp_path / "cas"
    artifact_root.mkdir()
    app = create_app(EvaluatorConfig("token", suite_path, base_dir, artifact_root))

    async with await _client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def _snapshot_case(
    tmp_path: Path, verifier_source: str
) -> tuple[str, str, Path, Path, EvaluatorConfig]:
    task_id, project_id = str(uuid4()), str(uuid4())
    base_dir = tmp_path / "private"
    archive = base_dir / "bases" / "base.tar"
    archive_digest = _write_source_archive(archive)
    verifier = base_dir / "verifiers" / "case-v1.py"
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text(verifier_source, encoding="utf-8")
    suite_path = base_dir / "suite.json"
    suite_path.write_text(json.dumps({
        "schema_version": 1,
        "dataset_id": "m13-acceptance-holdout",
        "dataset_revision": "1",
        "dataset_license": "MIT",
        "cases": [{
            "task_id": task_id,
            "project_id": project_id,
            "base_sha": "a" * 40,
            "source_archive": "bases/base.tar",
            "source_archive_sha256": archive_digest,
            "verifier_id": "case-v1",
        }],
    }), encoding="utf-8")
    artifact_root = tmp_path / "cas"
    artifact_root.mkdir()
    return task_id, project_id, archive, verifier, EvaluatorConfig(
        "token", suite_path, base_dir, artifact_root
    )


@pytest.mark.anyio
async def test_evaluation_uses_archived_snapshot_after_host_assets_change(tmp_path: Path) -> None:
    verifier_source = (
        "from pathlib import Path\nimport sys\n"
        "assert (Path(sys.argv[1]) / 'README.md').read_text() == "
        "'Synthetic acceptance fixture.\\nThe data is synthetic.\\n'\n"
    )
    task_id, project_id, archive, verifier, config = _snapshot_case(tmp_path, verifier_source)
    app = create_app(config)
    changed_archive = tmp_path / "changed.tar"
    _write_source_archive(changed_archive, b"Changed host archive.\n")
    changed_archive.replace(archive)
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    patch = (
        b"diff --git a/README.md b/README.md\n"
        b"--- a/README.md\n+++ b/README.md\n"
        b"@@ -1 +1,2 @@\n Synthetic acceptance fixture.\n"
        b"+The data is synthetic.\n"
    )
    LocalArtifactStore(config.artifact_root).put_bytes(
        patch, media_type="application/vnd.git-patch"
    )

    async with await _client(app) as client:
        ready = await client.get("/health/ready")
        response = await client.post(
            "/evaluate",
            json=_manifest(task_id, project_id, patch),
            headers={"Authorization": "Bearer token"},
        )

    assert ready.status_code == 200
    assert response.status_code == 200
    assert response.json()["status"] == "passed"


@pytest.mark.anyio
async def test_evaluation_does_not_reopen_changed_verifier_path(tmp_path: Path) -> None:
    verifier_source = (
        "from pathlib import Path\nimport sys\n"
        "assert (Path(sys.argv[1]) / 'README.md').read_text() == "
        "'Synthetic acceptance fixture.\\nThe data is synthetic.\\n'\n"
    )
    task_id, project_id, _archive, verifier, config = _snapshot_case(tmp_path, verifier_source)
    app = create_app(config)
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    patch = (
        b"diff --git a/README.md b/README.md\n"
        b"--- a/README.md\n+++ b/README.md\n"
        b"@@ -1 +1,2 @@\n Synthetic acceptance fixture.\n"
        b"+The data is wrong.\n"
    )
    LocalArtifactStore(config.artifact_root).put_bytes(
        patch, media_type="application/vnd.git-patch"
    )

    async with await _client(app) as client:
        response = await client.post(
            "/evaluate",
            json=_manifest(task_id, project_id, patch),
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_private_suite_rejects_traversal_and_duplicate_task_ids(tmp_path: Path) -> None:
    path = tmp_path / "suite.json"
    task_id, project_id = str(uuid4()), str(uuid4())
    template = {
        "task_id": task_id,
        "project_id": project_id,
        "base_sha": "a" * 40,
        "source_archive": "../base.tar",
        "source_archive_sha256": "b" * 64,
        "verifier_id": "case-v1",
    }
    suite = {
        "schema_version": 1,
        "dataset_id": "m13-acceptance-holdout",
        "dataset_revision": "1",
        "dataset_license": "MIT",
        "cases": [template],
    }
    path.write_text(json.dumps(suite), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        _load_cases(path)
    template["source_archive"] = "bases/base.tar"
    suite["cases"] = [template, template]
    path.write_text(json.dumps(suite), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        _load_cases(path)


def test_private_references_reject_symlink_components_and_boolean_sizes(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "suite.json").write_text("{}", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="reference"):
        _safe_child(root, "linked/suite.json")

    with pytest.raises(ValueError, match="artifact reference"):
        _verify_cas_artifact(tmp_path, "a" * 64, True)
