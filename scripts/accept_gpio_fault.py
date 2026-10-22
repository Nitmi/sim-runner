"""Native Renode fault controls; expected failures stay failed, no hardware access."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sim_runner.process import execute


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def accept(python, lock, fixture, output):
    output.mkdir(parents=True, exist_ok=False)
    calls, cases, nominal = [], [], []

    def call(name, arguments, expected=0):
        directory = output / ("cli-" + name)
        directory.mkdir()
        command = [str(python), "-I", "-m", "sim_runner", *map(str, arguments)]
        process = execute(command, directory, 180, 1048576, 1536)
        stdout = (directory / "backend.stdout.bin").read_bytes()
        stderr = (directory / "backend.stderr.bin").read_bytes()
        (output / (name + ".stdout.json")).write_bytes(stdout)
        calls.append({"name": name, "argv": command, "process": process})
        (output / "calls.json").write_text(json.dumps(calls, indent=2) + "\n")
        assert process["error"] is None and process["exit_code"] == expected, (
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
        result = json.loads(stdout)
        assert result["hardware_access"] is result["physical_board_verified"] is False
        return result

    manifest = json.loads((fixture / "manifest.json").read_bytes())
    image = fixture / "firmware.bin"
    assert sha(image) == manifest["firmware_sha256"]
    runtime = ["--runtime-lock", lock, "--runtime-sha256", sha(lock)]
    call("doctor", ["doctor", *runtime])
    for name, fault, error in (
        ("normal-a", "none", None),
        ("normal-b", "none", None),
        ("stuck-low", "gpio-stuck-low", "ASSERTION_FAILED"),
        ("stuck-high", "gpio-stuck-high", "ASSERTION_FAILED"),
        ("stopped-timer", "timer-stop", "ASSERTION_FAILED"),
        ("masked-irq", "timer-irq-mask", "ASSERTION_FAILED"),
        ("wrong-build", "none", "ASSERTION_FAILED"),
        ("host-timeout", "timer-stop", "TIMEOUT"),
    ):
        plan = output / (name + ".plan.json")
        call(
            name + "-plan",
            [
                "plan",
                "--profile",
                "nrf52840-gpio-timer-fault-v1",
                "--fault",
                fault,
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
        report = call(
            name + "-run",
            ["run", "--plan", plan, "--sha256", sha(plan), "--output-dir", directory],
            1 if error else 0,
        )
        assert report == json.loads((directory / "report.json").read_bytes())
        assert report["status"] == ("failed" if error else "passed")
        assert report["fault_scenario"] == fault
        assert report["model_coverage_complete"] is report["physical_timing_verified"] is False
        assert report["input_hashes_current"] is True
        assert report["process"]["cleanup"] == {
            "process_exited": True,
            "job_active_processes": 0,
            "capture_threads_stopped": True,
        }
        if error:
            assert report["errors"][0]["code"] == error, (name, report)
        if name in ("normal-a", "normal-b"):
            assert report["assertions"]["nominal_regression_passed"] is True
            assert report["assertions"]["fault"]["effect_observed"] is None
            assert len(report["assertions"]["samples"]) == 16
            nominal.append(
                {"uart": sha(directory / "uart.bin"), "assertions": report["assertions"]}
            )
        elif name == "host-timeout":
            assert report["assertions"] is None
        else:
            evidence = report["assertions"]
            assert evidence["nominal_regression_passed"] is False
            assert evidence["fault"]["effect_observed"] is (
                None if name == "wrong-build" else True
            ), (name, evidence)
            assert len(evidence["model_observations"]) == 4
            assert len(evidence["samples"]) == (4 if fault.startswith("timer-") else 16)
        saved = directory / "report.json"
        inspected = call(
            name + "-inspect-report", ["inspect-report", "--report", saved, "--sha256", sha(saved)]
        )
        assert inspected["report_status"] == report["status"]
        assert inspected["executables_started"] is False
        cases.append(
            {
                "case": name,
                "fault": fault,
                "status": report["status"],
                "error": error,
                "report": str(saved),
                "report_sha256": sha(saved),
                "effect_observed": inspected["fault_effect_observed"],
            }
        )
    assert nominal[0] == nominal[1], "normal control is not reproducible"
    original = json.loads((output / "stuck-low/report.json").read_bytes())
    original["assertions"]["fault"]["effect_observed"] = False
    altered = output / "stuck-low/rehashed-false-evidence.json"
    altered.write_text(json.dumps(original))
    assert (
        call(
            "reject-rehashed-false-effect",
            ["inspect-report", "--report", altered, "--sha256", sha(altered)],
            1,
        )["ok"]
        is False
    )
    result = {
        "ok": True,
        "execution_kind": "simulation",
        "hardware_access": False,
        "physical_board_verified": False,
        "commands": len(calls),
        "cases": cases,
        "normal_reproducible": True,
        "injection_does_not_convert_native_failure_to_pass": True,
    }
    (output / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("python", "runtime-lock", "fixture", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    accept(
        args.python.resolve(),
        args.runtime_lock.resolve(),
        args.fixture.resolve(),
        args.output.resolve(),
    )
