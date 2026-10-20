import pytest

from sim_runner.cli import main
from sim_runner.common import SimError, decode, sha, write_json
from sim_runner.plan import evaluate, firmware, load_plan, make_plan, runtime
from sim_runner.run import script


def test_plan_round_trip_is_offline(inputs, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline commands must not spawn processes")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    plan = make_plan(**inputs)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert load_plan(path, sha(path.read_bytes())) == plan
    assert (
        main(
            [
                "doctor",
                "--runtime-lock",
                str(inputs["lock"]),
                "--runtime-sha256",
                inputs["lock_sha"],
            ]
        )
        == 0
    )
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    assert plan["hardware_access"] is False
    assert plan["executables_started"] is False
    assert plan["physical_bridges"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"virtual_ms": 0},
        {"virtual_ms": 10001},
        {"virtual_ms": True},
        {"timeout": 0},
        {"timeout": 121},
        {"memory_mib": 511},
        {"output_limit": 4095},
        {"min_heartbeats": 2},
        {"ready": "READY\nquit"},
        {"build": "\x00"},
        {"heartbeat": "READY v1"},
        {"image_sha": "0" * 64},
        {"lock_sha": "bad"},
    ],
)
def test_invalid_plan_is_rejected(inputs, change):
    with pytest.raises(SimError):
        make_plan(**(inputs | change))


@pytest.mark.parametrize(
    "mutation",
    [
        {"hardware_access": True},
        {"hardware_access": 0},
        {"physical_board_verified": 0},
        {"physical_bridges": ["COM11"]},
        {"extra": "include other.resc"},
        {"retries": 1},
    ],
)
def test_tampered_plan_even_rehashed_is_rejected(inputs, tmp_path, mutation):
    path = tmp_path / "plan.json"
    write_json(path, make_plan(**inputs) | mutation)
    with pytest.raises(SimError):
        load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize("kind", ["extra", "modified", "missing"])
def test_runtime_inventory_drift(inputs, kind):
    root = inputs["lock"].parent / "runtime"
    if kind == "extra":
        (root / "injected.dll").write_bytes(b"bad")
    elif kind == "modified":
        (root / "readme.txt").write_bytes(b"bad")
    else:
        (root / "readme.txt").unlink()
    with pytest.raises(SimError):
        runtime(inputs["lock"], inputs["lock_sha"])


@pytest.mark.parametrize(
    "data",
    [
        b"",
        bytes(300),
        (0x20040004).to_bytes(4, "little") + bytes(300),
        (0x20040000).to_bytes(4, "little") + (0x100).to_bytes(4, "little") + bytes(300),
    ],
)
def test_invalid_vectors(data):
    with pytest.raises(SimError):
        firmware(data)


def test_uart_exact_ready_build_and_contiguous_sequence(assertions, valid_uart):
    assert evaluate(valid_uart, assertions)["heartbeats"] == 3


@pytest.mark.parametrize(
    "transform",
    [
        lambda d: d.replace(b"BUILD abc", b"BUILD old"),
        lambda d: d.replace(b"READY v1\r\n", b""),
        lambda d: d.replace(b"00000002", b"00000001"),
        lambda d: d.replace(b"00000001", b"00000000"),
        lambda d: d[:-2],
        lambda d: d + b"extra\r\n",
        lambda d: d + b"\xff\n",
        lambda d: d.replace(b"HEARTBEAT 00000003\r\n", b""),
        lambda d: d.replace(b"00000003", b"0000000a"),
    ],
)
def test_bad_uart_never_passes(assertions, valid_uart, transform):
    with pytest.raises((SimError, UnicodeError)):
        evaluate(transform(valid_uart), assertions)


def test_duplicate_json_keys_rejected():
    with pytest.raises(SimError):
        decode(b'{"hardware_access":false,"hardware_access":true}')


def test_fixed_script_has_no_host_bridge():
    value = script(250)
    assert 'RunFor "0.250"' in value
    assert value.endswith("quit\n")
    assert "$ORIGIN/uart.bin" in value
    for word in ("http", "socket", "COM11", "include", "python", "exec", "shell"):
        assert word not in value
