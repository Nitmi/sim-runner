"""Experimental fixed ESP-EMU WASM / ESP32-C3 Flash boot, without host bridges."""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import __version__, qemu_flash
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
from .flash_image import FLASH_SIZE, flash_info
from .plan import ASSETS, inventory, marker
from .process import execute

BACKEND = "espressif-esp-emulator-wasm"
PROFILE = "esp32c3-wasm-flash-v1"
PLAN_SCHEMA = "embedded-sim-runner.esp-wasm-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.esp-wasm-report.v1"
RUNTIME_SCHEMA = "embedded-sim-runner.esp-wasm-runtime.v1"
MODEL = ASSETS / "esp32c3-esp-wasm.json"
HOST = ASSETS / "esp_wasm_runner.mjs"
COMPONENTS = ("esp_emu.js", "esp_emu_bg.wasm", "package.json", "LICENSE")
MODEL_LIMITS = [
    "experimental_binary_only_core_not_independently_source_verified",
    "uart_api_returns_strings_original_uart_byte_fidelity_not_established",
    "embedded_rom_and_efuse_defaults_not_independently_dumped",
    "cycle_counter_and_cycles_per_us_are_model_claims_not_measured_timing",
    "no_radio_network_electrical_or_complete_chip_coverage",
]
VIRTUAL_TIME = {
    "domain": "esp_emulator_unmeasured_device_clock",
    "elapsed_ns": None,
    "budget_enforced": False,
}
INITIAL_STATE = "fresh_instance_private_flash_builtin_rom_and_opaque_default_efuse"
# Reuse the fixed unsigned factory image / boot and application UART oracle.
ROM_MARKER, STARTUP_DIAGNOSTIC, KNOWN_DIAGNOSTICS = "ESP-ROM:esp32c3-", None, {}


def model():
    return decode(payload(MODEL, 16384))


def same(left, right, message):
    require(
        json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True),
        message,
        "INPUT_CHANGED",
    )


def absolute(value):
    return isinstance(value, str) and (
        PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()
    )


def lock_fields(value):
    m = model()
    require(
        set(value)
        == {"schema_version", "backend", "version", "archive_sha256", "root", "files", "node"},
        "invalid WASM lock fields",
    )
    require(
        value["schema_version"] == RUNTIME_SCHEMA
        and value["backend"] == BACKEND
        and value["version"] == m["version"],
        "unsupported WASM runtime",
    )
    require(value["archive_sha256"] == m["archive_sha256"], "unsupported WASM archive")
    same(value["files"], m["files"], "WASM inventory differs")
    require(absolute(value["root"]), "WASM root must be absolute")
    node = value["node"]
    require(
        set(node) == {"path", "sha256", "version"} and absolute(node["path"]),
        "invalid Node identity",
    )
    require(node["version"] == m["node_version"], "unsupported Node version")
    digest(node["sha256"])
    return value


def runtime(lock, expected):
    info = lock_fields(decode(bound(lock, expected, 65536)))
    root = Path(info["root"])
    require(root.is_dir() and not root.is_symlink(), "invalid WASM runtime root")
    require(
        inventory(root) == set(info["files"]), "WASM runtime inventory differs", "INPUT_CHANGED"
    )
    for name, hashed in info["files"].items():
        bound(root / name, hashed, 16 * 1024 * 1024)
    bound(Path(info["node"]["path"]), info["node"]["sha256"], 256 * 1024 * 1024)
    return info


def settings(ready, build, heartbeat, minimum, timeout, limit, memory, budget):
    for value in (ready, build, heartbeat):
        marker(value)
    require(len({ready, build, heartbeat}) == 3, "markers must differ")
    for value, low, high, name in (
        (minimum, 3, 10000, "minimum"),
        (timeout, 1, 120, "timeout"),
        (limit, 4096, 4 * 1024 * 1024, "output_limit"),
        (memory, 1024, 4096, "memory_mib"),
        (budget, 10000, 100000000, "instruction_budget"),
    ):
        require(type(value) is int and low <= value <= high, f"invalid {name}")
    require(budget % 10000 == 0, "instruction budget must be a multiple of 10000")


def fields(
    lock,
    lock_sha,
    firmware,
    image_sha,
    image_info,
    ready,
    build,
    heartbeat,
    minimum,
    timeout,
    limit,
    memory,
    budget,
):
    settings(ready, build, heartbeat, minimum, timeout, limit, memory, budget)
    return {
        "schema_version": PLAN_SCHEMA,
        "runner_version": __version__,
        **FLAGS,
        "executables_started": False,
        "backend": BACKEND,
        "backend_version": model()["version"],
        "profile": PROFILE,
        "runtime_lock": {"path": str(lock), "sha256": digest(lock_sha)},
        "firmware": {"path": str(firmware), "sha256": digest(image_sha), **image_info},
        "model_sha256": sha(payload(MODEL, 16384)),
        "host_script_sha256": sha(payload(HOST, 16384)),
        "initial_state": INITIAL_STATE,
        "host_timeout_seconds": timeout,
        "output_limit_bytes_per_file": limit,
        "job_memory_limit_mib": memory,
        "instruction_budget": budget,
        "assertions": {
            "ready": ready,
            "build": build,
            "heartbeat_prefix": heartbeat,
            "minimum": minimum,
        },
        "physical_bridges": [],
        "retries": 0,
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
    memory_mib=1536,
    instruction_budget=50000000,
):
    runtime(lock, lock_sha)
    image_info = flash_info(bound(image, image_sha, FLASH_SIZE))
    return fields(
        lock.resolve(),
        lock_sha,
        image.resolve(),
        image_sha,
        image_info,
        ready,
        build,
        heartbeat,
        min_heartbeats,
        timeout,
        output_limit,
        memory_mib,
        instruction_budget,
    )


def load_plan(path, expected):
    doc = decode(bound(path, expected, 65536))
    a = doc["assertions"]
    wanted = make_plan(
        Path(doc["runtime_lock"]["path"]),
        doc["runtime_lock"]["sha256"],
        Path(doc["firmware"]["path"]),
        doc["firmware"]["sha256"],
        a["ready"],
        a["build"],
        a["heartbeat_prefix"],
        a["minimum"],
        doc["host_timeout_seconds"],
        doc["output_limit_bytes_per_file"],
        doc["job_memory_limit_mib"],
        doc["instruction_budget"],
    )
    same(doc, wanted, "unsupported or changed WASM plan")
    return doc


def command(node, directory):
    directory = str(directory)
    require(not any(c in directory for c in "*?[],"), "unsupported Node permission path")
    pure = (
        PureWindowsPath(directory)
        if PureWindowsPath(directory).is_absolute()
        else PurePosixPath(directory)
    )
    return [
        str(node),
        "--permission",
        f"--allow-fs-read={directory}",
        f"--allow-fs-write={directory}",
        "--no-addons",
        "--disable-proto=throw",
        "--max-old-space-size=256",
        str(pure / "runner.mjs"),
    ]


def observation(raw, plan, uart):
    d = decode(raw)
    require(
        set(d)
        == {
            "schema_version",
            "node_version",
            "chip",
            "harts",
            "xtensa",
            "batches",
            "requested_instruction_upper_bound",
            "cycle_counter",
            "cycles_per_us_claim",
            "stop_reason",
            "uart_utf8_bytes",
            "uart_representation",
            "restart_performed",
            "physical_bridges",
        },
        "invalid WASM host summary",
    )
    require(
        d["schema_version"] == "embedded-sim-runner.esp-wasm-host.v1"
        and d["node_version"] == model()["node_version"]
        and d["chip"] == "esp32c3",
        "WASM host identity differs",
    )
    require(
        type(d["harts"]) is int and d["harts"] == 1 and d["xtensa"] is False, "WASM chip differs"
    )
    require(
        type(d["batches"]) is int and 1 <= d["batches"] <= plan["instruction_budget"] // 10000,
        "invalid batch count",
    )
    require(
        type(d["requested_instruction_upper_bound"]) is int
        and d["requested_instruction_upper_bound"] == d["batches"] * 10000,
        "invalid requested instruction bound",
    )
    require(
        isinstance(d["cycle_counter"], str) and re.fullmatch(r"[0-9]{1,20}", d["cycle_counter"]),
        "invalid model cycle counter",
    )
    require(
        type(d["cycles_per_us_claim"]) is int and d["cycles_per_us_claim"] == 160,
        "unexpected model clock claim",
    )
    require(
        d["stop_reason"] in ("guest_restart_requested", "instruction_budget")
        and d["restart_performed"] is False
        and d["physical_bridges"] == [],
        "invalid WASM stop or bridge claim",
    )
    require(
        type(d["uart_utf8_bytes"]) is int
        and d["uart_utf8_bytes"] == len(uart)
        and d["uart_representation"] == "ascii_subset_of_wasm_strings_encoded_utf8",
        "UART representation differs",
    )
    require(
        d["stop_reason"] != "instruction_budget"
        or d["requested_instruction_upper_bound"] == plan["instruction_budget"],
        "incomplete instruction budget",
    )
    uart.decode("ascii", errors="strict")
    return d


def analyze(plan, process, loaded):
    seen, assertions, errors = None, None, []
    try:
        require(
            set(process)
            == {
                "pid",
                "execution_resumed",
                "exit_code",
                "error",
                "error_detail",
                "forced_termination",
                "host_elapsed_seconds",
                "cleanup",
            }
            and (process["pid"] is None or type(process["pid"]) is int and process["pid"] > 0)
            and type(process["execution_resumed"]) is bool
            and type(process["forced_termination"]) is bool
            and (process["exit_code"] is None or type(process["exit_code"]) is int)
            and type(process["host_elapsed_seconds"]) in (int, float)
            and math.isfinite(process["host_elapsed_seconds"])
            and process["host_elapsed_seconds"] >= 0,
            "invalid process record",
            "EVIDENCE_INVALID",
        )
        require(
            not process["error"],
            process["error"] or "backend failed",
            process["error"] or "BACKEND_FAILED",
        )
        require(
            process["exit_code"] == 0 and process["forced_termination"] is False,
            "WASM host exit failed",
            "BACKEND_FAILED",
        )
        require(
            process["execution_resumed"] is True
            and process["cleanup"]["process_exited"] is True
            and type(process["cleanup"]["job_active_processes"]) is int
            and process["cleanup"]["job_active_processes"] == 0
            and process["cleanup"]["capture_threads_stopped"] is True,
            "WASM cleanup incomplete",
            "CLEANUP_FAILED",
        )
        require(
            not loaded.get("backend.stderr.bin"),
            "unexpected WASM host diagnostic",
            "MODEL_COVERAGE_FAILED",
        )
        seen = observation(loaded.get("backend.stdout.bin", b""), plan, loaded.get("uart.bin", b""))
        require(
            seen["stop_reason"] == "guest_restart_requested",
            "instruction budget exhausted before guest restart",
            "INSTRUCTION_BUDGET_EXCEEDED",
        )
        assertions = qemu_flash.check_output(
            b"", b"", loaded.get("uart.bin", b""), plan["assertions"], profile=sys.modules[__name__]
        )
    except (SimError, ValueError, KeyError, TypeError, UnicodeError) as error:
        code = getattr(error, "code", "EVIDENCE_INVALID")
        errors.append(
            {"code": "EVIDENCE_INVALID" if code == "INVALID_INPUT" else code, "message": str(error)}
        )
    return seen, assertions, errors


def run(plan_path, expected, directory):
    plan = load_plan(plan_path, expected)
    lock = Path(plan["runtime_lock"]["path"])
    info = runtime(lock, plan["runtime_lock"]["sha256"])
    inputs = {
        "plan.json": bound(plan_path, expected, 65536),
        "runtime-lock.json": bound(lock, plan["runtime_lock"]["sha256"], 65536),
        "model.json": bound(MODEL, plan["model_sha256"], 16384),
        "runner.mjs": bound(HOST, plan["host_script_sha256"], 16384),
        "flash-input.bin": bound(
            Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], FLASH_SIZE
        ),
    }
    for name in COMPONENTS:
        inputs["component/" + name] = bound(
            Path(info["root"]) / "www/pkg" / name,
            info["files"]["www/pkg/" + name],
            16 * 1024 * 1024,
        )
    argv = command(info["node"]["path"], directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name, raw in inputs.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
    started = datetime.now(UTC).isoformat()
    process = execute(
        argv,
        directory,
        plan["host_timeout_seconds"],
        plan["output_limit_bytes_per_file"],
        plan["job_memory_limit_mib"],
        node_environment=True,
    )
    captures = {
        p.name: payload(p, plan["output_limit_bytes_per_file"])
        for p in directory.iterdir()
        if p.name in {"backend.stdout.bin", "backend.stderr.bin", "uart.bin"}
    }
    seen, assertions, errors = analyze(plan, process, captures)
    current = False
    try:
        runtime(lock, plan["runtime_lock"]["sha256"])
        bound(Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], FLASH_SIZE)
        for name, raw in inputs.items():
            bound(directory / name, sha(raw), 16 * 1024 * 1024)
        current = True
    except (SimError, OSError) as error:
        errors.append({"code": "INPUT_CHANGED", "message": str(error)})
    report = {
        "schema_version": REPORT_SCHEMA,
        **FLAGS,
        "ok": not errors,
        "status": "failed" if errors else "passed_with_model_limits",
        "backend": BACKEND,
        "backend_version": info["version"],
        "profile": PROFILE,
        "runner_version": __version__,
        "evidence_origin": "esp_emulator_wasm_execution_attempt"
        if process["pid"]
        else "backend_not_started",
        "executables_started": process["pid"] is not None,
        "plan_sha256": expected.lower(),
        "runtime_lock_sha256": plan["runtime_lock"]["sha256"],
        "runtime_archive_sha256": info["archive_sha256"],
        "backend_argv": argv,
        "working_directory": str(directory),
        "host_started_utc": started,
        "host_finished_utc": datetime.now(UTC).isoformat(),
        "virtual_time": VIRTUAL_TIME,
        "uart_timestamps_available": False,
        "uart_representation": "ascii_subset_of_wasm_strings_encoded_utf8",
        "raw_uart_bytes_verified": False,
        "model_coverage_complete": False,
        "physical_timing_verified": False,
        "security_features_verified": False,
        "initial_state": INITIAL_STATE,
        "model_limits": MODEL_LIMITS,
        "process": process,
        "runtime_observation": seen,
        "assertions": assertions,
        "input_hashes_current": current,
        "artifacts": {
            name: file_record(directory / name) for name in sorted(set(inputs) | set(captures))
        },
        "errors": errors,
    }
    write_json(directory / "report.json", report)
    return report


def inspect_report(path, expected):
    report = decode(bound(path, expected, 1024 * 1024))
    require(
        report["schema_version"] == REPORT_SCHEMA
        and report["profile"] == PROFILE
        and report["backend"] == BACKEND,
        "unsupported WASM report",
    )
    for key, value in {
        **FLAGS,
        "virtual_time": VIRTUAL_TIME,
        "uart_timestamps_available": False,
        "raw_uart_bytes_verified": False,
        "model_coverage_complete": False,
        "physical_timing_verified": False,
        "security_features_verified": False,
        "model_limits": MODEL_LIMITS,
        "initial_state": INITIAL_STATE,
        "uart_representation": "ascii_subset_of_wasm_strings_encoded_utf8",
    }.items():
        same(report[key], value, "invalid WASM evidence boundary")
    required = {"plan.json", "runtime-lock.json", "model.json", "runner.mjs", "flash-input.bin"} | {
        "component/" + n for n in COMPONENTS
    }
    optional = {"backend.stdout.bin", "backend.stderr.bin", "uart.bin"}
    require(
        required <= set(report["artifacts"]) <= required | optional,
        "invalid WASM artifact inventory",
    )
    loaded = {}
    for name, ref in report["artifacts"].items():
        raw = bound(path.parent / name, ref["sha256"], 16 * 1024 * 1024)
        require(type(ref["bytes"]) is int and len(raw) == ref["bytes"], "artifact size differs")
        loaded[name] = raw
    plan = decode(loaded["plan.json"])
    info = lock_fields(decode(loaded["runtime-lock.json"]))
    require(
        sha(loaded["plan.json"]) == report["plan_sha256"]
        and sha(loaded["runtime-lock.json"])
        == plan["runtime_lock"]["sha256"]
        == report["runtime_lock_sha256"],
        "WASM plan/lock binding differs",
    )
    require(
        report["runtime_archive_sha256"] == info["archive_sha256"]
        and report["backend_version"] == info["version"],
        "WASM archive identity differs",
    )
    require(report["runner_version"] == plan["runner_version"], "runner version differs")
    require(
        report["evidence_origin"]
        == (
            "esp_emulator_wasm_execution_attempt"
            if report["process"]["pid"] is not None
            else "backend_not_started"
        ),
        "invalid evidence origin",
    )
    require(absolute(report["working_directory"]), "invalid original working directory")
    require(
        absolute(plan["runtime_lock"]["path"]) and absolute(plan["firmware"]["path"]),
        "invalid saved input references",
    )
    a = plan["assertions"]
    wanted = fields(
        plan["runtime_lock"]["path"],
        plan["runtime_lock"]["sha256"],
        plan["firmware"]["path"],
        sha(loaded["flash-input.bin"]),
        flash_info(loaded["flash-input.bin"]),
        a["ready"],
        a["build"],
        a["heartbeat_prefix"],
        a["minimum"],
        plan["host_timeout_seconds"],
        plan["output_limit_bytes_per_file"],
        plan["job_memory_limit_mib"],
        plan["instruction_budget"],
    )
    same(plan, wanted, "invalid saved WASM plan")
    require(
        loaded["model.json"] == payload(MODEL, 16384)
        and loaded["runner.mjs"] == payload(HOST, 16384),
        "saved WASM profile differs",
    )
    for name in COMPONENTS:
        require(
            sha(loaded["component/" + name]) == info["files"]["www/pkg/" + name],
            "saved WASM component differs",
        )
    same(
        report["backend_argv"],
        command(info["node"]["path"], report["working_directory"]),
        "saved WASM command differs",
    )
    require(type(report["input_hashes_current"]) is bool, "invalid current-input claim")
    require(
        type(report["executables_started"]) is bool
        and report["executables_started"] is (report["process"]["pid"] is not None),
        "invalid process start claim",
    )
    seen, assertions, derived = analyze(plan, report["process"], loaded)
    same(report["runtime_observation"], seen, "WASM model summary differs")
    same(report["assertions"], assertions, "WASM nominal assertions differ")
    errors = report["errors"]
    require(
        isinstance(errors, list)
        and all(
            isinstance(e, dict)
            and isinstance(e.get("code"), str)
            and isinstance(e.get("message"), str)
            for e in errors
        ),
        "invalid errors",
    )
    require(
        [e["code"] for e in errors]
        == [e["code"] for e in derived]
        + ([] if report["input_hashes_current"] else ["INPUT_CHANGED"]),
        "WASM errors differ",
    )
    require(
        report["ok"] is (not errors)
        and report["status"] == ("failed" if errors else "passed_with_model_limits"),
        "WASM verdict differs",
    )
    return {
        **FLAGS,
        "ok": True,
        "status": "valid",
        "executables_started": False,
        "recorded_status": report["status"],
        "runtime_observation": seen,
        "assertions": assertions,
        "model_limits": MODEL_LIMITS,
        "virtual_time": VIRTUAL_TIME,
        "raw_uart_bytes_verified": False,
        "artifacts_verified": len(loaded),
    }
