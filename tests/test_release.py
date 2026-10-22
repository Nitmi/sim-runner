from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sim_release", Path(__file__).resolve().parents[1] / "scripts/release.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
VERSION = "0.1.0"
REVISION = "a" * 40
MODULES = (
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
)
ASSETS = (
    "nrf52840-uart.repl",
    "nrf52840-gpio-timer.repl",
    "esp32c3-uart.json",
    "esp32c3-idf-flash.json",
    "esp32s3-idf-flash.json",
)


def archives(directory, *, mutate=None, extra_wheel=None, extra_source=None):
    """Independent minimal distribution fixture; contents are never imported."""
    metadata = "embedded_sim_runner-0.1.0.dist-info/"
    wheel = {"sim_runner/" + name: b"# synthetic module\n" for name in MODULES}
    wheel.update(
        {f"sim_runner/assets/{name}": b"synthetic model, never executed\n" for name in ASSETS}
    )
    wheel.update(
        {
            metadata
            + "METADATA": b"Name: embedded-sim-runner\nVersion: 0.1.0\nRequires-Python: >=3.11\n",
            metadata + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            metadata + "entry_points.txt": b"[console_scripts]\nsim-runner = sim_runner.cli:main\n",
            metadata + "licenses/LICENSE": b"synthetic license\n",
        }
    )
    source = {
        "pyproject.toml": b'[project]\nname="embedded-sim-runner"\nversion="0.1.0"\n'
        b'requires-python=">=3.11"\ndependencies=[]\n[project.scripts]\n'
        b'sim-runner="sim_runner.cli:main"\n',
        "README.md": b"synthetic readme\n",
        "LICENSE": wheel[metadata + "licenses/LICENSE"],
        "PKG-INFO": wheel[metadata + "METADATA"],
    }
    source.update({"src/sim_runner/" + name: wheel["sim_runner/" + name] for name in MODULES})
    source.update(
        {"src/sim_runner/assets/" + name: wheel["sim_runner/assets/" + name] for name in ASSETS}
    )
    if mutate:
        mutate(wheel, source)
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for name, raw in wheel.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + encoded, len(raw)])
    writer.writerow([metadata + "RECORD", "", ""])
    wheel[metadata + "RECORD"] = records.getvalue().encode()
    wheel_name, source_name = release.names(VERSION)
    with zipfile.ZipFile(directory / wheel_name, "w") as archive:
        for name, data in wheel.items():
            item = zipfile.ZipInfo("placeholder")
            item.filename = name  # Keep intentionally unsafe raw paths on Windows.
            archive.writestr(item, data)
        if extra_wheel:
            extra_wheel(archive)
    with tarfile.open(directory / source_name, "w:gz") as archive:
        for name, data in source.items():
            item = tarfile.TarInfo("embedded_sim_runner-0.1.0/" + name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        if extra_source:
            extra_source(archive)


def manifest_edit(directory, change):
    path = directory / "release-manifest.json"
    value = json.loads(path.read_bytes())
    change(value)
    path.write_bytes((json.dumps(value, sort_keys=True) + "\n").encode())


def test_verify_binds_bytes_but_does_not_claim_publisher_authentication(tmp_path, monkeypatch):
    archives(tmp_path)
    release.create_manifest(tmp_path, VERSION, REVISION)

    def forbidden(*args, **kwargs):
        pytest.fail("Verification must not start executables")

    monkeypatch.setattr(release.subprocess, "run", forbidden)
    report = release.verify(tmp_path, REVISION)
    assert report["ok"] and report["source_revision"] == REVISION
    assert report["wheel_members"] == 26 and report["source_members"] == 25
    assert not report["executables_started"] and not report["hardware_access"]
    assert not report["publisher_authenticity_verified"]
    with pytest.raises(ValueError, match="Source revision differs"):
        release.verify(tmp_path, "b" * 40)


@pytest.mark.parametrize("name", [*release.names(VERSION), "release-manifest.json", "SHA256SUMS"])
def test_each_changed_asset_is_rejected(tmp_path, name):
    archives(tmp_path)
    release.create_manifest(tmp_path, VERSION, REVISION)
    path = tmp_path / name
    if name == "release-manifest.json":
        manifest_edit(tmp_path, lambda value: value.update(hardware_access=True))
    else:
        path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(ValueError):
        release.verify(tmp_path, REVISION)


@pytest.mark.parametrize(
    "name", ["../escape.py", "/absolute.py", "C:/escape.py", "a\\b.py", "a//b.py"]
)
def test_unsafe_wheel_path_is_rejected_without_extraction(tmp_path, name):
    archives(tmp_path, mutate=lambda wheel, source: wheel.update({name: b"bad"}))
    with pytest.raises(ValueError, match="Unsafe archive member"):
        release.create_manifest(tmp_path, VERSION, REVISION)
    assert not (tmp_path / "release-manifest.json").exists()


def test_wheel_symlink_rejected(tmp_path):
    def linked(archive):
        item = zipfile.ZipInfo("linked.py")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(item, b"elsewhere.py")

    archives(tmp_path, extra_wheel=linked)
    with pytest.raises(ValueError, match="Nonregular wheel member"):
        release.create_manifest(tmp_path, VERSION, REVISION)


def test_wheel_duplicate_rejected(tmp_path):
    def duplicate(archive):
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("sim_runner/cli.py", b"second version")

    archives(tmp_path, extra_wheel=duplicate)
    with pytest.raises(ValueError, match="Duplicate/directory"):
        release.create_manifest(tmp_path, VERSION, REVISION)


@pytest.mark.parametrize("mode", ["link", "duplicate", "traversal"])
def test_unsafe_source_member_rejected(tmp_path, mode):
    def unsafe(archive):
        item = tarfile.TarInfo("embedded_sim_runner-0.1.0/README.md")
        if mode == "link":
            item.type, item.linkname = tarfile.SYMTYPE, "outside"
        elif mode == "traversal":
            item.name = "embedded_sim_runner-0.1.0/../outside"
        archive.addfile(item)

    archives(tmp_path, extra_source=unsafe)
    with pytest.raises(
        ValueError, match="Invalid source member|Duplicate source member|Unsafe archive member"
    ):
        release.create_manifest(tmp_path, VERSION, REVISION)


@pytest.mark.parametrize(
    "mode", ["source-code", "runtime-dependency", "entrypoint", "metadata", "missing-model"]
)
def test_matching_outer_hashes_cannot_hide_package_contract_drift(tmp_path, mode):
    def change(wheel, source):
        prefix = "embedded_sim_runner-0.1.0.dist-info/"
        if mode == "source-code":
            source["src/sim_runner/cli.py"] = b"different source\n"
        elif mode == "runtime-dependency":
            wheel[prefix + "METADATA"] += b"Requires-Dist: requests\n"
        elif mode == "entrypoint":
            wheel[prefix + "entry_points.txt"] += b"other-cli = malicious:main\n"
        elif mode == "metadata":
            wheel[prefix + "METADATA"] += b"Version: 0.2.0\n"
        else:
            del wheel["sim_runner/assets/esp32s3-idf-flash.json"]

    archives(tmp_path, mutate=change)
    with pytest.raises(
        ValueError,
        match=(
            "Wheel/source mismatch|Runtime dependencies|CLI entry points|Wheel identity|wheel files"
        ),
    ):
        release.create_manifest(tmp_path, VERSION, REVISION)


def test_changed_record_detected_even_with_matching_outer_hashes(tmp_path):
    archives(tmp_path)
    wheel = tmp_path / release.names(VERSION)[0]
    with zipfile.ZipFile(wheel) as archive:
        entries = {item.filename: archive.read(item) for item in archive.infolist()}
    entries["sim_runner/cli.py"] += b"drift"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="Wheel RECORD integrity"):
        release.create_manifest(tmp_path, VERSION, REVISION)


@pytest.mark.parametrize(
    "mode", ["version", "revision", "ref", "extra", "size-type", "duplicate-key"]
)
def test_malformed_manifest_reports_structured_failure(tmp_path, capsys, mode):
    archives(tmp_path)
    release.create_manifest(tmp_path, VERSION, REVISION)
    if mode == "duplicate-key":
        path = tmp_path / "release-manifest.json"
        path.write_bytes(
            path.read_bytes().replace(b'"version": "0.1.0"', b'"version":"0.1.0","version":"0.1.0"')
        )
    else:

        def change(value):
            if mode == "size-type":
                value["files"][0]["size"] = float(value["files"][0]["size"])
            else:
                key, new = {
                    "version": ("version", None),
                    "revision": ("source_revision", "ABC"),
                    "ref": ("source_ref", "refs/heads/main"),
                    "extra": ("unknown", 1),
                }[mode]
                value[key] = new

        manifest_edit(tmp_path, change)
    assert release.main(["verify", str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and not result["hardware_access"]


@pytest.mark.parametrize("name", ["release-manifest.json", "SHA256SUMS"])
def test_existing_sidecar_refused_without_writing_other_files(tmp_path, name):
    archives(tmp_path)
    (tmp_path / name).write_bytes(b"preserved")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        release.create_manifest(tmp_path, VERSION, REVISION)
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


def test_unexpected_asset_rejected(tmp_path):
    archives(tmp_path)
    release.create_manifest(tmp_path, VERSION, REVISION)
    (tmp_path / "extra.exe").write_bytes(b"unreviewed")
    with pytest.raises(ValueError, match="Unexpected/missing"):
        release.verify(tmp_path)


def test_compressed_size_limits_apply_before_payload_use(tmp_path, monkeypatch):
    archives(tmp_path, mutate=lambda wheel, source: wheel.update({"padding": b"x" * 20000}))
    # Compressed bytes fit, but advertised decompressed contents exceed the limit.
    monkeypatch.setattr(release, "MAX_FILE", 16000)
    with pytest.raises(ValueError, match="Wheel too large"):
        release.create_manifest(tmp_path, VERSION, REVISION)


def test_source_limit_covers_inflated_tar_metadata_not_only_file_members(tmp_path, monkeypatch):
    archives(tmp_path)
    monkeypatch.setattr(release, "MAX_FILE", 16000)
    # A tiny gzip can conceal a large tar stream even without reported members.
    (tmp_path / release.names(VERSION)[1]).write_bytes(
        gzip.compress(b"\0" * (16000 + 512 * 1024 + 1))
    )
    with pytest.raises(ValueError, match="Source tar stream too large"):
        release.create_manifest(tmp_path, VERSION, REVISION)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return (
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Release Test",
                    "-c",
                    "user.email=release@example.invalid",
                    *args,
                ],
                cwd=root,
                capture_output=True,
                check=True,
                timeout=20,
            )
            .stdout.decode()
            .strip()
        )

    git("init", "--initial-branch=main")
    (root / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    git("add", "pyproject.toml")
    git("commit", "-m", "Synthetic release fixture")
    return root, git


@pytest.mark.parametrize("mode", ["dirty", "wrong-version", "wrong-commit", "inside-checkout"])
def test_build_preflight_prevents_uv_and_output_creation(repository, tmp_path, monkeypatch, mode):
    root, git = repository
    tag = None
    output = tmp_path / "never-created"
    if mode == "dirty":
        (root / "uncommitted.txt").write_text("dirty")
    elif mode == "wrong-version":
        tag = "v9.0.0"
    elif mode == "wrong-commit":
        tag = "v0.1.0"
        git("tag", tag)
        (root / "change").write_text("next commit")
        git("add", "change")
        git("commit", "-m", "Move HEAD, not tag")
    else:
        output = root / "output"
    original = release.subprocess.run

    def guard(command, **kwargs):
        assert command[0] == "git", "Invalid release preflight must not invoke uv"
        return original(command, **kwargs)

    monkeypatch.setattr(release.subprocess, "run", guard)
    with pytest.raises(ValueError, match="clean committed|Tag/version|Tag/commit|outside"):
        release.build(root, output, tag)
    assert not output.exists()
