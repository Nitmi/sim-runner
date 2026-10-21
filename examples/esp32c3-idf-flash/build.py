"""Build a pinned SDK fixture and merged Flash image, without device commands."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

SDK_COMMIT = "30aaf64524299d3bde422ca9a2848090d1bc5d0f"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(args, *, source=None, target="esp32c3"):
    source = source or Path(__file__).resolve().parent
    assert target in ("esp32c3", "esp32s3"), "Unsupported fixture target"
    sdk = args.idf_path.resolve()
    python = args.python.resolve()
    tools = args.tools_path.resolve()
    output = args.output.resolve()
    assert not output.exists(), "Use a new build directory"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.build_tag), "Invalid build tag"
    revision = (
        subprocess.check_output(["git", "-C", str(sdk), "rev-parse", "HEAD"], timeout=30)
        .decode()
        .strip()
    )
    assert revision == SDK_COMMIT, "Requires ESP-IDF v5.5.2 pinned source"
    submodules = {}
    for name in (
        "components/mbedtls/mbedtls",
        "components/bootloader/subproject/components/micro-ecc/micro-ecc",
        "components/mqtt/esp-mqtt",
        "components/heap/tlsf",
    ):
        expected = (
            subprocess.check_output(["git", "-C", str(sdk), "ls-tree", "HEAD", name], timeout=30)
            .decode()
            .split()[2]
        )
        actual = (
            subprocess.check_output(["git", "-C", str(sdk / name), "rev-parse", "HEAD"], timeout=30)
            .decode()
            .strip()
        )
        assert actual == expected, f"Initialize the pinned required submodule: {name}"
        submodules[name] = actual
    source_hashes = {
        p.relative_to(source).as_posix(): sha(p)
        for p in source.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    identity = hashlib.sha256(
        json.dumps(
            {
                "source": source_hashes,
                "sdk": revision,
                "variant": args.variant,
                "tag": args.build_tag,
                "builder_sha256": sha(Path(__file__)),
                "target": target,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    output.mkdir(parents=True)
    project = output / "project"
    project.mkdir()
    for name in ("CMakeLists.txt", "sdkconfig.defaults", "main/CMakeLists.txt", "main/main.c"):
        target_file = project / name
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target_file)
    variant = ("normal", "hang", "panic", "missing-core1").index(args.variant)
    (project / "main/build_identity.h").write_text(
        f'#define SIM_BUILD_ID "{identity}"\n#define SIM_VARIANT {variant}\n', encoding="ascii"
    )
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        environment.pop(key, None)
    environment.update(
        IDF_PATH=str(sdk),
        IDF_TOOLS_PATH=str(tools),
        IDF_PYTHON_ENV_PATH=str(python.parent.parent),
        IDF_CCACHE_ENABLE="0",
        IDF_COMPONENT_MANAGER="0",
        IDF_SKIP_CHECK_SUBMODULES="1",
        PYTHONUTF8="1",
    )
    binaries = []
    compiler = "xtensa-esp32s3-elf-gcc.exe" if target == "esp32s3" else "riscv32-esp-elf-gcc.exe"
    for executable in (compiler, "cmake.exe", "ninja.exe"):
        paths = list((tools / "tools").rglob(executable))
        assert len(paths) == 1, f"Select one installed {executable}"
        binaries.append(paths[0].parent)
    environment["PATH"] = (
        os.pathsep.join(map(str, [python.parent, *binaries])) + os.pathsep + environment["PATH"]
    )
    commands = [
        [
            str(python),
            str(sdk / "tools/idf.py"),
            "-C",
            str(project),
            "-B",
            str(output / "build"),
            "-DIDF_TARGET=" + target,
            "build",
        ],
        [
            str(python),
            "-m",
            "esptool",
            "--chip",
            target,
            "merge_bin",
            "--output",
            str(output / "flash.bin"),
            "--fill-flash-size",
            "4MB",
            "@flash_args",
        ],
    ]
    calls = []
    for index, command in enumerate(commands):
        cwd = project if index == 0 else output / "build"
        with (output / f"build-{index}.log").open("wb") as log:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                check=False,
            )
        calls.append({"argv": command, "cwd": str(cwd), "exit_code": result.returncode})
        (output / "calls.json").write_text(json.dumps(calls, indent=2))
        assert result.returncode == 0, f"Build failed; see build-{index}.log"
    artifacts = {
        name: {"sha256": sha(output / name), "bytes": (output / name).stat().st_size}
        for name in (
            "flash.bin",
            "build/sim_idf_flash.elf",
            "build/sim_idf_flash.bin",
            "build/bootloader/bootloader.bin",
            "build/partition_table/partition-table.bin",
            "build/flasher_args.json",
            "project/sdkconfig",
        )
    }
    manifest = {
        "schema_version": "embedded-sim-runner.idf-fixture.v1",
        "sdk_version": "5.5.2",
        "sdk_commit": revision,
        "target": target,
        "builder_sha256": sha(Path(__file__)),
        "required_submodules": submodules,
        "source_sha256": source_hashes,
        "build_id": identity,
        "variant": args.variant,
        "artifacts": artifacts,
        "hardware_access": False,
    }
    (output / "fixture.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


def main(*, source=None, target="esp32c3"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idf-path", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--tools-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    variants = ("normal", "hang", "panic") + (("missing-core1",) if target == "esp32s3" else ())
    parser.add_argument("--variant", choices=variants, default="normal")
    parser.add_argument("--build-tag", default="a")
    build(parser.parse_args(), source=source, target=target)


if __name__ == "__main__":
    main()
