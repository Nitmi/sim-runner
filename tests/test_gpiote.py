from __future__ import annotations

import copy
import json

import pytest

from sim_runner import gpiote
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=7)


def transcript(fault="none"):
    # Literal independent edge/CONFIG oracle; no production helpers generate expected registers.
    configs = [
        (1116929, 1182721, 205569, 6144, 0),
        (68353, 1182721, 205569, 4096, 0),
        (68353, 134145, 205569, 0, 0),
        (68353, 1182721, 205569, 4096, 0),
        (68353, 1182721, 1254145, 4096, 8),
        (68353, 1182721, 205569, 4096, 0),
        (1116929, 1182721, 205569, 6144, 0),
        (1116929, 134145, 205569, 2048, 0),
        (1116929, 134145, 1254145, 2048, 8),
    ]
    pres = [
        (1, 0, 0),
        (0, 0, 0),
        (0, 1, 0),
        (0, 0, 0),
        (0, 0, 1),
        (0, 0, 1),
        (1, 0, 0),
        (0, 1, 0),
        (0, 0, 1),
    ]
    posts = [(0, 0, 0)] * 9
    masks = [7] * 9
    delivered = [(0, 1, 7), (1, 0, 7), (2, 1, 7), (2, 0, 7), (0, 1, 7), (1, 0, 7), (2, 1, 7)]
    if fault == "gpiote-irq-mask":
        masks[4:] = [3] * 5
        pres[6], pres[7] = (1, 0, 1), (0, 1, 1)
        posts[4:] = [(0, 0, 1)] * 5
        delivered = [(0, 1, 7), (1, 0, 7), (0, 1, 3), (1, 0, 3)]
    if fault == "gpiote-wrong-polarity":
        configs[4:] = [(a, b, c - 65536, d, e) for a, b, c, d, e in configs[4:]]
        pres[4], pres[8] = (0, 0, 0), (0, 0, 0)
        delivered = [(0, 1, 7), (1, 0, 7), (2, 0, 7), (0, 1, 7), (1, 0, 7)]
    uart = b"READY\r\nBUILD\r\n"
    for i, (channel, level, mask) in enumerate(delivered, 1):
        uart += (
            f"SAMPLE {i:08X} C={channel:08X} P={level:08X} E=00000001 A=00000000 "
            f"M={mask:08X} V=00000016\r\n"
        ).encode()
    stdout = b""
    for i in range(9):
        if i == 4 and fault != "none":
            stdout += b"SIM_GPIOTE_FAULT\r\n"
        for phase, events in (("PRE", pres[i]), ("POST", posts[i])):
            stdout += (
                f"SIM_GPIOTE_OBS_{i}_{phase}\r\n"
                + "\r\n".join(map(str, (*events, masks[i], *configs[i])))
                + "\r\n"
            ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\n"


@pytest.mark.parametrize("fault", gpiote.FAULTS)
def test_fixed_edges_and_independent_fault_effect(fault):
    uart, stdout = transcript(fault)
    if fault == "none":
        evidence = gpiote.GPIOTEProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    else:
        with pytest.raises(AssertionFailure) as caught:
            gpiote.GPIOTEProfile(fault).evaluate(uart, ASSERTIONS, stdout)
        evidence = caught.value.evidence
    assert evidence["nominal_regression_passed"] is (fault == "none")
    assert evidence["fault"]["effect_observed"] is (None if fault == "none" else True)
    assert (
        len(evidence["samples"])
        == {"none": 7, "gpiote-irq-mask": 4, "gpiote-wrong-polarity": 5}[fault]
    )
    assert len(evidence["model_observations"]) == 18
    for observation in evidence["model_observations"]:
        raw = stdout[observation["stdout_offset"] :][: observation["stdout_bytes"]]
        assert sha(raw) == observation["stdout_sha256"]
    assert evidence["physical_timing_verified"] is False
    assert evidence["ppi_routing_verified"] is False


@pytest.mark.parametrize("fault", gpiote.FAULTS[1:])
def test_request_marker_does_not_establish_fault(fault):
    uart, stdout = transcript()
    stdout = stdout.replace(b"SIM_GPIOTE_OBS_4_PRE", b"SIM_GPIOTE_FAULT\nSIM_GPIOTE_OBS_4_PRE")
    with pytest.raises(AssertionFailure) as caught:
        gpiote.GPIOTEProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert caught.value.evidence["nominal_regression_passed"] is True
    assert caught.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize(
    "old,new",
    [
        (b"C=00000000", b"C=00000007"),
        (b"P=00000001", b"P=00000000"),
        (b"E=00000001", b"E=00000000"),
        (b"A=00000000", b"A=00000001"),
        (b"M=00000007", b"M=00000003"),
        (b"V=00000016", b"V=00000024"),
        (b"SAMPLE 00000002", b"SAMPLE 00000001"),
        (b"BUILD", b"OTHER_BUILD"),
    ],
)
def test_guest_contradictions_rejected(old, new):
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure):
        gpiote.GPIOTEProfile("none").evaluate(uart.replace(old, new), ASSERTIONS, stdout)


@pytest.mark.parametrize("fault", gpiote.FAULTS)
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "order",
        "partial",
        "range",
        "gpio",
        "configuration",
        "info",
        "excess-info",
    ],
)
def test_invalid_model_evidence_rejected(fault, mutation):
    uart, stdout = transcript(fault)
    if mutation == "missing":
        stdout = stdout[: stdout.index(b"SIM_GPIOTE_OBS_8_POST")]
    if mutation == "duplicate":
        stdout += stdout[: stdout.index(b"SIM_GPIOTE_OBS_0_POST")]
    if mutation == "order":
        stdout = stdout.replace(b"OBS_0_PRE", b"OBS_0_POST")
    if mutation == "partial":
        stdout += b"SIM_GPIOTE_OBS_"
    if mutation == "range":
        stdout = stdout.replace(b"OBS_0_PRE\r\n1", b"OBS_0_PRE\r\n2")
    if mutation == "gpio":
        stdout = stdout.replace(b"6144\r\n", b"4096\r\n")
    if mutation == "configuration":
        stdout = stdout.replace(b"205569\r\n", b"140033\r\n")
    if mutation == "info":
        stdout = stdout.replace(b"OBS_0_PRE\r\n", b"OBS_0_PRE\r\n[INFO] unexpected\n")
    if mutation == "excess-info":
        stdout = stdout.replace(
            b"OBS_0_PRE\r\n",
            b"OBS_0_PRE\r\n" + b"[12:00:00.0000] [INFO] nrf52840-gpiote: Machine paused.\n" * 5,
        )
    with pytest.raises(AssertionFailure) as caught:
        gpiote.GPIOTEProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert caught.value.evidence["fault"]["effect_observed"] is not True


@pytest.mark.parametrize("position", range(9))
def test_bounded_async_lifecycle_lines_are_preserved(position):
    uart, stdout = transcript()
    marker = b"SIM_GPIOTE_OBS_0_PRE\r\n"
    start = stdout.index(marker) + len(marker)
    for _ in range(position):
        start = stdout.index(b"\n", start) + 1
    stdout = (
        stdout[:start]
        + b"[12:00:00.0000] [INFO] nrf52840-gpiote: Machine paused.\n"
        + stdout[start:]
    )
    info = gpiote.GPIOTEProfile("none").evaluate(uart, ASSERTIONS, stdout)
    observation = info["model_observations"][0]
    raw = stdout[observation["stdout_offset"] :][: observation["stdout_bytes"]]
    assert sha(raw) == observation["stdout_sha256"] and b"[INFO]" in raw


def test_plan_and_cli_are_fixed_offline(inputs, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process"))
    profile = gpiote.GPIOTEProfile("none")
    plan = profile.make_plan(
        inputs["lock"],
        inputs["lock_sha"],
        inputs["image"],
        inputs["image_sha"],
        "READY",
        "BUILD",
        "SAMPLE",
    )
    assert plan["virtual_ms"] == 40 and plan["assertions"]["minimum"] == 7
    common = [
        "plan",
        "--profile",
        gpiote.PROFILE,
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
            ["--fault", "none", "--min-heartbeats", "9"],
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
    capsys.readouterr()


@pytest.mark.parametrize("fault", gpiote.FAULTS)
def test_saved_passed_and_failed_evidence_recomputed(inputs, tmp_path, monkeypatch, fault):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    path = tmp_path / "plan.json"
    write_json(path, gpiote.GPIOTEProfile(fault).make_plan(**inputs))

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
    report = gpiote.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    assert (
        gpiote.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    )
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
            gpiote.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"7\r\n", b"3\r\n", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        gpiote.inspect_report(saved, sha(saved.read_bytes()))
