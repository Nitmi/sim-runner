"""Explicit synthetic ELF/runtime/transcripts; actual QEMU uses accept_qemu.py."""

import shutil
import struct

import pytest

from sim_runner import qemu
from sim_runner.cli import main
from sim_runner.common import SimError, sha, write_json


def elf(segments=None, entry=0x40380000):
    segments = segments or [(0x40380000, 0x40380000, 4, 4, 5)]
    data = bytearray(512)
    struct.pack_into(
        "<16sHHIIIIIHHHHHH",
        data,
        0,
        b"\x7fELF\x01\x01\x01" + bytes(9),
        2,
        243,
        1,
        entry,
        52,
        0,
        1,
        52,
        32,
        len(segments),
        0,
        0,
        0,
    )
    for index, (virt, phys, size, memory, flags) in enumerate(segments):
        struct.pack_into(
            "<8I", data, 52 + index * 32, 1, 256 + index * 16, virt, phys, size, memory, flags, 1
        )
    return bytes(data)


@pytest.fixture
def qinputs(inputs):
    root = inputs["lock"].parent / "runtime"
    (root / "renode.exe").unlink()
    (root / "bin").mkdir()
    (root / "bin/qemu-system-riscv32.exe").write_bytes(b"synthetic QEMU, never launched")
    write_json(
        root.parent / "qemu-lock.json",
        {
            "schema_version": "embedded-sim-runner.runtime-lock.v1",
            "backend": qemu.BACKEND,
            "version": "esp-develop-9.2.2-20260417",
            "root": str(root),
            "executable": "bin/qemu-system-riscv32.exe",
            "archive_sha256": "697aa4800a1f52be0b1693b30e22a684f7ea93c46c489e619384cae7b0e9b87b",
            "files": {
                p.relative_to(root).as_posix(): sha(p.read_bytes())
                for p in root.rglob("*")
                if p.is_file()
            },
        },
    )
    image = root.parent / "firmware.elf"
    image.write_bytes(elf())
    return inputs | {
        "lock": root.parent / "qemu-lock.json",
        "lock_sha": sha((root.parent / "qemu-lock.json").read_bytes()),
        "image": image,
        "image_sha": sha(image.read_bytes()),
    }


def test_qemu_plan_and_cli_are_offline(qinputs, tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline command spawned"))
    path = tmp_path / "plan.json"
    doc = qemu.make_plan(**qinputs)
    write_json(path, doc)
    assert qemu.load_plan(path, sha(path.read_bytes())) == doc
    assert (
        main(
            [
                "doctor",
                "--backend",
                qemu.BACKEND,
                "--runtime-lock",
                str(qinputs["lock"]),
                "--runtime-sha256",
                qinputs["lock_sha"],
            ]
        )
        == 0
    )
    assert main(["inspect-plan", "--plan", str(path), "--sha256", sha(path.read_bytes())]) == 0
    assert (
        main(
            [
                "plan",
                "--backend",
                qemu.BACKEND,
                "--runtime-lock",
                str(qinputs["lock"]),
                "--runtime-sha256",
                qinputs["lock_sha"],
                "--firmware",
                str(qinputs["image"]),
                "--firmware-sha256",
                qinputs["image_sha"],
                "--ready-line",
                "READY",
                "--build-line",
                "BUILD",
                "--heartbeat-prefix",
                "HEARTBEAT",
                "--virtual-ms",
                "1",
                "--output",
                str(tmp_path / "reject.json"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "reject.json").exists()


@pytest.mark.parametrize(
    "offset,fmt,value",
    [
        (4, "B", 2),
        (5, "B", 2),
        (16, "H", 3),
        (18, "H", 40),
        (24, "I", 0x42000000),
        (28, "I", 500),
        (36, "I", 4),
        (40, "H", 51),
        (42, "H", 31),
        (44, "H", 65),
        (52, "I", 3),
        (56, "I", 510),
        (64, "I", 0x42000000),
        (72, "I", 0),
        (76, "I", 8),
    ],
)
def test_unsupported_or_malformed_elf_rejected(offset, fmt, value):
    image = bytearray(elf())
    struct.pack_into("<" + fmt, image, offset, value)
    with pytest.raises(SimError):
        qemu.elf_info(bytes(image))


def test_load_segments_cannot_overlap_through_dram_alias():
    image = elf([(0x40380000, 0x40380000, 4, 64, 5), (0x3FC80010, 0x3FC80010, 4, 4, 6)])
    with pytest.raises(SimError, match="overlapping"):
        qemu.elf_info(image)


@pytest.mark.parametrize(
    "change",
    [
        {"physical_bridges": ["COM11"]},
        {"retries": 1},
        {"extra_args": ["-nic", "user"]},
        {"hardware_access": True},
        {"host_timeout_seconds": True},
        {"virtual_ms": 10},
        {"profile": "esp32s3"},
        {"backend": "renode"},
    ],
)
def test_qemu_plan_rejects_rehashed_mutations(qinputs, tmp_path, change):
    path = tmp_path / "plan.json"
    write_json(path, qemu.make_plan(**qinputs) | change)
    with pytest.raises(SimError):
        qemu.load_plan(path, sha(path.read_bytes()))


@pytest.mark.parametrize(
    "fault,code",
    [
        ("none", None),
        ("warning", "MODEL_COVERAGE_FAILED"),
        ("mmio", "MODEL_COVERAGE_FAILED"),
        ("build", "ASSERTION_FAILED"),
        ("nonzero", "BACKEND_FAILED"),
        ("timeout", "TIMEOUT"),
        ("changed", "INPUT_CHANGED"),
    ],
)
def test_qemu_failures_preserve_evidence(qinputs, tmp_path, monkeypatch, valid_uart, fault, code):
    plan = tmp_path / "plan.json"
    write_json(plan, qemu.make_plan(**qinputs))

    def execute(argv, directory, *args):
        assert argv == qemu.command(qinputs["lock"].parent / "runtime/bin/qemu-system-riscv32.exe")
        log = b"Not initializing SPI Flash\nLoading kernel at address 0x40380000\n"
        if fault == "warning":
            log += b"warning: unsupported peripheral\n"
        if fault == "mmio":
            log += b"Invalid write at addr 0x70000000\n"
        (directory / "backend.stdout.bin").write_bytes(b"")
        (directory / "backend.stderr.bin").write_bytes(log)
        (directory / "uart.bin").write_bytes(
            valid_uart.replace(b"BUILD abc", b"BUILD old") if fault == "build" else valid_uart
        )
        if fault == "changed":
            (qinputs["lock"].parent / "runtime/readme.txt").write_bytes(b"changed")
        return {
            "pid": 1,
            "exit_code": 7 if fault == "nonzero" else 0,
            "error": "TIMEOUT" if fault == "timeout" else None,
            "forced_termination": fault == "timeout",
            "cleanup": {
                "process_exited": True,
                "job_active_processes": 0,
                "capture_threads_stopped": True,
            },
        }

    monkeypatch.setattr(qemu, "execute", execute)
    directory = tmp_path / "run with spaces"
    report = qemu.run(plan, sha(plan.read_bytes()), directory)
    assert report["ok"] is (code is None)
    assert report["hardware_access"] is False
    if code:
        assert report["errors"][0]["code"] == code
    copied = tmp_path / "movable evidence"
    shutil.copytree(directory, copied)
    saved = copied / "report.json"
    assert qemu.inspect_report(saved, sha(saved.read_bytes()))["ok"]
    (copied / "uart.bin").write_bytes(b"tampered")
    with pytest.raises(SimError, match="SHA-256"):
        qemu.inspect_report(saved, sha(saved.read_bytes()))


def test_qemu_command_has_no_external_control_or_bridges(tmp_path):
    command = qemu.command(tmp_path / "qemu.exe")
    assert command[command.index("-nic") + 1] == "none"
    assert command[command.index("-monitor") + 1] == "none"
    assert "-no-user-config" in command and "-nodefaults" in command
    for word in ("-qmp", "-gdb", "-semihosting", "-device", "-drive", "-readconfig"):
        assert word not in command


@pytest.mark.parametrize(
    "log",
    [
        b"",
        b"Not initializing SPI Flash\n",
        b"Not initializing SPI Flash\nNot initializing SPI Flash\n"
        b"Loading kernel at address 0x40380000\n",
    ],
)
def test_qemu_startup_diagnostics_required(log):
    with pytest.raises(SimError):
        qemu.check_logs(log, b"")
