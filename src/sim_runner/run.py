from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .backend import check_logs
from .common import (
    FLAGS,
    AssertionFailure,
    SimError,
    bound,
    file_record,
    payload,
    require,
    sha,
    write_json,
)
from .plan import ASSETS, evaluate, load_plan, runtime
from .process import execute


def script(virtual_ms: int) -> str:
    return "\n".join(
        [
            'mach create "nrf52840-uart"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            f'emulation RunFor "{virtual_ms / 1000:.3f}"',
            "sysbus.uart0 CloseFileBackend $ORIGIN/uart.bin",
            'log "SIM_RUNNER_COMPLETED_V1"',
            "quit",
            "",
        ]
    )


def run(plan_path: Path, expected: str, directory: Path, *, profile=None) -> dict:
    plan_data = bound(plan_path, expected, 65536)
    plan = (load_plan if profile is None else profile.load_plan)(plan_path, expected)
    require(not directory.exists(), "output directory already exists")
    lock_path = Path(plan["runtime_lock"]["path"])
    info = runtime(lock_path, plan["runtime_lock"]["sha256"])
    image = bound(Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], 1024 * 1024)
    model_path = ASSETS / "nrf52840-uart.repl" if profile is None else profile.MODEL
    model = bound(model_path, plan["model_sha256"], 16384)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "plan.json").write_bytes(plan_data)
    (directory / "runtime-lock.json").write_bytes(
        bound(lock_path, plan["runtime_lock"]["sha256"], 4 * 1024 * 1024)
    )
    (directory / "firmware.bin").write_bytes(image)
    (directory / "model.repl").write_bytes(model)
    (directory / "run.resc").write_text(
        (script if profile is None else profile.script)(plan["virtual_ms"]),
        encoding="utf-8",
        newline="\n",
    )
    (directory / "renode.config").write_text("", encoding="ascii")
    argv = [
        str(Path(info["root"]) / info["executable"]),
        "--disable-gui",
        "--port",
        "-1",
        "--plain",
        "--config",
        str(directory / "renode.config"),
        # Renode's implicit i @<full positional path> does not escape spaces.
        "--execute",
        "include @run.resc",
    ]
    started = datetime.now(UTC).isoformat()
    process = execute(
        argv,
        directory,
        plan["host_timeout_seconds"],
        plan["output_limit_bytes_per_file"],
        plan["job_memory_limit_mib"],
    )
    errors = []
    assertion_result = None
    current = False
    virtual_completed = False
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
        stdout = payload(directory / "backend.stdout.bin", plan["output_limit_bytes_per_file"])
        stderr = payload(directory / "backend.stderr.bin", plan["output_limit_bytes_per_file"])
        require(
            b"SIM_RUNNER_COMPLETED_V1" in stdout,
            "script completion marker missing",
            "BACKEND_FAILED",
        )
        virtual_completed = True
        # Renode may exit 0 after monitor/peripheral errors.
        check_logs(stdout, stderr)
        uart = payload(directory / "uart.bin", plan["output_limit_bytes_per_file"])
        assertion_result = (
            evaluate(uart, plan["assertions"])
            if profile is None
            else profile.evaluate(uart, plan["assertions"], stdout)
        )
    except (SimError, OSError, UnicodeError) as error:
        if isinstance(error, AssertionFailure):
            assertion_result = error.evidence
        errors.append({"code": getattr(error, "code", "EVIDENCE_INVALID"), "message": str(error)})
    try:
        runtime(lock_path, plan["runtime_lock"]["sha256"])
        bound(Path(plan["firmware"]["path"]), plan["firmware"]["sha256"], 1024 * 1024)
        bound(model_path, plan["model_sha256"], 16384)
        current = True
    except (SimError, OSError) as error:
        errors.append({"code": "INPUT_CHANGED", "message": str(error)})
    report = {
        "schema_version": "embedded-sim-runner.report.v1"
        if profile is None
        else profile.REPORT_SCHEMA,
        **FLAGS,
        "ok": not errors,
        "status": "passed" if not errors else "failed",
        "evidence_origin": "renode_execution_attempt" if process["pid"] else "backend_not_started",
        "executables_started": process["pid"] is not None,
        "plan_sha256": sha(plan_data),
        "profile": plan["profile"],
        "backend_version": info["version"],
        "runtime_lock_sha256": plan["runtime_lock"]["sha256"],
        "runtime_archive_sha256": info.get("archive_sha256"),
        "backend_argv": argv,
        "host_started_utc": started,
        "host_finished_utc": datetime.now(UTC).isoformat(),
        "virtual_time": {
            "domain": "renode_emulation",
            "requested_ms": plan["virtual_ms"],
            "completed": virtual_completed,
        },
        "uart_timestamps_available": False,
        "process": process,
        "assertions": assertion_result,
        "input_hashes_current": current,
        "artifacts": {p.name: file_record(p) for p in directory.iterdir() if p.is_file()},
        "errors": errors,
        **({} if profile is None else profile.COVERAGE),
    }
    write_json(directory / "report.json", report)
    return report
