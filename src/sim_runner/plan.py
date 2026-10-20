from __future__ import annotations

import json
import os
import re
import stat
import struct
from pathlib import Path

from . import __version__
from .common import FLAGS, bound, decode, digest, payload, require, sha

ASSETS = Path(__file__).parent / "assets"
PROFILE = "nrf52840-uart-v1"


def inventory(root: Path) -> set[str]:
    """Inspect each directory entry once; reject Windows junctions and symlinks."""
    files = set()
    pending = [root]
    directories = 0
    while pending:
        directory = pending.pop()
        directories += 1
        require(directories <= 5000, "too many runtime directories")
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                require(
                    not stat.S_ISLNK(info.st_mode)
                    and not getattr(info, "st_file_attributes", 0) & 0x400,
                    "runtime contains a link/reparse point",
                )
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                else:
                    require(stat.S_ISREG(info.st_mode), "runtime contains a non-regular file")
                    files.add(Path(entry.path).relative_to(root).as_posix())
                    require(len(files) <= 10000, "too many runtime files")
    return files


def runtime(lock: Path, expected: str) -> dict:
    value = decode(bound(lock, expected, 4 * 1024 * 1024))
    require(isinstance(value, dict), "runtime lock must be an object")
    require(
        value.get("schema_version") == "embedded-sim-runner.runtime-lock.v1", "bad runtime lock"
    )
    require(
        value.get("backend") == "renode" and value.get("version") == "1.17.0", "unsupported runtime"
    )
    require(value.get("executable") == "renode.exe", "unexpected runtime executable")
    root = Path(value["root"])
    require(
        root.is_absolute() and root.is_dir(), "runtime root must be an existing absolute directory"
    )
    files = value["files"]
    require(isinstance(files, dict) and 1 <= len(files) <= 10000, "invalid runtime inventory")
    require(inventory(root) == set(files), "runtime inventory differs", "INPUT_CHANGED")
    total = 0
    for name, expected_hash in files.items():
        require(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_. /+@()-]+", name),
            "unsafe runtime name",
        )
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "runtime path escape")
        target = root / relative
        data = bound(target, expected_hash, 128 * 1024 * 1024)
        total += len(data)
        require(total <= 1024 * 1024 * 1024, "runtime exceeds 1 GiB")
    require(inventory(root) == set(files), "runtime inventory differs", "INPUT_CHANGED")
    require(value["executable"] in files, "runtime executable missing")
    return value


def firmware(data: bytes) -> dict:
    require(256 <= len(data) <= 1024 * 1024, "raw firmware must be 256 B to 1 MiB")
    stack, reset = struct.unpack_from("<II", data)
    require(0x20000000 < stack <= 0x20040000 and stack % 8 == 0, "invalid initial stack")
    require(reset & 1 and 256 <= reset - 1 < len(data), "reset vector outside firmware")
    return {"bytes": len(data), "initial_sp": stack, "reset_vector": reset}


def marker(value: str) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[\x20-\x7e]{1,160}", value), "invalid UART marker"
    )
    return value


def make_plan(
    lock: Path,
    lock_sha: str,
    image: Path,
    image_sha: str,
    ready: str,
    build: str,
    heartbeat: str,
    min_heartbeats: int = 3,
    virtual_ms: int = 250,
    timeout: int = 30,
    output_limit: int = 1048576,
    memory_mib: int = 1024,
) -> dict:
    for number, low, high, name in (
        (min_heartbeats, 3, 10000, "min_heartbeats"),
        (virtual_ms, 1, 10000, "virtual_ms"),
        (timeout, 1, 120, "timeout"),
        (output_limit, 4096, 4 * 1024 * 1024, "output_limit"),
        (memory_mib, 512, 4096, "memory_mib"),
    ):
        require(type(number) is int and low <= number <= high, f"invalid {name}")
    for line in (ready, build, heartbeat):
        marker(line)
    require(len({ready, build, heartbeat}) == 3, "markers must differ")
    info = runtime(lock, lock_sha)
    image_data = bound(image, image_sha, 1024 * 1024)
    return {
        "schema_version": "embedded-sim-runner.plan.v1",
        "runner_version": __version__,
        "backend": "renode",
        "backend_version": info["version"],
        "profile": PROFILE,
        **FLAGS,
        "executables_started": False,
        "runtime_lock": {"path": str(lock.resolve()), "sha256": digest(lock_sha)},
        "firmware": {
            "path": str(image.resolve()),
            "sha256": digest(image_sha),
            **firmware(image_data),
        },
        "model_sha256": sha(payload(ASSETS / "nrf52840-uart.repl", 16384)),
        "initial_state": "new_process_zero_filled_flash_and_ram_plus_raw_image_at_zero",
        "virtual_ms": virtual_ms,
        "host_timeout_seconds": timeout,
        "output_limit_bytes_per_file": output_limit,
        "job_memory_limit_mib": memory_mib,
        "assertions": {
            "ready": ready,
            "build": build,
            "heartbeat_prefix": heartbeat,
            "minimum": min_heartbeats,
        },
        "physical_bridges": [],
        "retries": 0,
    }


def load_plan(path: Path, expected: str) -> dict:
    doc = decode(bound(path, expected, 65536))
    require(isinstance(doc, dict), "plan must be an object")
    expected_doc = make_plan(
        Path(doc["runtime_lock"]["path"]),
        doc["runtime_lock"]["sha256"],
        Path(doc["firmware"]["path"]),
        doc["firmware"]["sha256"],
        doc["assertions"]["ready"],
        doc["assertions"]["build"],
        doc["assertions"]["heartbeat_prefix"],
        doc["assertions"]["minimum"],
        doc["virtual_ms"],
        doc["host_timeout_seconds"],
        doc["output_limit_bytes_per_file"],
        doc["job_memory_limit_mib"],
    )
    require(
        json.dumps(doc, sort_keys=True) == json.dumps(expected_doc, sort_keys=True),
        "plan contains unsupported or changed fields",
        "INPUT_CHANGED",
    )
    return doc


def evaluate(data: bytes, assertions: dict) -> dict:
    require(data.endswith(b"\n"), "UART stream has no complete final line", "ASSERTION_FAILED")
    lines = data.decode("ascii", errors="strict").splitlines()
    ready = assertions["ready"]
    build = assertions["build"]
    require(
        lines[:2] == [ready, build],
        "READY and BUILD must be the first two complete lines",
        "ASSERTION_FAILED",
    )
    prefix = assertions["heartbeat_prefix"]
    sequences = []
    for line in lines[2:]:
        match = re.fullmatch(re.escape(prefix) + r" ([0-9A-F]{8})", line)
        require(match is not None, "unexpected UART line", "ASSERTION_FAILED")
        sequences.append(int(match[1], 16))
    require(
        len(sequences) >= assertions["minimum"], "insufficient HEARTBEAT lines", "ASSERTION_FAILED"
    )
    require(
        sequences == list(range(1, len(sequences) + 1)),
        "HEARTBEAT sequence must start at 1 and increment",
        "ASSERTION_FAILED",
    )
    return {
        "ready": True,
        "build": True,
        "heartbeats": len(sequences),
        "first": sequences[0],
        "last": sequences[-1],
    }
