"""Explicit synthetic S3 evidence. Actual SDK/CPU acceptance is separate."""

import json
import shutil
from pathlib import Path

import pytest
from test_qemu import qinputs as qinputs
from test_qemu_flash import merged

from sim_runner import flash_image, qemu_flash, qemu_s3
from sim_runner.cli import main
from sim_runner.common import SimError, sha, write_json
from sim_runner.plan import runtime


@pytest.fixture
def s3inputs(qinputs, monkeypatch):
    info = json.loads(qinputs["lock"].read_bytes())
    root = Path(info["root"])
    (root / info["executable"]).rename(root / qemu_s3.EXECUTABLE)
    rom = root / qemu_s3.ROM
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"synthetic S3 ROM, never executed")
    monkeypatch.setattr(qemu_s3, "ROM_SHA", sha(rom.read_bytes()))
    info.update(
        backend=qemu_s3.BACKEND,
        executable=qemu_s3.EXECUTABLE,
        archive_sha256=qemu_s3.ARCHIVE_SHA256,
        files={
            p.relative_to(root).as_posix(): sha(p.read_bytes())
            for p in root.rglob("*")
            if p.is_file()
        },
    )
    qinputs["lock"].write_text(json.dumps(info))
    qinputs["image"].write_bytes(merged(chip_id=9))
    return qinputs | {
        "ready": "READY",
        "lock_sha": sha(qinputs["lock"].read_bytes()),
        "image_sha": sha(qinputs["image"].read_bytes()),
    }


def transcript():
    return (
        b"ESP-ROM:esp32s3-20210327\r\n"
        b"I (0) boot: ESP-IDF v5.5.2 2nd stage bootloader\r\n"
        b"I (1) boot: Loaded app from partition at offset 0x10000\r\n"
        b"SIM_IDF_S3_CORE0 v1\r\nSIM_IDF_S3_CORE1 v1\r\n"
        b"READY\r\nBUILD abc\r\nHEARTBEAT 00000001\r\n"
        b"HEARTBEAT 00000002\r\nHEARTBEAT 00000003\r\n"
    )


def test_s3_plan_backend_and_chip_are_separate(s3inputs, monkeypatch, tmp_path):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline spawn"))
    doc = qemu_s3.make_plan(**s3inputs)
    assert doc["profile"] == qemu_s3.PROFILE and doc["backend"] == qemu_s3.BACKEND
    path = tmp_path / "s3-plan.json"
    write_json(path, doc)
    assert qemu_s3.load_plan(path, sha(path.read_bytes())) == doc
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    with pytest.raises(SimError):
        qemu_flash.make_plan(**s3inputs)
    with pytest.raises(SimError):
        runtime(s3inputs["lock"], s3inputs["lock_sha"], "espressif-qemu")
    with pytest.raises(SimError):
        flash_image.flash_info(s3inputs["image"].read_bytes())
    with pytest.raises(SimError):
        qemu_s3.flash_info(merged())
    altered = doc | {"physical_bridges": ["serial"]}
    changed = tmp_path / "changed.json"
    write_json(changed, altered)
    with pytest.raises(SimError):
        qemu_s3.load_plan(changed, sha(changed.read_bytes()))


@pytest.mark.parametrize(
    "fault", [None, "build", "panic", "core", "rom", "diagnostic", "efuse", "timeout", "input"]
)
def test_s3_lifecycle_storage_offline_integrity(s3inputs, monkeypatch, tmp_path, fault):
    plan = tmp_path / "plan.json"
    write_json(plan, qemu_s3.make_plan(**s3inputs))
    original = s3inputs["image"].read_bytes()

    def execute(argv, directory, *args):
        assert "-kernel" not in argv and argv[argv.index("-machine") + 1] == "esp32s3"
        assert argv[argv.index("-nic") + 1] == "none" and argv[argv.index("-m") + 1] == "32M"
        assert "driver=nvram.esp32s3.efuse,property=drive,value=efuse" in argv
        assert (directory / "flash-input.bin").read_bytes() == original
        assert (directory / "flash-work.bin").read_bytes() == original
        assert (directory / "efuse-work.bin").read_bytes() == qemu_s3.EFUSE
        uart = transcript()
        for name, before, after in (
            ("build", b"BUILD abc", b"BUILD old"),
            ("panic", b"READY", b"Guru Meditation\r\nREADY"),
            ("core", b"SIM_IDF_S3_CORE1 v1\r\n", b""),
            ("rom", b"ESP-ROM:esp32s3", b"ESP-ROM:esp32c3"),
        ):
            if fault == name:
                uart = uart.replace(before, after)
        if fault == "efuse":
            (directory / "efuse-work.bin").write_bytes(bytes(1024))
        if fault == "input":
            s3inputs["image"].write_bytes(b"changed")
        (directory / "backend.stdout.bin").write_bytes(b"")
        (directory / "backend.stderr.bin").write_bytes(
            b"Adding SPI flash device\n" + (b"unknown MMIO\n" if fault == "diagnostic" else b"")
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

    monkeypatch.setattr(qemu_flash, "execute", execute)
    directory = tmp_path / "native S3"
    report = qemu_s3.run(plan, sha(plan.read_bytes()), directory)
    assert report["ok"] is (fault is None)
    assert report["backend"] == qemu_s3.BACKEND and report["model_coverage_complete"] is False
    if fault is None:
        assert report["assertions"]["app_core_markers"] == [0, 1]
        for index, patch in enumerate(
            (
                {"backend": "espressif-qemu"},
                {"profile": qemu_flash.PROFILE},
                {"backend_argv": report["backend_argv"] + ["-gdb", "tcp::3333"]},
                {"security_features_verified": True},
                {"virtual_time": report["virtual_time"] | {"budget_enforced": 0}},
            )
        ):
            changed = directory / f"altered-{index}.json"
            write_json(changed, report | patch)
            with pytest.raises(SimError):
                qemu_s3.inspect_report(changed, sha(changed.read_bytes()))
    moved = tmp_path / "moved evidence"
    shutil.copytree(directory, moved)
    saved = moved / "report.json"
    assert (
        qemu_s3.inspect_report(saved, sha(saved.read_bytes()))["report_status"] == report["status"]
    )
    (moved / "flash-work.bin").write_bytes(b"changed")
    with pytest.raises(SimError):
        qemu_s3.inspect_report(saved, sha(saved.read_bytes()))
    if fault != "input":
        assert s3inputs["image"].read_bytes() == original


@pytest.mark.parametrize(
    "alter", ["repeat-core", "reverse-core", "core-after-ready", "error", "gap", "c3-pmp"]
)
def test_s3_output_never_borrows_c3_or_infers_core(alter):
    uart = transcript()
    diagnostics = b"Adding SPI flash device\n"
    if alter == "repeat-core":
        uart = uart.replace(b"SIM_IDF_S3_CORE1", b"SIM_IDF_S3_CORE0")
    elif alter == "reverse-core":
        uart = (
            uart.replace(b"CORE0", b"CORE2").replace(b"CORE1", b"CORE0").replace(b"CORE2", b"CORE1")
        )
    elif alter == "core-after-ready":
        uart = uart.replace(b"SIM_IDF_S3_CORE1 v1\r\n", b"").replace(
            b"READY\r\n", b"READY\r\nSIM_IDF_S3_CORE1 v1\r\n"
        )
    elif alter == "error":
        uart = uart.replace(b"READY\r\n", b"E (3) error\r\nREADY\r\n")
    elif alter == "gap":
        uart = uart.replace(b"00000002", b"00000009")
    else:
        diagnostics += b"ignoring pmpcfg write - locked\n"
    with pytest.raises(SimError):
        qemu_s3.check_output(
            b"",
            diagnostics,
            uart,
            {"ready": "READY", "build": "BUILD abc", "heartbeat_prefix": "HEARTBEAT", "minimum": 3},
        )


@pytest.mark.parametrize("message", list(qemu_s3.KNOWN_DIAGNOSTICS))
def test_s3_reviewed_diagnostics_are_retained_and_bounded(message):
    limit, meaning = qemu_s3.KNOWN_DIAGNOSTICS[message]
    assertions = {
        "ready": "READY",
        "build": "BUILD abc",
        "heartbeat_prefix": "HEARTBEAT",
        "minimum": 3,
    }
    stderr = ("Adding SPI flash device\n" + (message + "\n") * limit).encode()
    observed = qemu_s3.check_output(b"", stderr, transcript(), assertions)
    assert observed["model_diagnostics"] == [
        {"message": message, "count": limit, "meaning": meaning}
    ]
    with pytest.raises(SimError) as failure:
        qemu_s3.check_output(b"", stderr + (message + "\n").encode(), transcript(), assertions)
    assert failure.value.code == "MODEL_COVERAGE_FAILED"
    rejected = (
        b"Adding SPI flash device\n"
        b"Invalid read at addr 0x102010, size 4, region 'er', reason: rejected\n"
    )
    with pytest.raises(SimError):
        qemu_s3.check_output(b"", rejected, transcript(), assertions)


def test_s3_explicit_panic_is_not_hidden_by_debug_poll_diagnostics():
    message = "Invalid read at addr 0x10200C, size 4, region 'er', reason: rejected\n"
    uart = transcript() + b"abort() was called\r\n"
    with pytest.raises(SimError) as failure:
        qemu_s3.check_output(
            b"",
            message.encode() * 20,
            uart,
            {"ready": "READY", "build": "BUILD abc", "heartbeat_prefix": "HEARTBEAT", "minimum": 3},
        )
    assert failure.value.code == "ASSERTION_FAILED"
