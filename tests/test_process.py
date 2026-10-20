import os
import sys

import pytest

from sim_runner.process import execute

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job backend")


def test_normal_exit_and_binary_capture(tmp_path):
    result = execute(
        [sys.executable, "-c", "import os;os.write(1,b'hello\\x00');os.write(2,b'error')"],
        tmp_path,
        10,
        4096,
        512,
    )
    assert result["error"] is None
    assert result["exit_code"] == 0
    assert not result["forced_termination"]
    assert (tmp_path / "backend.stdout.bin").read_bytes() == b"hello\x00"
    assert result["cleanup"]["job_active_processes"] == 0
    assert result["cleanup"]["capture_threads_stopped"]


def test_timeout_kills_owned_tree_including_grandchild(tmp_path):
    code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print('spawned',flush=True);time.sleep(60)"
    )
    result = execute([sys.executable, "-c", code], tmp_path, 1, 4096, 512)
    assert result["error"] == "TIMEOUT"
    assert result["forced_termination"]
    assert result["cleanup"]["job_active_processes"] == 0


def test_output_overflow_is_bounded_and_stopped(tmp_path):
    result = execute(
        [sys.executable, "-c", "import os,time;os.write(1,b'X'*65536);time.sleep(60)"],
        tmp_path,
        10,
        4096,
        512,
    )
    assert result["error"] == "OUTPUT_LIMIT"
    assert (tmp_path / "backend.stdout.bin").stat().st_size <= 4096
    assert result["cleanup"]["job_active_processes"] == 0


def test_nonzero_exit_is_not_masked(tmp_path):
    result = execute([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, 10, 4096, 512)
    assert result["exit_code"] == 7
    assert result["error"] is None


def test_job_memory_limit_prevents_large_commit(tmp_path):
    code = (
        "import sys\ntry:\n"
        " x=bytearray(800*1024*1024)\n"
        " print('LIMIT_NOT_ENFORCED',flush=True);sys.exit(9)\n"
        "except MemoryError:\n"
        " print('MEMORY_LIMIT_ENFORCED',flush=True);sys.exit(7)\n"
    )
    result = execute([sys.executable, "-c", code], tmp_path, 10, 4096, 512)
    assert result["exit_code"] == 7
    assert result["error"] is None
    assert (tmp_path / "backend.stdout.bin").read_bytes().strip() == b"MEMORY_LIMIT_ENFORCED"
    assert result["cleanup"]["job_active_processes"] == 0


def test_spawn_failure_does_not_leave_tree(tmp_path):
    result = execute([str(tmp_path / "missing.exe")], tmp_path, 10, 4096, 512)
    assert result["pid"] is None
    assert result["error"] is not None


def test_cancellation_stops_owned_tree(tmp_path, monkeypatch):
    import time

    real_sleep = time.sleep
    interrupted = False

    def interrupt_once(seconds):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr("sim_runner.process.time.sleep", interrupt_once)
    result = execute([sys.executable, "-c", "import time;time.sleep(60)"], tmp_path, 10, 4096, 512)
    assert result["error"] == "CANCELLED"
    assert result["cleanup"]["job_active_processes"] == 0
    assert result["cleanup"]["capture_threads_stopped"]
