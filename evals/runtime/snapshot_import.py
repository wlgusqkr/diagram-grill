#!/usr/bin/python3
"""Import one bounded, link-free tar stream into an empty snapshot root."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tarfile
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, List, Tuple


def _canonical_name(name: str) -> Tuple[str, str]:
    if not name or "\x00" in name or "\\" in name:
        raise RuntimeError("snapshot member has an invalid name")
    pure = PurePosixPath(name.rstrip("/"))
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise RuntimeError("snapshot member escapes its root")
    normalized = "/".join(unicodedata.normalize("NFC", part) for part in pure.parts)
    folded = normalized.casefold()
    return normalized, folded


def _validated_members(
    archive: tarfile.TarFile, max_entries: int, max_bytes: int
) -> List[Tuple[tarfile.TarInfo, str]]:
    accepted: List[Tuple[tarfile.TarInfo, str]] = []
    seen = set()
    total_bytes = 0
    for member in archive:
        if len(accepted) >= max_entries:
            raise RuntimeError("snapshot exceeds its entry limit")
        normalized, folded = _canonical_name(member.name)
        if folded in seen:
            raise RuntimeError("snapshot contains a duplicate member")
        seen.add(folded)
        if member.isdir():
            pass
        elif member.isreg():
            if member.size < 0 or getattr(member, "sparse", None):
                raise RuntimeError("snapshot contains a sparse or invalid file")
            total_bytes += member.size
            if total_bytes > max_bytes:
                raise RuntimeError("snapshot exceeds its byte limit")
        else:
            raise RuntimeError("snapshot contains a link or special file")
        accepted.append((member, normalized))
    return accepted


def _mkdir(root: Path, relative: str) -> None:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            result = current.lstat()
            if not stat.S_ISDIR(result.st_mode) or current.is_symlink():
                raise RuntimeError("snapshot directory collides with a non-directory")


def import_snapshot(
    root: Path,
    source: BinaryIO,
    *,
    max_entries: int,
    max_bytes: int,
) -> None:
    target = root.resolve(strict=True)
    result = target.lstat()
    if not stat.S_ISDIR(result.st_mode) or target.is_symlink():
        raise RuntimeError("snapshot root must be a real directory")
    if any(target.iterdir()):
        raise RuntimeError("snapshot root must be empty")
    archive_limit = max_bytes + 8 * 1024 * 1024
    with tempfile.TemporaryFile() as buffered:
        received = 0
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > archive_limit:
                raise RuntimeError("snapshot archive exceeds its transport limit")
            buffered.write(chunk)
        buffered.seek(0)
        with tarfile.open(fileobj=buffered, mode="r|") as archive:
            members = _validated_members(archive, max_entries, max_bytes)
        buffered.seek(0)
        with tarfile.open(fileobj=buffered, mode="r|") as archive:
            for member, normalized in zip(archive, (name for _, name in members)):
                destination = target / normalized
                if member.isdir():
                    _mkdir(target, normalized)
                    continue
                _mkdir(target, str(PurePosixPath(normalized).parent))
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError("snapshot file content is unavailable")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(destination, flags, 0o600)
                try:
                    remaining = member.size
                    while remaining:
                        chunk = extracted.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError("snapshot file ended early")
                        written = os.write(descriptor, chunk)
                        if written != len(chunk):
                            raise RuntimeError("short snapshot write")
                        remaining -= len(chunk)
                    if extracted.read(1):
                        raise RuntimeError("snapshot file exceeded its declared size")
                finally:
                    os.close(descriptor)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--max-entries", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    return parser


def main(argv: Iterable[str] = ()) -> int:
    args = create_parser().parse_args(list(argv) if argv else None)
    if args.max_entries < 1 or args.max_bytes < 1:
        raise RuntimeError("snapshot limits must be positive")
    import_snapshot(
        args.root,
        sys.stdin.buffer,
        max_entries=args.max_entries,
        max_bytes=args.max_bytes,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, tarfile.TarError) as error:
        print("diagram-grill-eval-snapshot: {}".format(error), file=sys.stderr)
        raise SystemExit(1)
