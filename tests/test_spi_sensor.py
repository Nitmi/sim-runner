from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from sim_runner import spi_sensor as spi
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=16)


def transcript(fault="none"):
    # Independent literal protocol oracle, including cached-low bytes at transitions.
    words = {
        "none": [0x0C8F] * 4 + [0xFAF7] * 4 + [0x2AC7] * 4 + [0x000F] * 4,
        "temperature-stuck": [0x0C8F] * 16,
        "frame-slip": [0x0C8F] * 4
        + [0xF7FA] * 4
        + [0xF72A]
        + [0xC72A] * 3
        + [0xC700]
        + [0x0F00] * 3,
    }[fault]
    uart = b"READY\r\nBUILD\r\n"
    for index, word in enumerate(words, 1):
        q4 = (word if word < 32768 else word - 65536) >> 3
        uart += (
            f"SAMPLE {index:08X} R={word:04X} Q={q4 & 0xFFFFFFFF:08X}"
            f" T={index * 1250:08X} V=00000018 E=00000000\r\n"
        ).encode()
    stdout = b""
    for index, t in enumerate((401, -162, 1368, 1)):
        if index == 1 and fault == "frame-slip":
            stdout += b"SIM_SPI_SLIP\r\n250\r\n"
        if fault == "temperature-stuck":
            t = 401
        stdout += (
            f"SIM_SPI_OBS_{index}\r\n{Decimal(t) / 16}\r\n1\r\n{(index + 1) * 5000}\r\n"
        ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\n"


@pytest.mark.parametrize("fault", spi.FAULTS)
def test_protocol_uses_signed_fractional_values_and_independent_fault_verdict(fault):
    uart, stdout = transcript(fault)
    profile = spi.SPIProfile(fault)
    if fault == "none":
        e = profile.evaluate(uart, ASSERTIONS, stdout)
        assert [s["temperature_q4"] for s in e["samples"]][::4] == [401, -162, 1368, 1]
    else:
        with pytest.raises(AssertionFailure) as raised:
            profile.evaluate(uart, ASSERTIONS, stdout)
        e = raised.value.evidence
    assert e["nominal_regression_passed"] is (fault == "none")
    assert e["fault"]["effect_observed"] is (None if fault == "none" else True)
    assert [s["raw_word"] for s in e["samples"]] == spi.expected_words(fault)
    for item in [
        *e["model_observations"],
        *([e["frame_slip_observation"]] if fault == "frame-slip" else []),
    ]:
        assert sha(stdout[item["stdout_offset"] :][: item["stdout_bytes"]]) == item["stdout_sha256"]
    assert not e["physical_timing_verified"] and not e["spi_electrical_behavior_verified"]


@pytest.mark.parametrize("fault", spi.FAULTS[1:])
def test_fault_request_marker_alone_never_proves_effect(fault):
    uart, _ = transcript()
    _, stdout = transcript(fault)
    if fault == "temperature-stuck":
        _, stdout = transcript()
    with pytest.raises(AssertionFailure) as raised:
        spi.SPIProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is False
    assert raised.value.evidence["nominal_regression_passed"] is True


@pytest.mark.parametrize(
    "old,new",
    [
        (b"Q=FFFFFF5E", b"Q=00001F5E"),  # unsigned 13-bit value
        (b"Q=00000191", b"Q=00000190"),  # discarded fractional bit
        (b"R=FAF7", b"R=F7FA"),  # byte order
        (b"R=FAF7 Q=FFFFFF5E", b"R=0C8F Q=00000191"),  # stale sample
        (b"R=0C8F", b"R=0C88"),  # status bits
        (b"V=00000018", b"V=00000000"),
        (b"T=000004E2", b"T=00000000"),
        (b"E=00000000", b"E=00000001"),
        (b"SAMPLE 00000002", b"SAMPLE 00000001"),
        (b"BUILD", b"WRONG_BUILD"),
    ],
)
def test_guest_bugs_fail_even_with_correct_model(old, new):
    uart, stdout = transcript()
    assert old in uart
    with pytest.raises(AssertionFailure) as raised:
        spi.SPIProfile("none").evaluate(uart.replace(old, new), ASSERTIONS, stdout)
    assert not raised.value.evidence["nominal_regression_passed"]


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "order",
        "non-q4",
        "range",
        "overflow",
        "partial",
        "slip-byte",
        "slip-order",
    ],
)
def test_incomplete_or_invalid_model_observation_never_proves_fault(change):
    uart, stdout = transcript("frame-slip")
    if change == "missing":
        stdout = stdout[: stdout.index(b"SIM_SPI_OBS_3")]
    if change == "duplicate":
        stdout += b"SIM_SPI_OBS_0\n25.0625\n1\n5000\n"
    if change == "order":
        stdout = stdout.replace(b"OBS_1", b"OBS_9")
    if change == "non-q4":
        stdout = stdout.replace(b"25.0625", b"25.0624", 1)
    if change == "range":
        stdout = stdout.replace(b"25.0625", b"150.0625", 1)
    if change == "overflow":
        stdout = stdout.replace(b"5000\r\n", b"4294967296\r\n", 1)
    if change == "partial":
        stdout = stdout.replace(b"20000\r\n", b"20000", 1)
    if change == "slip-byte":
        stdout = stdout.replace(b"250\r\n", b"256\r\n", 1)
    if change == "slip-order":
        stdout = b"SIM_SPI_SLIP\n250\n" + stdout.replace(b"SIM_SPI_SLIP\r\n250\r\n", b"")
    with pytest.raises(AssertionFailure) as raised:
        spi.SPIProfile("frame-slip").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is None


@pytest.mark.parametrize("tail", [b"", b"\xff\n", b"\n" + b"EXTRA\n" * 1000])
def test_protocol_truncation_and_excess_are_bounded(tail):
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure) as raised:
        spi.SPIProfile("none").evaluate(uart[:-2] + tail, ASSERTIONS, stdout)
    assert len(raised.value.evidence["samples"]) <= 16
    assert len(json.dumps(raised.value.evidence)) < 16384


@pytest.mark.parametrize("fault", spi.FAULTS)
def test_plan_binds_model_fault_and_schedule_without_spawning(inputs, tmp_path, monkeypatch, fault):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline spawned"))
    plan = spi.SPIProfile(fault).make_plan(**inputs)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert spi.load_plan(path, sha(path.read_bytes())) == plan
    for field, value in (
        ("script_sha256", "a" * 64),
        ("virtual_ms", 83),
        ("profile", "nrf52840-i2c-sensor-v1"),
    ):
        changed = {**plan, field: value}
        path.write_text(json.dumps(changed))
        with pytest.raises(SimError):
            spi.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("fault", spi.FAULTS)
def test_saved_success_and_failure_are_recomputed_not_just_rehashed(
    inputs, tmp_path, monkeypatch, fault
):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    path = tmp_path / "plan.json"
    write_json(path, spi.SPIProfile(fault).make_plan(**inputs))

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
    report = spi.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    assert spi.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    for mutation in ("sample", "effect", "span", "verdict", "missing"):
        changed = copy.deepcopy(report)
        if mutation == "sample":
            changed["assertions"]["samples"][4]["temperature_q4"] += 1
        if mutation == "effect":
            changed["assertions"]["fault"]["effect_observed"] = "yes"
        if mutation == "span":
            changed["assertions"]["model_observations"][0]["stdout_offset"] += 1
        if mutation == "verdict":
            changed["assertions"]["nominal_regression_passed"] = not report["ok"]
        if mutation == "missing":
            changed["assertions"] = None
        saved.write_text(json.dumps(changed))
        with pytest.raises(SimError):
            spi.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"25.0625", b"25.1250", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        spi.inspect_report(saved, sha(saved.read_bytes()))


def test_cli_routes_spi_and_rejects_cross_profile_faults(inputs, tmp_path, capsys):
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
    for i, options in enumerate(
        (
            ["--profile", spi.PROFILE],
            ["--profile", spi.PROFILE, "--fault", "temperature-offset"],
            ["--profile", "nrf52840-i2c-sensor-v1", "--fault", "frame-slip"],
            ["--profile", spi.PROFILE, "--fault", "none", "--backend", "espressif-qemu"],
            ["--profile", spi.PROFILE, "--fault", "none", "--min-heartbeats", "15"],
        )
    ):
        path = tmp_path / f"bad-{i}.json"
        assert main([*common, *options, "--output", str(path)]) == 1
        assert not path.exists()
        capsys.readouterr()
    path = tmp_path / "good.json"
    assert main([*common, "--profile", spi.PROFILE, "--fault", "none", "--output", str(path)]) == 0
    capsys.readouterr()
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == spi.PROFILE
