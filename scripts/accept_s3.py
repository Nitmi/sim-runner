"""Cold-installed CLI acceptance using actual SDK-built S3 Flash fixtures."""

import argparse
import json
import shutil
from pathlib import Path

from sim_runner.common import FLAGS, sha
from sim_runner.process import execute


def accept(executable, lock, fixtures, output, c3_lock, c3_image):
    output.mkdir(parents=True, exist_ok=False)
    calls = []

    def call(name, arguments, expected=0):
        directory = output / ("cli-" + name)
        directory.mkdir()
        process = execute([str(executable), *map(str, arguments)], directory, 180, 1048576, 1536)
        raw = (directory / "backend.stdout.bin").read_bytes()
        calls.append({"id": name, "argv": list(map(str, arguments)), "process": process})
        (output / "calls.json").write_text(json.dumps(calls, indent=2))
        assert process["error"] is None and process["exit_code"] == expected, (name, process, raw)
        assert (directory / "backend.stderr.bin").read_bytes() == b"", name
        value = json.loads(raw)
        for key, flag in FLAGS.items():
            assert type(value[key]) is type(flag) and value[key] == flag
        return value

    common = [
        "--backend",
        "espressif-qemu-xtensa",
        "--profile",
        "esp32s3-idf-flash-v1",
        "--runtime-lock",
        lock,
        "--runtime-sha256",
        sha(lock.read_bytes()),
    ]
    call(
        "doctor",
        [
            "doctor",
            "--backend",
            "espressif-qemu-xtensa",
            "--runtime-lock",
            lock,
            "--runtime-sha256",
            sha(lock.read_bytes()),
        ],
    )
    images, records = {}, []
    for name, variant, error in (
        ("positive-a", "normal", None),
        ("positive-b", "normal", None),
        ("wrong-build", "normal", "ASSERTION_FAILED"),
        ("panic", "panic", "ASSERTION_FAILED"),
        ("missing-core1", "missing-core1", "ASSERTION_FAILED"),
        ("host-timeout", "hang", "TIMEOUT"),
    ):
        fixture = fixtures / variant
        metadata = json.loads((fixture / "fixture.json").read_bytes())
        image = fixture / "flash.bin"
        images[image] = sha(image.read_bytes())
        assert images[image] == metadata["artifacts"]["flash.bin"]["sha256"]
        plan = output / (name + ".plan.json")
        arguments = [
            "plan",
            *common,
            "--firmware",
            image,
            "--firmware-sha256",
            images[image],
            "--ready-line",
            "SIM_IDF_S3_READY v1",
            "--build-line",
            "SIM_IDF_S3_BUILD " + ("WRONG" if name == "wrong-build" else metadata["build_id"]),
            "--heartbeat-prefix",
            "SIM_IDF_S3_HEARTBEAT",
            "--timeout",
            "5" if variant == "hang" else "30",
            "--output",
            plan,
        ]
        planned = call(name + "-plan", arguments)
        assert planned["executables_started"] is False
        selected = ["--plan", plan, "--sha256", sha(plan.read_bytes())]
        call(name + "-inspect-plan", ["inspect-plan", *selected])
        report = call(
            name + "-run", ["run", *selected, "--output-dir", output / name], int(error is not None)
        )
        assert report["process"]["cleanup"] == {
            "process_exited": True,
            "job_active_processes": 0,
            "capture_threads_stopped": True,
        }
        assert report["input_hashes_current"] is True
        assert report["model_coverage_complete"] is report["security_features_verified"] is False
        assert report["virtual_time"] == {
            "domain": "qemu_device_clock_unmeasured",
            "elapsed_ns": None,
            "budget_enforced": False,
        }
        assert "-icount" not in report["backend_argv"]
        if error:
            assert report["errors"][0]["code"] == error, (name, report["errors"])
        else:
            assert report["status"] == "passed_with_model_limits"
            assert report["assertions"]["heartbeats"] == 5
            assert report["assertions"]["app_core_markers"] == [0, 1]
            assert sorted(d["count"] for d in report["assertions"]["model_diagnostics"]) == [
                1,
                1,
                1,
                3,
            ]
        saved = output / name / "report.json"
        inspected = call(
            name + "-inspect-report",
            ["inspect-report", "--report", saved, "--sha256", sha(saved.read_bytes())],
        )
        assert (
            inspected["report_status"] == report["status"]
            and inspected["executables_started"] is False
        )
        records.append({"name": name, "status": report["status"], "pid": report["process"]["pid"]})
    moved = output / "moved evidence"
    shutil.copytree(output / "positive-a", moved)
    saved = moved / "report.json"
    call("moved", ["inspect-report", "--report", saved, "--sha256", sha(saved.read_bytes())])
    changed = json.loads(saved.read_bytes())
    changed["model_coverage_complete"] = True
    altered = moved / "changed.json"
    altered.write_text(json.dumps(changed))
    call(
        "coverage-rejected",
        ["inspect-report", "--report", altered, "--sha256", sha(altered.read_bytes())],
        1,
    )
    (moved / "uart.bin").write_bytes(b"tampered")
    call(
        "tampered-rejected",
        ["inspect-report", "--report", saved, "--sha256", sha(saved.read_bytes())],
        1,
    )

    meta = json.loads((fixtures / "normal/fixture.json").read_bytes())
    extra = [
        "--ready-line",
        "SIM_IDF_S3_READY v1",
        "--build-line",
        "SIM_IDF_S3_BUILD " + meta["build_id"],
        "--heartbeat-prefix",
        "SIM_IDF_S3_HEARTBEAT",
    ]
    image = fixtures / "normal/flash.bin"
    corrupt = output / "corrupt.bin"
    raw = bytearray(image.read_bytes())
    raw[0x8000] ^= 1
    corrupt.write_bytes(raw)
    for name, selected_lock, selected_image in (
        ("corrupt", lock, corrupt),
        ("c3-runtime", c3_lock, image),
        ("c3-image", lock, c3_image),
    ):
        target = output / (name + ".must-not-exist.json")
        value = call(
            name + "-rejected",
            [
                "plan",
                "--backend",
                "espressif-qemu-xtensa",
                "--profile",
                "esp32s3-idf-flash-v1",
                "--runtime-lock",
                selected_lock,
                "--runtime-sha256",
                sha(selected_lock.read_bytes()),
                "--firmware",
                selected_image,
                "--firmware-sha256",
                sha(selected_image.read_bytes()),
                *extra,
                "--output",
                target,
            ],
            1,
        )
        assert value["ok"] is False and not target.exists()

    drift = output / "drift.bin"
    shutil.copyfile(image, drift)
    plan = output / "drift.plan.json"
    call(
        "drift-plan",
        [
            "plan",
            *common,
            "--firmware",
            drift,
            "--firmware-sha256",
            sha(drift.read_bytes()),
            *extra,
            "--output",
            plan,
        ],
    )
    drift.write_bytes(b"changed after planning")
    target = output / "drift.must-not-start"
    value = call(
        "drift-run-rejected",
        ["run", "--plan", plan, "--sha256", sha(plan.read_bytes()), "--output-dir", target],
        1,
    )
    assert value["error"]["code"] == "INPUT_CHANGED" and not target.exists()
    assert all(sha(path.read_bytes()) == expected for path, expected in images.items())
    result = {
        "ok": True,
        "calls": len(calls),
        "actual_emulator_runs": 6,
        "native": records,
        "known_model_limitations_retained": True,
        "input_images_unchanged": True,
        **FLAGS,
        "model_coverage_complete": False,
        "security_features_verified": False,
    }
    (output / "acceptance.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("executable", "runtime-lock", "fixtures", "output", "c3-lock", "c3-image"):
        parser.add_argument("--" + argument, type=Path, required=True)
    args = parser.parse_args()
    accept(
        args.executable.resolve(),
        args.runtime_lock.resolve(),
        args.fixtures.resolve(),
        args.output.resolve(),
        args.c3_lock.resolve(),
        args.c3_image.resolve(),
    )
