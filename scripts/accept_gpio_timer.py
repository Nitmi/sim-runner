"""Actual cold-install Renode GPIO/TIMER0 positive and negative acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sim_runner.process import execute


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def accept(python, lock, fixtures, output):
    output.mkdir(parents=True, exist_ok=False)
    calls = []
    results = []

    def call(name, arguments, expected=0):
        directory = output / ("cli-" + name)
        directory.mkdir()
        command = [str(python), "-I", "-m", "sim_runner", *map(str, arguments)]
        process = execute(command, directory, 180, 1048576, 1536)
        stdout = (directory / "backend.stdout.bin").read_bytes()
        stderr = (directory / "backend.stderr.bin").read_bytes()
        (output / (name + ".stdout.json")).write_bytes(stdout)
        (output / (name + ".stderr.bin")).write_bytes(stderr)
        calls.append({"name": name, "argv": command, "process": process})
        (output / "calls.json").write_text(json.dumps(calls, indent=2) + "\n")
        assert not process["error"] and process["exit_code"] == expected, (
            name,
            process,
            stdout,
            stderr,
        )
        assert process["cleanup"] == {
            "process_exited": True,
            "job_active_processes": 0,
            "capture_threads_stopped": True,
        }
        assert not stderr, (name, stderr)
        doc = json.loads(stdout)
        assert (
            doc["execution_kind"] == "simulation"
            and doc["hardware_access"] is doc["physical_board_verified"] is False
        )
        return doc

    runtime = ["--runtime-lock", lock, "--runtime-sha256", sha(lock)]
    assert call("doctor", ["doctor", *runtime])["executables_started"] is False
    positive = []
    for name, variant, error in (
        ("positive-a", "normal", None),
        ("positive-b", "normal", None),
        ("wrong-build", "normal", "ASSERTION_FAILED"),
        ("inverted-output", "invert_output", "ASSERTION_FAILED"),
        ("slow-timer", "slow_timer", "ASSERTION_FAILED"),
        ("missing-irq", "missing_irq", "ASSERTION_FAILED"),
        ("unmapped-access", "unmapped_write", "MODEL_COVERAGE_FAILED"),
        ("host-timeout", "normal", "TIMEOUT"),
    ):
        fixture = fixtures / variant
        manifest = json.loads((fixture / "manifest.json").read_bytes())
        image = fixture / "firmware.bin"
        assert sha(image) == manifest["firmware_sha256"]
        plan = output / (name + ".plan.json")
        call(
            name + "-plan",
            [
                "plan",
                "--profile",
                "nrf52840-gpio-timer-v1",
                *runtime,
                "--firmware",
                image,
                "--firmware-sha256",
                sha(image),
                "--ready-line",
                manifest["ready_line"],
                "--build-line",
                "SIM_NRF_IO_BUILD WRONG" if name == "wrong-build" else manifest["build_line"],
                "--heartbeat-prefix",
                manifest["sample_prefix"],
                "--timeout",
                "1" if name == "host-timeout" else "30",
                "--output",
                plan,
            ],
        )
        call(name + "-inspect-plan", ["inspect-plan", "--plan", plan, "--sha256", sha(plan)])
        directory = output / name
        doc = call(
            name + "-run",
            ["run", "--plan", plan, "--sha256", sha(plan), "--output-dir", directory],
            1 if error else 0,
        )
        assert doc == json.loads((directory / "report.json").read_bytes())
        assert doc["model_coverage_complete"] is doc["physical_timing_verified"] is False
        assert doc["process"]["cleanup"] == {
            "process_exited": True,
            "job_active_processes": 0,
            "capture_threads_stopped": True,
        }
        assert doc["input_hashes_current"] is True
        assert doc["ok"] is (error is None)
        if error:
            assert doc["errors"][0]["code"] == error, (name, doc["errors"])
            assert doc["assertions"] is None
        else:
            assert doc["status"] == "passed" and doc["virtual_time"]["completed"] is True
            samples = doc["assertions"]["samples"]
            assert len(samples) == 16 and [s["irq_vector"] for s in samples] == [24] * 16
            assert [s["input"] for s in samples] == [0] * 4 + [1] * 4 + [0] * 4 + [1] * 4
            assert all(s["input"] == s["output"] == s["loopback"] for s in samples)
            assert all(abs(s["timer_ticks"] - 1250 * s["sequence"]) <= 25 for s in samples)
            snapshots = doc["assertions"]["model_observations"]
            assert [s["requested_virtual_ms"] for s in snapshots] == [20, 40, 60, 80]
            positive.append(
                {"uart_sha256": sha(directory / "uart.bin"), "assertions": doc["assertions"]}
            )
        saved = directory / "report.json"
        result = call(
            name + "-inspect-report", ["inspect-report", "--report", saved, "--sha256", sha(saved)]
        )
        assert result["report_status"] == doc["status"] and result["executables_started"] is False
        results.append(
            {"case": name, "report": str(saved), "status": doc["status"], "expected_error": error}
        )
        assert sha(image) == manifest["firmware_sha256"]
    assert positive[0] == positive[1], (
        "independent initial state produced different UART/assertions"
    )
    # Saved evidence checks must catch a rehashed false model-state summary.
    original = json.loads((output / "positive-a/report.json").read_bytes())
    original["assertions"]["model_observations"][1]["in_register"] = 0
    altered = output / "positive-a/changed-report.json"
    altered.write_text(json.dumps(original))
    assert (
        call(
            "reject-altered-summary",
            ["inspect-report", "--report", altered, "--sha256", sha(altered)],
            1,
        )["ok"]
        is False
    )
    result = {
        "ok": True,
        "commands": len(calls),
        "execution_kind": "simulation",
        "hardware_access": False,
        "physical_board_verified": False,
        "physical_timing_verified": False,
        "repeat_uart_and_assertions_identical": True,
        "cases": results,
    }
    (output / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("python", "runtime-lock", "fixtures", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    accept(
        args.python.resolve(),
        args.runtime_lock.resolve(),
        args.fixtures.resolve(),
        args.output.resolve(),
    )
