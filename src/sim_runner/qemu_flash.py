"""Fixed ESP32-C3 ROM/ESP-IDF Flash boot; private storage and no physical bridges."""

import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, qemu
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
from .flash_image import FLASH_SIZE
from .flash_image import flash_info as flash_info
from .plan import ASSETS, evaluate, marker, runtime
from .process import execute

PROFILE = "esp32c3-idf-flash-v1"
PLAN_SCHEMA = "embedded-sim-runner.qemu-flash-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.qemu-flash-report.v1"
MODEL = ASSETS / "esp32c3-idf-flash.json"
ROM = "share/qemu/esp32c3-rom.bin"
ROM_SHA = "0de1e65020e803bea0d7443dca149d61895e01fca3bb9c82d073234eebd73f99"
EFUSE = bytes(38) + b"\x0c" + bytes(985)
BACKEND = qemu.BACKEND
EXECUTABLE = "bin/qemu-system-riscv32.exe"
ARCHIVE_SHA256 = qemu.ARCHIVE_SHA256
MACHINE = "esp32c3"
MACHINE_ARGS = []
ROM_MARKER = "ESP-ROM:esp32c3-"
STARTUP_DIAGNOSTIC = "Adding SPI flash device"
VIRTUAL_TIME = {"domain": "qemu_icount_shift_0", "elapsed_ns": None, "budget_enforced": False}
KNOWN_DIAGNOSTICS = {
    "M25P80: Read id (command 0x90/0xAB) is not supported by device": (
        1,
        "legacy Flash ID probe unsupported",
    ),
    "M25P80: Unknown cmd 5a": (1, "Flash SFDP command unsupported"),
    "M25P80: Unknown cmd 7a": (1, "Flash resume command unsupported"),
    "ignoring pmpcfg write - locked": (256, "writes to locked PMP configuration ignored"),
    "ignoring pmpaddr write - pmpcfg + 1 locked": (
        64,
        "writes adjacent to locked PMP entries ignored",
    ),
    "ignoring pmpaddr write - locked": (64, "writes to locked PMP address ignored"),
}


def make_plan(
    lock,
    lock_sha,
    image,
    image_sha,
    ready,
    build,
    heartbeat,
    min_heartbeats=3,
    timeout=30,
    output_limit=1048576,
    memory_mib=1024,
    *,
    profile=None,
):
    p = profile or sys.modules[__name__]
    for value, low, high, name in (
        (min_heartbeats, 3, 10000, "minimum"),
        (timeout, 1, 120, "timeout"),
        (output_limit, 4096, 4 * 1024 * 1024, "output_limit"),
        (memory_mib, 512, 4096, "memory_mib"),
    ):
        require(type(value) is int and low <= value <= high, f"invalid {name}")
    for value in (ready, build, heartbeat):
        marker(value)
    require(len({ready, build, heartbeat}) == 3, "markers must differ")
    info = runtime(lock, lock_sha, p.BACKEND)
    require(info["files"].get(p.ROM) == p.ROM_SHA, "Flash boot requires pinned profile ROM")
    return {
        "schema_version": p.PLAN_SCHEMA,
        "runner_version": __version__,
        **FLAGS,
        "executables_started": False,
        "backend": p.BACKEND,
        "backend_version": info["version"],
        "profile": p.PROFILE,
        "runtime_lock": {"path": str(lock.resolve()), "sha256": digest(lock_sha)},
        "firmware": {
            "path": str(image.resolve()),
            "sha256": digest(image_sha),
            **p.flash_info(bound(image, image_sha, FLASH_SIZE)),
        },
        "model_sha256": sha(payload(p.MODEL, 16384)),
        "rom_sha256": p.ROM_SHA,
        "efuse_initial_sha256": sha(p.EFUSE),
        "initial_state": "private_flash_copy_erased_data_partitions_fresh_revision_0_3_efuse",
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


def load_plan(path, expected, *, profile=None):
    p = profile or sys.modules[__name__]
    doc = decode(bound(path, expected, 65536))
    wanted = p.make_plan(
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
    require(doc == wanted, "unsupported or changed Flash plan fields", "INPUT_CHANGED")
    # bool and int compare equal in Python; canonical JSON keeps their types distinct.
    import json

    require(
        json.dumps(doc, sort_keys=True) == json.dumps(wanted, sort_keys=True),
        "changed Flash plan field types",
        "INPUT_CHANGED",
    )
    return doc


def command(executable, *, profile=None):
    p = profile or sys.modules[__name__]
    base = qemu.command(executable)
    base[base.index("-machine") + 1] = p.MACHINE
    at = base.index("-kernel")
    del base[at : at + 2]
    return (
        base
        + p.MACHINE_ARGS
        + [
            "-L",
            ".",
            "-drive",
            "file=flash-work.bin,if=mtd,format=raw",
            "-drive",
            "file=efuse-work.bin,if=none,format=raw,id=efuse",
            "-global",
            f"driver=nvram.{p.MACHINE}.efuse,property=drive,value=efuse",
            "-global",
            f"driver=timer.{p.MACHINE}.timg,property=wdt_disable,value=true",
        ]
    )


def check_output(stdout, stderr, uart, assertions, *, profile=None):
    p = profile or sys.modules[__name__]
    lines = uart.decode("utf-8", errors="strict").splitlines()
    # Prefer an explicit firmware failure over incidental diagnostics from its
    # panic/restart path. A failed run retains all raw diagnostics for review.
    forbidden = (
        "Guru Meditation",
        "abort()",
        "panic",
        "Backtrace:",
        "assert failed",
        "Brownout",
        "ERROR",
    )
    require(
        not any(word in line for line in lines for word in forbidden),
        "firmware failure in UART",
        "ASSERTION_FAILED",
    )
    require(
        not any(line.startswith("E (") for line in lines), "ESP-IDF error log", "ASSERTION_FAILED"
    )
    diagnostics = (stdout + b"\n" + stderr).decode("ascii", errors="strict").splitlines()
    diagnostics = [line for line in diagnostics if line]
    if p.STARTUP_DIAGNOSTIC is not None:
        require(
            diagnostics
            and diagnostics[0] == p.STARTUP_DIAGNOSTIC
            and diagnostics.count(p.STARTUP_DIAGNOSTIC) == 1,
            "missing/repeated Flash startup diagnostic",
            "MODEL_COVERAGE_FAILED",
        )
        diagnostics = diagnostics[1:]
    counts = {}
    for line in diagnostics:
        require(
            line in p.KNOWN_DIAGNOSTICS,
            f"unexpected QEMU diagnostic: {line[:300]}",
            "MODEL_COVERAGE_FAILED",
        )
        counts[line] = counts.get(line, 0) + 1
        require(
            counts[line] <= p.KNOWN_DIAGNOSTICS[line][0],
            "known diagnostic exceeded profile bound",
            "MODEL_COVERAGE_FAILED",
        )
    require(uart.endswith(b"\n"), "incomplete UART tail", "ASSERTION_FAILED")
    require(lines and lines[0].startswith(p.ROM_MARKER), "ROM boot marker missing", "BOOT_FAILED")
    boot = [
        i
        for i, line in enumerate(lines)
        if "boot: ESP-IDF v5.5.2" in line and "2nd stage bootloader" in line
    ]
    loaded = [
        i
        for i, line in enumerate(lines)
        if "boot: Loaded app from partition at offset 0x10000" in line
    ]
    ready = [i for i, line in enumerate(lines) if line == assertions["ready"]]
    require(
        len(boot) == len(loaded) == len(ready) == 1 and boot[0] < loaded[0] < ready[0],
        "ROM/second-stage/factory/app boot sequence missing or repeated",
        "BOOT_FAILED",
    )
    application_uart = ("\n".join(lines[ready[0] :]) + "\n").encode("ascii")
    return {
        **evaluate(application_uart, assertions),
        "model_diagnostics": [
            {"message": line, "count": count, "meaning": p.KNOWN_DIAGNOSTICS[line][1]}
            for line, count in sorted(counts.items())
        ],
    }


def run(plan_path, expected, directory, *, profile=None):
    p = profile or sys.modules[__name__]
    raw = bound(plan_path, expected, 65536)
    plan = p.load_plan(plan_path, expected)
    lock = Path(plan["runtime_lock"]["path"])
    info = runtime(lock, plan["runtime_lock"]["sha256"], p.BACKEND)
    image = bound(Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], FLASH_SIZE)
    rom = bound(Path(info["root"]) / p.ROM, p.ROM_SHA, 1024 * 1024)
    directory.mkdir(parents=True, exist_ok=False)
    for name, content in {
        "plan.json": raw,
        "runtime-lock.json": bound(lock, plan["runtime_lock"]["sha256"], 4 * 1024 * 1024),
        "model.json": bound(p.MODEL, plan["model_sha256"], 16384),
        "flash-input.bin": image,
        "flash-work.bin": image,
        Path(p.ROM).name: rom,
        "efuse-initial.bin": p.EFUSE,
        "efuse-work.bin": p.EFUSE,
    }.items():
        (directory / name).write_bytes(content)
    argv = p.command(Path(info["root"]) / info["executable"])
    started = datetime.now(UTC).isoformat()
    process = execute(
        argv,
        directory,
        plan["host_timeout_seconds"],
        plan["output_limit_bytes_per_file"],
        plan["job_memory_limit_mib"],
    )
    errors, assertions, current = [], None, False
    try:
        require(
            not process["error"],
            process["error"] or "backend failed",
            process["error"] or "BACKEND_FAILED",
        )
        require(
            process["exit_code"] == 0 and process["forced_termination"] is False,
            "backend exit failed",
            "BACKEND_FAILED",
        )
        assertions = p.check_output(
            payload(directory / "backend.stdout.bin", plan["output_limit_bytes_per_file"]),
            payload(directory / "backend.stderr.bin", plan["output_limit_bytes_per_file"]),
            payload(directory / "uart.bin", plan["output_limit_bytes_per_file"]),
            plan["assertions"],
        )
        bound(directory / "efuse-work.bin", sha(p.EFUSE), 1024)
    except (SimError, OSError, UnicodeError) as error:
        errors.append({"code": getattr(error, "code", "EVIDENCE_INVALID"), "message": str(error)})
    try:
        runtime(lock, plan["runtime_lock"]["sha256"], p.BACKEND)
        bound(Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], FLASH_SIZE)
        bound(p.MODEL, plan["model_sha256"], 16384)
        bound(directory / "flash-input.bin", plan["firmware"]["sha256"], FLASH_SIZE)
        bound(directory / Path(p.ROM).name, p.ROM_SHA, 1024 * 1024)
        current = True
    except (SimError, OSError) as error:
        errors.append({"code": "INPUT_CHANGED", "message": str(error)})
    report = {
        "schema_version": p.REPORT_SCHEMA,
        **FLAGS,
        "ok": not errors,
        "status": "failed"
        if errors
        else ("passed_with_model_limits" if assertions["model_diagnostics"] else "passed"),
        "backend": p.BACKEND,
        "profile": p.PROFILE,
        "backend_version": info["version"],
        "evidence_origin": "qemu_execution_attempt" if process["pid"] else "backend_not_started",
        "executables_started": process["pid"] is not None,
        "plan_sha256": sha(raw),
        "runtime_lock_sha256": plan["runtime_lock"]["sha256"],
        "runtime_archive_sha256": info["archive_sha256"],
        "backend_argv": argv,
        "host_started_utc": started,
        "host_finished_utc": datetime.now(UTC).isoformat(),
        "virtual_time": dict(p.VIRTUAL_TIME),
        "uart_timestamps_available": False,
        "process": process,
        "assertions": assertions,
        "input_hashes_current": current,
        "security_features_verified": False,
        "model_coverage_complete": False,
        "artifacts": {p.name: file_record(p) for p in directory.iterdir() if p.is_file()},
        "errors": errors,
    }
    write_json(directory / "report.json", report)
    return report


def inspect_report(path, expected, *, profile=None):
    p = profile or sys.modules[__name__]
    report = decode(bound(path, expected, 1024 * 1024))
    require(
        report["schema_version"] == p.REPORT_SCHEMA
        and report["profile"] == p.PROFILE
        and report["backend"] == p.BACKEND,
        "unsupported Flash report",
    )
    require(
        report["security_features_verified"] is False
        and report["uart_timestamps_available"] is False
        and report["virtual_time"] == p.VIRTUAL_TIME,
        "unsupported verification/timestamp claim",
    )
    require(
        report["virtual_time"]["budget_enforced"] is False
        and report["virtual_time"]["elapsed_ns"] is None,
        "invalid virtual-time field types",
    )
    for key, value in FLAGS.items():
        require(
            type(report[key]) is type(value) and report[key] == value, "invalid evidence boundary"
        )
    require(
        type(report["ok"]) is bool
        and report["status"] in ("passed", "passed_with_model_limits", "failed")
        and report["ok"] is (report["status"] != "failed")
        and report["model_coverage_complete"] is False,
        "invalid report status",
    )
    required = {
        "plan.json",
        "runtime-lock.json",
        "model.json",
        "flash-input.bin",
        "flash-work.bin",
        Path(p.ROM).name,
        "efuse-initial.bin",
        "efuse-work.bin",
    }
    require(
        required
        <= set(report["artifacts"])
        <= required | {"backend.stdout.bin", "backend.stderr.bin", "uart.bin"},
        "invalid Flash artifact inventory",
    )
    loaded = {}
    for name, record in report["artifacts"].items():
        loaded[name] = bound(path.parent / name, record["sha256"], FLASH_SIZE)
        require(len(loaded[name]) == record["bytes"], "artifact size differs")
    plan = decode(loaded["plan.json"])
    info = decode(loaded["runtime-lock.json"])
    require(
        plan["schema_version"] == p.PLAN_SCHEMA
        and plan["profile"] == p.PROFILE
        and plan["backend"] == p.BACKEND,
        "plan profile differs",
    )
    for key, value in FLAGS.items():
        require(type(plan[key]) is type(value) and plan[key] == value, "invalid plan boundary")
    require(
        sha(loaded["plan.json"]) == report["plan_sha256"]
        and sha(loaded["runtime-lock.json"])
        == plan["runtime_lock"]["sha256"]
        == report["runtime_lock_sha256"]
        and sha(loaded["flash-input.bin"]) == plan["firmware"]["sha256"]
        and sha(loaded["model.json"]) == plan["model_sha256"],
        "input binding differs",
    )
    require(
        info["backend"] == p.BACKEND
        and info["version"]
        == plan["backend_version"]
        == report["backend_version"]
        == qemu.RUNTIME_VERSION
        and info["executable"] == p.EXECUTABLE
        and info["archive_sha256"] == report["runtime_archive_sha256"] == p.ARCHIVE_SHA256
        and info["files"][p.ROM] == p.ROM_SHA,
        "runtime identity differs",
    )
    require(
        sha(loaded[Path(p.ROM).name]) == plan["rom_sha256"] == p.ROM_SHA
        and loaded["efuse-initial.bin"] == p.EFUSE
        and sha(p.EFUSE) == plan["efuse_initial_sha256"],
        "ROM/eFuse identity differs",
    )
    require(
        p.flash_info(loaded["flash-input.bin"])
        == {k: v for k, v in plan["firmware"].items() if k not in ("path", "sha256")},
        "Flash layout metadata differs",
    )
    model = decode(loaded["model.json"])
    require(
        loaded["model.json"] == payload(p.MODEL, 16384)
        and model["profile"] == p.PROFILE
        and model["bridges"] == []
        and plan["physical_bridges"] == []
        and type(plan["retries"]) is int
        and plan["retries"] == 0
        and report["backend_argv"] == p.command(Path(info["root"]) / info["executable"]),
        "command/profile boundary differs",
    )
    require(
        len(loaded["flash-work.bin"]) == FLASH_SIZE and len(loaded["efuse-work.bin"]) == 1024,
        "private storage size differs",
    )
    if report["ok"]:
        process = report["process"]
        cleanup = process["cleanup"]
        require(
            report["executables_started"] is True
            and report["evidence_origin"] == "qemu_execution_attempt"
            and type(process["pid"]) is int
            and process["pid"] > 0
            and process["execution_resumed"] is True
            and process["exit_code"] == 0
            and process["error"] is None
            and process["forced_termination"] is False
            and cleanup["process_exited"] is True
            and cleanup["job_active_processes"] == 0
            and cleanup["capture_threads_stopped"] is True
            and report["errors"] == []
            and report["input_hashes_current"] is True,
            "passing report lacks native completion/cleanup",
        )
        require(
            loaded["efuse-work.bin"] == p.EFUSE
            and p.check_output(
                loaded["backend.stdout.bin"],
                loaded["backend.stderr.bin"],
                loaded["uart.bin"],
                plan["assertions"],
            )
            == report["assertions"],
            "saved runtime assertions differ",
        )
        require(
            (report["status"] == "passed_with_model_limits")
            is bool(report["assertions"]["model_diagnostics"]),
            "diagnostic status mismatch",
        )
    return {
        **FLAGS,
        "ok": True,
        "operation": "inspect-report",
        "executables_started": False,
        "report_status": report["status"],
        "artifacts_verified": len(loaded),
        "scope": "saved_simulation_evidence_integrity",
    }
