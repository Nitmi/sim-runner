"""Synthetic native transcripts test rejection; actual Renode uses accept_local.py."""

import pytest

from sim_runner.common import sha, write_json
from sim_runner.inspect import inspect_report
from sim_runner.plan import make_plan
from sim_runner.run import run


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("none", None),
        ("warning", "MODEL_COVERAGE_FAILED"),
        ("unhandled", "MODEL_COVERAGE_FAILED"),
        ("missing-completion", "BACKEND_FAILED"),
        ("nonzero", "BACKEND_FAILED"),
        ("timeout", "TIMEOUT"),
        ("wrong-build", "ASSERTION_FAILED"),
        ("runtime-changed", "INPUT_CHANGED"),
    ],
)
def test_exit_zero_or_valid_uart_cannot_hide_other_failures(
    inputs, tmp_path, monkeypatch, valid_uart, fault, expected
):
    plan_path = tmp_path / "plan.json"
    write_json(plan_path, make_plan(**inputs))

    def execute_double(argv, directory, *args):
        assert "--port" in argv and "-1" in argv and "--console" not in argv
        assert argv[-2:] == ["--execute", "include @run.resc"]
        stdout = b"SIM_RUNNER_COMPLETED_V1\n"
        if fault == "warning":
            stdout += b"[WARNING] unsupported register\n"
        if fault == "unhandled":
            stdout += b"unhandled write\n"
        if fault == "missing-completion":
            stdout = b""
        (directory / "backend.stdout.bin").write_bytes(stdout)
        (directory / "backend.stderr.bin").write_bytes(b"")
        data = (
            valid_uart.replace(b"BUILD abc", b"BUILD old") if fault == "wrong-build" else valid_uart
        )
        (directory / "uart.bin").write_bytes(data)
        if fault == "runtime-changed":
            (inputs["lock"].parent / "runtime/readme.txt").write_bytes(b"changed")
        return {
            "pid": 1,
            "exit_code": 7 if fault == "nonzero" else 0,
            "error": "TIMEOUT" if fault == "timeout" else None,
            "forced_termination": fault == "timeout",
            "cleanup": {
                "process_exited": True,
                "job_active_processes": 0,
                "capture_threads_stopped": True,
            },
        }

    monkeypatch.setattr("sim_runner.run.execute", execute_double)
    directory = tmp_path / "run"
    report = run(plan_path, sha(plan_path.read_bytes()), directory)
    assert report["ok"] is (expected is None)
    if expected:
        assert report["errors"][0]["code"] == expected
    assert report["physical_board_verified"] is False
    assert report["hardware_access"] is False
    assert (directory / "uart.bin").is_file()
    assert (directory / "report.json").is_file()
    saved = directory / "report.json"
    inspection = inspect_report(saved, sha(saved.read_bytes()))
    assert inspection["ok"] and inspection["executables_started"] is False
    assert inspection["report_status"] == report["status"]
    (directory / "uart.bin").write_bytes(b"tampered\n")
    with pytest.raises(Exception, match="SHA-256 mismatch"):
        inspect_report(saved, sha(saved.read_bytes()))
