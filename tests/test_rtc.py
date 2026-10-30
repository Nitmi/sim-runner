from __future__ import annotations

import copy
import json

import pytest

from sim_runner import rtc
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=10)


def transcript(fault="none"):
    # Literal independent 1024 Hz oracle, ten CC0 IRQs 16 ticks apart.
    uart = b"READY\r\nBUILD\r\n"
    for sequence in range(1, (10 if fault == "none" else 2) + 1):
        uart += (
            f"SAMPLE {sequence:08X} C={16 * sequence:08X} E=00000001 A=00000000"
            f" N={16 * (sequence + 1):08X} P=0000001F V=0000001B\r\n"
        ).encode()
    stdout = b""
    for i, counter in enumerate([40, 81, 122, 163]):
        if i == 1 and fault != "none":
            stdout += b"SIM_RTC_FAULT\r\n"
        if fault == "rtc-stop" and i:
            counter = 43
        compare = [48, 96, 128, 176][i] if fault == "none" else 48
        event = int(fault == "rtc-irq-mask" and i > 0)
        mask = 0 if fault == "rtc-irq-mask" and i else 65536
        stdout += (
            f"SIM_RTC_OBS_{i}\r\n{counter}\r\n31\r\n{compare}\r\n{event}\r\n{mask}\r\n"
        ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\n"


@pytest.mark.parametrize("fault", rtc.FAULTS)
def test_nominal_verdict_and_observed_fault_are_independent(fault):
    uart, stdout = transcript(fault)
    if fault == "none":
        evidence = rtc.RTCProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    else:
        with pytest.raises(AssertionFailure) as raised:
            rtc.RTCProfile(fault).evaluate(uart, ASSERTIONS, stdout)
        evidence = raised.value.evidence
    assert evidence["nominal_regression_passed"] is (fault == "none")
    assert evidence["fault"]["effect_observed"] is (None if fault == "none" else True)
    assert len(evidence["samples"]) == (10 if fault == "none" else 2)
    for observation in evidence["model_observations"]:
        raw = stdout[observation["stdout_offset"] :][: observation["stdout_bytes"]]
        assert sha(raw) == observation["stdout_sha256"]
    assert evidence["physical_timing_verified"] is False
    assert evidence["low_power_behavior_verified"] is False


@pytest.mark.parametrize("fault", rtc.FAULTS[1:])
def test_fault_marker_is_not_proof_of_effect(fault):
    uart, stdout = transcript()
    stdout = stdout.replace(b"SIM_RTC_OBS_1", b"SIM_RTC_FAULT\nSIM_RTC_OBS_1")
    with pytest.raises(AssertionFailure) as raised:
        rtc.RTCProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["nominal_regression_passed"] is True
    assert raised.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize(
    "old,new",
    [
        (b"E=00000001", b"E=00000000"),
        (b"A=00000000", b"A=00000001"),
        (b"N=00000020", b"N=00000010"),
        (b"P=0000001F", b"P=0000000F"),
        (b"V=0000001B", b"V=00000018"),
        (b"C=00000010", b"C=00000008"),
        (b"SAMPLE 00000002", b"SAMPLE 00000001"),
        (b"BUILD", b"WRONG_BUILD"),
    ],
)
def test_firmware_faults_cannot_pass_correct_model(old, new):
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure):
        rtc.RTCProfile("none").evaluate(uart.replace(old, new), ASSERTIONS, stdout)


@pytest.mark.parametrize("fault", rtc.FAULTS[1:])
@pytest.mark.parametrize("mutation", ["counter", "event", "mask", "compare", "guest"])
def test_effect_needs_model_and_guest_evidence(fault, mutation):
    uart, stdout = transcript(fault)
    if mutation == "counter":
        stdout = (
            stdout.replace(b"43\r\n", b"44\r\n", 1)
            if fault == "rtc-stop"
            else stdout.replace(b"81\r\n", b"43\r\n")
        )
    if mutation == "event":
        old, new = (b"0\r\n65536", b"1\r\n65536") if fault == "rtc-stop" else (b"1\r\n0", b"0\r\n0")
        stdout = stdout.replace(old, new)
    if mutation == "mask":
        stdout = (
            stdout.replace(b"65536\r\n", b"0\r\n")
            if fault == "rtc-stop"
            else stdout.replace(b"1\r\n0\r\n", b"1\r\n65536\r\n")
        )
    if mutation == "compare":
        stdout = stdout.replace(b"48\r\n", b"64\r\n")
    if mutation == "guest":
        uart = uart.replace(b"C=00000020", b"C=00000010")
    with pytest.raises(AssertionFailure) as raised:
        rtc.RTCProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is not True


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "order", "partial", "range", "large", "fault-order"]
)
def test_invalid_snapshot_inventory_stays_unknown(mutation):
    uart, stdout = transcript("rtc-stop")
    if mutation == "missing":
        stdout = stdout[: stdout.index(b"SIM_RTC_OBS_3")]
    if mutation == "duplicate":
        stdout += stdout[: stdout.index(b"SIM_RTC_FAULT")]
    if mutation == "order":
        stdout = stdout.replace(b"OBS_2", b"OBS_4")
    if mutation == "partial":
        stdout = stdout.replace(b"43\r\n31", b"43\r31")
    if mutation == "range":
        stdout = stdout.replace(b"43\r\n", b"16777216\r\n")
    if mutation == "large":
        stdout = stdout.replace(b"43\r\n", b"9" * 5000 + b"\r\n")
    if mutation == "fault-order":
        stdout = b"SIM_RTC_FAULT\n" + stdout.replace(b"SIM_RTC_FAULT\r\n", b"")
    with pytest.raises(AssertionFailure) as raised:
        rtc.RTCProfile("rtc-stop").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is None


@pytest.mark.parametrize("fault", rtc.FAULTS)
def test_offline_plan_requires_fixed_inputs(inputs, tmp_path, monkeypatch, fault):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline process"))
    profile = rtc.RTCProfile(fault)
    plan = profile.make_plan(**inputs)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert rtc.load_plan(path, sha(path.read_bytes())) == plan
    for key, value in (("virtual_ms", 163), ("script_sha256", "a" * 64), ("fault_scenario", "bad")):
        path.write_text(json.dumps({**plan, key: value}))
        with pytest.raises(SimError):
            rtc.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("fault", rtc.FAULTS)
def test_saved_passed_and_failed_evidence_recomputed(inputs, tmp_path, monkeypatch, fault):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    path = tmp_path / "plan.json"
    write_json(path, rtc.RTCProfile(fault).make_plan(**inputs))

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
    report = rtc.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    assert rtc.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    for mutation in ("sample", "effect", "span", "verdict", "missing", "cleanup"):
        changed = copy.deepcopy(report)
        if mutation == "sample":
            changed["assertions"]["samples"][0]["counter"] = 1
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
            rtc.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"31\r\n", b"30\r\n", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        rtc.inspect_report(saved, sha(saved.read_bytes()))


def test_cli_dispatch_and_cross_profile_rejection(inputs, tmp_path, capsys):
    common = [
        "plan",
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
            ["--fault", "watchdog-stop"],
            ["--fault", "none", "--min-heartbeats", "16"],
            ["--fault", "none", "--virtual-ms", "82"],
            ["--fault", "none", "--backend", "espressif-qemu"],
        )
    ):
        path = tmp_path / f"bad-{i}.json"
        assert main([*common, "--profile", rtc.PROFILE, *extra, "--output", str(path)]) == 1
        assert not path.exists()
        capsys.readouterr()
    path = tmp_path / "good.json"
    assert main([*common, "--profile", rtc.PROFILE, "--fault", "none", "--output", str(path)]) == 0
    capsys.readouterr()
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == rtc.PROFILE
