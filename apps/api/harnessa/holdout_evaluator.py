"""Isolated, locally operated evaluator for private Harnessa holdout cases.

This process receives immutable manifests and CAS references only. The private
suite, repository snapshots, verifier scripts, and artifact volume are mounted
read-only; none are added to an agent workspace or returned in the receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("harnessa.holdout")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_VERIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DATASET_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_MAX_REQUEST_BYTES = 1_000_000
_MAX_SUITE_BYTES = 1_000_000
_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
_MAX_CASE_ASSET_BYTES = 512 * 1024 * 1024
_MAX_VERIFIER_BYTES = 1 * 1024 * 1024
_MAX_ARCHIVE_FILES = 50_000
_MAX_PATCH_BYTES = 10 * 1024 * 1024
_MAX_PATCHES = 8
_MAX_VERIFIER_SECONDS = 120


@dataclass(frozen=True)
class EvaluatorConfig:
    bearer_token: str
    suite_path: Path
    base_dir: Path
    artifact_root: Path


@dataclass(frozen=True)
class HoldoutCase:
    task_id: str
    project_id: str
    base_sha: str
    source_archive: str
    source_archive_sha256: str
    verifier_id: str
    verifier_sha256: str = ""


def _read_private_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("configured evaluator data is unavailable")
        if path.stat().st_size > max_bytes:
            raise ValueError("configured evaluator data exceeds its size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("configured evaluator data is unavailable") from exc
    if not isinstance(value, dict):
        raise TypeError("configured evaluator data must be an object")
    return value


def _load_cases(path: Path) -> tuple[dict[str, HoldoutCase], str]:
    suite = _read_private_json(path, max_bytes=_MAX_SUITE_BYTES)
    raw_cases = suite.get("cases")
    dataset_id = suite.get("dataset_id")
    dataset_revision = suite.get("dataset_revision")
    dataset_license = suite.get("dataset_license")
    if (
        suite.get("schema_version") != 1
        or not isinstance(dataset_id, str)
        or not _DATASET_ID.fullmatch(dataset_id)
        or not isinstance(dataset_revision, str)
        or not 1 <= len(dataset_revision) <= 40
        or not isinstance(dataset_license, str)
        or not 1 <= len(dataset_license) <= 80
        or not isinstance(raw_cases, list)
        or not raw_cases
    ):
        raise ValueError("private holdout suite schema is invalid")
    if len(raw_cases) > 1_000:
        raise ValueError("private holdout suite has too many cases")

    cases: dict[str, HoldoutCase] = {}
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise TypeError("private holdout case schema is invalid")
        task_id = raw.get("task_id")
        project_id = raw.get("project_id")
        base_sha = raw.get("base_sha")
        archive = raw.get("source_archive")
        archive_digest = raw.get("source_archive_sha256")
        verifier_id = raw.get("verifier_id")
        if not isinstance(task_id, str) or not isinstance(project_id, str):
            raise TypeError("private holdout case identity is invalid")
        try:
            UUID(task_id)
            UUID(project_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("private holdout case identity is invalid") from exc
        if (
            not isinstance(base_sha, str)
            or not _REVISION.fullmatch(base_sha)
            or not isinstance(archive, str)
            or not archive
            or Path(archive).is_absolute()
            or ".." in PurePosixPath(archive).parts
            or not isinstance(archive_digest, str)
            or not _DIGEST.fullmatch(archive_digest)
            or not isinstance(verifier_id, str)
            or not _VERIFIER.fullmatch(verifier_id)
            or task_id in cases
        ):
            raise ValueError("private holdout case schema is invalid")
        cases[task_id] = HoldoutCase(
            task_id=task_id,
            project_id=project_id,
            base_sha=base_sha,
            source_archive=archive,
            source_archive_sha256=archive_digest,
            verifier_id=verifier_id,
        )
    suite_digest = hashlib.sha256(
        json.dumps(suite, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return cases, f"private:{dataset_id}@{dataset_revision}:{suite_digest[:16]}"


def _safe_child(root: Path, relative: str) -> Path:
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("evaluator reference is invalid")
    candidate = root
    for part in relative_path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("evaluator reference is invalid")
    if not candidate.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError("evaluator reference is invalid")
    return candidate


def _validate_case_assets(
    *, base_dir: Path, cases: dict[str, HoldoutCase]
) -> tuple[dict[str, HoldoutCase], str, dict[str, tuple[int, int, int, int]]]:
    """Validate and fingerprint every immutable input before reporting readiness."""

    validated: dict[str, HoldoutCase] = {}
    fingerprints: list[str] = []
    metadata: dict[str, tuple[int, int, int, int]] = {}
    total_bytes = 0
    for task_id, case in sorted(cases.items()):
        archive = _safe_child(base_dir, case.source_archive)
        verifier = _safe_child(base_dir, f"verifiers/{case.verifier_id}.py")
        if not archive.is_file() or not verifier.is_file():
            raise ValueError("configured evaluator case assets are unavailable")
        archive_size = archive.stat().st_size
        verifier_size = verifier.stat().st_size
        total_bytes += archive_size + verifier_size
        if (
            archive_size > _MAX_ARCHIVE_BYTES
            or verifier_size <= 0
            or verifier_size > _MAX_VERIFIER_BYTES
            or total_bytes > _MAX_CASE_ASSET_BYTES
        ):
            raise ValueError("configured evaluator case assets exceed their size limits")
        archive_digest = _sha256_file(archive)
        if not hmac.compare_digest(archive_digest, case.source_archive_sha256):
            raise ValueError("configured evaluator source archive checksum is invalid")
        verifier_digest = _sha256_file(verifier)
        validated[task_id] = HoldoutCase(
            task_id=case.task_id,
            project_id=case.project_id,
            base_sha=case.base_sha,
            source_archive=case.source_archive,
            source_archive_sha256=case.source_archive_sha256,
            verifier_id=case.verifier_id,
            verifier_sha256=verifier_digest,
        )
        fingerprints.append(f"{task_id}:{archive_digest}:{verifier_digest}")
        for path, key in ((archive, f"{task_id}:archive"), (verifier, f"{task_id}:verifier")):
            stat = path.stat()
            metadata[key] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    asset_digest = hashlib.sha256("\n".join(fingerprints).encode("ascii")).hexdigest()
    return validated, asset_digest, metadata


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _case_assets_unchanged(
    *,
    base_dir: Path,
    cases: dict[str, HoldoutCase],
    metadata: dict[str, tuple[int, int, int, int]],
) -> bool:
    for task_id, case in cases.items():
        for relative, key in (
            (case.source_archive, f"{task_id}:archive"),
            (f"verifiers/{case.verifier_id}.py", f"{task_id}:verifier"),
        ):
            try:
                path = _safe_child(base_dir, relative)
                stat = path.stat()
            except (OSError, ValueError):
                return False
            if not path.is_file() or metadata.get(key) != (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
            ):
                return False
    return True


def _verify_cas_artifact(root: Path, digest: str, size_bytes: int) -> bytes:
    if (
        not _DIGEST.fullmatch(digest)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 <= size_bytes <= _MAX_PATCH_BYTES
    ):
        raise ValueError("candidate artifact reference is invalid")
    key = f"sha256/{digest[:2]}/{digest}"
    path = _safe_child(root, key)
    if not path.is_file() or path.stat().st_size != size_bytes:
        raise ValueError("candidate artifact is unavailable")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("candidate artifact integrity check failed")
    return content


def _verify_manifest(manifest: dict[str, Any]) -> str:
    digest = manifest.get("digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("manifest digest is invalid")
    unsigned = dict(manifest)
    unsigned.pop("digest", None)
    actual = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(digest, actual):
        raise ValueError("manifest digest verification failed")
    if (
        not isinstance(manifest.get("task_id"), str)
        or not isinstance(manifest.get("project_id"), str)
        or not isinstance(manifest.get("base_sha"), str)
        or not isinstance(manifest.get("artifacts"), list)
    ):
        raise TypeError("manifest identity is incomplete")
    return digest


def _extract_archive(archive: Path, destination: Path) -> None:
    if archive.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("source archive exceeds its size limit")
    total_bytes = 0
    entries = 0
    with tarfile.open(archive, mode="r:*") as bundle:
        for member in bundle:
            entries += 1
            path = PurePosixPath(member.name)
            if (
                entries > _MAX_ARCHIVE_FILES
                or path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("source archive contains an unsafe path")
            output = destination.joinpath(*path.parts)
            if not output.resolve().is_relative_to(destination.resolve()):
                raise ValueError("source archive contains an unsafe path")
            if member.isdir():
                output.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            total_bytes += member.size
            if total_bytes > _MAX_EXTRACTED_BYTES:
                raise ValueError("source archive expands beyond its size limit")
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("source archive entry cannot be read")
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with source, output.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            output.chmod(0o600)


def _run_verifier(
    *,
    config: EvaluatorConfig,
    case: HoldoutCase,
    patches: list[bytes],
) -> bool:
    archive = _safe_child(config.base_dir, case.source_archive)
    archive_bytes = archive.read_bytes()
    if not hmac.compare_digest(
        hashlib.sha256(archive_bytes).hexdigest(), case.source_archive_sha256
    ):
        raise ValueError("source archive integrity check failed")
    verifier = _safe_child(config.base_dir, f"verifiers/{case.verifier_id}.py")
    verifier_bytes = verifier.read_bytes()
    if not hmac.compare_digest(
        hashlib.sha256(verifier_bytes).hexdigest(), case.verifier_sha256
    ):
        raise ValueError("configured evaluator verifier checksum changed")
    scratch_root = Path("/tmp")
    with tempfile.TemporaryDirectory(prefix="harnessa-holdout-", dir=scratch_root) as temp:
        work = Path(temp)
        repo = work / "repo"
        repo.mkdir(mode=0o700)
        _extract_archive(archive, repo)
        for index, patch in enumerate(patches):
            patch_path = work / f"candidate-{index}.patch"
            patch_path.write_bytes(patch)
            patch_path.chmod(0o600)
            base_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(work),
                "TMPDIR": str(work),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            check = subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=repo,
                env=base_env,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            if check.returncode != 0:
                return False
            apply = subprocess.run(
                ["git", "apply", str(patch_path)],
                cwd=repo,
                env=base_env,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            if apply.returncode != 0:
                return False
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(verifier), str(repo)],
                cwd=repo,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(work),
                    "TMPDIR": str(work),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_MAX_VERIFIER_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0


def create_app(config: EvaluatorConfig | None = None) -> FastAPI:
    """Create an app instance; deployment data is supplied only through env/mounts."""

    app = FastAPI(title="Harnessa private holdout evaluator", docs_url=None, redoc_url=None)
    app.state.config = config
    if config is None:
        app.state.cases = None
        app.state.suite_digest = None
        app.state.asset_metadata = {}
    else:
        try:
            cases, judge_revision = _load_cases(config.suite_path)
            cases, asset_digest, asset_metadata = _validate_case_assets(
                base_dir=config.base_dir, cases=cases
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Private holdout data unavailable (%s)", type(exc).__name__)
            app.state.cases = None
            app.state.judge_revision = None
            app.state.suite_digest = None
            app.state.asset_metadata = {}
        else:
            app.state.cases = cases
            app.state.judge_revision = f"{judge_revision}:{asset_digest[:16]}"
            app.state.suite_digest = asset_digest
            app.state.asset_metadata = asset_metadata
    app.state.lock = asyncio.Semaphore(1)

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        ready = bool(
            app.state.config is not None
            and app.state.cases
            and app.state.suite_digest
            and app.state.config.artifact_root.is_dir()
            and _case_assets_unchanged(
                base_dir=app.state.config.base_dir,
                cases=app.state.cases,
                metadata=app.state.asset_metadata,
            )
        )
        return JSONResponse(
            {"status": "ready" if ready else "unavailable"}, status_code=200 if ready else 503
        )

    @app.post("/evaluate")
    async def evaluate(request: Request) -> dict[str, str]:
        active = app.state.config
        if active is None or not app.state.cases or not app.state.suite_digest:
            raise HTTPException(status_code=503, detail="evaluator unavailable")
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {active.bearer_token}"
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_REQUEST_BYTES:
                    raise HTTPException(status_code=413, detail="manifest exceeds size limit")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content length") from exc
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > _MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="manifest exceeds size limit")
        try:
            manifest = json.loads(raw)
            if not isinstance(manifest, dict):
                raise TypeError("manifest must be an object")
            manifest_digest = _verify_manifest(manifest)
            case = app.state.cases.get(manifest.get("task_id"))
            if (
                case is None
                or case.project_id != manifest.get("project_id")
                or case.base_sha != manifest.get("base_sha")
            ):
                raise ValueError("manifest does not match an authorized holdout case")
            patches: list[bytes] = []
            for item in manifest["artifacts"]:
                if not isinstance(item, dict) or item.get("phase") != "composed":
                    continue
                if item.get("deliverable") is not True or not isinstance(item.get("artifacts"), list):
                    continue
                for artifact in item["artifacts"]:
                    if not isinstance(artifact, dict) or artifact.get("media_type") != "application/vnd.git-patch":
                        continue
                    digest, size_bytes = artifact.get("sha256"), artifact.get("size_bytes")
                    if not isinstance(digest, str) or not isinstance(size_bytes, int):
                        raise TypeError("candidate artifact reference is invalid")
                    patches.append(_verify_cas_artifact(active.artifact_root, digest, size_bytes))
                    if len(patches) > _MAX_PATCHES:
                        raise ValueError("manifest has too many candidate patches")
            async with app.state.lock:
                passed = bool(patches) and await run_in_threadpool(
                    _run_verifier, config=active, case=case, patches=patches
                )
            return {
                "status": "passed" if passed else "failed",
                "manifest_digest": manifest_digest,
                "judge_revision": app.state.judge_revision,
            }
        except HTTPException:
            raise
        except (ValueError, TypeError, KeyError, OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
            logger.warning("Private holdout evaluation rejected (%s)", type(exc).__name__)
            raise HTTPException(status_code=422, detail="manifest could not be evaluated") from exc

    return app


def _config_from_environment() -> EvaluatorConfig | None:
    token = os.environ.get("HARNESS_HOLDOUT_EVALUATOR_TOKEN", "").strip()
    suite_path = os.environ.get("HARNESS_HOLDOUT_SUITE_PATH", "").strip()
    base_dir = os.environ.get("HARNESS_HOLDOUT_BASE_DIR", "").strip()
    artifact_root = os.environ.get("HARNESS_HOLDOUT_ARTIFACT_ROOT", "").strip()
    if not any((token, suite_path, base_dir, artifact_root)):
        return None
    if not all((token, suite_path, base_dir, artifact_root)):
        raise ValueError("holdout evaluator environment is incomplete")
    return EvaluatorConfig(
        bearer_token=token,
        suite_path=Path(suite_path).resolve(),
        base_dir=Path(base_dir).resolve(),
        artifact_root=Path(artifact_root).resolve(),
    )


app = create_app(_config_from_environment())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001, access_log=False)
