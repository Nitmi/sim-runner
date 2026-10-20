"""Run actual Renode positive/negative cases through an installed sim-runner CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sim_runner.process import execute


def sha(data):
    return hashlib.sha256(data).hexdigest()


def accept(executable: Path, lock: Path, firmware: Path, fault_firmware: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((firmware / "manifest.json").read_bytes())
    common = ["--runtime-lock", str(lock), "--runtime-sha256", sha(lock.read_bytes())]
    image = firmware / "firmware.bin"
    calls = []

    def call(name, arguments, expected=0):
        call_dir = output / ("cli-" + name)
        call_dir.mkdir()
        result = execute([str(executable), *arguments], call_dir, 180, 1048576, 1536)
        stdout = (call_dir / "backend.stdout.bin").read_bytes()
        stderr = (call_dir / "backend.stderr.bin").read_bytes()
        (output / f"{name}.stdout.json").write_bytes(stdout)
        (output / f"{name}.stderr.bin").write_bytes(stderr)
        record = {"name": name, "argv": arguments, "process": result}
        calls.append(record)
        (output / "calls.json").write_text(json.dumps(calls, indent=2) + "\n")
        if result["error"] or result["exit_code"] != expected:
            raise AssertionError(f"{name}: expected exit {expected}, got {result}")
        value = json.loads(stdout)
        if value["hardware_access"] is not False or value["physical_board_verified"] is not False:
            raise AssertionError("evidence boundaries changed")
        return value

    doctor = call("doctor", ["doctor", *common])
    assert doctor["executables_started"] is False
    hashes = []
    for name, build in (
        ("positive-a", manifest["build_line"]),
        ("positive-b", manifest["build_line"]),
        ("wrong-build", "SIM_NRF52840_BUILD WRONG"),
    ):
        plan_path = output / f"{name}.plan.json"
        call(
            name + "-plan",
            [
                "plan",
                *common,
                "--firmware",
                str(image),
                "--firmware-sha256",
                sha(image.read_bytes()),
                "--ready-line",
                manifest["ready_line"],
                "--build-line",
                build,
                "--heartbeat-prefix",
                manifest["heartbeat_prefix"],
                "--output",
                str(plan_path),
            ],
        )
        plan_sha = sha(plan_path.read_bytes())
        call(name + "-inspect", ["inspect-plan", "--plan", str(plan_path), "--sha256", plan_sha])
        value = call(
            name + "-run",
            [
                "run",
                "--plan",
                str(plan_path),
                "--sha256",
                plan_sha,
                "--output-dir",
                str(output / name),
            ],
            1 if name == "wrong-build" else 0,
        )
        assert value["input_hashes_current"] is True
        assert value["process"]["cleanup"]["job_active_processes"] == 0
        assert value["process"]["cleanup"]["capture_threads_stopped"] is True
        assert value["process"]["exit_code"] == 0
        if name == "wrong-build":
            assert value["status"] == "failed"
            assert value["errors"][0]["code"] == "ASSERTION_FAILED"
        else:
            assert value["ok"] is True and value["assertions"]["heartbeats"] >= 3
            hashes.append(sha((output / name / "uart.bin").read_bytes()))
            for artifact in value["artifacts"].values():
                assert sha(Path(artifact["path"]).read_bytes()) == artifact["sha256"]
        report_path = output / name / "report.json"
        inspected = call(
            name + "-inspect-report",
            [
                "inspect-report",
                "--report",
                str(report_path),
                "--sha256",
                sha(report_path.read_bytes()),
            ],
        )
        assert inspected["executables_started"] is False
    assert hashes[0] == hashes[1], "UART output changed between fresh fixed-input runs"
    fault_manifest = json.loads((fault_firmware / "manifest.json").read_bytes())
    assert fault_manifest["intentional_unmapped_write"] is True
    for name, source, details, extra, error in (
        ("unmapped-write", fault_firmware, fault_manifest, [], "MODEL_COVERAGE_FAILED"),
        (
            "host-timeout",
            firmware,
            manifest,
            ["--virtual-ms", "10000", "--timeout", "1"],
            "TIMEOUT",
        ),
    ):
        plan_path = output / f"{name}.plan.json"
        image_path = source / "firmware.bin"
        call(
            name + "-plan",
            [
                "plan",
                *common,
                "--firmware",
                str(image_path),
                "--firmware-sha256",
                sha(image_path.read_bytes()),
                "--ready-line",
                details["ready_line"],
                "--build-line",
                details["build_line"],
                "--heartbeat-prefix",
                details["heartbeat_prefix"],
                *extra,
                "--output",
                str(plan_path),
            ],
        )
        value = call(
            name + "-run",
            [
                "run",
                "--plan",
                str(plan_path),
                "--sha256",
                sha(plan_path.read_bytes()),
                "--output-dir",
                str(output / name),
            ],
            1,
        )
        assert value["errors"][0]["code"] == error
        assert value["process"]["cleanup"]["job_active_processes"] == 0
        assert value["process"]["cleanup"]["capture_threads_stopped"] is True
    # Input mismatch must fail before creating a directory or executing the backend.
    reject = output / "rejected-input"
    value = call(
        "reject-plan-hash",
        ["run", "--plan", str(plan_path), "--sha256", "0" * 64, "--output-dir", str(reject)],
        1,
    )
    assert value["error"]["code"] == "INPUT_CHANGED" and not reject.exists()
    result = {
        "ok": True,
        "evidence_origin": "actual_renode_execution",
        "hardware_access": False,
        "physical_board_verified": False,
        "positive_runs": 2,
        "negative_build_runs": 1,
        "negative_unmapped_write_runs": 1,
        "negative_timeout_runs": 1,
        "same_uart_sha256": hashes[0],
        "calls": len(calls),
    }
    (output / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--firmware-dir", type=Path, required=True)
    parser.add_argument("--fault-firmware-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    accept(
        args.executable, args.runtime_lock, args.firmware_dir, args.fault_firmware_dir, args.output
    )
