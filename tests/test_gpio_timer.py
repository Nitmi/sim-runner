"""Synthetic transcripts validate negative controls; native acceptance uses Renode."""

import json

import pytest

from sim_runner import gpio_timer as gpio
from sim_runner.cli import main
from sim_runner.common import SimError, sha, write_json
from sim_runner.inspect import inspect_report
from sim_runner.run import run


@pytest.fixture
def transcript():
    uart = b"READY v1\r\nBUILD abc\r\n"
    for i in range(1, 17):
        state = gpio.PHASES[(i - 1) // 4]["input"]
        uart += (
            f"SAMPLE {i:08X} I={state} O={state} L={state} T={i * 1250:08X} V=00000018\r\n".encode()
        )
    stdout = b"SIM_RUNNER_COMPLETED_V1\n"
    for index, phase in enumerate(gpio.PHASES):
        value = (1 << 11 | 1 << 13 | 1 << 14) if phase["input"] else 0
        stdout += f"SIM_GPIO_OBS_{index}\n{value}\n{value}\n".encode()
    return uart, stdout


def arguments():
    return {"ready": "READY v1", "build": "BUILD abc", "heartbeat_prefix": "SAMPLE", "minimum": 16}


def test_offline_plan_cannot_start_process_and_binds_fixed_schedule(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline command spawned"))
    plan = gpio.make_plan(**inputs)
    assert plan["scenario"] == gpio.SCENARIO and plan["virtual_ms"] == 82
    assert plan["script_sha256"] == sha(gpio.script(82).encode())
    path = tmp_path / "gpio.json"
    write_json(path, plan)
    assert gpio.load_plan(path, sha(path.read_bytes())) == plan
    plan["scenario"]["input_pin"] = 10
    assert gpio.SCENARIO["input_pin"] == 11
    path.write_text(json.dumps(plan))
    with pytest.raises(SimError, match="fixed profile"):
        gpio.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize(
    "key,value",
    [("virtual_ms", True), ("virtual_ms", 83), ("min_heartbeats", 3), ("min_heartbeats", True)],
)
def test_fixed_scenario_budgets_are_not_user_scripts(inputs, key, value):
    with pytest.raises(SimError):
        gpio.make_plan(**inputs, **{key: value})


@pytest.mark.parametrize(
    "fault",
    [
        "build",
        "input",
        "output",
        "loopback",
        "irq",
        "timer",
        "sequence",
        "missing",
        "partial",
        "unknown",
        "observation",
        "duplicate",
        "order",
        "oversize",
    ],
)
def test_sample_claims_do_not_replace_independent_gpio_or_irq_assertions(transcript, fault):
    uart, stdout = transcript
    if fault == "build":
        uart = uart.replace(b"BUILD abc", b"BUILD old")
    if fault == "input":
        uart = uart.replace(b"I=1", b"I=0", 1)
    if fault == "output":
        uart = uart.replace(b"O=1", b"O=0", 1)
    if fault == "loopback":
        uart = uart.replace(b"L=1", b"L=0", 1)
    if fault == "irq":
        uart = uart.replace(b"V=00000018", b"V=00000000", 1)
    if fault == "timer":
        uart = uart.replace(b"T=000004E2", b"T=000009C4", 1)
    if fault == "sequence":
        uart = uart.replace(b"SAMPLE 00000001", b"SAMPLE 00000002", 1)
    if fault == "missing":
        uart = uart[: uart.rindex(b"SAMPLE")]
    if fault == "partial":
        uart = uart[:-1]
    if fault == "unknown":
        uart += b"OTHER\n"
    if fault == "observation":
        stdout = stdout.replace(b"26624\n26624", b"0\n0", 1)
    if fault == "duplicate":
        stdout += b"SIM_GPIO_OBS_0\n0\n0\n"
    if fault == "order":
        stdout = stdout.replace(b"SIM_GPIO_OBS_0", b"SIM_GPIO_OBS_9")
    if fault == "oversize":
        stdout = stdout.replace(b"26624\n26624", b"4294967296\n4294967296", 1)
    with pytest.raises(SimError) as error:
        gpio.evaluate(uart, arguments(), stdout)
    assert error.value.code == "ASSERTION_FAILED"


def test_nominal_profile_asserts_exact_samples_and_snapshots(transcript):
    result = gpio.evaluate(*[transcript[0], arguments(), transcript[1]])
    assert len(result["samples"]) == 16 and len(result["model_observations"]) == 4
    assert result["gpio_transitions"] == 3
    assert result["model_coverage_complete"] is result["physical_timing_verified"] is False
    assert [s["timer_ticks"] for s in result["samples"]] == [1250 * i for i in range(1, 17)]


def test_locale_encoded_host_paths_do_not_change_ascii_observation_protocol(transcript):
    uart, stdout = transcript
    stdout = b"[INFO] Including D:/\xd1\xe9\xca\xd5/run.resc\r\n" + stdout
    assert len(gpio.evaluate(uart, arguments(), stdout)["model_observations"]) == 4
    with pytest.raises(SimError):
        gpio.evaluate(uart, arguments(), stdout.replace(b"26624\n26624", b"\xff26624\n26624", 1))


@pytest.mark.parametrize(
    "fault", [None, "warning", "timeout", "missing-completion", "runtime-change"]
)
def test_shared_process_owner_keeps_failure_and_cleanup(
    inputs, tmp_path, monkeypatch, transcript, fault
):
    inputs["heartbeat"] = "SAMPLE"
    path = tmp_path / "plan.json"
    write_json(path, gpio.make_plan(**inputs))

    def execute_double(argv, directory, *args):
        uart, stdout = transcript
        if fault == "warning":
            stdout += b"[WARNING] unmapped access\n"
        if fault == "missing-completion":
            stdout = stdout.replace(b"SIM_RUNNER_COMPLETED_V1", b"absent")
        for name, data in (
            ("uart.bin", uart),
            ("backend.stdout.bin", stdout),
            ("backend.stderr.bin", b""),
        ):
            (directory / name).write_bytes(data)
        if fault == "runtime-change":
            (inputs["lock"].parent / "runtime/readme.txt").write_bytes(b"changed")
        return {
            "pid": 1,
            "exit_code": 0,
            "error": "TIMEOUT" if fault == "timeout" else None,
            "forced_termination": fault == "timeout",
            "cleanup": {
                "process_exited": True,
                "job_active_processes": 0,
                "capture_threads_stopped": True,
            },
        }

    monkeypatch.setattr("sim_runner.run.execute", execute_double)
    output = tmp_path / "run"
    doc = run(path, sha(path.read_bytes()), output, profile=gpio)
    assert doc["ok"] is (fault is None)
    saved = output / "report.json"
    inspected = inspect_report(saved, sha(saved.read_bytes()), profile=gpio)
    assert inspected["report_status"] == doc["status"]
    assert (output / "run.resc").read_text() == gpio.script(82)
    if fault is None:
        for mutation in (
            "coverage",
            "coverage-type",
            "scenario",
            "script",
            "snapshot",
            "uart",
            "virtual-time",
            "virtual-type",
        ):
            changed = json.loads(saved.read_bytes())
            if mutation == "coverage":
                changed["model_coverage_complete"] = True
            if mutation == "coverage-type":
                changed["model_coverage_complete"] = 0
            if mutation == "virtual-type":
                changed["virtual_time"] = None
            if mutation == "virtual-time":
                changed["virtual_time"]["completed"] = False
            if mutation == "snapshot":
                changed["assertions"]["model_observations"][1]["out_register"] = 0
            if mutation in ("scenario", "script", "uart"):
                name = {"scenario": "plan.json", "script": "run.resc", "uart": "uart.bin"}[mutation]
                source = output / name
                original = source.read_bytes()
                if mutation == "scenario":
                    plan = json.loads(original)
                    plan["scenario"]["period_ticks"] = 10000
                    data = json.dumps(plan).encode()
                elif mutation == "uart":
                    data = original.replace(b"O=1", b"O=0", 1)
                else:
                    data = original + b"start\n"
                source.write_bytes(data)
                changed["artifacts"][name].update(sha256=sha(data), bytes=len(data))
                if name == "plan.json":
                    changed["plan_sha256"] = sha(data)
            saved.write_text(json.dumps(changed))
            with pytest.raises(SimError):
                inspect_report(saved, sha(saved.read_bytes()), profile=gpio)
            saved.write_text(json.dumps(doc))
            if mutation in ("scenario", "script", "uart"):
                source.write_bytes(original)


def test_cli_profile_defaults_and_backend_mismatch(inputs, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline CLI spawned"))
    args = [
        "plan",
        "--profile",
        gpio.PROFILE,
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
        str(tmp_path / "cli-plan.json"),
    ]
    assert main(args) == 0
    doc = json.loads((tmp_path / "cli-plan.json").read_bytes())
    assert doc["virtual_ms"] == 82 and doc["assertions"]["minimum"] == 16
    capsys.readouterr()
    assert (
        main(
            [
                "inspect-plan",
                "--plan",
                str(tmp_path / "cli-plan.json"),
                "--sha256",
                sha((tmp_path / "cli-plan.json").read_bytes()),
            ]
        )
        == 0
    )
    assert main([*args, "--backend", "espressif-qemu"]) == 1
