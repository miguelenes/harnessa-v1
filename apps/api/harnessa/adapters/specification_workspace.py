"""Private Git workspaces and bounded OpenSpec CLI calls for interviews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from harnessa.config import settings
from harnessa.db.models import Repository
from harnessa.domain.retrieval import RepositoryCitation, RepositorySearchResult
from harnessa.services.retrieval_governance import eligible_path, safe_retrieval_content

_CHANGE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_GIT_TIMEOUT_SECONDS = 60
_CLI_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
_SEARCH_FILE_LIMIT = 2500
_SEARCH_TOTAL_BYTES_LIMIT = 32 * 1024 * 1024
_SEARCH_FILE_BYTES_LIMIT = 512 * 1024
_SEARCH_EXTENSIONS = frozenset(
    {".astro", ".cs", ".go", ".java", ".js", ".jsx", ".kt", ".md", ".py", ".rs",
     ".svelte", ".sql", ".ts", ".tsx", ".yaml", ".yml"}
)
_SEARCH_EXCLUDED_PARTS = frozenset(
    {".git", ".next", ".svelte-kit", "build", "dist", "node_modules", "vendor"}
)


class SpecificationWorkspaceError(RuntimeError):
    """A trusted local repository or OpenSpec operation is unavailable."""


@dataclass(frozen=True)
class SpecificationWorkspace:
    interview_id: UUID
    repository_path: Path
    workspace_path: Path
    default_branch: str
    base_sha: str
    branch: str
    change_id: str


@dataclass(frozen=True)
class OpenSpecValidation:
    version: str
    valid: bool
    result: dict[str, object]


@dataclass(frozen=True)
class RepositorySourceFile:
    path: str
    content: str
    content_hash: str


class SpecificationWorkspaceManager:
    """Create task-scoped local branches with no remote and an exact base SHA."""

    def __init__(self, *, root: Path | None = None) -> None:
        self.root = (root or Path(settings.openspec_workspace_root)).expanduser().resolve()

    async def source_path(self, repository: Repository) -> Path:
        return await asyncio.to_thread(self._source_path, repository.target_id)

    async def current_base(self, repository: Repository) -> str:
        source = await self.source_path(repository)
        return await asyncio.to_thread(self._current_base, source, repository.default_branch)

    async def open(
        self,
        interview_id: UUID,
        repository: Repository,
        *,
        change_id: str,
        base_sha: str,
        head_sha: str,
        allow_one_pending_commit: bool = False,
    ) -> SpecificationWorkspace:
        return await asyncio.to_thread(
            self._open, interview_id, repository, change_id, base_sha, head_sha,
            allow_one_pending_commit
        )

    async def create(
        self,
        interview_id: UUID,
        repository: Repository,
        change_id: str,
        files: dict[str, str],
    ) -> tuple[SpecificationWorkspace, str, str]:
        return await asyncio.to_thread(
            self._create, interview_id, repository, change_id, files
        )

    async def commit_bundle(
        self,
        workspace: SpecificationWorkspace,
        files: dict[str, str],
        *,
        revision: int,
        expected_head: str,
    ) -> tuple[str, str, list[str]]:
        return await asyncio.to_thread(
            self._commit_bundle, workspace, files, revision, expected_head
        )

    async def validate(self, workspace: SpecificationWorkspace) -> OpenSpecValidation:
        version, result = await asyncio.gather(
            self._run_cli(workspace.workspace_path, ["--version"]),
            self._run_cli(
                workspace.workspace_path,
                [
                    "validate",
                    workspace.change_id,
                    "--type",
                    "change",
                    "--json",
                    "--no-interactive",
                ],
            ),
        )
        try:
            payload = json.loads(result)
        except json.JSONDecodeError as exc:
            raise SpecificationWorkspaceError("OpenSpec returned invalid JSON validation output") from exc
        if not isinstance(payload, dict):
            raise SpecificationWorkspaceError("OpenSpec validation output was not an object")
        items = payload.get("items")
        valid = (
            isinstance(items, list)
            and len(items) == 1
            and isinstance(items[0], dict)
            and items[0].get("valid") is True
        )
        return OpenSpecValidation(version=version.strip(), valid=valid, result=payload)

    async def artifact_diff(
        self, workspace: SpecificationWorkspace, base_sha: str, head_sha: str
    ) -> str:
        return await asyncio.to_thread(
            self._git,
            workspace.workspace_path,
            "diff",
            "--no-ext-diff",
            "--unified=3",
            base_sha,
            head_sha,
            "--",
            "openspec/config.yaml",
            f"openspec/changes/{workspace.change_id}",
        )

    async def read_bundle(
        self, workspace: SpecificationWorkspace, head_sha: str
    ) -> dict[str, str]:
        return await asyncio.to_thread(self._read_bundle, workspace, head_sha)

    async def search(
        self, repository_target_id: str, default_branch: str, query: str, limit: int
    ) -> RepositorySearchResult:
        return await asyncio.to_thread(
            self._search, repository_target_id, default_branch, query, limit
        )

    async def snapshot(
        self, repository: Repository, approved_skill_paths: tuple[str, ...] = ()
    ) -> tuple[str, tuple[RepositorySourceFile, ...], dict[str, int]]:
        """Read the bounded current default-branch tree for metadata-only indexing."""
        return await asyncio.to_thread(
            self._snapshot,
            repository.target_id,
            repository.default_branch,
            approved_skill_paths,
        )

    async def remove(self, workspace: SpecificationWorkspace) -> None:
        await asyncio.to_thread(self._remove, workspace)

    def _source_path(self, target_id: str) -> Path:
        try:
            mapping = json.loads(settings.openspec_repository_sources_json)
        except json.JSONDecodeError as exc:
            raise SpecificationWorkspaceError(
                "OPENSPEC_REPOSITORY_SOURCES_JSON must be valid JSON"
            ) from exc
        if not isinstance(mapping, dict):
            raise SpecificationWorkspaceError(
                "OPENSPEC_REPOSITORY_SOURCES_JSON must map repository target IDs to local paths"
            )
        raw_path = mapping.get(target_id)
        if not isinstance(raw_path, str) or not raw_path:
            raise SpecificationWorkspaceError(
                "No local OpenSpec source is configured for this repository target"
            )
        untrusted_path = Path(raw_path).expanduser()
        if untrusted_path.is_symlink():
            raise SpecificationWorkspaceError("Configured repository sources may not be symlinks")
        source = untrusted_path.resolve(strict=True)
        if not source.is_dir():
            raise SpecificationWorkspaceError("Configured repository source is not a directory")
        root = Path(self._git(source, "rev-parse", "--show-toplevel").strip()).resolve()
        if root != source:
            raise SpecificationWorkspaceError("Configured repository source must be a Git root")
        return source

    def _current_base(self, source: Path, default_branch: str) -> str:
        if (
            not default_branch
            or default_branch.startswith("-")
            or ".." in default_branch
            or "@{" in default_branch
            or any(character in default_branch for character in " ~^:?*[\\")
        ):
            raise SpecificationWorkspaceError("Repository default branch is not a valid Git ref")
        revision = f"refs/heads/{default_branch}^{{commit}}"
        self._git(source, "check-ref-format", f"refs/heads/{default_branch}")
        return self._git(source, "rev-parse", "--verify", revision).strip()

    def _create(
        self,
        interview_id: UUID,
        repository: Repository,
        change_id: str,
        files: dict[str, str],
    ) -> tuple[SpecificationWorkspace, str, str]:
        if not _CHANGE_ID.fullmatch(change_id):
            raise SpecificationWorkspaceError("OpenSpec change ID is not a safe slug")
        source = self._source_path(repository.target_id)
        base_sha = self._current_base(source, repository.default_branch)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        workspace_path = self.root / interview_id.hex
        if workspace_path.exists() or workspace_path.is_symlink():
            recovered = self._recover_created_workspace(
                workspace_path, interview_id, source, repository.default_branch,
                base_sha, change_id, files
            )
            if recovered is not None:
                head = self._git(recovered.workspace_path, "rev-parse", "HEAD").strip()
                return recovered, head, self._content_digest(files, self._bundle_paths(change_id))
        branch = f"harnessa/spec-{interview_id.hex}"
        workspace = SpecificationWorkspace(
            interview_id=interview_id,
            repository_path=source,
            workspace_path=workspace_path,
            default_branch=repository.default_branch,
            base_sha=base_sha,
            branch=branch,
            change_id=change_id,
        )
        workspace_path.mkdir(mode=0o700)
        try:
            object_format = self._git(source, "rev-parse", "--show-object-format").strip()
            init_args = ["init", "--quiet"]
            if object_format != "sha1":
                init_args.append(f"--object-format={object_format}")
            self._git(workspace_path, *init_args)
            self._git(
                workspace_path,
                "-c",
                "core.hooksPath=/dev/null",
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                str(source),
                base_sha,
            )
            self._git(workspace_path, "checkout", "--quiet", "--detach", base_sha)
            self._git(workspace_path, "checkout", "--quiet", "-b", branch)
            for key, value in (
                ("user.name", "Harnessa Specification"),
                ("user.email", "specification@harnessa.invalid"),
                ("credential.helper", ""),
                ("core.hooksPath", "/dev/null"),
            ):
                self._git(workspace_path, "config", key, value)
            if not (workspace_path / "openspec" / "config.yaml").is_file():
                asyncio.run(self._run_cli(workspace_path, ["init", "--tools", "none", "--no-animation"]))
            if (workspace_path / "openspec" / "changes" / change_id).exists():
                raise SpecificationWorkspaceError("OpenSpec change ID already exists in the source")
            asyncio.run(self._run_cli(workspace_path, ["new", "change", change_id]))
            digest, _ = self._write_files(workspace, files)
            paths = ["openspec/config.yaml", *self._bundle_paths(change_id)]
            paths = [path for path in paths if (workspace_path / path).is_file()]
            self._git(workspace_path, "add", "--", *paths)
            self._git(workspace_path, "commit", "--quiet", "--no-gpg-sign", "-m", "Harnessa specification revision 1")
            head = self._git(workspace_path, "rev-parse", "HEAD").strip()
            return workspace, head, digest
        except Exception:
            shutil.rmtree(workspace_path, ignore_errors=True)
            raise

    def _recover_created_workspace(
        self,
        workspace_path: Path,
        interview_id: UUID,
        source: Path,
        default_branch: str,
        base_sha: str,
        change_id: str,
        files: dict[str, str],
    ) -> SpecificationWorkspace | None:
        if workspace_path.is_symlink() or not workspace_path.is_dir():
            raise SpecificationWorkspaceError("An OpenSpec workspace path is not a private directory")
        expected_branch = f"harnessa/spec-{interview_id.hex}"
        top = Path(self._git(workspace_path, "rev-parse", "--show-toplevel").strip()).resolve()
        if top != workspace_path.resolve():
            raise SpecificationWorkspaceError("Existing OpenSpec workspace is not an owned Git root")
        if self._git(workspace_path, "remote").strip():
            raise SpecificationWorkspaceError("Existing OpenSpec workspace unexpectedly has a remote")
        branch = self._git(workspace_path, "branch", "--show-current").strip()
        head = self._git(workspace_path, "rev-parse", "HEAD").strip()
        if branch != expected_branch:
            raise SpecificationWorkspaceError("Existing OpenSpec workspace branch identity differs")
        if head == base_sha:
            shutil.rmtree(workspace_path)
            return None
        parent = self._git(workspace_path, "rev-parse", "HEAD^").strip()
        if parent != base_sha:
            raise SpecificationWorkspaceError("Existing OpenSpec workspace has an unexpected base commit")
        workspace = SpecificationWorkspace(
            interview_id=interview_id,
            repository_path=source,
            workspace_path=workspace_path,
            default_branch=default_branch,
            base_sha=base_sha,
            branch=expected_branch,
            change_id=change_id,
        )
        if self._hash_committed_bundle(workspace, head) != self._content_digest(
            files, self._bundle_paths(change_id)
        ):
            raise SpecificationWorkspaceError(
                "Existing OpenSpec workspace content differs from the idempotent create request"
            )
        return workspace

    def _open(
        self,
        interview_id: UUID,
        repository: Repository,
        change_id: str,
        base_sha: str,
        head_sha: str,
        allow_one_pending_commit: bool,
    ) -> SpecificationWorkspace:
        source = self._source_path(repository.target_id)
        workspace_path = self.root / interview_id.hex
        expected_branch = f"harnessa/spec-{interview_id.hex}"
        if workspace_path.is_symlink() or not workspace_path.is_dir():
            raise SpecificationWorkspaceError("The private OpenSpec workspace is unavailable")
        top = Path(self._git(workspace_path, "rev-parse", "--show-toplevel").strip()).resolve()
        if top != workspace_path.resolve():
            raise SpecificationWorkspaceError("The private OpenSpec workspace root changed")
        if self._git(workspace_path, "remote").strip():
            raise SpecificationWorkspaceError("The private OpenSpec workspace unexpectedly has a remote")
        if self._git(workspace_path, "branch", "--show-current").strip() != expected_branch:
            raise SpecificationWorkspaceError("The private OpenSpec workspace branch changed")
        actual_head = self._git(workspace_path, "rev-parse", "HEAD").strip()
        if actual_head != head_sha and (
            not allow_one_pending_commit
            or self._git(workspace_path, "rev-parse", "HEAD^").strip() != head_sha
        ):
            raise SpecificationWorkspaceError("The Git artifact head requires reconciliation")
        self._git(workspace_path, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
        return SpecificationWorkspace(
            interview_id=interview_id,
            repository_path=source,
            workspace_path=workspace_path,
            default_branch=repository.default_branch,
            base_sha=base_sha,
            branch=expected_branch,
            change_id=change_id,
        )

    def _commit_bundle(
        self,
        workspace: SpecificationWorkspace,
        files: dict[str, str],
        revision: int,
        expected_head: str,
    ) -> tuple[str, str, list[str]]:
        paths = self._bundle_paths(workspace.change_id)
        if set(files) != set(paths):
            raise SpecificationWorkspaceError("Artifact update must contain the complete OpenSpec bundle")
        digest = self._content_digest(files, paths)
        actual_head = self._git(workspace.workspace_path, "rev-parse", "HEAD").strip()
        if actual_head != expected_head:
            committed_digest = self._hash_committed_bundle(workspace, actual_head)
            if committed_digest == digest:
                return actual_head, digest, paths
            raise SpecificationWorkspaceError(
                "The private specification branch has an unrecorded commit requiring reconciliation"
            )
        self._write_files(workspace, files)
        self._git(workspace.workspace_path, "add", "--", *paths)
        changed = subprocess.run(
            ["git", "-C", str(workspace.workspace_path), "diff", "--cached", "--quiet"],
            check=False,
            capture_output=True,
            env=self._git_env(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if changed.returncode == 0:
            raise SpecificationWorkspaceError("The artifact update does not change the bundle")
        if changed.returncode != 1:
            raise SpecificationWorkspaceError("Could not compare the staged specification update")
        self._git(
            workspace.workspace_path,
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            f"Harnessa specification revision {revision}",
        )
        return self._git(workspace.workspace_path, "rev-parse", "HEAD").strip(), digest, paths

    def _write_files(
        self, workspace: SpecificationWorkspace, files: dict[str, str]
    ) -> tuple[str, list[str]]:
        expected = self._bundle_paths(workspace.change_id)
        if set(files) != set(expected):
            raise SpecificationWorkspaceError("Artifact update must contain the complete OpenSpec bundle")
        digest = self._content_digest(files, expected)
        for relative in sorted(expected):
            destination = workspace.workspace_path / relative
            resolved_parent = destination.parent.resolve()
            if not resolved_parent.is_relative_to(workspace.workspace_path.resolve()):
                raise SpecificationWorkspaceError("Artifact path escaped its private Git workspace")
            if destination.is_symlink():
                raise SpecificationWorkspaceError("Refusing to overwrite a symlinked artifact")
            value = files[relative]
            data = value.encode("utf-8")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            temporary.write_bytes(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        return digest, expected

    @staticmethod
    def _content_digest(files: dict[str, str], paths: list[str]) -> str:
        digest = hashlib.sha256()
        for relative in sorted(paths):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(files[relative].encode("utf-8"))
        return digest.hexdigest()

    def _hash_committed_bundle(self, workspace: SpecificationWorkspace, head_sha: str) -> str:
        digest = hashlib.sha256()
        for relative in sorted(self._bundle_paths(workspace.change_id)):
            content = self._git(workspace.workspace_path, "show", f"{head_sha}:{relative}").encode("utf-8")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
        return digest.hexdigest()

    def _read_bundle(
        self, workspace: SpecificationWorkspace, head_sha: str
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in self._bundle_paths(workspace.change_id):
            result[relative] = self._git(
                workspace.workspace_path, "show", f"{head_sha}:{relative}"
            )
        return result

    def _search(
        self, target_id: str, default_branch: str, query: str, limit: int
    ) -> RepositorySearchResult:
        source = self._source_path(target_id)
        revision = self._current_base(source, default_branch)
        terms = list(dict.fromkeys(re.findall(r"[\w-]{2,}", query.casefold())))[:16]
        if not terms:
            return RepositorySearchResult(revision, ())

        tree = self._git(source, "ls-tree", "-r", "-z", revision)
        blobs: list[tuple[str, str]] = []
        for entry in tree.split("\0"):
            if not entry or "\t" not in entry:
                continue
            metadata, relative = entry.split("\t", 1)
            fields = metadata.split()
            if len(fields) != 3 or fields[1] != "blob" or fields[0] not in {"100644", "100755"}:
                continue
            if self._searchable_path(relative) and eligible_path(relative):
                blobs.append((fields[2], relative))
            if len(blobs) >= _SEARCH_FILE_LIMIT:
                break

        checked = cast(
            list[str],
            self._git_batch(
                source, "cat-file", "--batch-check", objects=[oid for oid, _ in blobs]
            ),
        )
        bounded: list[tuple[str, str, int]] = []
        total_bytes = 0
        for (oid, relative), metadata in zip(blobs, checked, strict=True):
            fields = metadata.split()
            if len(fields) < 3 or fields[1] != "blob":
                continue
            try:
                size = int(fields[2])
            except ValueError:
                continue
            if size < 0 or size > _SEARCH_FILE_BYTES_LIMIT:
                continue
            if total_bytes + size > _SEARCH_TOTAL_BYTES_LIMIT:
                break
            total_bytes += size
            bounded.append((oid, relative, size))

        object_contents = self._git_batch(
            source,
            "cat-file",
            "--batch",
            objects=[oid for oid, _, _ in bounded],
            binary=True,
        )
        ranked: list[tuple[int, RepositoryCitation]] = []
        for (_, relative, _), raw_content in zip(
            bounded, cast(list[bytes], object_contents), strict=True
        ):
            content = raw_content.decode("utf-8", errors="replace")
            if not safe_retrieval_content(content):
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                folded = line.casefold()
                matched = tuple(term for term in terms if term in folded)
                if not matched:
                    continue
                score = sum(folded.count(term) for term in matched)
                ranked.append(
                    (
                        score,
                        RepositoryCitation(
                            revision=revision,
                            path=relative,
                            line=line_number,
                            snippet=line[:800],
                            matched_terms=matched,
                        ),
                    )
                )
            if len(ranked) >= max(2000, limit * 100):
                ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
                del ranked[max(2000, limit * 100) :]
        ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
        return RepositorySearchResult(
            revision, tuple(citation for _, citation in ranked[:limit])
        )

    def _snapshot(
        self, target_id: str, default_branch: str, approved_skill_paths: tuple[str, ...]
    ) -> tuple[str, tuple[RepositorySourceFile, ...], dict[str, int]]:
        source = self._source_path(target_id)
        revision = self._current_base(source, default_branch)
        tree = self._git(source, "ls-tree", "-r", "-z", revision)
        blobs: list[tuple[str, str]] = []
        eligible_count = 0
        for entry in tree.split("\0"):
            if not entry or "\t" not in entry:
                continue
            metadata, relative = entry.split("\t", 1)
            fields = metadata.split()
            if (
                len(fields) == 3
                and fields[1] == "blob"
                and fields[0] in {"100644", "100755"}
                and self._searchable_path(relative, approved_skill_paths)
            ):
                eligible_count += 1
                if len(blobs) < _SEARCH_FILE_LIMIT:
                    blobs.append((fields[2], relative))
        omitted: dict[str, int] = {}
        if eligible_count > _SEARCH_FILE_LIMIT:
            omitted["file_limit"] = eligible_count - _SEARCH_FILE_LIMIT
        checked = cast(
            list[str],
            self._git_batch(source, "cat-file", "--batch-check", objects=[oid for oid, _ in blobs]),
        )
        bounded: list[tuple[str, str]] = []
        total_bytes = 0
        for index, ((oid, relative), metadata) in enumerate(zip(blobs, checked, strict=True)):
            fields = metadata.split()
            if len(fields) < 3 or fields[1] != "blob":
                continue
            try:
                size = int(fields[2])
            except ValueError:
                omitted["invalid_file_size"] = omitted.get("invalid_file_size", 0) + 1
                continue
            if size < 0:
                omitted["invalid_file_size"] = omitted.get("invalid_file_size", 0) + 1
                continue
            if size > _SEARCH_FILE_BYTES_LIMIT:
                omitted["file_bytes"] = omitted.get("file_bytes", 0) + 1
                continue
            if total_bytes + size > _SEARCH_TOTAL_BYTES_LIMIT:
                omitted["total_bytes"] = omitted.get("total_bytes", 0) + len(blobs) - index
                break
            total_bytes += size
            bounded.append((oid, relative))
        contents = cast(
            list[bytes],
            self._git_batch(
                source, "cat-file", "--batch", objects=[oid for oid, _ in bounded], binary=True
            ),
        )
        files = tuple(
            RepositorySourceFile(
                path=relative,
                content=raw.decode("utf-8", errors="replace"),
                content_hash=hashlib.sha256(raw).hexdigest(),
            )
            for (_, relative), raw in zip(bounded, contents, strict=True)
        )
        return revision, files, omitted

    @staticmethod
    def _searchable_path(relative: str, approved_skill_paths: tuple[str, ...] = ()) -> bool:
        path = Path(relative)
        approved_skill = relative in approved_skill_paths
        if set(path.parts) & _SEARCH_EXCLUDED_PARTS:
            return False
        if path.name.startswith(".") and not approved_skill:
            return False
        if approved_skill and not (
            relative.startswith(".agents/skills/") and path.name == "SKILL.md"
        ):
            return False
        if relative == "skills-lock.json":
            return True
        if path.suffix.casefold() not in _SEARCH_EXTENSIONS:
            return False
        return relative.startswith(
            ("openspec/specs/", "apps/", "packages/", "planning/adr", "docs/")
        ) or relative in {"planning/architecture-decisions.md", "docs/architecture.md"}

    @classmethod
    def _git_batch(
        cls,
        cwd: Path,
        *arguments: str,
        objects: list[str],
        binary: bool = False,
    ) -> list[str] | list[bytes]:
        if not objects:
            return []
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            check=False,
            input=("\n".join(objects) + "\n").encode("ascii"),
            capture_output=True,
            env=cls._git_env(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace")[-500:]
            raise SpecificationWorkspaceError(f"Git batch read failed: {detail}")
        if not binary:
            return result.stdout.decode("utf-8", errors="replace").splitlines()
        output = result.stdout
        items: list[bytes] = []
        offset = 0
        for expected_oid in objects:
            header_end = output.find(b"\n", offset)
            if header_end < 0:
                raise SpecificationWorkspaceError("Git returned a truncated batch response")
            header = output[offset:header_end].split()
            if len(header) < 3 or header[0].decode("ascii", errors="ignore") != expected_oid:
                raise SpecificationWorkspaceError("Git batch response did not match requested content")
            try:
                size = int(header[2])
            except ValueError as exc:
                raise SpecificationWorkspaceError("Git returned an invalid batch object size") from exc
            content_start = header_end + 1
            content_end = content_start + size
            if size < 0 or content_end >= len(output) or output[content_end : content_end + 1] != b"\n":
                raise SpecificationWorkspaceError("Git returned a truncated batch object")
            items.append(output[content_start:content_end])
            offset = content_end + 1
        return items

    def _remove(self, workspace: SpecificationWorkspace) -> None:
        resolved_root = self.root.resolve()
        resolved_workspace = workspace.workspace_path.resolve()
        if (
            resolved_workspace.parent != resolved_root
            or resolved_workspace.name != workspace.interview_id.hex
        ):
            raise SpecificationWorkspaceError("Refusing to remove an unowned specification workspace")
        if workspace.workspace_path.is_symlink():
            raise SpecificationWorkspaceError("Refusing to remove a symlinked specification workspace")
        shutil.rmtree(workspace.workspace_path, ignore_errors=False)

    @staticmethod
    def _bundle_paths(change_id: str) -> list[str]:
        prefix = f"openspec/changes/{change_id}"
        return [
            f"{prefix}/proposal.md",
            f"{prefix}/design.md",
            f"{prefix}/tasks.md",
            f"{prefix}/specs/{change_id}/spec.md",
        ]

    async def _run_cli(self, cwd: Path, arguments: list[str]) -> str:
        cli_path = self._cli_path()
        configured_node_path = settings.openspec_node_path.strip()
        node_path = (
            str(Path(configured_node_path).expanduser().resolve())
            if configured_node_path
            else shutil.which("node")
        )
        if not cli_path.is_file() or node_path is None or not Path(node_path).is_file():
            raise SpecificationWorkspaceError("Pinned OpenSpec CLI or Node.js is unavailable")
        environment = {
            "PATH": f"{Path(node_path).parent}{os.pathsep}{os.defpath}",
            "HOME": str(cwd),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        process = await asyncio.create_subprocess_exec(
            str(cli_path),
            *arguments,
            cwd=cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        if process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise SpecificationWorkspaceError("OpenSpec CLI pipes could not be initialized")
        async def read_bounded(stream: asyncio.StreamReader) -> bytes:
            chunks: list[bytes] = []
            size = 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > _CLI_OUTPUT_LIMIT_BYTES:
                    raise SpecificationWorkspaceError(
                        "OpenSpec CLI output exceeded its configured bound"
                    )
                chunks.append(chunk)
            return b"".join(chunks)

        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(
                    read_bounded(process.stdout),
                    read_bounded(process.stderr),
                    process.wait(),
                ),
                timeout=settings.openspec_cli_timeout_seconds,
            )
        except (TimeoutError, SpecificationWorkspaceError) as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            if isinstance(exc, TimeoutError):
                raise SpecificationWorkspaceError(
                    "OpenSpec CLI exceeded its configured timeout"
                ) from exc
            raise
        if process.returncode:
            if arguments[:1] == ["validate"] and "--json" in arguments:
                try:
                    validation_payload = json.loads(stdout.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    validation_payload = None
                if (
                    isinstance(validation_payload, dict)
                    and isinstance(validation_payload.get("items"), list)
                ):
                    # OpenSpec 1.14.3 exits nonzero for invalid changes while
                    # still returning the machine-readable validation result.
                    return stdout.decode("utf-8", errors="replace")
            detail = stderr.decode("utf-8", errors="replace")[-1000:]
            raise SpecificationWorkspaceError(
                f"OpenSpec CLI exited with status {process.returncode}: {detail}"
            )
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _cli_path() -> Path:
        configured = Path(__file__).resolve().parents[4] / "node_modules" / ".bin" / "openspec"
        return configured

    @staticmethod
    def _git_env() -> dict[str, str]:
        return {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_TEMPLATE_DIR": "/dev/null",
        }

    @classmethod
    def _git(cls, cwd: Path, *arguments: str) -> str:
        environment = cls._git_env()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Harnessa Specification",
                "GIT_AUTHOR_EMAIL": "specification@harnessa.invalid",
                "GIT_COMMITTER_NAME": "Harnessa Specification",
                "GIT_COMMITTER_EMAIL": "specification@harnessa.invalid",
            }
        )
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode:
            detail = result.stderr.strip()[:500]
            raise SpecificationWorkspaceError(
                f"Git specification workspace operation failed ({arguments[0]}): {detail}"
            )
        return result.stdout
