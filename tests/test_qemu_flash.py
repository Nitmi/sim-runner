"""Synthetic image/transcript tests; SDK/QEMU execution is separately accepted."""

import hashlib
import json
import shutil
import struct

import pytest
from test_qemu import qinputs as qinputs

from sim_runner import flash_image
from sim_runner import qemu_flash as flash
from sim_runner.cli import main
from sim_runner.common import SimError, sha, write_json


def esp_image():
    header = bytearray(24)
    header[:4] = b"\xe9\x01\x02\x20"
    struct.pack_into("<I", header, 4, 0x40380000)
    struct.pack_into("<H", header, 12, 5)
    header[23] = 1
    content = bytes(header) + struct.pack("<II", 0x40380000, 4) + b"\x01\x02\x03\x04"
    content += bytes(15 - len(content) % 16) + bytes([0xEF ^ 1 ^ 2 ^ 3 ^ 4])
    return content + hashlib.sha256(content).digest()


def merged():
    data = bytearray(b"\xff" * flash_image.FLASH_SIZE)
    image = esp_image()
    data[: len(image)] = image
    data[0x10000 : 0x10000 + len(image)] = image
    table = b"".join(
        struct.pack("<HBBII16sI", 0x50AA, t, s, o, z, name, 0)
        for t, s, o, z, name in flash_image.PARTITIONS
    )
    table += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(table, usedforsecurity=False).digest()
    data[0x8000 : 0x8000 + len(table)] = table
    return bytes(data)


@pytest.fixture
def finputs(qinputs, monkeypatch):
    root = qinputs["lock"].parent / "runtime"
    rom = root / flash.ROM
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"synthetic ROM - not executable")
    monkeypatch.setattr(flash, "ROM_SHA", sha(rom.read_bytes()))
    lock = json.loads(qinputs["lock"].read_bytes())
    lock["files"][flash.ROM] = flash.ROM_SHA
    qinputs["lock"].write_text(json.dumps(lock))
    image = root.parent / "flash.bin"
    image.write_bytes(merged())
    return qinputs | {
        "lock_sha": sha(qinputs["lock"].read_bytes()),
        "image": image,
        "image_sha": sha(image.read_bytes()),
        "ready": "READY",
    }


def transcript():
    return (
        b"ESP-ROM:esp32c3-api1-20210207\r\n"
        b"I (20) boot: ESP-IDF v5.5.2 2nd stage bootloader\r\n"
        b"I (60) boot: Loaded app from partition at offset 0x10000\r\n"
        b"READY\r\nBUILD abc\r\nHEARTBEAT 00000001\r\nHEARTBEAT 00000002\r\nHEARTBEAT 00000003\r\n"
    )


@pytest.mark.parametrize(
    "position", [0, 1, 2, 3, 12, 23, 34, 47, 50, 0x8000, 0x8065, 0x9000, 0x10000, 0x10022, 0x120000]
)
def test_corrupt_or_unsupported_flash_rejected(position):
    data = bytearray(merged())
    data[position] ^= 1
    with pytest.raises(SimError):
        flash_image.flash_info(bytes(data))


def test_truncated_and_extra_storage_rejected():
    for raw in (merged()[:-1], merged() + b"\xff"):
        with pytest.raises(SimError):
            flash_image.flash_info(raw)


def test_flash_plan_does_not_execute_and_rehashed_extra_fields_fail(finputs, tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("offline process start"))
    path = tmp_path / "plan.json"
    doc = flash.make_plan(**finputs)
    write_json(path, doc)
    assert flash.load_plan(path, sha(path.read_bytes())) == doc
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    for change in (
        {"physical_bridges": ["COM11"]},
        {"retries": 1},
        {"executables_started": 0},
        {"extra_args": ["-nic", "user"]},
        {"firmware": doc["firmware"] | {"flash_size": True}},
    ):
        mutated = tmp_path / ("mutated-" + str(len(list(tmp_path.iterdir()))) + ".json")
        write_json(mutated, doc | change)
        with pytest.raises(SimError):
            flash.load_plan(mutated, sha(mutated.read_bytes()))


@pytest.mark.parametrize(
    "fault,code",
    [
        ("none", None),
        ("build", "ASSERTION_FAILED"),
        ("rom", "BOOT_FAILED"),
        ("panic", "ASSERTION_FAILED"),
        ("diagnostic", "MODEL_COVERAGE_FAILED"),
        ("timeout", "TIMEOUT"),
        ("efuse", "INPUT_CHANGED"),
        ("input", "INPUT_CHANGED"),
    ],
)
def test_private_storage_native_verdicts_and_saved_evidence(
    finputs, tmp_path, monkeypatch, fault, code
):
    path = tmp_path / "plan.json"
    write_json(path, flash.make_plan(**finputs))
    original = finputs["image"].read_bytes()

    def execute(argv, directory, *args):
        assert "-kernel" not in argv and argv[argv.index("-nic") + 1] == "none"
        assert (directory / "flash-input.bin").read_bytes() == original
        assert (directory / "flash-work.bin").read_bytes() == original
        assert (directory / "efuse-work.bin").read_bytes() == flash.EFUSE
        uart = transcript()
        if fault == "build":
            uart = uart.replace(b"BUILD abc", b"BUILD old")
        elif fault == "rom":
            uart = uart.split(b"\r\n", 1)[1]
        elif fault == "panic":
            uart = uart.replace(b"READY", b"Guru Meditation\r\nREADY")
        elif fault == "efuse":
            (directory / "efuse-work.bin").write_bytes(bytes(1024))
        elif fault == "input":
            finputs["image"].write_bytes(b"changed")
        (directory / "backend.stdout.bin").write_bytes(b"")
        (directory / "backend.stderr.bin").write_bytes(
            b"Adding SPI flash device\n" + (b"unimplemented\n" if fault == "diagnostic" else b"")
        )
        (directory / "uart.bin").write_bytes(uart)
        return {
            "pid": 123,
            "execution_resumed": True,
            "error": "TIMEOUT" if fault == "timeout" else None,
            "exit_code": 0,
            "forced_termination": fault == "timeout",
            "cleanup": {
                "process_exited": True,
                "job_active_processes": 0,
                "capture_threads_stopped": True,
            },
        }

    monkeypatch.setattr(flash, "execute", execute)
    directory = tmp_path / "native run"
    report = flash.run(path, sha(path.read_bytes()), directory)
    assert report["ok"] is (code is None)
    if code:
        assert report["errors"][0]["code"] == code
    else:
        for index, patch in enumerate(
            (
                {"security_features_verified": True},
                {"model_coverage_complete": True},
                {"status": "passed_with_model_limits"},
                {"executables_started": False},
                {"process": report["process"] | {"execution_resumed": False}},
                {"backend_argv": report["backend_argv"] + ["-gdb", "tcp::3333"]},
            )
        ):
            changed = directory / f"changed-report-{index}.json"
            write_json(changed, report | patch)
            with pytest.raises(SimError):
                flash.inspect_report(changed, sha(changed.read_bytes()))
    copied = tmp_path / "moved"
    shutil.copytree(directory, copied)
    saved = copied / "report.json"
    assert flash.inspect_report(saved, sha(saved.read_bytes()))["ok"]
    (copied / "flash-work.bin").write_bytes(b"tampered")
    with pytest.raises(SimError):
        flash.inspect_report(saved, sha(saved.read_bytes()))
    if fault != "input":
        assert finputs["image"].read_bytes() == original


@pytest.mark.parametrize("alter", ["boot-order", "second-ready", "incomplete", "gap", "error-log"])
def test_boot_and_application_windows_are_both_required(alter):
    raw = transcript()
    if alter == "boot-order":
        raw = raw.replace(b"READY\r\n", b"").replace(b"ESP-ROM:", b"READY\r\nESP-ROM:")
    elif alter == "second-ready":
        raw += b"READY\r\n"
    elif alter == "incomplete":
        raw = raw[:-1]
    elif alter == "gap":
        raw = raw.replace(b"00000002", b"00000004")
    else:
        raw = raw.replace(b"READY\r\n", b"E (23) spi_flash: error\r\nREADY\r\n")
    with pytest.raises(SimError):
        flash.check_output(
            b"",
            b"Adding SPI flash device\n",
            raw,
            {"ready": "READY", "build": "BUILD abc", "heartbeat_prefix": "HEARTBEAT", "minimum": 3},
        )


def test_reviewed_diagnostics_are_visible_bounded_and_do_not_cover_other_errors():
    assertions = {
        "ready": "READY",
        "build": "BUILD abc",
        "heartbeat_prefix": "HEARTBEAT",
        "minimum": 3,
    }
    message = "M25P80: Unknown cmd 5a"
    raw = ("Adding SPI flash device\n" + message + "\n").encode()
    result = flash.check_output(b"", raw, transcript(), assertions)
    assert result["model_diagnostics"] == [
        {"message": message, "count": 1, "meaning": "Flash SFDP command unsupported"}
    ]
    for tail in (message + "\n", "M25P80: Unknown cmd 01\n", "Invalid write at addr 0x70000000\n"):
        with pytest.raises(SimError) as error:
            flash.check_output(b"", raw + tail.encode(), transcript(), assertions)
        assert error.value.code == "MODEL_COVERAGE_FAILED"
