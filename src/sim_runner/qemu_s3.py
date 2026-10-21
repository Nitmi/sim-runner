"""Fixed Xtensa ESP32-S3 ROM/ESP-IDF Flash profile; shared bounded lifecycle."""

import sys
from pathlib import Path

from . import flash_image, qemu_flash
from .common import require
from .plan import ASSETS

BACKEND = "espressif-qemu-xtensa"
PROFILE = "esp32s3-idf-flash-v1"
PLAN_SCHEMA = "embedded-sim-runner.qemu-s3-flash-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.qemu-s3-flash-report.v1"
MODEL = ASSETS / "esp32s3-idf-flash.json"
ROM = "share/qemu/esp32s3_rev0_rom.bin"
ROM_SHA = "b5c7090cc22efce51c1323cef31ca96fcd673a5b1d1b1017e219d393f2659913"
EFUSE = bytes(38) + b"\x0c" + bytes(985)
ARCHIVE_SHA256 = "3c483d77f5350a568df1faf4d8dbc82c95d6bc2b826d0d4be910485e0a68ca2a"
EXECUTABLE = "bin/qemu-system-xtensa.exe"
MACHINE = "esp32s3"
# Matches IDF's QEMU target recipe. This provisions model PSRAM, not physical RAM.
MACHINE_ARGS = ["-m", "32M"]
ROM_MARKER = "ESP-ROM:esp32s3-"
STARTUP_DIAGNOSTIC = "Adding SPI flash device"
VIRTUAL_TIME = {
    "domain": "qemu_device_clock_unmeasured",
    "elapsed_ns": None,
    "budget_enforced": False,
}
KNOWN_DIAGNOSTICS = {
    "M25P80: Read id (command 0x90/0xAB) is not supported by device": (
        1,
        "legacy Flash ID probe unsupported",
    ),
    "M25P80: Unknown cmd 5a": (1, "Flash SFDP command unsupported"),
    "M25P80: Unknown cmd 7a": (1, "Flash resume command unsupported"),
    "Invalid read at addr 0x10200C, size 4, region 'er', reason: rejected": (
        3,
        "Xtensa DSRSET debugger-presence query is unmodeled; debug attachment not verified",
    ),
}


def flash_info(data):
    return flash_image.flash_info(data, chip_id=9)


def make_plan(*args, **kwargs):
    return qemu_flash.make_plan(*args, **kwargs, profile=sys.modules[__name__])


def load_plan(path, expected):
    return qemu_flash.load_plan(path, expected, profile=sys.modules[__name__])


def command(executable: Path):
    args = qemu_flash.command(executable, profile=sys.modules[__name__])
    # Do not transplant the RISC-V icount setting to the Xtensa SMP model.
    at = args.index("-icount")
    del args[at : at + 2]
    return args


def check_output(stdout, stderr, uart, assertions):
    result = qemu_flash.check_output(
        stdout, stderr, uart, assertions, profile=sys.modules[__name__]
    )
    lines = uart.decode("utf-8", errors="strict").splitlines()
    ready = lines.index(assertions["ready"])
    first = "SIM_IDF_S3_CORE0 v1"
    second = "SIM_IDF_S3_CORE1 v1"
    require(
        lines.count(first) == lines.count(second) == 1
        and lines.index(first) < lines.index(second) < ready,
        "S3 fixture requires ordered CPU0 and CPU1 application markers before READY",
        "ASSERTION_FAILED",
    )
    return {**result, "app_core_markers": [0, 1]}


def run(path, expected, directory):
    return qemu_flash.run(path, expected, directory, profile=sys.modules[__name__])


def inspect_report(path, expected):
    return qemu_flash.inspect_report(path, expected, profile=sys.modules[__name__])
