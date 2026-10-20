"""Build UART-only fixture with the installed Rust ARM target; no device access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def build(output: Path, unmapped_write: bool = False):
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    compiler = subprocess.check_output(["rustc", "-vV"], timeout=30).decode()
    inputs = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ("main.rs", "memory.x", "build.py")
    }
    identity = hashlib.sha256(
        json.dumps(
            {"inputs": inputs, "compiler": compiler, "unmapped_write": unmapped_write},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    environment = dict(os.environ, SIM_BUILD_ID=identity)
    for name in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        environment.pop(name, None)
    args = [
        "rustc",
        "--edition=2024",
        "--target",
        "thumbv7em-none-eabihf",
        "--crate-type",
        "bin",
        "-C",
        "opt-level=s",
        "-C",
        "panic=abort",
        "-C",
        f"link-arg=-T{source / 'memory.x'}",
        "-C",
        "link-arg=--nmagic",
        "-o",
        str(output / "firmware.elf"),
        str(source / "main.rs"),
    ]
    if unmapped_write:
        args.extend(["--cfg", "sim_unmapped_write"])
    result = subprocess.run(args, env=environment, capture_output=True, timeout=120, check=True)
    (output / "build.stdout.bin").write_bytes(result.stdout)
    (output / "build.stderr.bin").write_bytes(result.stderr)
    sysroot = Path(
        subprocess.check_output(["rustc", "--print", "sysroot"], timeout=30).decode().strip()
    )
    host = next(line[6:] for line in compiler.splitlines() if line.startswith("host: "))
    objcopy = sysroot / "lib/rustlib" / host / "bin/llvm-objcopy.exe"
    subprocess.run(
        [str(objcopy), "-O", "binary", str(output / "firmware.elf"), str(output / "firmware.bin")],
        check=True,
        capture_output=True,
        timeout=30,
    )
    manifest = {
        "hardware_access": False,
        "execution_kind": "simulation_fixture_build",
        "physical_board_verified": False,
        "compiler": compiler,
        "source_sha256": inputs,
        "build_id": identity,
        "ready_line": "SIM_NRF52840_READY v1",
        "build_line": f"SIM_NRF52840_BUILD {identity}",
        "heartbeat_prefix": "SIM_NRF52840_HEARTBEAT",
        "firmware_sha256": hashlib.sha256((output / "firmware.bin").read_bytes()).hexdigest(),
        "elf_sha256": hashlib.sha256((output / "firmware.elf").read_bytes()).hexdigest(),
        "same_binary_as_hardware_fixture": False,
        "intentional_unmapped_write": unmapped_write,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--unmapped-write", action="store_true")
    args = parser.parse_args()
    build(args.output.resolve(), args.unmapped_write)
