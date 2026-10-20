from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .common import SimError, require
from .winjob import Job


def execute(command: list[str], directory: Path, timeout: int, limit: int, memory_mib: int) -> dict:
    """Only the internal fixed Renode command is accepted by public run()."""
    require(os.name == "nt", "execution requires Windows x64", "UNSUPPORTED_PLATFORM")
    job = Job(memory_mib)
    process = None
    threads = []
    errors = []
    reason = None
    forced = False
    resumed = False
    started = time.monotonic()
    cleanup = {
        "process_exited": False,
        "job_active_processes": None,
        "capture_threads_stopped": False,
    }

    def capture(stream, path):
        count = 0
        try:
            with path.open("xb") as destination:
                while block := os.read(stream.fileno(), 4096):
                    accepted = min(len(block), max(0, limit - count))
                    destination.write(block[:accepted])
                    count += len(block)
                    if count > limit:
                        errors.append("OUTPUT_LIMIT")
                        return
        except OSError:
            errors.append("CAPTURE_FAILED")

    try:
        environment = dict(os.environ)
        # Direct supported host caches/temporaries to this run and disable shared
        # Renode cache/config writes. This is lifecycle isolation, not a sandbox.
        for key in (
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "DOTNET_CLI_HOME",
        ):
            location = directory / "host" / key
            location.mkdir(parents=True)
            environment[key] = str(location)
        environment["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        environment["RENODE_CI_MODE"] = "YES"
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=4 | 0x08000000,
        )
        for stream, name in (
            (process.stdout, "backend.stdout.bin"),
            (process.stderr, "backend.stderr.bin"),
        ):
            thread = threading.Thread(target=capture, args=(stream, directory / name), daemon=True)
            thread.start()
            threads.append(thread)
        job.assign_and_resume(process.pid)
        resumed = True
        while process.poll() is None or job.active():
            if errors:
                reason = errors[0]
                break
            if time.monotonic() - started >= timeout:
                reason = "TIMEOUT"
                break
            uart = directory / "uart.bin"
            if uart.exists() and uart.stat().st_size > limit:
                reason = "OUTPUT_LIMIT"
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        reason = "CANCELLED"
    except (OSError, SimError) as error:
        reason = getattr(error, "code", "PROCESS_START_FAILED")
    finally:
        if process is not None:
            if process.poll() is None or job.active():
                forced = True
                job.terminate()
                # Assignment may have failed while the process was suspended.
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=5)
                deadline = time.monotonic() + 5
                while job.active() and time.monotonic() < deadline:
                    time.sleep(0.01)
                cleanup["process_exited"] = process.poll() is not None
                cleanup["job_active_processes"] = job.active()
            except subprocess.TimeoutExpired:
                reason = "CLEANUP_FAILED"
            for thread in threads:
                thread.join(timeout=2)
            cleanup["capture_threads_stopped"] = all(not t.is_alive() for t in threads)
            for stream in (process.stdout, process.stderr):
                stream.close()
        else:
            cleanup.update(
                process_exited=True,
                job_active_processes=job.active(),
                capture_threads_stopped=True,
            )
        job.close()
    if not all(
        (
            cleanup["process_exited"],
            cleanup["job_active_processes"] == 0,
            cleanup["capture_threads_stopped"],
        )
    ):
        reason = "CLEANUP_FAILED"
    return {
        "pid": process.pid if process else None,
        "execution_resumed": resumed,
        "exit_code": process.returncode if process else None,
        "error": reason or (errors[0] if errors else None),
        "forced_termination": forced,
        "host_elapsed_seconds": round(time.monotonic() - started, 6),
        "cleanup": cleanup,
    }
