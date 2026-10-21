"""Cold-installed CLI acceptance using actual SDK-built C3 Flash fixtures."""

import argparse
import json
import shutil
from pathlib import Path

from sim_runner.common import FLAGS, sha
from sim_runner.process import execute


def accept(executable, lock, fixtures, output):
    output.mkdir(parents=True, exist_ok=False)
    calls = []

    def call(name, args, expected=0):
        directory = output / ("cli-" + name)
        directory.mkdir()
        process = execute([str(executable), *map(str, args)], directory, 180, 1048576, 1536)
        raw = (directory / "backend.stdout.bin").read_bytes()
        calls.append({"id": name, "argv": list(map(str, args)), "process": process})
        (output / "calls.json").write_text(json.dumps(calls, indent=2))
        assert process["error"] is None and process["exit_code"] == expected, (name, process, raw)
        assert (directory / "backend.stderr.bin").read_bytes() == b"", name
        report = json.loads(raw)
        for key, value in FLAGS.items():
            assert type(report[key]) is type(value) and report[key] == value
        return report

    common = [
        "--backend",
        "espressif-qemu",
        "--profile",
        "esp32c3-idf-flash-v1",
        "--runtime-lock",
        lock,
        "--runtime-sha256",
        sha(lock.read_bytes()),
    ]
    images = {}
    for name, variant, error in (
        ("positive-a", "normal-5", None),
        ("positive-b", "normal-5", None),
        ("wrong-build", "normal-5", "ASSERTION_FAILED"),
        ("panic", "panic", "ASSERTION_FAILED"),
        ("host-timeout", "hang", "TIMEOUT"),
    ):
        fixture = fixtures / variant
        metadata = json.loads((fixture / "fixture.json").read_bytes())
        image = fixture / "flash.bin"
        images[image] = sha(image.read_bytes())
        assert images[image] == metadata["artifacts"]["flash.bin"]["sha256"]
        plan = output / (name + ".plan.json")
        call(
            name + "-plan",
            [
                "plan",
                *common,
                "--firmware",
                image,
                "--firmware-sha256",
                images[image],
                "--ready-line",
                "SIM_IDF_C3_READY v1",
                "--build-line",
                "SIM_IDF_C3_BUILD " + ("WRONG" if name == "wrong-build" else metadata["build_id"]),
                "--heartbeat-prefix",
                "SIM_IDF_C3_HEARTBEAT",
                "--timeout",
                "5" if variant == "hang" else "30",
                "--output",
                plan,
            ],
        )
        bound_plan = ["--plan", plan, "--sha256", sha(plan.read_bytes())]
        call(name + "-inspect-plan", ["inspect-plan", *bound_plan])
        report = call(
            name + "-run",
            ["run", *bound_plan, "--output-dir", output / name],
            int(error is not None),
        )
        assert report["process"]["cleanup"] == {
            "process_exited": True,
            "job_active_processes": 0,
            "capture_threads_stopped": True,
        }
        assert report["input_hashes_current"] is True and report["model_coverage_complete"] is False
        if error:
            assert report["errors"][0]["code"] == error, (name, report["errors"])
        else:
            assert report["status"] == "passed_with_model_limits"
            assert report["assertions"]["heartbeats"] == 5
            assert report["assertions"]["model_diagnostics"]
        saved = output / name / "report.json"
        result = call(
            name + "-inspect-report",
            ["inspect-report", "--report", saved, "--sha256", sha(saved.read_bytes())],
        )
        assert (
            result["report_status"] == report["status"] and result["executables_started"] is False
        )
    moved = output / "moved evidence"
    shutil.copytree(output / "positive-a", moved)
    report_path = moved / "report.json"
    call(
        "moved",
        ["inspect-report", "--report", report_path, "--sha256", sha(report_path.read_bytes())],
    )
    (moved / "uart.bin").write_bytes(b"tampered")
    call(
        "tampered-rejected",
        ["inspect-report", "--report", report_path, "--sha256", sha(report_path.read_bytes())],
        1,
    )
    corrupt = output / "corrupt.bin"
    data = bytearray(next(iter(images)).read_bytes())
    data[0x8000] ^= 1
    corrupt.write_bytes(data)
    result = call(
        "corrupt-image-rejected",
        [
            "plan",
            *common,
            "--firmware",
            corrupt,
            "--firmware-sha256",
            sha(data),
            "--ready-line",
            "READY",
            "--build-line",
            "BUILD",
            "--heartbeat-prefix",
            "HB",
            "--output",
            output / "must-not-exist.json",
        ],
        1,
    )
    assert result["ok"] is False and not (output / "must-not-exist.json").exists()
    for path, digest in images.items():
        assert sha(path.read_bytes()) == digest
    result = {
        "ok": True,
        "calls": len(calls),
        "actual_emulator_runs": 5,
        "passing_scoped_runs": 2,
        "known_model_limitations_retained": True,
        "input_images_unchanged": True,
        "execution_kind": "simulation",
        "hardware_access": False,
        "physical_board_verified": False,
        "model_coverage_complete": False,
    }
    (output / "acceptance.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    accept(
        args.executable.resolve(),
        args.runtime_lock.resolve(),
        args.fixtures.resolve(),
        args.output.resolve(),
    )
