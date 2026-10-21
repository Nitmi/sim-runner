"""Compile the fixed GPIO/TIMER interrupt fixture; no hardware access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def build(output, variant="normal"):
    source = Path(__file__).resolve().parent
    output.mkdir(parents=True, exist_ok=False)
    compiler = subprocess.check_output(["rustc", "-vV"], timeout=30).decode()
    inputs = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ("main.rs", "memory.x", "build.py")
    }
    identity = hashlib.sha256(
        json.dumps(
            {"source": inputs, "compiler": compiler, "variant": variant}, sort_keys=True
        ).encode()
    ).hexdigest()
    env = dict(os.environ, SIM_BUILD_ID=identity)
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(key, None)
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
    if variant != "normal":
        args += ["--cfg", "sim_" + variant]
    result = subprocess.run(args, env=env, capture_output=True, timeout=120)
    (output / "build.stdout.bin").write_bytes(result.stdout)
    (output / "build.stderr.bin").write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
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
        "execution_kind": "simulation_fixture_build",
        "hardware_access": False,
        "physical_board_verified": False,
        "compiler": compiler,
        "source_sha256": inputs,
        "variant": variant,
        "build_id": identity,
        "ready_line": "SIM_NRF_IO_READY v1",
        "build_line": "SIM_NRF_IO_BUILD " + identity,
        "sample_prefix": "SIM_NRF_IO_SAMPLE",
        "firmware_sha256": hashlib.sha256((output / "firmware.bin").read_bytes()).hexdigest(),
        "elf_sha256": hashlib.sha256((output / "firmware.elf").read_bytes()).hexdigest(),
        "same_binary_as_hardware_fixture": False,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--variant",
        choices=("normal", "invert_output", "slow_timer", "missing_irq", "unmapped_write"),
        default="normal",
    )
    args = parser.parse_args()
    build(args.output.resolve(), args.variant)
