from __future__ import annotations

import json
from pathlib import Path

from .backend import check_logs
from .common import FLAGS, bound, decode, payload, require, sha
from .plan import evaluate


def inspect_report(path: Path, expected: str, *, profile=None) -> dict:
    """Validate a movable evidence directory without loading or launching Renode."""
    report = decode(bound(path, expected, 1024 * 1024))
    schema = "embedded-sim-runner.report.v1" if profile is None else profile.REPORT_SCHEMA
    require(report["schema_version"] == schema, "unsupported report")
    if profile is None:
        require(report.get("profile") == "nrf52840-uart-v1", "unsupported Renode report profile")
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
    if profile is not None:
        require(
            plan["schema_version"] == profile.PLAN_SCHEMA
            and report["profile"] == plan["profile"] == profile.PROFILE,
            "GPIO profile binding differs",
        )
        for key, value in FLAGS.items():
            require(
                type(plan.get(key)) is type(value) and plan[key] == value,
                "GPIO plan boundary differs",
            )
        require(
            all(
                type(report.get(key)) is type(value)
                and type(plan.get(key)) is type(value)
                and report[key] == value
                and plan[key] == value
                for key, value in profile.COVERAGE.items()
            ),
            "GPIO coverage claim differs",
        )
        require(
            loaded["model.repl"] == payload(profile.MODEL, 16384)
            and loaded["run.resc"] == profile.script(plan["virtual_ms"]).encode()
            and sha(loaded["run.resc"]) == plan["script_sha256"]
            and type(plan["virtual_ms"]) is int
            and plan["virtual_ms"] == profile.VIRTUAL_MS
            and json.dumps(plan.get("scenario"), sort_keys=True)
            == json.dumps(profile.SCENARIO, sort_keys=True)
            and loaded["renode.config"] == b"",
            "GPIO model/script differs",
        )
        require(
            report.get("uart_timestamps_available") is False
            and isinstance(report.get("virtual_time"), dict)
            and report.get("virtual_time")
            == {
                "domain": "renode_emulation",
                "requested_ms": 82,
                "completed": report["virtual_time"].get("completed"),
            }
            and type(report["virtual_time"]["completed"]) is bool,
            "GPIO virtual-time declaration differs",
        )
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
        if profile is not None:
            require(
                report["virtual_time"]["completed"] is True
                and report["executables_started"] is True
                and report["evidence_origin"] == "renode_execution_attempt",
                "GPIO completion evidence differs",
            )
        evaluated = (
            evaluate(loaded["uart.bin"], plan["assertions"])
            if profile is None
            else profile.evaluate(
                loaded["uart.bin"], plan["assertions"], loaded["backend.stdout.bin"]
            )
        )
        require(
            evaluated == report["assertions"],
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
