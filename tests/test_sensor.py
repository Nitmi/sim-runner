"""Synthetic protocol/integrity checks; native Renode acceptance is separate."""

import copy
import json

import pytest

from sim_runner import sensor
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY v1", build="BUILD abc", heartbeat_prefix="SAMPLE", minimum=16)


def transcript(fault="none"):
    profile = sensor.SensorProfile(fault)
    uart = b"READY v1\r\nBUILD abc\r\n"
    for sequence in range(1, 17):
        value = profile.SCENARIO["phases"][(sequence - 1) // 4]["applied_celsius"]
        uart += (
            f"SAMPLE {sequence:08X} R={value & 255:02X} C={value & 0xFFFFFFFF:08X} "
            f"T={1250 * sequence:08X} V=00000018 E=00000000\r\n"
        ).encode()
    stdout = b"[INFO] path \xff\n"
    for index, phase in enumerate(profile.SCENARIO["phases"]):
        stdout += (
            f"SIM_SENSOR_OBS_{index}\r\n{phase['applied_celsius']}\r\n72\r\n"
            f"{phase['observe_at_ms'] * 250}\r\n"
        ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\n"


@pytest.mark.parametrize("fault", sensor.FAULTS)
def test_nominal_result_and_injected_effect_are_independent(fault):
    profile = sensor.SensorProfile(fault)
    uart, stdout = transcript(fault)
    if fault == "none":
        result = profile.evaluate(uart, ASSERTIONS, stdout)
        assert result["nominal_regression_passed"] is True
    else:
        with pytest.raises(AssertionFailure) as raised:
            profile.evaluate(uart, ASSERTIONS, stdout)
        result = raised.value.evidence
        assert raised.value.code == "ASSERTION_FAILED"
        assert result["nominal_regression_passed"] is False
    assert result["fault"]["effect_observed"] is (None if fault == "none" else True)
    assert len(result["samples"]) == 16
    assert result["model_coverage_complete"] is result["sensor_accuracy_verified"] is False
    for observation in result["model_observations"]:
        raw = stdout[observation["stdout_offset"] :][: observation["stdout_bytes"]]
        assert sha(raw) == observation["stdout_sha256"]
        assert raw.startswith(f"SIM_SENSOR_OBS_{observation['phase']}\r\n".encode())


@pytest.mark.parametrize("fault", sensor.FAULTS[1:])
def test_selected_fault_is_not_proof_it_happened(fault):
    profile = sensor.SensorProfile(fault)
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure) as raised:
        profile.evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is False
    assert raised.value.evidence["nominal_regression_passed"] is True
    uart, _ = transcript(fault)
    with pytest.raises(AssertionFailure) as raised:
        profile.evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize(
    "change",
    [
        "unsigned",
        "raw",
        "stale",
        "irq",
        "clock",
        "bus-error",
        "sequence",
        "build",
        "partial",
        "non-ascii",
        "extra",
    ],
)
def test_guest_failure_cannot_pass(change):
    uart, stdout = transcript()
    if change == "unsigned":
        uart = uart.replace(b"C=FFFFFFF6", b"C=000000F6")
    if change == "raw":
        uart = uart.replace(b"R=F6", b"R=19")
    if change == "stale":
        uart = uart.replace(b"R=F6 C=FFFFFFF6", b"R=19 C=00000019")
    if change == "irq":
        uart = uart.replace(b"V=00000018", b"V=00000000")
    if change == "clock":
        uart = uart.replace(b"T=000004E2", b"T=00000400", 1)
    if change == "bus-error":
        uart = uart.replace(b"E=00000000", b"E=00000001", 1)
    if change == "sequence":
        uart = uart.replace(b"SAMPLE 00000002", b"SAMPLE 00000001", 1)
    if change == "build":
        uart = uart.replace(b"BUILD abc", b"BUILD other")
    if change == "partial":
        uart = uart[:-2]
    if change == "non-ascii":
        uart += b"\xff\n"
    if change == "extra":
        uart += uart.splitlines(keepends=True)[-1] * 1000
    with pytest.raises(AssertionFailure) as raised:
        sensor.SensorProfile("none").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["nominal_regression_passed"] is False
    assert len(raised.value.evidence["samples"]) <= 16
    assert len(json.dumps(raised.value.evidence)) < 16384


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "order",
        "overflow",
        "partial",
        "non-ascii",
        "temperature-range",
        "address-range",
    ],
)
def test_incomplete_or_invalid_model_snapshot_keeps_effect_unknown(change):
    uart, stdout = transcript("temperature-offset")
    if change == "missing":
        stdout = stdout[: stdout.index(b"SIM_SENSOR_OBS_3")]
    if change == "duplicate":
        stdout += b"SIM_SENSOR_OBS_0\n25\n72\n5000\n"
    if change == "order":
        stdout = stdout.replace(b"OBS_1", b"OBS_9")
    if change == "overflow":
        stdout = stdout.replace(b"5000\r\n", b"4294967296\r\n", 1)
    if change == "partial":
        stdout = stdout[: stdout.index(b"20000\r\n") + 5]
    if change == "non-ascii":
        stdout = stdout.replace(b"25\r\n", b"\xff25\r\n", 1)
    if change == "temperature-range":
        stdout = stdout.replace(b"25\r\n", b"128\r\n", 1)
    if change == "address-range":
        stdout = stdout.replace(b"72\r\n", b"128\r\n", 1)
    with pytest.raises(AssertionFailure) as raised:
        sensor.SensorProfile("temperature-offset").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is None


@pytest.mark.parametrize("change", ["temperature", "address", "counter"])
def test_normal_uart_does_not_replace_model_snapshot(change):
    uart, stdout = transcript()
    stdout = stdout.replace(
        b"25\r\n72\r\n5000",
        {
            "temperature": b"26\r\n72\r\n5000",
            "address": b"25\r\n71\r\n5000",
            "counter": b"25\r\n72\r\n0",
        }[change],
        1,
    )
    with pytest.raises(AssertionFailure) as raised:
        sensor.SensorProfile("none").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["model_nominal"] is False


@pytest.mark.parametrize("fault", sensor.FAULTS)
def test_plan_is_offline_and_binds_full_schedule(inputs, tmp_path, monkeypatch, fault):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline spawned"))
    profile = sensor.SensorProfile(fault)
    plan = profile.make_plan(**inputs)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert sensor.load_plan(path, sha(path.read_bytes())) == plan
    for change in ("time", "phase", "script", "fault"):
        changed = copy.deepcopy(plan)
        if change == "time":
            changed["virtual_ms"] = 83
        if change == "phase":
            changed["scenario"]["phases"][1]["applied_celsius"] += 1
        if change == "script":
            changed["script_sha256"] = "a" * 64
        if change == "fault":
            changed["fault_scenario"] = "other"
        path.write_text(json.dumps(changed))
        with pytest.raises(SimError):
            sensor.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("fault", [None, True, [], "other", "temperature-stuck\nquit"])
def test_only_fixed_fault_enum_is_accepted(fault):
    with pytest.raises(SimError):
        sensor.SensorProfile(fault)


@pytest.mark.parametrize("fault", sensor.FAULTS)
def test_completed_report_and_failed_analysis_are_recomputed(inputs, tmp_path, monkeypatch, fault):
    inputs["heartbeat"] = "SAMPLE"
    profile = sensor.SensorProfile(fault)
    path = tmp_path / "plan.json"
    write_json(path, profile.make_plan(**inputs))

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
    report = sensor.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    checked = sensor.inspect_report(saved, sha(saved.read_bytes()))
    assert checked["report_status"] == report["status"]
    assert checked["executables_started"] is False
    for mutation in ("effect", "sample", "span", "counter", "verdict", "missing", "scenario"):
        changed = copy.deepcopy(report)
        if mutation == "effect":
            changed["assertions"]["fault"]["effect_observed"] = "yes"
        if mutation == "sample":
            changed["assertions"]["samples"][4]["celsius"] += 1
        if mutation == "span":
            changed["assertions"]["model_observations"][0]["stdout_offset"] += 1
        if mutation == "counter":
            changed["assertions"]["model_observations"][1]["timer_counter"] += 1
        if mutation == "verdict":
            changed.update(ok=True, status="passed", errors=[])
            if fault == "none":
                changed["assertions"]["nominal_regression_passed"] = False
        if mutation == "missing":
            changed["assertions"] = None
        if mutation == "scenario":
            changed["fault_scenario"] = "other"
        saved.write_text(json.dumps(changed))
        with pytest.raises((SimError, TypeError)):
            sensor.inspect_report(saved, sha(saved.read_bytes()))

    # Updating container hashes must not hide raw model data contradicting the summary.
    changed = copy.deepcopy(report)
    stdout = directory / "backend.stdout.bin"
    stdout.write_bytes(stdout.read_bytes().replace(b"72\r\n", b"71\r\n", 1))
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(stdout.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        sensor.inspect_report(saved, sha(saved.read_bytes()))


def test_cli_routes_sensor_and_rejects_cross_profile_faults(inputs, tmp_path, capsys):
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
    for index, options in enumerate(
        (
            ["--profile", sensor.PROFILE],
            ["--profile", sensor.PROFILE, "--fault", "timer-stop"],
            ["--profile", "nrf52840-gpio-timer-fault-v1", "--fault", "temperature-stuck"],
            ["--profile", sensor.PROFILE, "--fault", "none", "--backend", "espressif-qemu"],
            ["--profile", sensor.PROFILE, "--fault", "none", "--min-heartbeats", "15"],
        )
    ):
        output = tmp_path / f"bad-{index}.json"
        assert main([*common, *options, "--output", str(output)]) == 1
        assert not output.exists()
        capsys.readouterr()
    output = tmp_path / "good.json"
    assert (
        main([*common, "--profile", sensor.PROFILE, "--fault", "none", "--output", str(output)])
        == 0
    )
    capsys.readouterr()
    assert main(["inspect-plan", "--plan", str(output), "--sha256", sha(output.read_bytes())]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == sensor.PROFILE
