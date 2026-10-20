"""Double-build a local candidate; never publish."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def package(output: Path):
    source = Path(__file__).resolve().parents[1]
    output.mkdir(parents=True, exist_ok=False)
    hashes = []
    for name in ("build-a", "build-b"):
        destination = output / name
        subprocess.run(
            [sys.executable, "-m", "hatchling", "build", "-d", str(destination)],
            cwd=source,
            check=True,
            timeout=120,
        )
        hashes.append(
            {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in destination.iterdir()
                if p.is_file()
            }
        )
    if hashes[0] != hashes[1] or len(hashes[0]) != 2:
        raise AssertionError("double build differs")
    (output / "package-result.json").write_text(
        json.dumps({"ok": True, "double_build_identical": True, "assets": hashes[0]}, indent=2)
        + "\n"
    )
    print(json.dumps(hashes[0], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    package(parser.parse_args().output.resolve())
