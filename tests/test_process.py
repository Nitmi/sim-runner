import os
import sys
from pathlib import Path

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


def test_node_environment_drops_preload_and_search_options(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--import=never-load-untrusted-code")
    monkeypatch.setenv("NODE_PATH", "never-search-this-directory")
    monkeypatch.setenv("NODE_REPL_EXTERNAL_MODULE", "never-load-this-module")
    code = (
        "import os,json;print(json.dumps([os.environ.get(k) for k in "
        "('NODE_OPTIONS','NODE_PATH','NODE_REPL_EXTERNAL_MODULE')]))"
    )
    result = execute([sys.executable, "-c", code], tmp_path, 10, 4096, 512, node_environment=True)
    assert result["error"] is None and result["exit_code"] == 0
    assert (tmp_path / "backend.stdout.bin").read_bytes().strip() == b"[null, null, null]"


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


def test_uart_limit_while_native_writer_has_exclusive_file_handle(tmp_path):
    fixture = Path(__file__).with_name("exclusive_writer.py")
    result = execute(
        [sys.executable, str(fixture), str(tmp_path / "uart.bin")], tmp_path, 10, 4096, 512
    )
    assert result["error"] == "OUTPUT_LIMIT", result
    assert result["cleanup"]["job_active_processes"] == 0


def test_uart_path_can_appear_and_disappear_during_native_startup(tmp_path):
    code = (
        "from pathlib import Path;import sys\n"
        "p=Path(sys.argv[1])\n"
        "for i in range(3000):\n p.write_bytes(b'');p.unlink()\n"
    )
    result = execute(
        [sys.executable, "-c", code, str(tmp_path / "uart.bin")], tmp_path, 15, 4096, 512
    )
    assert result["error"] is None, result
    assert result["exit_code"] == 0


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
