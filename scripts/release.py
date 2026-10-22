"""Development-only source-bound release build/verification; no device access.

Build invokes Git/uv. Verify only parses bounded local files without extraction,
imports or subprocesses. Neither operation verifies publisher authenticity.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import email.parser
import gzip
import hashlib
import io
import json
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = "embedded-sim-runner.python-release.v1"
REPOSITORY = "Nitmi/sim-runner"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
REVISION = re.compile(r"[a-f0-9]{40}")
MAX_FILE = 8 * 1024 * 1024
RUNTIME_FILES = {
    "sim_runner/" + name
    for name in (
        "__init__.py",
        "__main__.py",
        "backend.py",
        "cli.py",
        "common.py",
        "flash_image.py",
        "gpio_timer.py",
        "gpio_fault.py",
        "inspect.py",
        "plan.py",
        "process.py",
        "qemu.py",
        "qemu_flash.py",
        "qemu_s3.py",
        "run.py",
        "winjob.py",
        "assets/nrf52840-uart.repl",
        "assets/nrf52840-gpio-timer.repl",
        "assets/esp32c3-uart.json",
        "assets/esp32c3-idf-flash.json",
        "assets/esp32s3-idf-flash.json",
    )
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read(path: Path, limit: int = MAX_FILE) -> bytes:
    metadata = path.lstat()
    require(
        stat.S_ISREG(metadata.st_mode) and not getattr(metadata, "st_file_attributes", 0) & 0x400,
        "Expected a regular non-reparse file",
    )
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "Release input exceeds limit")
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pairs(items: list) -> dict:
    result = {}
    for key, value in items:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def names(version: str) -> tuple[str, str]:
    return (
        f"embedded_sim_runner-{version}-py3-none-any.whl",
        f"embedded_sim_runner-{version}.tar.gz",
    )


def identity(version: object, revision: object) -> None:
    require(
        isinstance(version, str)
        and VERSION.fullmatch(version) is not None
        and isinstance(revision, str)
        and REVISION.fullmatch(revision) is not None,
        "Invalid release identity",
    )


def safe_member(name: str) -> None:
    require(
        bool(name)
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
        and not PurePosixPath(name).is_absolute()
        and all(part not in ("", ".", "..") for part in name.split("/")),
        "Unsafe archive member",
    )


def payloads(snapshots: dict[str, bytes], version: str) -> tuple[dict, dict]:
    """Parse in memory; never extract or import archive contents."""
    wheel_name, source_name = names(version)
    wheel = {}
    with zipfile.ZipFile(io.BytesIO(snapshots[wheel_name])) as archive:
        require(len(archive.infolist()) <= 128, "Too many wheel members")
        require(sum(item.file_size for item in archive.infolist()) <= MAX_FILE, "Wheel too large")
        for item in archive.infolist():
            safe_member(item.orig_filename)
            require(item.filename == item.orig_filename, "Normalized wheel member differs")
            require(
                item.filename not in wheel and not item.is_dir(), "Duplicate/directory wheel member"
            )
            mode = (item.external_attr >> 16) & 0o170000
            require(mode in (0, stat.S_IFREG), "Nonregular wheel member")
            wheel[item.filename] = archive.read(item)
    prefix = f"embedded_sim_runner-{version}.dist-info/"
    expected_metadata = {
        prefix + name
        for name in (
            "METADATA",
            "WHEEL",
            "entry_points.txt",
            "licenses/LICENSE",
            "RECORD",
        )
    }
    runtime_files = RUNTIME_FILES
    require(set(wheel) == runtime_files | expected_metadata, "Unexpected/missing wheel files")
    metadata = email.parser.BytesParser().parsebytes(wheel[prefix + "METADATA"])
    require(
        metadata.get_all("Name") == ["embedded-sim-runner"]
        and metadata.get_all("Version") == [version]
        and metadata.get_all("Requires-Python") == [">=3.11"],
        "Wheel identity/Python requirement differs",
    )
    require(
        all(
            ";" in item and item.split(";", 1)[1].strip() in ("extra == 'dev'", 'extra == "dev"')
            for item in metadata.get_all("Requires-Dist", [])
        ),
        "Runtime dependencies found",
    )
    wheel_metadata = email.parser.BytesParser().parsebytes(wheel[prefix + "WHEEL"])
    require(
        wheel_metadata.get_all("Wheel-Version") == ["1.0"]
        and wheel_metadata.get_all("Root-Is-Purelib") == ["true"]
        and wheel_metadata.get_all("Tag") == ["py3-none-any"],
        "Wheel platform contract differs",
    )
    entrypoints = configparser.ConfigParser(interpolation=None)
    entrypoints.read_string(wheel[prefix + "entry_points.txt"].decode())
    require(
        entrypoints.sections() == ["console_scripts"]
        and dict(entrypoints["console_scripts"]) == {"sim-runner": "sim_runner.cli:main"},
        "CLI entry points differ",
    )
    # RECORD must bind every installed payload, not only the outer ZIP.
    records = list(csv.reader(io.StringIO(wheel[prefix + "RECORD"].decode())))
    require(len(records) == len(wheel), "Wheel RECORD coverage differs")
    seen = set()
    for row in records:
        require(len(row) == 3 and row[0] in wheel and row[0] not in seen, "Invalid wheel RECORD")
        seen.add(row[0])
        expected = (
            ["", ""]
            if row[0] == prefix + "RECORD"
            else [
                "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(wheel[row[0]]).digest())
                .decode()
                .rstrip("="),
                str(len(wheel[row[0]])),
            ]
        )
        require(row[1:] == expected, "Wheel RECORD integrity differs")
    source = {}
    total = 0
    # Bound the whole inflated stream before tarfile parses PAX/long-name metadata.
    # A member-size check alone does not bound those internal metadata records.
    tar_limit = MAX_FILE + 512 * 1024
    with gzip.GzipFile(fileobj=io.BytesIO(snapshots[source_name])) as compressed:
        tar_bytes = compressed.read(tar_limit + 1)
    require(len(tar_bytes) <= tar_limit, "Source tar stream too large")
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for item in archive:
            total += item.size
            require(
                len(source) < 256 and 0 <= item.size <= MAX_FILE and total <= MAX_FILE,
                "Source archive too large",
            )
            safe_member(item.name)
            source_prefix = f"embedded_sim_runner-{version}/"
            require(item.isfile() and item.name.startswith(source_prefix), "Invalid source member")
            relative = item.name.removeprefix(source_prefix)
            require(relative not in source, "Duplicate source member")
            with archive.extractfile(item) as stream:
                source[relative] = stream.read(MAX_FILE + 1)
    require(
        {"pyproject.toml", "README.md", "LICENSE", "PKG-INFO"} <= source.keys(),
        "Incomplete source archive",
    )
    project = tomllib.loads(source["pyproject.toml"].decode())["project"]
    require(
        project["name"] == "embedded-sim-runner"
        and project["version"] == version
        and project["requires-python"] == ">=3.11"
        and project["dependencies"] == []
        and project["scripts"] == {"sim-runner": "sim_runner.cli:main"},
        "Source identity/runtime contract differs",
    )
    require(source["PKG-INFO"] == wheel[prefix + "METADATA"], "Package metadata differs")
    require(source["LICENSE"] == wheel[prefix + "licenses/LICENSE"], "Package license differs")
    for name in runtime_files:
        target = "src/" + name
        require(source.get(target) == wheel[name], f"Wheel/source mismatch: {name}")
    return wheel, source


def checksums(snapshots: dict[str, bytes]) -> bytes:
    return "".join(f"{digest(data)}  {name}\n" for name, data in snapshots.items()).encode("ascii")


def create_manifest(directory: Path, version: str, revision: str) -> dict:
    identity(version, revision)
    require(
        not any(
            (directory / name).exists() or (directory / name).is_symlink()
            for name in ("release-manifest.json", "SHA256SUMS")
        ),
        "Release sidecar already exists",
    )
    snapshots = {name: read(directory / name) for name in names(version)}
    payloads(snapshots, version)
    manifest = {
        "schema_version": SCHEMA,
        "repository": REPOSITORY,
        "version": version,
        "source_revision": revision,
        "source_ref": f"refs/tags/v{version}",
        "hardware_access": False,
        "files": [
            {"name": name, "size": len(data), "sha256": digest(data)}
            for name, data in snapshots.items()
        ],
    }
    snapshots["release-manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode()
    with (directory / "release-manifest.json").open("xb") as stream:
        stream.write(snapshots["release-manifest.json"])
    with (directory / "SHA256SUMS").open("xb") as stream:
        stream.write(checksums(snapshots))
    return manifest


def verify(directory: Path, expected_revision: str | None = None) -> dict:
    manifest_bytes = read(directory / "release-manifest.json", 16384)
    manifest = json.loads(manifest_bytes, object_pairs_hook=pairs)
    require(
        isinstance(manifest, dict)
        and set(manifest)
        == {
            "schema_version",
            "repository",
            "version",
            "source_revision",
            "source_ref",
            "hardware_access",
            "files",
        },
        "Manifest fields differ",
    )
    version, revision = manifest["version"], manifest["source_revision"]
    identity(version, revision)
    require(
        manifest["schema_version"] == SCHEMA
        and manifest["repository"] == REPOSITORY
        and manifest["source_ref"] == f"refs/tags/v{version}"
        and manifest["hardware_access"] is False,
        "Manifest contract differs",
    )
    require(expected_revision is None or revision == expected_revision, "Source revision differs")
    require(
        {item.name for item in directory.iterdir()}
        == {*names(version), "release-manifest.json", "SHA256SUMS"},
        "Unexpected/missing release files",
    )
    snapshots = {name: read(directory / name) for name in names(version)}
    require(
        isinstance(manifest["files"], list)
        and all(
            isinstance(item, dict) and type(item.get("size")) is int for item in manifest["files"]
        ),
        "Invalid archive metadata types",
    )
    require(
        manifest["files"]
        == [
            {"name": name, "size": len(data), "sha256": digest(data)}
            for name, data in snapshots.items()
        ],
        "Archive integrity/list differs",
    )
    snapshots["release-manifest.json"] = manifest_bytes
    require(
        read(directory / "SHA256SUMS", 1024) == checksums(snapshots), "Checksum bindings differ"
    )
    wheel, source = payloads(snapshots, version)
    return {
        "ok": True,
        "version": version,
        "source_revision": revision,
        "files": manifest["files"],
        "wheel_members": len(wheel),
        "source_members": len(source),
        "hardware_access": False,
        "executables_started": False,
        "publisher_authenticity_verified": False,
    }


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, timeout=30
    ).stdout


def build(root: Path, output: Path, tag: str | None = None) -> dict:
    require(not git(root, "status", "--porcelain"), "Build requires a clean committed worktree")
    revision = git(root, "rev-parse", "HEAD").decode().strip()
    version = tomllib.loads(read(root / "pyproject.toml").decode())["project"]["version"]
    identity(version, revision)
    if tag is not None:
        require(tag == "v" + version, "Tag/version mismatch")
        require(
            git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip() == revision,
            "Tag/commit mismatch",
        )
    output = output.resolve()
    require(
        not output.is_relative_to(root.resolve()),
        "Build output must be outside the source checkout",
    )
    output.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        [
            "uv",
            "build",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--out-dir",
            str(output),
        ],
        cwd=root,
        check=True,
        stdout=sys.stderr,
        timeout=180,
    )
    ignored = output / ".gitignore"
    if ignored.exists():
        require(read(ignored) in (b"*", b"*\n"), "Unexpected generated ignore file")
        ignored.unlink()
    _, source = payloads({name: read(output / name) for name in names(version)}, version)
    tracked = git(root, "ls-tree", "-rz", "--name-only", revision).decode().split("\0")[:-1]
    require(set(source) == {*tracked, "PKG-INFO"}, "Source archive coverage differs from Git")
    for name in tracked:
        require(
            git(root, "show", f"{revision}:{name}") == source[name],
            f"Source differs from Git: {name}",
        )
    require(
        not git(root, "status", "--porcelain")
        and git(root, "rev-parse", "HEAD").decode().strip() == revision,
        "Source changed while building",
    )
    if tag is not None:
        require(
            git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip() == revision,
            "Tag changed while building",
        )
    create_manifest(output, version, revision)
    report = verify(output, revision)
    report.update(executables_started=True, source_matches_git=True, tag_checked=tag)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--output-dir", required=True, type=Path)
    builder.add_argument("--tag")
    checker = commands.add_parser("verify")
    checker.add_argument("directory", type=Path)
    checker.add_argument("--source-revision")
    args = parser.parse_args(argv)
    try:
        report = (
            build(Path(__file__).resolve().parents[1], args.output_dir, args.tag)
            if args.operation == "build"
            else verify(args.directory, args.source_revision)
        )
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        EOFError,
        RuntimeError,
        configparser.Error,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error), "hardware_access": False}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
