from __future__ import annotations

import copy
import json

import pytest

from sim_runner import watchdog as wdt
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=16)


def transcript(fault="none"):
    # Independent oracle: two enabled channels require both magic reloads, not one.
    counts = [4, 6] if fault == "feed-timer-stop" else [16]
    uart = b""
    for count in counts:
        uart += b"READY\r\nBUILD\r\n"
        for seq in range(1, count + 1):
            running = int(fault != "watchdog-stop" or seq < 5)
            uart += (
                f"SAMPLE {seq:08X} A=00000002 B=00000003 S={running:08X}"
                f" T={1250 * seq:08X} V=00000018\r\n"
            ).encode()
    timers = [5000, 5500, 2175, 7175] if fault == "feed-timer-stop" else [5000, 10000, 15000, 20000]
    stdout = b""
    for index, timer in enumerate(timers):
        if index == 1 and fault != "none":
            stdout += b"SIM_WDT_FAULT\r\n"
        if index == 2 and fault == "feed-timer-stop":
            stdout += b"[00:00] [INFO] watchdog: Reseting machine\r\nSIM_WDT_RESET_MACRO\r\n"
        running = int(fault != "watchdog-stop" or index == 0)
        counter = (
            [860, 369, 902, 903][index]
            if fault == "feed-timer-stop"
            else 1024
            if fault == "watchdog-stop" and index
            else 860
        )
        stdout += (
            f"SIM_WDT_OBS_{index}\r\n{running}\r\n3\r\n1023\r\n3\r\n{counter}\r\n{timer}\r\n"
        ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\n"


@pytest.mark.parametrize("fault", wdt.FAULTS)
def test_native_assertions_keep_nominal_failure_separate_from_observed_effect(fault):
    uart, stdout = transcript(fault)
    if fault == "none":
        evidence = wdt.WatchdogProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    else:
        with pytest.raises(AssertionFailure) as raised:
            wdt.WatchdogProfile(fault).evaluate(uart, ASSERTIONS, stdout)
        evidence = raised.value.evidence
    assert evidence["nominal_regression_passed"] is (fault == "none")
    assert evidence["fault"]["effect_observed"] is (None if fault == "none" else True)
    assert len(evidence["boots"]) == (2 if fault == "feed-timer-stop" else 1)
    for span in [*evidence["model_observations"], *evidence["reset_observations"]]:
        assert sha(stdout[span["stdout_offset"] :][: span["stdout_bytes"]]) == span["stdout_sha256"]
    assert evidence["physical_reset_verified"] is False
    assert evidence["low_power_behavior_verified"] is False


@pytest.mark.parametrize("fault", wdt.FAULTS[1:])
def test_fault_marker_alone_does_not_prove_effect(fault):
    uart, stdout = transcript()
    stdout = stdout.replace(b"SIM_WDT_OBS_1", b"SIM_WDT_FAULT\nSIM_WDT_OBS_1")
    with pytest.raises(AssertionFailure) as raised:
        wdt.WatchdogProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is False
    assert raised.value.evidence["nominal_regression_passed"] is True


@pytest.mark.parametrize(
    "old,new",
    [
        (b"A=00000002", b"A=00000003"),  # wrong magic never clears RR0
        (b"B=00000003", b"B=00000002"),  # missing second reload
        (b"S=00000001", b"S=00000000"),  # watchdog never started
        (b"V=00000018", b"V=00000000"),
        (b"T=000004E2", b"T=00000000"),
        (b"SAMPLE 00000002", b"SAMPLE 00000001"),
        (b"BUILD", b"WRONG_BUILD"),
    ],
)
def test_firmware_regression_fails_even_with_correct_model(old, new):
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure):
        wdt.WatchdogProfile("none").evaluate(uart.replace(old, new), ASSERTIONS, stdout)


@pytest.mark.parametrize("mutation", ["reboot", "reset", "callback", "timer", "channel", "state"])
def test_reset_marker_needs_independent_guest_and_model_effect(mutation):
    uart, stdout = transcript("feed-timer-stop")
    if mutation == "reboot":
        uart = uart[: uart.index(b"READY", 1)]
    if mutation == "reset":
        stdout = stdout.replace(b"[INFO] watchdog: Reseting machine", b"unrelated message")
    if mutation == "callback":
        stdout = stdout.replace(b"SIM_WDT_RESET_MACRO\r\n", b"")
    if mutation == "timer":
        stdout = stdout.replace(b"5500\r\n", b"10000\r\n")
    if mutation == "channel":
        uart = uart.replace(b"B=00000003", b"B=00000002")
    if mutation == "state":
        stdout = stdout.replace(b"1023\r\n", b"1022\r\n")
    with pytest.raises(AssertionFailure) as raised:
        wdt.WatchdogProfile("feed-timer-stop").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is not True


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "order", "partial", "range", "overflow", "fault-order"]
)
def test_invalid_observations_fail_closed(mutation):
    uart, stdout = transcript("watchdog-stop")
    if mutation == "missing":
        stdout = stdout[: stdout.index(b"SIM_WDT_OBS_3")]
    if mutation == "duplicate":
        stdout += b"SIM_WDT_OBS_0\n1\n3\n1023\n3\n860\n5000\n"
    if mutation == "order":
        stdout = stdout.replace(b"OBS_2", b"OBS_4")
    if mutation == "partial":
        stdout = stdout.replace(b"20000\r\n", b"20000")
    if mutation == "range":
        stdout = stdout.replace(b"\n1\r\n", b"\n2\r\n", 1)
    if mutation == "overflow":
        stdout = stdout.replace(b"860\r\n", b"4294967296\r\n", 1)
    if mutation == "fault-order":
        stdout = b"SIM_WDT_FAULT\n" + stdout.replace(b"SIM_WDT_FAULT\r\n", b"")
    with pytest.raises(AssertionFailure) as raised:
        wdt.WatchdogProfile("watchdog-stop").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is None


@pytest.mark.parametrize("tail", [b"", b"\r", b"\n" + b"extra\n" * 100])
def test_partial_or_oversized_protocol_stays_bounded(tail):
    uart, stdout = transcript()
    with pytest.raises(AssertionFailure) as raised:
        wdt.WatchdogProfile("none").evaluate(uart[:-2] + tail, ASSERTIONS, stdout)
    assert len(json.dumps(raised.value.evidence)) < 16384


@pytest.mark.parametrize("fault", wdt.FAULTS)
def test_offline_plan_binds_fixed_schedule_and_channel_configuration(
    inputs, tmp_path, monkeypatch, fault
):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline spawned"))
    plan = wdt.WatchdogProfile(fault).make_plan(**inputs)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert wdt.load_plan(path, sha(path.read_bytes())) == plan
    for key, value in (
        ("script_sha256", "a" * 64),
        ("virtual_ms", 83),
        ("fault_scenario", "other"),
    ):
        path.write_text(json.dumps({**plan, key: value}))
        with pytest.raises(SimError):
            wdt.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("fault", wdt.FAULTS)
def test_saved_passed_and_failed_reports_are_semantically_recomputed(
    inputs, tmp_path, monkeypatch, fault
):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    path = tmp_path / "plan.json"
    write_json(path, wdt.WatchdogProfile(fault).make_plan(**inputs))

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
    report = wdt.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    assert wdt.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    for mutation in ("sample", "effect", "span", "verdict", "missing", "cleanup"):
        changed = copy.deepcopy(report)
        if mutation == "sample":
            changed["assertions"]["boots"][0]["samples"][0]["partial_requests"] = 3
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
            wdt.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"1023\r\n", b"1022\r\n", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        wdt.inspect_report(saved, sha(saved.read_bytes()))


def test_cli_routes_watchdog_and_rejects_other_faults(inputs, tmp_path, capsys):
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
            ["--profile", wdt.PROFILE],
            ["--profile", wdt.PROFILE, "--fault", "temperature-stuck"],
            ["--profile", "nrf52840-spi-sensor-v1", "--fault", "watchdog-stop"],
            ["--profile", wdt.PROFILE, "--fault", "none", "--backend", "espressif-qemu"],
            ["--profile", wdt.PROFILE, "--fault", "none", "--min-heartbeats", "15"],
        )
    ):
        path = tmp_path / f"bad-{i}.json"
        assert main([*common, *options, "--output", str(path)]) == 1
        assert not path.exists()
        capsys.readouterr()
    path = tmp_path / "good.json"
    assert main([*common, "--profile", wdt.PROFILE, "--fault", "none", "--output", str(path)]) == 0
    capsys.readouterr()
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == wdt.PROFILE
