from __future__ import annotations

import copy
import json

import pytest

from sim_runner import uarte_dma as dma
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=8)


def transcript(fault="none"):
    # Independent literal little-endian byte/guard oracle, not profile helpers.
    words = (
        0x23FF8001,
        0x557F00F0,
        0x33CC11AA,
        0x80402010,
        0xEFBEADDE,
        0x10080402,
        0x98BADCFE,
        0x7E81FF00,
    )
    uart, stdout = b"READY\r\nBUILD\r\n", b""
    for i, word in enumerate(words):
        short = i >= 4 and fault == "rx-short-buffer"
        shifted = i >= 4 and fault == "rx-pointer-shift"
        count = 3 if short else 4
        high = 0x2468AC00 | (word >> 24) if shifted else 0x2468ACE0
        if short:
            word = 0xAA000000 | (word & 0xFFFFFF)
        if shifted:
            word = ((word << 8) & 0xFFFFFF00) | 0xAA
        if i == 4 and fault != "none":
            stdout += b"SIM_UARTE_FAULT\r\n"
        for phase in ("PRE", "POST"):
            post = phase == "POST"
            values = [
                8,
                0x20001005 if shifted else 0x20001004,
                count,
                count if post else 0,
                int(post),
                int(post),
                1,
                0,
                0,
                0x13579BDF,
                word if post else 0xAAAAAAAA,
                high if post else 0x2468ACE0,
            ]
            stdout += (
                f"SIM_UARTE_OBS_{i}_{phase}\r\n" + "\r\n".join(map(str, values)) + "\r\n"
            ).encode()
        uart += (
            f"SAMPLE {i + 1:08X} WORD={word:08X} N={count:08X} LO=13579BDF HI={high:08X}\r\n"
        ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\r\n"


@pytest.mark.parametrize("fault", dma.FAULTS)
def test_independent_nominal_and_descriptor_faults(fault):
    uart, stdout = transcript(fault)
    profile = dma.UARTEDMAProfile(fault)
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
    assert len(result["model_observations"]) == 16 and len(result["samples"]) == 8
    for key in (
        "physical_timing_verified",
        "dma_hardware_verified",
        "event_time_alignment_verified",
        "uart_electrical_behavior_verified",
    ):
        assert result[key] is False


@pytest.mark.parametrize("fault", dma.FAULTS[1:])
def test_marker_without_descriptor_and_guest_effect_cannot_pass(fault):
    uart, stdout = transcript()
    stdout = stdout.replace(b"SIM_UARTE_OBS_4_PRE", b"SIM_UARTE_FAULT\r\nSIM_UARTE_OBS_4_PRE")
    with pytest.raises(AssertionFailure) as caught:
        dma.UARTEDMAProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert caught.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize("field", range(12))
def test_each_independent_model_field_is_required(field):
    uart, stdout = transcript()
    lines = stdout.splitlines(keepends=True)
    lines[1 + field] = str(int(lines[1 + field]) ^ 1).encode() + b"\r\n"
    with pytest.raises(AssertionFailure):
        dma.UARTEDMAProfile("none").evaluate(uart, ASSERTIONS, b"".join(lines))


@pytest.mark.parametrize(
    "mutation",
    [
        "build",
        "sequence",
        "word",
        "amount",
        "guard",
        "partial",
        "encoding",
        "missing",
        "duplicate",
        "order",
        "overflow",
        "info",
        "too_many_info",
        "partial_marker",
    ],
)
def test_bad_protocol_and_observation_inventory_rejected(mutation):
    uart, stdout = transcript()
    if mutation == "build":
        uart = uart.replace(b"BUILD", b"WRONG")
    if mutation == "sequence":
        uart = uart.replace(b"SAMPLE 00000001", b"SAMPLE 00000002")
    if mutation == "word":
        uart = uart.replace(b"23FF8001", b"0180FF23", 1)
    if mutation == "amount":
        uart = uart.replace(b"N=00000004", b"N=00000003", 1)
    if mutation == "guard":
        uart = uart.replace(b"HI=2468ACE0", b"HI=2468ACE1", 1)
    if mutation == "partial":
        uart = uart[:-2]
    if mutation == "encoding":
        uart += b"\xff\n"
    if mutation == "missing":
        stdout = stdout.replace(b"OBS_0_PRE", b"MISSING", 1)
    if mutation == "duplicate":
        stdout += stdout[: stdout.index(b"SIM_UARTE_OBS_0_POST")]
    if mutation == "order":
        stdout = stdout.replace(b"OBS_0_PRE", b"OBS_1_PRE", 1)
    if mutation == "overflow":
        stdout = stdout.replace(b"2863311530\r\n", b"4294967296\r\n", 1)
    if mutation == "info":
        stdout = stdout.replace(b"OBS_0_PRE\r\n", b"OBS_0_PRE\r\n[INFO] unexpected\n")
    if mutation == "too_many_info":
        stdout = stdout.replace(
            b"OBS_0_PRE\r\n",
            b"OBS_0_PRE\r\n" + b"[12:00:00.0000] [INFO] nrf52840-uarte-dma: Machine paused.\n" * 5,
        )
    if mutation == "partial_marker":
        stdout += b"SIM_UARTE_OBS_"
    with pytest.raises(AssertionFailure) as caught:
        dma.UARTEDMAProfile("none").evaluate(uart, ASSERTIONS, stdout)
    if mutation == "build":
        assert len(caught.value.evidence["model_observations"]) == 16
        assert caught.value.evidence["samples"] == []
        assert caught.value.evidence["fault"]["effect_observed"] is None


@pytest.mark.parametrize("position", range(12))
def test_bounded_lifecycle_messages_bind_raw_spans(position):
    uart, stdout = transcript()
    offset = 0
    for _ in range(position + 1):
        offset = stdout.index(b"\n", offset) + 1
    stdout = (
        stdout[:offset]
        + b"[12:00:00.0000] [INFO] nrf52840-uarte-dma: Machine paused.\n"
        + stdout[offset:]
    )
    result = dma.UARTEDMAProfile("none").evaluate(uart, ASSERTIONS, stdout)
    o = result["model_observations"][0]
    assert sha(stdout[o["stdout_offset"] :][: o["stdout_bytes"]]) == o["stdout_sha256"]


def test_plan_cli_is_fixed_offline_and_requires_explicit_fault(
    inputs, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process"))
    common = [
        "plan",
        "--profile",
        dma.PROFILE,
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
            ["--fault", "ppi-channel-disable"],
            ["--fault", "none", "--min-heartbeats", "7"],
            ["--fault", "none", "--virtual-ms", "82"],
            ["--fault", "none", "--backend", "espressif-qemu"],
        )
    ):
        output = tmp_path / f"bad-{i}.json"
        assert main([*common, *extra, "--output", str(output)]) == 1 and not output.exists()
        capsys.readouterr()
    good = tmp_path / "good.json"
    assert main([*common, "--fault", "none", "--output", str(good)]) == 0
    capsys.readouterr()
    assert main(["inspect-plan", "--plan", str(good), "--sha256", sha(good.read_bytes())]) == 0
    original = json.loads(good.read_bytes())
    for field, value in (("buffer_address", 0x20001005), ("service_budget_ms", 999)):
        changed = copy.deepcopy(original)
        changed["scenario"][field] = value
        path = tmp_path / (field + ".json")
        write_json(path, changed)
        with pytest.raises(SimError):
            dma.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("fault", dma.FAULTS)
def test_saved_results_recompute_failed_effects_without_original_inputs(
    inputs, tmp_path, monkeypatch, fault
):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    path = tmp_path / "plan.json"
    write_json(path, dma.UARTEDMAProfile(fault).make_plan(**inputs))

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
    report = dma.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    inputs["image"].unlink()
    inputs["lock"].unlink()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process"))
    assert dma.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    for mutation in ("sample", "effect", "span", "verdict", "missing", "cleanup"):
        changed = copy.deepcopy(report)
        if mutation == "sample":
            changed["assertions"]["samples"][0]["word"] ^= 1
        if mutation == "effect":
            changed["assertions"]["fault"]["effect_observed"] = "yes"
        if mutation == "span":
            changed["assertions"]["model_observations"][0]["stdout_offset"] += 1
        if mutation == "verdict":
            changed.update(ok=not report["ok"], status="passed", errors=[])
        if mutation == "missing":
            changed["assertions"] = None
        if mutation == "cleanup":
            changed["process"]["cleanup"]["job_active_processes"] = 1
        saved.write_text(json.dumps(changed))
        with pytest.raises(SimError):
            dma.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"2863311530\r\n", b"2863311531\r\n", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        dma.inspect_report(saved, sha(saved.read_bytes()))
