"""Verify every extracted file against the fixed official Renode ZIP, then pin it."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath

EXPECTED = "18bcf145422039e8702c3e5f9864c7787561bfd5deb7a3ee6e168ecf19e2b9db"


def pin(archive: Path, root: Path, output: Path):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED:
        raise ValueError("archive differs from GitHub release digest")
    with zipfile.ZipFile(archive) as package:
        members = [item for item in package.infolist() if not item.is_dir()]
        names = [PurePosixPath(item.filename) for item in members]
        prefix = names[0].parts[0]
        # The portable ZIP may or may not have a single enclosing directory.
        enclosed = all(len(p.parts) > 1 and p.parts[0] == prefix for p in names)
        files = {}
        for item, name in zip(members, names, strict=True):
            relative = PurePosixPath(*name.parts[1:]) if enclosed else name
            if name.is_absolute() or ".." in name.parts or relative.as_posix() in files:
                raise ValueError("unsafe/duplicate archive entry")
            expected = hashlib.sha256(package.read(item)).hexdigest()
            actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"extracted file mismatch: {relative}")
            files[relative.as_posix()] = expected
    observed = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if observed != set(files):
        raise ValueError("extra or missing extracted files")
    executable = next(name for name in files if name == "renode.exe")
    result = {
        "schema_version": "embedded-sim-runner.runtime-lock.v1",
        "backend": "renode",
        "version": "1.17.0",
        "root": str(root.resolve()),
        "executable": executable,
        "archive_sha256": EXPECTED,
        "archive_source": "https://github.com/renode/renode/releases/tag/v1.17.0",
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
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pin(args.archive, args.root, args.output)
