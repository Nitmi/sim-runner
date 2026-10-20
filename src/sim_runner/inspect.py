from __future__ import annotations

from pathlib import Path

from .backend import check_logs
from .common import FLAGS, bound, decode, require, sha
from .plan import evaluate


def inspect_report(path: Path, expected: str) -> dict:
    """Validate a movable evidence directory without loading or launching Renode."""
    report = decode(bound(path, expected, 1024 * 1024))
    require(report["schema_version"] == "embedded-sim-runner.report.v1", "unsupported report")
    for key, value in FLAGS.items():
        require(
            type(report[key]) is type(value) and report[key] == value, "invalid evidence boundary"
        )
    require(report["status"] in ("passed", "failed"), "invalid report status")
    require(report["ok"] is (report["status"] == "passed"), "inconsistent result")
    artifacts = report["artifacts"]
    allowed = {
        "plan.json",
        "runtime-lock.json",
        "model.repl",
        "firmware.bin",
        "run.resc",
        "renode.config",
        "backend.stdout.bin",
        "backend.stderr.bin",
        "uart.bin",
    }
    require(set(artifacts) <= allowed, "unexpected artifact name")
    require(
        {
            "plan.json",
            "runtime-lock.json",
            "model.repl",
            "firmware.bin",
            "run.resc",
            "renode.config",
        }
        <= set(artifacts),
        "missing input evidence",
    )
    loaded = {}
    for name, record in artifacts.items():
        data = bound(path.parent / name, record["sha256"], 16 * 1024 * 1024)
        require(len(data) == record["bytes"], "artifact size mismatch")
        loaded[name] = data
    require(sha(loaded["plan.json"]) == report["plan_sha256"], "plan binding mismatch")
    plan = decode(loaded["plan.json"])
    require(
        sha(loaded["runtime-lock.json"])
        == report["runtime_lock_sha256"]
        == plan["runtime_lock"]["sha256"],
        "runtime lock binding mismatch",
    )
    require(sha(loaded["firmware.bin"]) == plan["firmware"]["sha256"], "firmware binding mismatch")
    require(sha(loaded["model.repl"]) == plan["model_sha256"], "model binding mismatch")
    if report["ok"]:
        process = report["process"]
        require(process["exit_code"] == 0 and process["error"] is None, "backend did not succeed")
        require(process["forced_termination"] is False, "backend was forcibly terminated")
        cleanup = process["cleanup"]
        require(
            cleanup["process_exited"] is True
            and cleanup["job_active_processes"] == 0
            and cleanup["capture_threads_stopped"] is True,
            "cleanup incomplete",
        )
        require(
            report["input_hashes_current"] is True and report["errors"] == [],
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
