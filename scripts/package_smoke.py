"""Development-only installed-wheel checks; synthetic files, no backend execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    wheels = list(args.dist_dir.glob("*.whl"))
    assert len(wheels) == 1, "Expected one wheel"
    wheel = wheels[0].resolve()
    source = Path(__file__).resolve().parents[1]
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    root = args.output_dir.absolute()
    root.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    calls = []

    def call(label, command, expected=0):
        result = subprocess.run(
            list(map(str, command)),
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=90,
            check=False,
        )
        (root / (label + ".stdout")).write_bytes(result.stdout)
        (root / (label + ".stderr")).write_bytes(result.stderr)
        calls.append(
            {"name": label, "argv": list(map(str, command)), "exit_code": result.returncode}
        )
        (root / "calls.json").write_text(json.dumps(calls, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == expected, (label, result.stdout, result.stderr)
        return result.stdout

    uv = shutil.which("uv")
    assert uv, "uv required for this development helper"
    venv = root / "venv"
    call("venv", [uv, "venv", "--python", sys.executable, venv])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    launcher = venv / ("Scripts/sim-runner.exe" if os.name == "nt" else "bin/sim-runner")
    call("install", [uv, "pip", "install", "--python", python, "--no-index", "--no-deps", wheel])
    identity = json.loads(
        call(
            "identity",
            [
                python,
                "-I",
                "-c",
                "import sim_runner,sys,json;print(json.dumps({'version':sim_runner.__version__,"
                "'path':sim_runner.__file__,'python':sys.version}))",
            ],
        )
    )
    assert identity["version"] == version and Path(identity["path"]).is_relative_to(venv)
    assert (
        call("launcher-version", [launcher, "--version"]).strip()
        == ("sim-runner " + version).encode()
    )
    call("help", [python, "-I", "-m", "sim_runner", "--help"])
    # Run the installed offline CLI with an audit hook that rejects process creation.
    # Only this smoke helper launches Python/uv; no dummy runtime may be executed.
    guard = (
        "import sys,runpy\n"
        "def audit(event,args):\n"
        " if event in ('subprocess.Popen','os.system','os.posix_spawn','os.fork'):\n"
        "  raise RuntimeError('Offline smoke attempted process creation')\n"
        "sys.addaudithook(audit)\n"
        "sys.argv=['sim-runner',*sys.argv[1:]]\n"
        "runpy.run_module('sim_runner',run_name='__main__')\n"
    )

    def offline(name, arguments, expected=0):
        doc = json.loads(call(name, [python, "-I", "-c", guard, *arguments], expected))
        assert doc["hardware_access"] is False and doc["physical_board_verified"] is False
        if expected == 0:
            assert doc["ok"] is True and doc["executables_started"] is False
        return doc

    runtime = root / "synthetic runtime"
    runtime.mkdir()
    (runtime / "renode.exe").write_bytes(b"SYNTHETIC ONLY, NEVER EXECUTE")
    lock = root / "runtime.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": "embedded-sim-runner.runtime-lock.v1",
                "backend": "renode",
                "version": "1.17.0",
                "root": str(runtime),
                "executable": "renode.exe",
                "files": {"renode.exe": digest(runtime / "renode.exe")},
            }
        ),
        encoding="utf-8",
    )
    firmware = root / "synthetic.bin"
    firmware.write_bytes(
        (0x20040000).to_bytes(4, "little") + (0x101).to_bytes(4, "little") + bytes(256)
    )
    plan = root / "plan.json"
    common = ["--runtime-lock", str(lock), "--runtime-sha256", digest(lock)]
    offline("doctor", ["doctor", *common])
    offline(
        "plan",
        [
            "plan",
            *common,
            "--firmware",
            str(firmware),
            "--firmware-sha256",
            digest(firmware),
            "--ready-line",
            "SYNTHETIC_READY",
            "--build-line",
            "SYNTHETIC_BUILD",
            "--heartbeat-prefix",
            "SYNTHETIC_HEARTBEAT",
            "--output",
            str(plan),
        ],
    )
    saved = plan.read_bytes()
    inspected = offline("inspect", ["inspect-plan", "--plan", str(plan), "--sha256", digest(plan)])
    assert inspected["profile"] == "nrf52840-uart-v1"
    # Fixed sensor model assets must also work from the wheel, without any backend launch.
    for profile, fault in (
        *(
            ("nrf52840-i2c-sensor-v1", f)
            for f in ("none", "temperature-stuck", "temperature-offset")
        ),
        *(("nrf52840-spi-sensor-v1", f) for f in ("none", "temperature-stuck", "frame-slip")),
        *(("nrf52840-watchdog-v1", f) for f in ("none", "feed-timer-stop", "watchdog-stop")),
    ):
        label = profile + "-" + fault
        sensor_plan = root / (label + ".plan.json")
        offline(
            "sensor-plan-" + label,
            [
                "plan",
                *common,
                "--profile",
                profile,
                "--fault",
                fault,
                "--firmware",
                str(firmware),
                "--firmware-sha256",
                digest(firmware),
                "--ready-line",
                "SYNTHETIC_READY",
                "--build-line",
                "SYNTHETIC_BUILD",
                "--heartbeat-prefix",
                "SYNTHETIC_SAMPLE",
                "--output",
                str(sensor_plan),
            ],
        )
        checked = offline(
            "sensor-inspect-" + label,
            ["inspect-plan", "--plan", str(sensor_plan), "--sha256", digest(sensor_plan)],
        )
        assert checked["profile"] == profile
    assert not offline(
        "wrong-hash", ["inspect-plan", "--plan", str(plan), "--sha256", "0" * 64], 1
    )["ok"]
    firmware.write_bytes(b"changed")
    assert not offline(
        "input-drift", ["inspect-plan", "--plan", str(plan), "--sha256", digest(plan)], 1
    )["ok"]
    assert plan.read_bytes() == saved
    package = Path(identity["path"]).parent
    verified = 0
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            if item.filename.startswith("sim_runner/") and not item.is_dir():
                assert (package.parent / item.filename).read_bytes() == archive.read(item)
                verified += 1
    assert verified == 30, "Expected 20 modules and ten fixed model/host assets"
    report = {
        "ok": True,
        "version": version,
        "wheel_sha256": digest(wheel),
        "identity": identity,
        "commands": len(calls),
        "package_files_verified": verified,
        "hardware_access": False,
        "emulator_started": False,
        "physical_board_verified": False,
    }
    (root / "package-smoke.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
