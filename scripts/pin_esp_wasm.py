"""Verify an exact official ESP-EMU WASM archive/extraction and bind a trusted Node.

Only reads host files and writes a new runtime lock; never extracts or launches.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sim_runner import esp_wasm
from sim_runner.common import bound, digest, payload, require, sha, write_json


def pin(archive, root, node, node_sha, output):
    m = esp_wasm.model()
    bound(archive, m["archive_sha256"], 8 * 1024 * 1024)
    with tarfile.open(archive, "r:gz") as bundle:
        actual = {}
        for member in bundle.getmembers():
            require(member.isdir() or member.isfile(), "archive links or special files unsupported")
            relative = Path(member.name)
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and "\\" not in member.name
                and ":" not in member.name,
                "unsafe archive member",
            )
            require(relative.parts[0] == "esp-emu-0.50.0-wasm", "archive prefix differs")
            if member.isdir():
                continue
            require(member.size <= 16 * 1024 * 1024, "archive member too large")
            name = relative.relative_to("esp-emu-0.50.0-wasm").as_posix()
            require(name not in actual, "duplicate archive member")
            actual[name] = sha(bundle.extractfile(member).read())
        require(actual == m["files"], "archive payload differs")
    lock = {
        "schema_version": esp_wasm.RUNTIME_SCHEMA,
        "backend": esp_wasm.BACKEND,
        "version": m["version"],
        "archive_sha256": m["archive_sha256"],
        "root": str(root.resolve()),
        "files": actual,
        "node": {
            "path": str(node.resolve()),
            "sha256": digest(node_sha),
            "version": m["node_version"],
        },
    }
    # Validate extracted files and the selected Node bytes without executing them.
    require(esp_wasm.inventory(root) == set(actual), "extracted inventory differs")
    for name, expected in actual.items():
        bound(root / name, expected, 16 * 1024 * 1024)
    bound(node, node_sha, 256 * 1024 * 1024)
    write_json(output, lock)
    print(
        json.dumps(
            {
                "ok": True,
                "runtime_lock": str(output.resolve()),
                "sha256": sha(payload(output, 65536)),
                "runtime_files": len(actual),
                "node_version_claim": m["node_version"],
                "executables_started": False,
                "hardware_access": False,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "root", "node", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--node-sha256", required=True)
    args = parser.parse_args()
    pin(args.archive, args.root, args.node, args.node_sha256, args.output)


if __name__ == "__main__":
    main()
