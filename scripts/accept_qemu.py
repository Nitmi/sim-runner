"""Actual pinned QEMU, installed CLI, positive/negative evidence and offline checks."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from sim_runner.common import FLAGS, sha
from sim_runner.process import execute


def accept(executable: Path, lock: Path, fixtures: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    calls = []

    def call(name, arguments, expected=0):
        directory = output / ("cli-" + name)
        directory.mkdir()
        result = execute([str(executable), *arguments], directory, 180, 1048576, 1536)
        stdout = (directory / "backend.stdout.bin").read_bytes()
        calls.append({"name": name, "argv": arguments, "process": result})
        (output / "calls.json").write_text(json.dumps(calls, indent=2) + "\n")
        assert result["error"] is None and result["exit_code"] == expected, (name, result, stdout)
        value = json.loads(stdout)
        for key, wanted in FLAGS.items():
            assert type(value[key]) is type(wanted) and value[key] == wanted
        return value

    common = ["--runtime-lock", str(lock), "--runtime-sha256", sha(lock.read_bytes())]
    doctor = call("doctor", ["doctor", "--backend", "espressif-qemu", *common])
    assert doctor["executables_started"] is False
    hashes = []
    for name, variant, failure in (
        ("positive-a", "normal", None),
        ("positive-b", "normal", None),
        ("wrong-build", "normal", "ASSERTION_FAILED"),
        ("unmapped-write", "unmapped-write", "MODEL_COVERAGE_FAILED"),
        ("host-timeout", "hang", "TIMEOUT"),
    ):
        fixture = fixtures / variant
        manifest = json.loads((fixture / "manifest.json").read_bytes())
        image = fixture / "firmware.elf"
        assert sha(image.read_bytes()) == manifest["elf_sha256"]
        plan = output / f"{name}.plan.json"
        call(
            name + "-plan",
            [
                "plan",
                "--backend",
                "espressif-qemu",
                *common,
                "--firmware",
                str(image),
                "--firmware-sha256",
                manifest["elf_sha256"],
                "--ready-line",
                manifest["ready_line"],
                "--build-line",
                "SIM_ESP32C3_BUILD WRONG" if name == "wrong-build" else manifest["build_line"],
                "--heartbeat-prefix",
                manifest["heartbeat_prefix"],
                "--timeout",
                "1" if variant == "hang" else "30",
                "--output",
                str(plan),
            ],
        )
        plan_args = ["--plan", str(plan), "--sha256", sha(plan.read_bytes())]
        call(name + "-inspect-plan", ["inspect-plan", *plan_args])
        report = call(
            name + "-run",
            ["run", *plan_args, "--output-dir", str(output / name)],
            int(failure is not None),
        )
        assert report["process"]["cleanup"] == {
            "process_exited": True,
            "job_active_processes": 0,
            "capture_threads_stopped": True,
        }
        assert report["input_hashes_current"] is True
        if failure:
            assert report["errors"][0]["code"] == failure, report["errors"]
        else:
            assert report["assertions"]["heartbeats"] == 5
            hashes.append(sha((output / name / "uart.bin").read_bytes()))
        saved = output / name / "report.json"
        inspected = call(
            name + "-inspect-report",
            ["inspect-report", "--report", str(saved), "--sha256", sha(saved.read_bytes())],
        )
        assert inspected["executables_started"] is False
    assert hashes[0] == hashes[1], "fresh QEMU UART outputs differ"
    copied = output / "copied evidence"
    shutil.copytree(output / "positive-a", copied)
    saved = copied / "report.json"
    call(
        "copied-inspect",
        ["inspect-report", "--report", str(saved), "--sha256", sha(saved.read_bytes())],
    )
    (copied / "uart.bin").write_bytes(b"tampered\n")
    tampered = call(
        "tampered-inspect",
        ["inspect-report", "--report", str(saved), "--sha256", sha(saved.read_bytes())],
        1,
    )
    assert tampered["error"]["code"] == "INPUT_CHANGED"
    rejected = output / "rejected-input"
    mismatch = call(
        "wrong-plan-hash",
        ["run", "--plan", str(plan), "--sha256", "0" * 64, "--output-dir", str(rejected)],
        1,
    )
    assert mismatch["error"]["code"] == "INPUT_CHANGED" and not rejected.exists()
    summary = {
        "ok": True,
        **FLAGS,
        "evidence_origin": "actual_espressif_qemu_execution",
        "positive_runs": 2,
        "negative_runs": 3,
        "same_uart_sha256": hashes[0],
        "calls": len(calls),
    }
    (output / "acceptance.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    accept(args.executable, args.runtime_lock, args.fixtures, args.output)
