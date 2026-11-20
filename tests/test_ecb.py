from __future__ import annotations

import copy
import json

import pytest

from sim_runner import ecb
from sim_runner.cli import main
from sim_runner.common import AssertionFailure, SimError, sha, write_json

ASSERTIONS = dict(ready="READY", build="BUILD", heartbeat_prefix="SAMPLE", minimum=8)


def transcript(fault="none"):
    # NIST F.1.1 literals, independently encoded in little-endian model words.
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    clear = [
        bytes.fromhex(v)
        for v in (
            "6bc1bee22e409f96e93d7e117393172a",
            "ae2d8a571e03ac9c9eb76fac45af8e51",
            "30c81c46a35ce411e5fbc1191a0a52ef",
            "f69f2445df4f9b17ad2b417be66c3710",
        )
    ]
    cipher = [
        bytes.fromhex(v)
        for v in (
            "3ad77bb40d7a3660a89ecaf32466ef97",
            "f5d3d58503b9699de785895a96fdbaaf",
            "43b1cd7f598ece23881b00e3ed030688",
            "7b0c785e27e8ad3f8223207104725dd4",
        )
    ]

    def unpack(data):
        return [int.from_bytes(data[i : i + 4], "little") for i in range(0, 16, 4)]

    uart, stdout = b"READY\r\nBUILD\r\n", b""
    for i in range(8):
        redirected = i >= 4 and fault == "context-redirect"
        vector = 3 if i >= 4 and fault == "plaintext-stuck" else i % 4
        alt = (i + 1) % 4
        output = b"\xaa" * 16 if redirected else cipher[vector]
        if i == 4 and fault != "none":
            stdout += b"SIM_ECB_FAULT\r\n"
        for phase in ("PRE", "POST"):
            post = phase == "POST"
            values = [0x20001104 if redirected else 0x20001004, int(post), 0, 2 if post else 0]
            for other in (False, True):
                selected = alt if other else vector
                written = post and (other == redirected)
                values += [
                    0x13579BDF,
                    *unpack(key),
                    *unpack(clear[selected]),
                    *unpack(cipher[selected] if written else b"\xaa" * 16),
                    0x2468ACE0,
                ]
            stdout += (
                f"SIM_ECB_OBS_{i}_{phase}\r\n" + "\r\n".join(map(str, values)) + "\r\n"
            ).encode()
        uart += (
            f"SAMPLE {i + 1:08X} OUT={output.hex().upper()}"
            " END=00000001 LO=13579BDF HI=2468ACE0\r\n"
        ).encode()
    return uart, stdout + b"SIM_RUNNER_COMPLETED_V1\r\n"


@pytest.mark.parametrize("fault", ecb.FAULTS)
def test_independent_nist_vectors_and_observed_faults(fault):
    uart, stdout = transcript(fault)
    profile = ecb.ECBProfile(fault)
    if fault == "none":
        result = profile.evaluate(uart, ASSERTIONS, stdout)
        assert result["nominal_regression_passed"] is True
    else:
        with pytest.raises(AssertionFailure) as caught:
            profile.evaluate(uart, ASSERTIONS, stdout)
        result = caught.value.evidence
        assert result["nominal_regression_passed"] is False
        assert result["fault"]["effect_observed"] is True
    assert len(result["model_observations"]) == 16 and len(result["samples"]) == 8
    for key in (
        "crypto_hardware_verified",
        "cryptographic_security_qualified",
        "side_channel_resistance_verified",
        "physical_timing_verified",
    ):
        assert result[key] is False


@pytest.mark.parametrize("field", range(32))
def test_every_descriptor_event_handshake_and_sram_field_is_required(field):
    uart, stdout = transcript()
    rows = stdout.splitlines(keepends=True)
    rows[field + 1] = str(int(rows[field + 1]) ^ 1).encode() + b"\r\n"
    with pytest.raises(AssertionFailure):
        ecb.ECBProfile("none").evaluate(uart, ASSERTIONS, b"".join(rows))


@pytest.mark.parametrize("fault", ecb.FAULTS[1:])
def test_fault_marker_without_effect_cannot_establish_effect(fault):
    uart, stdout = transcript()
    stdout = stdout.replace(b"SIM_ECB_OBS_4_PRE", b"SIM_ECB_FAULT\r\nSIM_ECB_OBS_4_PRE")
    with pytest.raises(AssertionFailure) as caught:
        ecb.ECBProfile(fault).evaluate(uart, ASSERTIONS, stdout)
    assert caught.value.evidence["fault"]["effect_observed"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "build",
        "sequence",
        "cipher",
        "event",
        "guard",
        "tail",
        "nonascii",
        "missing",
        "duplicate",
        "order",
        "overflow",
        "partial_marker",
        "extra_info",
    ],
)
def test_malformed_identity_protocol_and_raw_inventory_rejected(mutation):
    uart, stdout = transcript()
    if mutation == "build":
        uart = uart.replace(b"BUILD", b"WRONG")
    if mutation == "sequence":
        uart = uart.replace(b"SAMPLE 00000001", b"SAMPLE 00000002")
    if mutation == "cipher":
        uart = uart.replace(b"3AD77BB4", b"B47BD73A", 1)
    if mutation == "event":
        uart = uart.replace(b"END=00000001", b"END=00000000", 1)
    if mutation == "guard":
        uart = uart.replace(b"HI=2468ACE0", b"HI=2468ACE1", 1)
    if mutation == "tail":
        uart = uart[:-2]
    if mutation == "nonascii":
        uart += b"\xff\n"
    if mutation == "missing":
        stdout = stdout.replace(b"OBS_0_PRE", b"MISSING", 1)
    if mutation == "duplicate":
        stdout += stdout[: stdout.index(b"SIM_ECB_OBS_0_POST")]
    if mutation == "order":
        stdout = stdout.replace(b"OBS_0_PRE", b"OBS_1_PRE", 1)
    if mutation == "overflow":
        stdout = stdout.replace(b"2863311530\r\n", b"4294967296\r\n", 1)
    if mutation == "partial_marker":
        stdout += b"SIM_ECB_OBS_"
    if mutation == "extra_info":
        stdout = stdout.replace(b"OBS_0_PRE\r\n", b"OBS_0_PRE\r\n[INFO] unexpected\n")
    with pytest.raises(AssertionFailure) as caught:
        ecb.ECBProfile("none").evaluate(uart, ASSERTIONS, stdout)
    if mutation == "build":
        assert len(caught.value.evidence["model_observations"]) == 16
        assert caught.value.evidence["samples"] == []
        assert caught.value.evidence["fault"]["effect_observed"] is None


@pytest.mark.parametrize("position", range(32))
def test_bounded_lifecycle_info_is_bound_to_exact_raw_span(position):
    uart, stdout = transcript()
    offset = 0
    for _ in range(position + 1):
        offset = stdout.index(b"\n", offset) + 1
    stdout = (
        stdout[:offset]
        + b"[12:00:00.0000] [INFO] nrf52840-ecb: Machine paused.\n"
        + stdout[offset:]
    )
    result = ecb.ECBProfile("none").evaluate(uart, ASSERTIONS, stdout)
    observation = result["model_observations"][0]
    assert (
        sha(stdout[observation["stdout_offset"] :][: observation["stdout_bytes"]])
        == observation["stdout_sha256"]
    )


def test_plan_is_offline_fixed_and_requires_explicit_fault(inputs, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process"))
    common = [
        "plan",
        "--profile",
        ecb.PROFILE,
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
            ["--fault", "rx-short-buffer"],
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
    for field, value in (("context_address", 0x20001008), ("encryption_service_budget_ms", 999)):
        changed = copy.deepcopy(original)
        changed["scenario"][field] = value
        path = tmp_path / (field + ".json")
        write_json(path, changed)
        with pytest.raises(SimError):
            ecb.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("fault", ecb.FAULTS)
def test_saved_evidence_recomputes_verdict_without_original_inputs(
    inputs, tmp_path, monkeypatch, fault
):
    inputs.update(ready="READY", build="BUILD", heartbeat="SAMPLE")
    plan = tmp_path / "plan.json"
    write_json(plan, ecb.ECBProfile(fault).make_plan(**inputs))

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
    report = ecb.run(plan, sha(plan.read_bytes()), directory)
    assert report["ok"] is (fault == "none")
    saved = directory / "report.json"
    inputs["image"].unlink()
    inputs["lock"].unlink()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process"))
    assert ecb.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    for mutation in ("sample", "effect", "span", "verdict", "missing", "cleanup"):
        changed = copy.deepcopy(report)
        if mutation == "sample":
            changed["assertions"]["samples"][0]["ciphertext"] = "00" * 16
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
            ecb.inspect_report(saved, sha(saved.read_bytes()))
    raw = directory / "backend.stdout.bin"
    raw.write_bytes(raw.read_bytes().replace(b"2863311530\r\n", b"2863311531\r\n", 1))
    changed = copy.deepcopy(report)
    changed["artifacts"]["backend.stdout.bin"]["sha256"] = sha(raw.read_bytes())
    saved.write_text(json.dumps(changed))
    with pytest.raises(SimError):
        ecb.inspect_report(saved, sha(saved.read_bytes()))
