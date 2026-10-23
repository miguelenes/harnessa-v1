"""Content-addressed local run artifacts, written with private permissions."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class StoredArtifact:
    digest: str
    media_type: str
    size_bytes: int
    storage_key: str


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def put_bytes(self, content: bytes, *, media_type: str) -> StoredArtifact:
        digest = hashlib.sha256(content).hexdigest()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        destination = self.root / "sha256" / digest[:2] / digest
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        if not destination.exists():
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
            try:
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)
        return StoredArtifact(
            digest=digest,
            media_type=media_type,
            size_bytes=len(content),
            storage_key=f"sha256/{digest[:2]}/{digest}",
        )

    def verify_reference(
        self,
        storage_key: str,
        *,
        expected_digest: str,
        expected_size: int,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> str:
        """Verify one referenced CAS object with a fixed byte and path boundary."""
        if not re.fullmatch(r"sha256/[0-9a-f]{2}/[0-9a-f]{64}", storage_key):
            return "invalid_reference"
        _, prefix, digest = storage_key.split("/")
        if digest != expected_digest or prefix != expected_digest[:2]:
            return "invalid_reference"
        path = self.root / storage_key
        try:
            if (
                any(
                    item.is_symlink()
                    for item in (self.root / "sha256", self.root / "sha256" / prefix, path)
                )
                or not path.is_file()
            ):
                return "missing"
            if not path.resolve(strict=True).is_relative_to(self.root):
                return "invalid_reference"
            stat_result = path.stat()
            if stat_result.st_size != expected_size:
                return "size_mismatch"
            if stat_result.st_size > max_bytes:
                return "over_limit"
            digest_state = hashlib.sha256()
            read_bytes = 0
            with path.open("rb") as content:
                while chunk := content.read(1024 * 1024):
                    read_bytes += len(chunk)
                    if read_bytes > max_bytes:
                        return "over_limit"
                    digest_state.update(chunk)
            return (
                "verified" if digest_state.hexdigest() == expected_digest else "checksum_mismatch"
            )
        except FileNotFoundError:
            return "missing"

    def remove_unreferenced(self, referenced_keys: set[str], *, minimum_age_seconds: int) -> int:
        """Collect old CAS files without a committed artifact row."""
        root = self.root / "sha256"
        if not root.exists():
            return 0
        cutoff = datetime.now(UTC).timestamp() - minimum_age_seconds
        removed = 0
        for prefix in root.iterdir():
            if not prefix.is_dir() or prefix.is_symlink():
                continue
            for path in prefix.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not re.fullmatch(r"[0-9a-f]{64}", path.name)
                ):
                    continue
                key = f"sha256/{prefix.name}/{path.name}"
                if key in referenced_keys:
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
            try:
                prefix.rmdir()
            except OSError:
                pass
        return removed
