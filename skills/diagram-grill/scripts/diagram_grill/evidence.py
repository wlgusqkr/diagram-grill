"""Bounded, read-only access to untrusted repository evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Sequence, Tuple


MAX_EVIDENCE_FILES = 50
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 1024 * 1024


class EvidenceReadError(RuntimeError):
    """Raised when repository evidence cannot be read within the safe boundary."""


@dataclass(frozen=True)
class EvidenceFile:
    """One safely read UTF-8 evidence file."""

    path: str
    sha256: str
    size_bytes: int
    lines: Tuple[str, ...]


def _relative_parts(value: str) -> Tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise EvidenceReadError("evidence paths must contain 1 to 512 characters")
    if any(ord(character) < 32 for character in value) or "\\" in value:
        raise EvidenceReadError("evidence paths must be normalized POSIX paths")

    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or value != posix.as_posix()
        or any(part in ("", ".", "..") for part in posix.parts)
    ):
        raise EvidenceReadError("evidence paths must stay relative to the repository")
    return tuple(posix.parts)


def _repository_root(value: Path) -> Path:
    supplied = Path(value)
    if supplied.is_symlink():
        raise EvidenceReadError("repository root must not be a symlink")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise EvidenceReadError("repository root is unavailable") from exc
    if not resolved.is_dir():
        raise EvidenceReadError("repository root must be a directory")
    return resolved


def _read_with_directory_fds(root: Path, parts: Sequence[str]) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    directory_descriptors: List[int] = []
    file_descriptor = -1
    try:
        current = os.open(str(root), directory_flags | os.O_NOFOLLOW)
        directory_descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(
                part,
                directory_flags | os.O_NOFOLLOW,
                dir_fd=current,
            )
            directory_descriptors.append(current)

        file_descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        file_status = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise EvidenceReadError("evidence path is not a regular file")
        with os.fdopen(file_descriptor, "rb", closefd=True) as evidence_file:
            file_descriptor = -1
            return evidence_file.read(MAX_FILE_BYTES + 1)
    except EvidenceReadError:
        raise
    except OSError as exc:
        raise EvidenceReadError(
            "evidence path is missing, inaccessible, or crosses a symlink"
        ) from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _read_with_path_checks(root: Path, parts: Sequence[str]) -> bytes:
    candidate = root
    try:
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise EvidenceReadError("evidence paths must not cross symlinks")
            os.lstat(str(candidate))
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise EvidenceReadError("evidence path is not a regular file")
        with resolved.open("rb") as evidence_file:
            return evidence_file.read(MAX_FILE_BYTES + 1)
    except EvidenceReadError:
        raise
    except (OSError, ValueError) as exc:
        raise EvidenceReadError(
            "evidence path is missing, inaccessible, or leaves the repository"
        ) from exc


def _read_bytes(root: Path, parts: Sequence[str]) -> bytes:
    secure_dir_fd_support = (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
    )
    if secure_dir_fd_support:
        return _read_with_directory_fds(root, parts)
    return _read_with_path_checks(root, parts)


def read_evidence_files(repository: Path, paths: Sequence[str]) -> Tuple[EvidenceFile, ...]:
    """Read normalized UTF-8 files without following repository symlinks."""

    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
        raise EvidenceReadError("paths must be a sequence of relative file paths")
    requested = tuple(paths)
    if not requested or len(requested) > MAX_EVIDENCE_FILES:
        raise EvidenceReadError(
            "read between 1 and {} evidence files per request".format(MAX_EVIDENCE_FILES)
        )
    if len(set(requested)) != len(requested):
        raise EvidenceReadError("duplicate evidence paths are not allowed")

    root = _repository_root(Path(repository))
    results = []
    total_bytes = 0
    for relative_path in requested:
        parts = _relative_parts(relative_path)
        data = _read_bytes(root, parts)
        if len(data) > MAX_FILE_BYTES:
            raise EvidenceReadError("evidence file exceeds the 512 KiB limit")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise EvidenceReadError("evidence request exceeds the 1 MiB total limit")
        if b"\x00" in data:
            raise EvidenceReadError("binary evidence files are not supported")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceReadError("evidence files must be valid UTF-8") from exc
        results.append(
            EvidenceFile(
                path=relative_path,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                lines=tuple(text.splitlines()),
            )
        )
    return tuple(results)


__all__ = [
    "EvidenceFile",
    "EvidenceReadError",
    "MAX_EVIDENCE_FILES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "read_evidence_files",
]
