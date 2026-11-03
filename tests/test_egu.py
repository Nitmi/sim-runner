from __future__ import annotations

import copy
import json

import pytest

from sim_runner import egu
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=9)


def transcript(fault="none"):
    # Literal independent register and IRQ oracle. Deliberately no profile helpers.
    delivered = {
        "none": [(0, 32777), (3, 32777), (15, 32777)] * 3,
        "egu-irq-mask": [
            (0, 32777),
            (3, 32777),
            (15, 32777),
            (0, 32777),
            (15, 32769),
            (0, 32769),
            (15, 32769),
        ],
        "egu-channel-misroute": [
            (0, 32777),
            (3, 32777),
            (15, 32777),
            (0, 32777),
            (15, 32777),
            (0, 32777),
            (3, 32777),
            (15, 32777),
        ],
    }[fault]
    nominal_pre = [(1, 0, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)] * 3
    pres, posts, masks = list(nominal_pre), [(0, 0, 0, 0)] * 9, [32777] * 9
    if fault == "egu-irq-mask":
        pres[4:] = [(0, 0, 1, 0), (0, 0, 1, 1), (1, 0, 1, 0), (0, 0, 1, 0), (0, 0, 1, 1)]
        posts[4:] = [(0, 0, 1, 0)] * 5
        masks[4:] = [32769] * 5
    if fault == "egu-channel-misroute":
        pres[4:] = [(0, 1, 0, 0), (0, 1, 0, 1), (1, 1, 0, 0), (0, 1, 1, 0), (0, 1, 0, 1)]
        posts[4:] = [(0, 1, 0, 0)] * 5
    uart = b"READY\r\nBUILD\r\n"
    for i, (channel, mask) in enumerate(delivered, 1):
        uart += (
            f"SAMPLE {i:08X} C={channel:08X} E=00000001 A=00000000 M={mask:08X} V=00000024\r\n"
        ).encode()
    stdout = b""
    for i in range(9):
        if i == 4 and fault != "none":
            stdout += b"SIM_EGU_FAULT\r\n"
        for phase, events in (("PRE", pres[i]), ("POST", posts[i])):
            stdout += (
                f"SIM_EGU_OBS_{i}_{phase}\r\n" + "\r\n".join(map(str, (*events, masks[i]))) + "\r\n"
            ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\n"


@pytest.mark.parametrize("fault", egu.FAULTS)
def test_nominal_verdict_and_observed_fault_are_independent(fault):
    uart, stdout = transcript(fault)
    if fault == "none":
        evidence = egu.EGUProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    else:
        with pytest.raises(AssertionFailure) as raised:
            egu.EGUProfile(fault).evaluate(uart, ASSERTIONS, stdout)
        evidence = raised.value.evidence
    assert evidence["nominal_regression_passed"] is (fault == "none")
    assert evidence["fault"]["effect_observed"] is (None if fault == "none" else True)
    assert (
        len(evidence["samples"]) == {"none": 9, "egu-irq-mask": 7, "egu-channel-misroute": 8}[fault]
    )
    for observation in evidence["model_observations"]:
        raw = stdout[observation["stdout_offset"] :][: observation["stdout_bytes"]]
        assert sha(raw) == observation["stdout_sha256"]
    assert evidence["physical_timing_verified"] is False
    assert evidence["ppi_routing_verified"] is False


@pytest.mark.parametrize("fault", egu.FAULTS[1:])
def test_fault_marker_is_not_proof_of_effect(fault):
    uart, stdout = transcript()
    stdout = stdout.replace(b"SIM_EGU_OBS_4_PRE", b"SIM_EGU_FAULT\nSIM_EGU_OBS_4_PRE")
    with pytest.raises(AssertionFailure) as raised:
        egu.EGUProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["nominal_regression_passed"] is True
    assert raised.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize(
    "old,new",
    [
        (b"E=00000001", b"E=00000000"),
        (b"A=00000000", b"A=00000001"),
        (b"C=00000003", b"C=00000001"),
        (b"M=00008009", b"M=00008001"),
        (b"V=00000024", b"V=0000001B"),
        (b"SAMPLE 00000002", b"SAMPLE 00000001"),
        (b"BUILD", b"WRONG_BUILD"),
    ],
)
def test_bad_firmware_cannot_pass_correct_model(old, new):
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure):
        egu.EGUProfile("none").evaluate(uart.replace(old, new), ASSERTIONS, stdout)


@pytest.mark.parametrize("fault", egu.FAULTS[1:])
@pytest.mark.parametrize("mutation", ["event", "mask", "guest", "truncate"])
def test_fault_requires_both_raw_model_and_guest_evidence(fault, mutation):
    uart, stdout = transcript(fault)
    if mutation == "event":
        stdout = stdout.replace(b"OBS_4_PRE\r\n0\r\n", b"OBS_4_PRE\r\n1\r\n")
    if mutation == "mask":
        stdout = stdout.replace(b"32777\r\n", b"32769\r\n", 1)
    if mutation == "guest":
        uart = uart.replace(b"C=00000000", b"C=00000001", 1)
    if mutation == "truncate":
        uart = uart[: uart.rfind(b"SAMPLE")]
    with pytest.raises(AssertionFailure) as raised:
        egu.EGUProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is not True


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "order", "partial", "range", "large", "fault-order"]
)
def test_invalid_snapshot_inventory_stays_unknown(mutation):
    uart, stdout = transcript("egu-irq-mask")
    if mutation == "missing":
        stdout = stdout[: stdout.index(b"SIM_EGU_OBS_8_POST")]
    if mutation == "duplicate":
        stdout += stdout[: stdout.index(b"SIM_EGU_OBS_0_POST")]
    if mutation == "order":
        stdout = stdout.replace(b"OBS_2_PRE", b"OBS_2_POST")
    if mutation == "partial":
        stdout = stdout.replace(b"32777\r\n", b"32777\r", 1)
    if mutation == "range":
        stdout = stdout.replace(b"OBS_0_PRE\r\n1", b"OBS_0_PRE\r\n2")
    if mutation == "large":
        stdout = stdout.replace(b"32777\r\n", b"9" * 5000 + b"\r\n", 1)
    if mutation == "fault-order":
        stdout = b"SIM_EGU_FAULT\n" + stdout.replace(b"SIM_EGU_FAULT\r\n", b"")
    with pytest.raises(AssertionFailure) as raised:
        egu.EGUProfile("egu-irq-mask").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is None


@pytest.mark.parametrize("fault", egu.FAULTS)
def test_offline_plan_requires_fixed_inputs(inputs, tmp_path, monkeypatch, fault):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline process"))
    profile = egu.EGUProfile(fault)
    plan = profile.make_plan(**inputs)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert egu.load_plan(path, sha(path.read_bytes())) == plan
    for key, value in (("virtual_ms", 41), ("script_sha256", "a" * 64), ("fault_scenario", "bad")):
        path.write_text(json.dumps({**plan, key: value}))
        with pytest.raises(SimError):
            egu.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("tail", [b"SIM_EGU_OBS_", b"SIM_EGU_FAULT junk"])
def test_partial_extra_marker_cannot_hide_after_complete_inventory(tail):
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure):
        egu.EGUProfile("none").evaluate(uart, ASSERTIONS, stdout + tail)


@pytest.mark.parametrize("position", range(5))
def test_async_lifecycle_info_preserves_raw_snapshot_span(position):
    uart, stdout = transcript()
    block = b"SIM_EGU_OBS_0_POST\r\n0\r\n0\r\n0\r\n0\r\n32777\r\n"
    lines = block.splitlines(keepends=True)
    lines.insert(position + 1, b"[11:11:35.4640] [INFO] nrf52840-egu: Machine paused.\r\n")
    changed = b"".join(lines)
    stdout = stdout.replace(block, changed)
    result = egu.EGUProfile("none").evaluate(uart, ASSERTIONS, stdout)
    observation = result["model_observations"][1]
    assert result["nominal_regression_passed"] is True
    assert observation["stdout_sha256"] == sha(changed)
    assert observation["stdout_bytes"] == len(changed)


@pytest.mark.parametrize(
    "interleaved",
    [
        b"[11:11:35.4640] [WARNING] nrf52840-egu: Machine paused.\r\n",
        b"[11:11:35.4640] [INFO] nrf52840-egu: different message.\r\n",
        b"[11:11:35.4640] [INFO] another-machine: Machine paused.\r\n",
        b"[11:11:35.4640] [INFO] nrf52840-egu: Machine paused.\r\n" * 5,
    ],
)
def test_unknown_or_excessive_interleaved_lines_cannot_pass(interleaved):
    uart, stdout = transcript()
    stdout = stdout.replace(b"OBS_0_POST\r\n", b"OBS_0_POST\r\n" + interleaved)
    with pytest.raises(AssertionFailure):
        egu.EGUProfile("none").evaluate(uart, ASSERTIONS, stdout)


@pytest.mark.parametrize("fault", egu.FAULTS)
def test_saved_passed_and_failed_evidence_recomputed(inputs, tmp_path, monkeypatch, fault):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    path = tmp_path / "plan.json"
    write_json(path, egu.EGUProfile(fault).make_plan(**inputs))

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
    report = egu.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    assert egu.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    for mutation in ("sample", "effect", "span", "verdict", "missing", "cleanup"):
        changed = copy.deepcopy(report)
        if mutation == "sample":
            changed["assertions"]["samples"][0]["channel"] = 1
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
            egu.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"32777\r\n", b"32769\r\n", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        egu.inspect_report(saved, sha(saved.read_bytes()))


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
        assert main([*common, "--profile", egu.PROFILE, *extra, "--output", str(path)]) == 1
        assert not path.exists()
        capsys.readouterr()
    path = tmp_path / "good.json"
    assert main([*common, "--profile", egu.PROFILE, "--fault", "none", "--output", str(path)]) == 0
    capsys.readouterr()
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == egu.PROFILE
