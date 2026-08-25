#!/usr/bin/python3
"""Export a bounded, link-free container snapshot as a tar stream."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class Entry:
    path: Path
    relative: str
    mode: int
    size: int
    is_directory: bool
    device: int
    inode: int


def _require_quiescent_processes() -> None:
    allowed = {1, os.getpid()}
    unexpected = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        pid = int(item.name)
        if pid not in allowed:
            unexpected.append(pid)
    if unexpected:
        raise RuntimeError(
            "container is not quiescent; unexpected process IDs: {}".format(
                ", ".join(str(pid) for pid in sorted(unexpected))
            )
        )


def _scan(
    root: Path,
    excludes: Sequence[str],
    max_entries: int,
    max_bytes: int,
) -> List[Entry]:
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        raise RuntimeError("snapshot root must be a real directory")
    excluded = frozenset(excludes)
    pending: List[Tuple[Path, str]] = [(root, "")]
    entries: List[Entry] = []
    total_bytes = 0
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as scanner:
            children = sorted(scanner, key=lambda item: item.name, reverse=True)
        for child in children:
            relative = "{}/{}".format(prefix, child.name).lstrip("/")
            top_level = relative.split("/", 1)[0]
            if top_level in excluded:
                continue
            child_stat = child.stat(follow_symlinks=False)
            mode = child_stat.st_mode
            if stat.S_ISLNK(mode):
                raise RuntimeError("snapshot contains a symlink: {}".format(relative))
            if stat.S_ISDIR(mode):
                entries.append(
                    Entry(
                        Path(child.path),
                        relative,
                        mode,
                        0,
                        True,
                        child_stat.st_dev,
                        child_stat.st_ino,
                    )
                )
                pending.append((Path(child.path), relative))
            elif stat.S_ISREG(mode):
                total_bytes += child_stat.st_size
                if total_bytes > max_bytes:
                    raise RuntimeError("snapshot exceeds its byte limit")
                entries.append(
                    Entry(
                        Path(child.path),
                        relative,
                        mode,
                        child_stat.st_size,
                        False,
                        child_stat.st_dev,
                        child_stat.st_ino,
                    )
                )
            else:
                raise RuntimeError(
                    "snapshot contains a special file: {}".format(relative)
                )
            if len(entries) > max_entries:
                raise RuntimeError("snapshot exceeds its entry limit")
    return sorted(entries, key=lambda item: item.relative)


def _open_verified(entry: Entry) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(entry.path, flags)
    result = os.fstat(descriptor)
    if (
        not stat.S_ISREG(result.st_mode)
        or result.st_size != entry.size
        or result.st_dev != entry.device
        or result.st_ino != entry.inode
    ):
        os.close(descriptor)
        raise RuntimeError("snapshot file changed during export: {}".format(entry.relative))
    return os.fdopen(descriptor, "rb")


def export_snapshot(
    root: Path,
    output: BinaryIO,
    *,
    excludes: Sequence[str],
    max_entries: int,
    max_bytes: int,
) -> None:
    _require_quiescent_processes()
    entries = _scan(root.resolve(strict=True), excludes, max_entries, max_bytes)
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry.relative + ("/" if entry.is_directory else ""))
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if entry.is_directory:
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
                continue
            info.type = tarfile.REGTYPE
            info.mode = 0o600
            info.size = entry.size
            with _open_verified(entry) as source:
                archive.addfile(info, source)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--max-entries", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    return parser


def main(argv: Iterable[str] = ()) -> int:
    args = create_parser().parse_args(list(argv) if argv else None)
    if args.max_entries < 1 or args.max_bytes < 1:
        raise RuntimeError("snapshot limits must be positive")
    export_snapshot(
        args.root,
        sys.stdout.buffer,
        excludes=args.exclude,
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
