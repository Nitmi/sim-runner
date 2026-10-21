"""Verify and extract the fixed official Windows QEMU distribution; never launch it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

EXPECTED = "697aa4800a1f52be0b1693b30e22a684f7ea93c46c489e619384cae7b0e9b87b"
VERSION = "esp-develop-9.2.2-20260417"
URL = (
    f"https://github.com/espressif/qemu/releases/download/{VERSION}/"
    "qemu-riscv32-softmmu-esp_develop_9.2.2_20260417-x86_64-w64-mingw32.tar.xz"
)


def pin(archive: Path, destination: Path, output: Path):
    if (
        archive.stat().st_size != 38496020
        or hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED
    ):
        raise ValueError("archive differs from Espressif tools.json")
    if destination.exists() or output.exists():
        raise ValueError("destination/lock already exists")
    files = {}
    total = 0
    with tarfile.open(archive, "r:xz") as package:
        members = package.getmembers()
        if len(members) > 10000:
            raise ValueError("archive inventory too large")
        names = set()
        for item in members:
            path = PurePosixPath(item.name)
            if (
                not re.fullmatch(r"[A-Za-z0-9_. /+@()-]+", item.name)
                or path.is_absolute()
                or ".." in path.parts
                or path.parts[0] != "qemu"
                or item.name.rstrip("/").casefold() in names
                or not (item.isdir() or item.isfile())
                or item.size > 128 * 1024 * 1024
            ):
                raise ValueError("unsafe/duplicate archive entry")
            names.add(item.name.rstrip("/").casefold())
            total += item.size
        if total > 1024 * 1024 * 1024:
            raise ValueError("archive exceeds 1 GiB")
        destination.mkdir(parents=True, exist_ok=False)
        for item in members:
            target = destination / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.extractfile(item) as source, target.open("xb") as sink:
                data = source.read(item.size + 1)
                if len(data) != item.size:
                    raise ValueError("archive member size changed")
                sink.write(data)
            if target.read_bytes() != data:
                raise ValueError("extraction mismatch")
            files[PurePosixPath(*PurePosixPath(item.name).parts[1:]).as_posix()] = hashlib.sha256(
                data
            ).hexdigest()
    result = {
        "schema_version": "embedded-sim-runner.runtime-lock.v1",
        "backend": "espressif-qemu",
        "version": VERSION,
        "root": str((destination / "qemu").resolve()),
        "executable": "bin/qemu-system-riscv32.exe",
        "archive_sha256": EXPECTED,
        "archive_source": URL,
        "files": files,
    }
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "files": len(files),
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "output": str(output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pin(args.archive, args.destination.resolve(), args.output)
