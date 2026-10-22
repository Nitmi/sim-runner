"""Synthetic runtime/host protocol; actual official WASM runs are accepted separately."""

import copy
import json
import shutil
import subprocess

import pytest
from test_qemu_flash import merged, transcript

from sim_runner import esp_wasm as wasm
from sim_runner.cli import main
from sim_runner.common import SimError, sha, write_json


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    spec = copy.deepcopy(wasm.model())
    for name in spec["files"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-not-executable-" + name.encode())
        spec["files"][name] = sha(path.read_bytes())
    model = tmp_path / "model.json"
    write_json(model, spec)
    monkeypatch.setattr(wasm, "MODEL", model)
    node = tmp_path / "node.exe"
    node.write_bytes(b"synthetic Node - never execute")
    lock = tmp_path / "lock.json"
    write_json(
        lock,
        {
            "schema_version": wasm.RUNTIME_SCHEMA,
            "backend": wasm.BACKEND,
            "version": spec["version"],
            "archive_sha256": spec["archive_sha256"],
            "root": str(root),
            "files": spec["files"],
            "node": {
                "path": str(node),
                "sha256": sha(node.read_bytes()),
                "version": spec["node_version"],
            },
        },
    )
    firmware = tmp_path / "flash.bin"
    firmware.write_bytes(merged())
    return {
        "lock": lock,
        "lock_sha": sha(lock.read_bytes()),
        "image": firmware,
        "image_sha": sha(firmware.read_bytes()),
        "ready": "READY",
        "build": "BUILD abc",
        "heartbeat": "HEARTBEAT",
    }


def summary(uart, *, budget=False):
    return {
        "schema_version": "embedded-sim-runner.esp-wasm-host.v1",
        "node_version": "24.15.0",
        "chip": "esp32c3",
        "harts": 1,
        "xtensa": False,
        "batches": 5000 if budget else 100,
        "requested_instruction_upper_bound": 50000000 if budget else 1000000,
        "cycle_counter": "17766314",
        "cycles_per_us_claim": 160,
        "stop_reason": "instruction_budget" if budget else "guest_restart_requested",
        "uart_utf8_bytes": len(uart),
        "uart_representation": "ascii_subset_of_wasm_strings_encoded_utf8",
        "restart_performed": False,
        "physical_bridges": [],
    }


def clean_process():
    return {
        "pid": 123,
        "execution_resumed": True,
        "exit_code": 0,
        "error": None,
        "error_detail": None,
        "forced_termination": False,
        "host_elapsed_seconds": 0.1,
        "cleanup": {
            "process_exited": True,
            "job_active_processes": 0,
            "capture_threads_stopped": True,
        },
    }


def test_offline_inspection_never_starts_a_process(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("offline process launch"))
    plan = tmp_path / "plan.json"
    write_json(plan, wasm.make_plan(**inputs))
    assert (
        main(
            [
                "doctor",
                "--backend",
                wasm.BACKEND,
                "--runtime-lock",
                str(inputs["lock"]),
                "--runtime-sha256",
                inputs["lock_sha"],
            ]
        )
        == 0
    )
    assert main(["inspect-plan", "--plan", str(plan), "--sha256", sha(plan.read_bytes())]) == 0


@pytest.mark.parametrize("change", ["drift", "extra", "node", "version", "archive", "fields"])
def test_changed_runtime_or_node_fails_before_launch(inputs, change):
    lock = json.loads(inputs["lock"].read_bytes())
    from pathlib import Path

    root = Path(lock["root"])
    if change == "drift":
        (root / "www/pkg/esp_emu.js").write_bytes(b"drift")
    elif change == "extra":
        (root / "extra.js").write_bytes(b"never run")
    elif change == "node":
        Path(lock["node"]["path"]).write_bytes(b"drift")
    elif change == "version":
        lock["node"]["version"] = "25.0.0"
    elif change == "archive":
        lock["archive_sha256"] = "0" * 64
    else:
        lock["argv"] = ["arbitrary"]
    inputs["lock"].write_text(json.dumps(lock))
    with pytest.raises(SimError):
        wasm.runtime(inputs["lock"], sha(inputs["lock"].read_bytes()))


@pytest.mark.parametrize(
    "change",
    [
        {"instruction_budget": True},
        {"instruction_budget": 1},
        {"instruction_budget": 10001},
        {"memory_mib": 512},
    ],
)
def test_invalid_budgets_rejected(inputs, change):
    with pytest.raises(SimError):
        wasm.make_plan(**inputs, **change)


def test_plan_types_and_untyped_commands_rejected(inputs, tmp_path):
    doc = wasm.make_plan(**inputs)
    for index, change in enumerate(
        (
            {"executables_started": 0},
            {"retries": True},
            {"extra_args": ["--allow-child-process"]},
            {"physical_bridges": ["COM11"]},
        )
    ):
        plan = tmp_path / f"bad-{index}.json"
        write_json(plan, doc | change)
        with pytest.raises(SimError):
            wasm.load_plan(plan, sha(plan.read_bytes()))


def test_instruction_budget_does_not_apply_to_other_backends():
    assert (
        main(
            [
                "plan",
                "--runtime-lock",
                "missing",
                "--runtime-sha256",
                "0" * 64,
                "--firmware",
                "missing",
                "--firmware-sha256",
                "0" * 64,
                "--ready-line",
                "R",
                "--build-line",
                "B",
                "--heartbeat-prefix",
                "H",
                "--instruction-budget",
                "10000",
                "--output",
                "never-created.json",
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "fault,code",
    [
        ("normal", None),
        ("build", "ASSERTION_FAILED"),
        ("panic", "ASSERTION_FAILED"),
        ("budget", "INSTRUCTION_BUDGET_EXCEEDED"),
        ("timeout", "TIMEOUT"),
        ("metadata", "EVIDENCE_INVALID"),
        ("encoding", "EVIDENCE_INVALID"),
        ("host", "BACKEND_FAILED"),
        ("cleanup", "CLEANUP_FAILED"),
        ("input", "INPUT_CHANGED"),
    ],
)
def test_native_outcomes_and_movable_saved_evidence(inputs, tmp_path, monkeypatch, fault, code):
    plan = tmp_path / "plan.json"
    write_json(plan, wasm.make_plan(**inputs))

    def execute(argv, directory, timeout, limit, memory, *, node_environment):
        assert node_environment and "--permission" in argv and "--allow-child-process" not in argv
        assert (
            "--allow-fs-read=" + str(directory) in argv
            and "--allow-fs-write=" + str(directory) in argv
        )
        assert "--no-addons" in argv and argv[-1].endswith("runner.mjs")
        assert (directory / "flash-input.bin").read_bytes() == inputs["image"].read_bytes()
        uart = transcript()
        if fault == "build":
            uart = uart.replace(b"BUILD abc", b"BUILD old")
        if fault == "panic":
            uart += b"Guru Meditation panic\n"
        if fault == "encoding":
            uart += "bad \ufffd\n".encode()
        doc = summary(uart, budget=fault == "budget")
        if fault == "metadata":
            doc["chip"] = "esp32s3"
        (directory / "uart.bin").write_bytes(uart)
        (directory / "backend.stdout.bin").write_text(json.dumps(doc))
        (directory / "backend.stderr.bin").write_bytes(b"")
        process = clean_process()
        if fault == "timeout":
            process.update(error="TIMEOUT", forced_termination=True, exit_code=-1)
        if fault == "host":
            process["exit_code"] = 1
        if fault == "cleanup":
            process["cleanup"]["job_active_processes"] = 1
        if fault == "input":
            inputs["image"].write_bytes(b"drift")
        return process

    monkeypatch.setattr(wasm, "execute", execute)
    run = tmp_path / "run"
    result = wasm.run(plan, sha(plan.read_bytes()), run)
    assert result["ok"] is (code is None)
    assert [e["code"] for e in result["errors"]] == ([] if code is None else [code])
    assert result["status"] == ("passed_with_model_limits" if code is None else "failed")
    assert result["raw_uart_bytes_verified"] is False and result["model_coverage_complete"] is False
    moved = tmp_path / "moved evidence"
    shutil.copytree(run, moved)
    monkeypatch.setattr(
        wasm, "runtime", lambda *a, **k: pytest.fail("saved inspector read live runtime")
    )
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("saved inspector started process")
    )
    report = moved / "report.json"
    inspected = wasm.inspect_report(report, sha(report.read_bytes()))
    assert inspected["ok"] and inspected["executables_started"] is False
    assert (
        inspected["recorded_status"] == result["status"]
        and inspected["model_limits"] == wasm.MODEL_LIMITS
    )
    for index, change in enumerate(
        (
            {"raw_uart_bytes_verified": True},
            {"virtual_time": wasm.VIRTUAL_TIME | {"budget_enforced": True}},
            {"ok": not result["ok"]},
            {"model_limits": []},
            {
                "runtime_observation": (result["runtime_observation"] or {})
                | {"cycles_per_us_claim": 999}
            },
        )
    ):
        altered = moved / f"altered-{index}.json"
        write_json(altered, result | change)
        with pytest.raises(SimError):
            wasm.inspect_report(altered, sha(altered.read_bytes()))


def test_permission_path_cannot_widen_with_wildcards():
    for path in ("D:/new*", "D:/new,other", "D:/new?"):
        with pytest.raises(SimError):
            wasm.command("node.exe", path)
