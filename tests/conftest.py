import json

import pytest

from sim_runner.common import sha


@pytest.fixture
def inputs(tmp_path):
    """Synthetic inventory, never launched as Renode."""
    root = tmp_path / "runtime"
    root.mkdir(parents=True)
    (root / "renode.exe").write_bytes(b"test-double-not-an-executable")
    (root / "readme.txt").write_bytes(b"explicit synthetic fixture")
    files = {
        p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in root.rglob("*") if p.is_file()
    }
    lock = tmp_path / "runtime-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": "embedded-sim-runner.runtime-lock.v1",
                "backend": "renode",
                "version": "1.17.0",
                "root": str(root),
                "executable": "renode.exe",
                "files": files,
            }
        )
    )
    image = tmp_path / "firmware.bin"
    image.write_bytes(
        (0x20040000).to_bytes(4, "little") + (0x101).to_bytes(4, "little") + bytes(256)
    )
    return {
        "lock": lock,
        "lock_sha": sha(lock.read_bytes()),
        "image": image,
        "image_sha": sha(image.read_bytes()),
        "ready": "READY v1",
        "build": "BUILD abc",
        "heartbeat": "HEARTBEAT",
    }


@pytest.fixture
def assertions():
    return {
        "ready": "READY v1",
        "build": "BUILD abc",
        "heartbeat_prefix": "HEARTBEAT",
        "minimum": 3,
    }


@pytest.fixture
def valid_uart():
    return (
        b"READY v1\r\nBUILD abc\r\nHEARTBEAT 00000001\r\n"
        b"HEARTBEAT 00000002\r\nHEARTBEAT 00000003\r\n"
    )
