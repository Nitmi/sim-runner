"""Synthetic fault evidence and saved-report integrity; native runs are separate."""

import copy
import json

import pytest

from sim_runner import gpio_fault as fault
from sim_runner import gpio_timer
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = {
    "ready": "READY v1",
    "build": "BUILD abc",
    "heartbeat_prefix": "SAMPLE",
    "minimum": 16,
}


def transcript(name):
    profile = fault.FaultProfile(name)
    uart = b"READY v1\r\nBUILD abc\r\n"
    count = 4 if name.startswith("timer-") else 16
    for sequence in range(1, count + 1):
        phase = profile.SCENARIO["phases"][(sequence - 1) // 4]
        state = phase["applied_input"]
        uart += (
            f"SAMPLE {sequence:08X} I={state} O={state} L={state} "
            f"T={1250 * sequence:08X} V=00000018\r\n"
        ).encode()
    stdout = b"[INFO] path \xd1\xe9\xca\xd5\r\nSIM_RUNNER_COMPLETED_V1\n"
    for index, phase in enumerate(profile.SCENARIO["phases"]):
        input_ = phase["applied_input"]
        output = 0 if name.startswith("timer-") else input_
        out_register = output << 13
        in_register = input_ << 11 | output << 14
        counter = 5500 if name == "timer-stop" and index else phase["observe_at_ms"] * 250
        irq = 0 if name == "timer-irq-mask" and index else 65536
        stdout += (
            f"SIM_FAULT_OBS_{index}\n{out_register}\n{in_register}\n{counter}\n{irq}\n".encode()
        )
    return uart, stdout


@pytest.mark.parametrize("name", fault.FAULTS)
def test_fixed_faults_keep_the_nominal_verdict_and_independent_model_state(name):
    profile = fault.FaultProfile(name)
    uart, stdout = transcript(name)
    if name == "none":
        result = profile.evaluate(uart, ASSERTIONS, stdout)
        assert result["nominal_regression_passed"] is True
        assert result["fault"] == {"id": "none", "effect_observed": None, "status": "not_requested"}
    else:
        with pytest.raises(AssertionFailure) as raised:
            profile.evaluate(uart, ASSERTIONS, stdout)
        assert raised.value.code == "ASSERTION_FAILED"
        result = raised.value.evidence
        assert result["nominal_regression_passed"] is False
        assert result["fault"] == {"id": name, "effect_observed": True, "status": "observed"}
        assert len(result["samples"]) == (4 if name.startswith("timer-") else 16)
    assert len(result["model_observations"]) == 4
    assert result["model_coverage_complete"] is result["physical_timing_verified"] is False


@pytest.mark.parametrize("name", fault.FAULTS[1:])
def test_selected_fault_and_guest_uart_cannot_replace_observed_model_effect(name):
    profile = fault.FaultProfile(name)
    normal_uart, normal_stdout = transcript("none")
    with pytest.raises(AssertionFailure) as raised:
        profile.evaluate(normal_uart, ASSERTIONS, normal_stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is False
    assert raised.value.evidence["nominal_regression_passed"] is True
    uart, _ = transcript(name)
    with pytest.raises(AssertionFailure) as raised:
        profile.evaluate(uart, ASSERTIONS, normal_stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "order", "overflow", "partial", "non-ascii"]
)
def test_broken_snapshot_protocol_keeps_effect_unknown(change):
    uart, stdout = transcript("gpio-stuck-low")
    if change == "missing":
        stdout = stdout[: stdout.index(b"SIM_FAULT_OBS_3")]
    if change == "duplicate":
        stdout += b"SIM_FAULT_OBS_0\n0\n0\n5000\n65536\n"
    if change == "order":
        stdout = stdout.replace(b"SIM_FAULT_OBS_0", b"SIM_FAULT_OBS_9")
    if change == "overflow":
        stdout = stdout.replace(b"5000\n", b"4294967296\n", 1)
    if change == "partial":
        stdout = stdout[:-4]
    if change == "non-ascii":
        stdout = stdout.replace(b"5000\n", b"\xff5000\n", 1)
    with pytest.raises(AssertionFailure) as raised:
        fault.FaultProfile("gpio-stuck-low").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["fault"]["effect_observed"] is None


def test_timer_stop_and_irq_mask_have_distinct_independent_signatures():
    for selected, transcript_name in (
        ("timer-stop", "timer-irq-mask"),
        ("timer-irq-mask", "timer-stop"),
    ):
        with pytest.raises(AssertionFailure) as raised:
            fault.FaultProfile(selected).evaluate(
                *[transcript(transcript_name)[0], ASSERTIONS, transcript(transcript_name)[1]]
            )
        assert raised.value.evidence["fault"]["effect_observed"] is False


def test_extra_uart_does_not_make_unbounded_json_diagnostics():
    uart, stdout = transcript("none")
    uart += uart.splitlines(keepends=True)[-1] * 1000
    with pytest.raises(AssertionFailure) as raised:
        fault.FaultProfile("none").evaluate(uart, ASSERTIONS, stdout)
    evidence = raised.value.evidence
    assert len(evidence["samples"]) == 16 and evidence["sample_summary_truncated"] is True
    assert len(json.dumps(evidence)) < 16384


def test_normal_guest_claims_do_not_replace_independent_timer_snapshot():
    uart, stdout = transcript("none")
    stdout = stdout.replace(b"5000\n65536", b"0\n65536", 1)
    with pytest.raises(AssertionFailure) as raised:
        fault.FaultProfile("none").evaluate(uart, ASSERTIONS, stdout)
    assert raised.value.evidence["model_timer_nominal"] is False
    assert raised.value.evidence["nominal_regression_passed"] is False


@pytest.mark.parametrize("name", fault.FAULTS)
def test_plan_binds_the_scenario_script_and_rejects_changed_inputs(
    inputs, tmp_path, monkeypatch, name
):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline spawned process"))
    profile = fault.FaultProfile(name)
    plan = profile.make_plan(**inputs)
    path = tmp_path / "fault.json"
    write_json(path, plan)
    assert fault.load_plan(path, sha(path.read_bytes())) == plan
    assert plan["script_sha256"] == sha(profile.script(82).encode())
    assert plan["physical_bridges"] == [] and plan["retries"] == 0
    for key, value in (
        ("script_sha256", "a" * 64),
        ("virtual_ms", 83),
        ("fault_scenario", "other"),
    ):
        changed = copy.deepcopy(plan)
        changed[key] = value
        path.write_text(json.dumps(changed))
        with pytest.raises(SimError):
            fault.load_plan(path, sha(path.read_bytes()))
    changed = copy.deepcopy(plan)
    changed["scenario"]["phases"][1]["input_at_ms"] = 21
    path.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        fault.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("name", [None, True, [], "other", "gpio-stuck-low\nquit"])
def test_fault_selection_is_a_fixed_enum(name):
    with pytest.raises(SimError):
        fault.FaultProfile(name)


@pytest.mark.parametrize("name", fault.FAULTS)
def test_process_owner_preserves_fault_failure_evidence_and_offline_checks(
    inputs, tmp_path, monkeypatch, name
):
    inputs["heartbeat"] = "SAMPLE"
    profile = fault.FaultProfile(name)
    path = tmp_path / "plan.json"
    write_json(path, profile.make_plan(**inputs))

    def execute_double(argv, directory, *args):
        uart, stdout = transcript(name)
        for leaf, data in (
            ("uart.bin", uart),
            ("backend.stdout.bin", stdout),
            ("backend.stderr.bin", b""),
        ):
            (directory / leaf).write_bytes(data)
        return {
            "pid": 1,
            "exit_code": 0,
            "error": None,
            "forced_termination": False,
            "cleanup": {
                "process_exited": True,
                "job_active_processes": 0,
                "capture_threads_stopped": True,
            },
        }

    monkeypatch.setattr("sim_runner.run.execute", execute_double)
    directory = tmp_path / "run"
    report = fault.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (name == "none")
    assert report["assertions"]["fault"]["effect_observed"] is (None if name == "none" else True)
    saved = directory / "report.json"
    checked = fault.inspect_report(saved, sha(saved.read_bytes()))
    assert checked["report_status"] == report["status"]
    assert checked["executables_started"] is False
    assert checked["fault_requested"] == name
    for mutation in (
        "effect",
        "samples",
        "counter",
        "verdict",
        "missing-evidence",
        "scenario",
        "errors",
    ):
        changed = copy.deepcopy(report)
        if mutation == "effect":
            changed["assertions"]["fault"]["effect_observed"] = "yes"
        if mutation == "samples":
            changed["assertions"]["samples"][0]["input"] = 1
        if mutation == "counter":
            changed["assertions"]["model_observations"][1]["timer_counter"] += 1
        if mutation == "verdict":
            changed["ok"] = True
            changed["status"] = "passed"
            changed["errors"] = []
            if name == "none":
                changed["assertions"]["nominal_regression_passed"] = False
        if mutation == "missing-evidence":
            changed["assertions"] = None
        if mutation == "scenario":
            changed["fault_scenario"] = "other"
        if mutation == "errors":
            changed["errors"] = []
            if name == "none":
                changed["assertions"]["fault"]["effect_observed"] = True
        saved.write_text(json.dumps(changed))
        with pytest.raises((SimError, TypeError)):
            fault.inspect_report(saved, sha(saved.read_bytes()))
    saved.write_text(json.dumps(report))


def test_cli_requires_explicit_fault_and_cannot_apply_it_to_other_backends(
    inputs, tmp_path, capsys
):
    base = [
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
        "--output",
        str(tmp_path / "cli.json"),
    ]
    assert main([*base, "--profile", fault.PROFILE]) == 1
    assert main([*base, "--fault", "none"]) == 1
    assert main([*base, "--profile", gpio_timer.PROFILE, "--fault", "none"]) == 1
    assert (
        main([*base, "--profile", fault.PROFILE, "--fault", "none", "--backend", "espressif-qemu"])
        == 1
    )
    assert not (tmp_path / "cli.json").exists()
    assert main([*base, "--profile", fault.PROFILE, "--fault", "none"]) == 0
    capsys.readouterr()
    path = tmp_path / "cli.json"
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
