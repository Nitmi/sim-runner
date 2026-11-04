from __future__ import annotations

import copy
import json

import pytest

from sim_runner import ppi
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=8)


def transcript(fault="none"):
    # Independent literal oracle: main/fork/sentinel outputs after each trigger.
    states = [(1, 1, 0), (0, 0, 0)] * 4
    masks = [1] * 8
    forks = [0x40006004] * 8
    if fault == "ppi-channel-disable":
        states[4:] = [(0, 0, 0)] * 4
        masks[4:] = [0] * 4
    if fault == "ppi-fork-misroute":
        states[4:] = [(1, 0, 1), (0, 0, 0)] * 2
        forks[4:] = [0x40006008] * 4
    uart, stdout = b"READY\r\nBUILD\r\n", b""
    last, seq = (0, 0, 0), 0
    for i, state in enumerate(states):
        if i == 4 and fault != "none":
            stdout += b"SIM_PPI_FAULT\r\n"
        for phase, pins, event in (("PRE", last, 0), ("POST", state, 1)):
            a, b, sentinel = pins
            values = [
                event,
                4,
                1000,
                masks[i],
                0x40008140,
                0x40006000,
                forks[i],
                199427,
                205571,
                199939,
                a * 2048 + sentinel * 8192,
                b * 8,
                a * 2048 + sentinel * 8192,
                b * 8,
            ]
            stdout += (
                f"SIM_PPI_OBS_{i}_{phase}\r\n" + "\r\n".join(map(str, values)) + "\r\n"
            ).encode()
        if state != last:
            seq += 1
            a, b, sentinel = state
            uart += f"SAMPLE {seq:08X} A={a:08X} B={b:08X} S={sentinel:08X}\r\n".encode()
        last = state
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\r\n"


@pytest.mark.parametrize("fault", ppi.FAULTS)
def test_nominal_and_fault_are_distinct(fault):
    uart, stdout = transcript(fault)
    profile = ppi.PPIProfile(fault)
    if fault == "none":
        result = profile.evaluate(uart, ASSERTIONS, stdout)
        assert result["nominal_regression_passed"] is True
        assert result["fault"]["effect_observed"] is None
    else:
        with pytest.raises(AssertionFailure) as caught:
            profile.evaluate(uart, ASSERTIONS, stdout)
        result = caught.value.evidence
        assert result["nominal_regression_passed"] is False
        assert result["fault"]["effect_observed"] is True
    assert len(result["model_observations"]) == 16
    assert result["physical_timing_verified"] is False
    assert result["event_time_alignment_verified"] is False
    assert result["ppi_routing_verified"] is False


@pytest.mark.parametrize("fault", ppi.FAULTS[1:])
def test_fault_marker_alone_cannot_prove_effect(fault):
    uart, stdout = transcript()
    stdout = stdout.replace(b"SIM_PPI_OBS_4_PRE", b"SIM_PPI_FAULT\r\nSIM_PPI_OBS_4_PRE")
    with pytest.raises(AssertionFailure) as caught:
        ppi.PPIProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert caught.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "build",
        "sequence",
        "pin",
        "partial",
        "encoding",
        "extra",
        "missing",
        "duplicate",
        "order",
        "endpoint",
        "mask",
        "event",
        "overflow",
        "info",
        "too_many_info",
    ],
)
def test_contradictory_protocol_or_model_cannot_pass(mutation):
    uart, stdout = transcript()
    if mutation == "build":
        uart = uart.replace(b"BUILD", b"WRONG")
    if mutation == "sequence":
        uart = uart.replace(b"SAMPLE 00000001", b"SAMPLE 00000002")
    if mutation == "pin":
        uart = uart.replace(b"A=00000001", b"A=00000000", 1)
    if mutation == "partial":
        uart = uart[:-2]
    if mutation == "encoding":
        uart += b"\xff\n"
    if mutation == "extra":
        stdout += b"SIM_PPI_OBS_9_PRE\r\n"
    if mutation == "missing":
        stdout = stdout.replace(b"SIM_PPI_OBS_0_PRE", b"MISSING", 1)
    if mutation == "duplicate":
        stdout += stdout[: stdout.index(b"SIM_PPI_OBS_0_POST")]
    if mutation == "order":
        stdout = stdout.replace(b"OBS_0_PRE", b"OBS_1_PRE", 1)
    if mutation == "endpoint":
        stdout = stdout.replace(b"1073774912\r\n", b"1073774916\r\n", 1)
    if mutation == "mask":
        stdout = stdout.replace(b"1000\r\n1\r\n", b"1000\r\n0\r\n", 1)
    if mutation == "event":
        stdout = stdout.replace(b"OBS_0_POST\r\n1", b"OBS_0_POST\r\n0", 1)
    if mutation == "overflow":
        stdout = stdout.replace(b"1000\r\n", b"4294967296\r\n", 1)
    if mutation == "info":
        stdout = stdout.replace(b"OBS_0_PRE\r\n", b"OBS_0_PRE\r\n[INFO] unexpected\n")
    if mutation == "too_many_info":
        stdout = stdout.replace(
            b"OBS_0_PRE\r\n",
            b"OBS_0_PRE\r\n" + b"[12:00:00.0000] [INFO] nrf52840-ppi: Machine paused.\n" * 5,
        )
    with pytest.raises(AssertionFailure) as caught:
        ppi.PPIProfile("none").evaluate(uart, ASSERTIONS, stdout)
    if mutation == "build":
        assert len(caught.value.evidence["model_observations"]) == 16
        assert caught.value.evidence["samples"] == []


@pytest.mark.parametrize("position", range(14))
def test_bounded_lifecycle_preserves_raw_span(position):
    uart, stdout = transcript()
    start = stdout.index(b"\n") + 1
    for _ in range(position):
        start = stdout.index(b"\n", start) + 1
    stdout = (
        stdout[:start] + b"[12:00:00.0000] [INFO] nrf52840-ppi: Machine paused.\n" + stdout[start:]
    )
    result = ppi.PPIProfile("none").evaluate(uart, ASSERTIONS, stdout)
    o = result["model_observations"][0]
    assert sha(stdout[o["stdout_offset"] :][: o["stdout_bytes"]]) == o["stdout_sha256"]


def test_fixed_plan_cli_and_no_launch(inputs, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process"))
    common = [
        "plan",
        "--profile",
        ppi.PROFILE,
        "--runtime-lock",
        str(inputs["lock"]),
        "--runtime-sha256",
        inputs["lock_sha"],
        "--firmware",
        str(inputs["image"]),
        "--firmware-sha256",
        inputs["image_sha"],
        "--ready-line",
        "READY",
        "--build-line",
        "BUILD",
        "--heartbeat-prefix",
        "SAMPLE",
    ]
    for i, extra in enumerate(
        (
            [],
            ["--fault", "egu-irq-mask"],
            ["--fault", "none", "--min-heartbeats", "7"],
            ["--fault", "none", "--virtual-ms", "82"],
            ["--fault", "none", "--backend", "espressif-qemu"],
        )
    ):
        path = tmp_path / f"bad-{i}.json"
        assert main([*common, *extra, "--output", str(path)]) == 1 and not path.exists()
        capsys.readouterr()
    good = tmp_path / "good.json"
    assert main([*common, "--fault", "none", "--output", str(good)]) == 0
    capsys.readouterr()
    assert main(["inspect-plan", "--plan", str(good), "--sha256", sha(good.read_bytes())]) == 0
    plan = json.loads(good.read_bytes())
    assert plan["assertions"]["minimum"] == 8
    for mutation in ("endpoint", "budget", "coverage"):
        changed = copy.deepcopy(plan)
        if mutation == "endpoint":
            changed["scenario"]["task"] = "arbitrary"
        if mutation == "budget":
            changed["virtual_ms"] = 41
        if mutation == "coverage":
            changed["event_time_alignment_verified"] = True
        write_json(tmp_path / (mutation + ".json"), changed)
        path = tmp_path / (mutation + ".json")
        with pytest.raises(SimError):
            ppi.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("fault", ppi.FAULTS)
def test_saved_passed_failed_unknown_are_recomputed(inputs, tmp_path, monkeypatch, fault):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    path = tmp_path / "plan.json"
    write_json(path, ppi.PPIProfile(fault).make_plan(**inputs))

    def execute_double(argv, directory, *args):
        uart, stdout = transcript(fault)
        for name, data in (
            ("uart.bin", uart),
            ("backend.stdout.bin", stdout),
            ("backend.stderr.bin", b""),
        ):
            (directory / name).write_bytes(data)
        return dict(
            pid=1,
            exit_code=0,
            error=None,
            forced_termination=False,
            cleanup=dict(process_exited=True, job_active_processes=0, capture_threads_stopped=True),
        )

    monkeypatch.setattr("sim_runner.run.execute", execute_double)
    directory = tmp_path / "run"
    report = ppi.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    # Saved evidence is movable and does not execute or read the original inputs.
    inputs["image"].unlink()
    inputs["lock"].unlink()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process"))
    assert ppi.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    for mutation in ("sample", "effect", "span", "verdict", "missing", "cleanup"):
        changed = copy.deepcopy(report)
        if mutation == "sample":
            changed["assertions"]["samples"][0]["a"] ^= 1
        if mutation == "effect":
            changed["assertions"]["fault"]["effect_observed"] = "yes"
        if mutation == "span":
            changed["assertions"]["model_observations"][0]["stdout_offset"] += 1
        if mutation == "verdict":
            changed["ok"] = not report["ok"]
            changed["status"] = "passed" if changed["ok"] else "failed"
            changed["errors"] = [] if changed["ok"] else [dict(code="FAKE", message="fake")]
        if mutation == "missing":
            changed["assertions"] = None
        if mutation == "cleanup":
            changed["process"]["cleanup"]["job_active_processes"] = 1
        saved.write_text(json.dumps(changed))
        with pytest.raises(SimError):
            ppi.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"1000\r\n", b"1001\r\n", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        ppi.inspect_report(saved, sha(saved.read_bytes()))


def test_extra_numeric_field_rejected():
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure):
        ppi.PPIProfile("none").evaluate(uart, ASSERTIONS, stdout + b"123\r\n")
