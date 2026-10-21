"""Build a RAM-only RV32IMC smoke fixture; no SDK, downloads or hardware access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def build(output: Path, variant: str):
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    compiler = subprocess.check_output(["rustc", "-vV"], timeout=30).decode()
    inputs = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ("main.rs", "memory.x", "build.py")
    }
    identity = hashlib.sha256(
        json.dumps(
            {"inputs": inputs, "compiler": compiler, "variant": variant}, sort_keys=True
        ).encode()
    ).hexdigest()
    env = dict(os.environ, SIM_BUILD_ID=identity)
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(key, None)
    command = [
        "rustc",
        "--edition=2024",
        "--target",
        "riscv32imc-unknown-none-elf",
        "--crate-type",
        "bin",
        "-C",
        "opt-level=s",
        "-C",
        "panic=abort",
        "-C",
        f"link-arg=-T{source / 'memory.x'}",
        "-C",
        "link-arg=--no-relax",
        "-o",
        str(output / "firmware.elf"),
        str(source / "main.rs"),
    ]
    if variant != "normal":
        command += ["--cfg", "sim_" + variant.replace("-", "_")]
    result = subprocess.run(command, env=env, capture_output=True, timeout=120)
    (output / "build.stdout.bin").write_bytes(result.stdout)
    (output / "build.stderr.bin").write_bytes(result.stderr)
    result.check_returncode()
    manifest = {
        "hardware_access": False,
        "execution_kind": "simulation_fixture_build",
        "physical_board_verified": False,
        "compiler": compiler,
        "source_sha256": inputs,
        "build_id": identity,
        "variant": variant,
        "ready_line": "SIM_ESP32C3_READY v1",
        "build_line": f"SIM_ESP32C3_BUILD {identity}",
        "heartbeat_prefix": "SIM_ESP32C3_HEARTBEAT",
        "elf_sha256": hashlib.sha256((output / "firmware.elf").read_bytes()).hexdigest(),
        "same_binary_as_hardware_fixture": False,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", choices=("normal", "hang", "unmapped-write"), default="normal")
    args = parser.parse_args()
    build(args.output.resolve(), args.variant)
