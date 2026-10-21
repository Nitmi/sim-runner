"""Fixed Espressif ESP32-C3 RAM/UART backend, separate from Renode profiles."""

from __future__ import annotations

import json
import re
import struct
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .common import (
    FLAGS,
    SimError,
    bound,
    decode,
    digest,
    file_record,
    payload,
    require,
    sha,
    write_json,
)
from .plan import ASSETS, evaluate, marker, runtime
from .process import execute

BACKEND = "espressif-qemu"
PROFILE = "esp32c3-ram-uart-v1"
MODEL = ASSETS / "esp32c3-uart.json"
PLAN_SCHEMA = "embedded-sim-runner.qemu-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.qemu-report.v1"
RUNTIME_VERSION = "esp-develop-9.2.2-20260417"
ARCHIVE_SHA256 = "697aa4800a1f52be0b1693b30e22a684f7ea93c46c489e619384cae7b0e9b87b"


def elf_info(data: bytes) -> dict:
    """Reject flash/ROM images, host paths, dynamic ELFs and ambiguous RAM aliases."""
    require(52 <= len(data) <= 8 * 1024 * 1024, "ELF must be 52 B to 8 MiB")
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    ident, kind, machine, version, entry, offset = header[:6]
    require(ident[:7] == b"\x7fELF\x01\x01\x01", "requires ELF32 little-endian")
    require(kind == 2 and machine == 243 and version == 1, "requires RISC-V ET_EXEC")
    require(header[7] in (0, 1), "requires soft-float RV32I/RVC ABI")
    require(header[8] == 52 and header[9] == 32 and 1 <= header[10] <= 64, "invalid ELF headers")
    require(offset >= 52 and offset + header[10] * 32 <= len(data), "truncated program headers")
    require(entry % 2 == 0, "unaligned entry")
    segments = []
    ranges = []
    executable_entry = False
    for index in range(header[10]):
        typ, pos, virt, phys, size, memory, flags, align = struct.unpack_from(
            "<8I", data, offset + index * 32
        )
        require(typ in (0, 1, 0x6474E551, 0x70000003), "unsupported ELF program header")
        if typ != 1:
            continue
        require(phys == virt and memory > 0 and size <= memory, "invalid ELF load segment")
        require(pos + size <= len(data) and flags & ~7 == 0, "invalid segment data/flags")
        require(
            align in (0, 1) or (align & (align - 1) == 0 and pos % align == virt % align),
            "invalid segment alignment",
        )
        if phys >= 0x4037C000 and phys + memory <= 0x403E0000:
            start = phys - 0x4037C000
        elif phys >= 0x3FC80000 and phys + memory <= 0x3FCE0000:
            start = phys - 0x3FC80000 + 0x4000
        else:
            raise SimError("UNSUPPORTED_FIRMWARE", "ELF load segment outside profile RAM")
        require(
            all(start + memory <= low or start >= high for low, high in ranges),
            "overlapping RAM/alias segments",
        )
        ranges.append((start, start + memory))
        executable_entry |= bool(flags & 1 and virt <= entry < virt + size)
        segments.append(
            {"address": phys, "file_bytes": size, "memory_bytes": memory, "flags": flags}
        )
    require(segments and executable_entry, "ELF entry not in executable load data")
    return {"bytes": len(data), "entry": entry, "segments": segments}


def make_plan(
    lock: Path,
    lock_sha: str,
    image: Path,
    image_sha: str,
    ready: str,
    build: str,
    heartbeat: str,
    min_heartbeats: int = 3,
    timeout: int = 30,
    output_limit: int = 1048576,
    memory_mib: int = 1024,
) -> dict:
    for value, low, high, name in (
        (min_heartbeats, 3, 10000, "min_heartbeats"),
        (timeout, 1, 120, "timeout"),
        (output_limit, 4096, 4 * 1024 * 1024, "output_limit"),
        (memory_mib, 512, 4096, "memory_mib"),
    ):
        require(type(value) is int and low <= value <= high, f"invalid {name}")
    for line in (ready, build, heartbeat):
        marker(line)
    require(len({ready, build, heartbeat}) == 3, "markers must differ")
    info = runtime(lock, lock_sha, BACKEND)
    return {
        "schema_version": PLAN_SCHEMA,
        "runner_version": __version__,
        "backend": BACKEND,
        "backend_version": info["version"],
        "profile": PROFILE,
        **FLAGS,
        "executables_started": False,
        "runtime_lock": {"path": str(lock.resolve()), "sha256": digest(lock_sha)},
        "firmware": {
            "path": str(image.resolve()),
            "sha256": digest(image_sha),
            **elf_info(bound(image, image_sha, 8 * 1024 * 1024)),
        },
        "model_sha256": sha(payload(MODEL, 16384)),
        "initial_state": "new_process_zero_filled_ram_plus_elf_segments_no_persistent_storage",
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
    wanted = make_plan(
        Path(doc["runtime_lock"]["path"]),
        doc["runtime_lock"]["sha256"],
        Path(doc["firmware"]["path"]),
        doc["firmware"]["sha256"],
        doc["assertions"]["ready"],
        doc["assertions"]["build"],
        doc["assertions"]["heartbeat_prefix"],
        doc["assertions"]["minimum"],
        doc["host_timeout_seconds"],
        doc["output_limit_bytes_per_file"],
        doc["job_memory_limit_mib"],
    )
    require(
        json.dumps(doc, sort_keys=True) == json.dumps(wanted, sort_keys=True),
        "unsupported or changed plan fields",
        "INPUT_CHANGED",
    )
    return doc


def command(executable: Path) -> list[str]:
    # All paths after the executable are fixed relative names in the private cwd.
    # No QMP/GDB/monitor server, user config, network, semihosting, USB or host serial.
    return [
        str(executable),
        "-no-user-config",
        "-nodefaults",
        "-machine",
        "esp32c3",
        "-accel",
        "tcg,thread=single,tb-size=16",
        "-display",
        "none",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-serial",
        "file:uart.bin",
        "-kernel",
        "firmware.elf",
        "-no-reboot",
        "-icount",
        "shift=0,align=off,sleep=on",
        "-d",
        "guest_errors,unimp",
    ]


def check_logs(stdout: bytes, stderr: bytes):
    # Strict allowlist, not an assumption that exit zero means full model coverage.
    lines = (stdout + b"\n" + stderr).decode("ascii", errors="strict").splitlines()
    for line in lines:
        require(
            not line
            or line == "Not initializing SPI Flash"
            or re.fullmatch(r"Loading kernel at address 0x[0-9a-fA-F]+", line),
            f"unexpected QEMU diagnostic: {line[:300]}",
            "MODEL_COVERAGE_FAILED",
        )
    nonempty = [line for line in lines if line]
    require(
        len(nonempty) == 2 and nonempty[0] == "Not initializing SPI Flash",
        "QEMU startup diagnostics missing/duplicated",
        "BACKEND_FAILED",
    )


def run(plan_path: Path, expected: str, directory: Path) -> dict:
    raw = bound(plan_path, expected, 65536)
    plan = load_plan(plan_path, expected)
    require(not directory.exists(), "output directory already exists")
    lock = Path(plan["runtime_lock"]["path"])
    info = runtime(lock, plan["runtime_lock"]["sha256"], BACKEND)
    image = bound(Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], 8 * 1024 * 1024)
    model = bound(MODEL, plan["model_sha256"], 16384)
    directory.mkdir(parents=True, exist_ok=False)
    for name, data in {
        "plan.json": raw,
        "runtime-lock.json": bound(lock, plan["runtime_lock"]["sha256"], 4 * 1024 * 1024),
        "firmware.elf": image,
        "model.json": model,
    }.items():
        (directory / name).write_bytes(data)
    argv = command(Path(info["root"]) / info["executable"])
    started = datetime.now(UTC).isoformat()
    process = execute(
        argv,
        directory,
        plan["host_timeout_seconds"],
        plan["output_limit_bytes_per_file"],
        plan["job_memory_limit_mib"],
    )
    errors = []
    assertions = None
    current = False
    try:
        require(
            not process["error"],
            process["error"] or "process failed",
            process["error"] or "BACKEND_FAILED",
        )
        require(
            process["exit_code"] == 0 and not process["forced_termination"],
            "backend exit failed",
            "BACKEND_FAILED",
        )
        check_logs(
            payload(directory / "backend.stdout.bin", plan["output_limit_bytes_per_file"]),
            payload(directory / "backend.stderr.bin", plan["output_limit_bytes_per_file"]),
        )
        assertions = evaluate(
            payload(directory / "uart.bin", plan["output_limit_bytes_per_file"]), plan["assertions"]
        )
    except (SimError, OSError, UnicodeError) as error:
        errors.append({"code": getattr(error, "code", "EVIDENCE_INVALID"), "message": str(error)})
    try:
        runtime(lock, plan["runtime_lock"]["sha256"], BACKEND)
        bound(Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], 8 * 1024 * 1024)
        bound(MODEL, plan["model_sha256"], 16384)
        current = True
    except (SimError, OSError) as error:
        errors.append({"code": "INPUT_CHANGED", "message": str(error)})
    report = {
        "schema_version": REPORT_SCHEMA,
        **FLAGS,
        "ok": not errors,
        "status": "failed" if errors else "passed",
        "backend": BACKEND,
        "evidence_origin": "qemu_execution_attempt" if process["pid"] else "backend_not_started",
        "executables_started": process["pid"] is not None,
        "plan_sha256": sha(raw),
        "profile": PROFILE,
        "backend_version": info["version"],
        "runtime_lock_sha256": plan["runtime_lock"]["sha256"],
        "runtime_archive_sha256": info["archive_sha256"],
        "backend_argv": argv,
        "host_started_utc": started,
        "host_finished_utc": datetime.now(UTC).isoformat(),
        "virtual_time": {
            "domain": "qemu_icount_shift_0",
            "elapsed_ns": None,
            "budget_enforced": False,
        },
        "uart_timestamps_available": False,
        "process": process,
        "assertions": assertions,
        "input_hashes_current": current,
        "artifacts": {p.name: file_record(p) for p in directory.iterdir() if p.is_file()},
        "errors": errors,
    }
    write_json(directory / "report.json", report)
    return report


def inspect_report(path: Path, expected: str) -> dict:
    report = decode(bound(path, expected, 1024 * 1024))
    require(report["schema_version"] == REPORT_SCHEMA, "unsupported report")
    for key, value in FLAGS.items():
        require(
            type(report[key]) is type(value) and report[key] == value, "invalid evidence boundary"
        )
    require(
        report["status"] in ("passed", "failed") and report["ok"] is (report["status"] == "passed"),
        "inconsistent report",
    )
    artifacts = report["artifacts"]
    required = {"plan.json", "runtime-lock.json", "firmware.elf", "model.json"}
    require(
        required
        <= set(artifacts)
        <= required | {"backend.stdout.bin", "backend.stderr.bin", "uart.bin"},
        "invalid evidence inventory",
    )
    loaded = {}
    for name, record in artifacts.items():
        data = bound(path.parent / name, record["sha256"], 16 * 1024 * 1024)
        require(len(data) == record["bytes"], "artifact size mismatch")
        loaded[name] = data
    plan = decode(loaded["plan.json"])
    require(
        plan["schema_version"] == PLAN_SCHEMA
        and plan["backend"] == report["backend"] == BACKEND
        and plan["profile"] == report["profile"] == PROFILE,
        "profile/backend mismatch",
    )
    for key, value in FLAGS.items():
        require(type(plan[key]) is type(value) and plan[key] == value, "invalid plan boundary")
    require(sha(loaded["plan.json"]) == report["plan_sha256"], "plan binding mismatch")
    require(
        sha(loaded["runtime-lock.json"])
        == report["runtime_lock_sha256"]
        == plan["runtime_lock"]["sha256"],
        "lock binding mismatch",
    )
    require(sha(loaded["firmware.elf"]) == plan["firmware"]["sha256"], "firmware binding mismatch")
    require(sha(loaded["model.json"]) == plan["model_sha256"], "model binding mismatch")
    info = decode(loaded["runtime-lock.json"])
    model = decode(loaded["model.json"])
    require(
        info["backend"] == BACKEND
        and info["version"]
        == plan["backend_version"]
        == report["backend_version"]
        == RUNTIME_VERSION
        and info["executable"] == "bin/qemu-system-riscv32.exe"
        and info["archive_sha256"] == report["runtime_archive_sha256"] == ARCHIVE_SHA256,
        "runtime metadata mismatch",
    )
    require(
        model["profile"] == PROFILE
        and model["backend"] == BACKEND
        and model["bridges"] == []
        and plan["physical_bridges"] == []
        and type(plan["retries"]) is int
        and plan["retries"] == 0,
        "profile boundary mismatch",
    )
    require(
        report["backend_argv"] == command(Path(info["root"]) / info["executable"]),
        "backend command mismatch",
    )
    require(
        elf_info(loaded["firmware.elf"])
        == {k: v for k, v in plan["firmware"].items() if k not in ("path", "sha256")},
        "ELF metadata mismatch",
    )
    if report["ok"]:
        process = report["process"]
        require(
            process["exit_code"] == 0
            and process["error"] is None
            and process["forced_termination"] is False,
            "backend failed",
        )
        cleanup = process["cleanup"]
        require(
            cleanup["process_exited"] is True
            and cleanup["job_active_processes"] == 0
            and cleanup["capture_threads_stopped"] is True,
            "cleanup incomplete",
        )
        require(
            report["errors"] == [] and report["input_hashes_current"] is True,
            "input/error mismatch",
        )
        require(
            evaluate(loaded["uart.bin"], plan["assertions"]) == report["assertions"],
            "UART assertion mismatch",
        )
        check_logs(loaded["backend.stdout.bin"], loaded["backend.stderr.bin"])
    return {
        **FLAGS,
        "ok": True,
        "operation": "inspect-report",
        "executables_started": False,
        "report_status": report["status"],
        "artifacts_verified": len(loaded),
        "scope": "saved_simulation_evidence_integrity",
    }
