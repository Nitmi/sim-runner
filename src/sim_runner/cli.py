from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    __version__,
    esp_wasm,
    gpio_fault,
    gpio_timer,
    qemu,
    qemu_flash,
    qemu_s3,
    sensor,
    spi_sensor,
    watchdog,
)
from .common import FLAGS, SimError, bound, decode, file_record, require, write_json
from .inspect import inspect_report
from .plan import load_plan, make_plan, runtime
from .run import run


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bounded Renode/QEMU/experimental ESP-EMU simulation; no hardware access"
    )
    parser.add_argument("--version", action="version", version=f"sim-runner {__version__}")
    commands = parser.add_subparsers(dest="operation", required=True)
    doctor = commands.add_parser("doctor", help="inspect hash-bound runtime without launching it")
    doctor.add_argument("--runtime-lock", type=Path, required=True)
    doctor.add_argument("--runtime-sha256", required=True)
    doctor.add_argument(
        "--backend",
        choices=("renode", qemu.BACKEND, qemu_s3.BACKEND, esp_wasm.BACKEND),
        default="renode",
    )
    plan = commands.add_parser(
        "plan", help="prepare an offline plan; no per-run confirmation needed"
    )
    plan.add_argument("--runtime-lock", type=Path, required=True)
    plan.add_argument("--runtime-sha256", required=True)
    plan.add_argument(
        "--backend",
        choices=("renode", qemu.BACKEND, qemu_s3.BACKEND, esp_wasm.BACKEND),
        default="renode",
    )
    plan.add_argument(
        "--profile",
        choices=(
            "nrf52840-uart-v1",
            gpio_timer.PROFILE,
            gpio_fault.PROFILE,
            sensor.PROFILE,
            spi_sensor.PROFILE,
            watchdog.PROFILE,
            qemu.PROFILE,
            qemu_flash.PROFILE,
            qemu_s3.PROFILE,
            esp_wasm.PROFILE,
        ),
    )
    plan.add_argument(
        "--fault",
        choices=tuple(
            dict.fromkeys(
                (*gpio_fault.FAULTS, *sensor.FAULTS, *spi_sensor.FAULTS, *watchdog.FAULTS)
            )
        ),
        help="required for GPIO fault, sensor and watchdog profiles; none is the nominal control",
    )
    plan.add_argument("--firmware", type=Path, required=True)
    plan.add_argument("--firmware-sha256", required=True)
    plan.add_argument("--ready-line", required=True)
    plan.add_argument("--build-line", required=True)
    plan.add_argument("--heartbeat-prefix", required=True)
    plan.add_argument(
        "--min-heartbeats",
        type=int,
        help="defaults to 3; GPIO/sensor profiles require/default to 16 IRQ samples",
    )
    plan.add_argument(
        "--virtual-ms", type=int, help="Renode only; defaults to 250 ms (GPIO/sensor: fixed 82)"
    )
    plan.add_argument("--timeout", type=int, default=30)
    plan.add_argument("--output-limit", type=int, default=1048576)
    plan.add_argument("--memory-mib", type=int, default=1024)
    plan.add_argument(
        "--instruction-budget", type=int, help="ESP-EMU WASM only; defaults to 50000000"
    )
    plan.add_argument("--output", type=Path, required=True)
    for operation in ("inspect-plan", "run"):
        command = commands.add_parser(operation)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--sha256", required=True)
        if operation == "run":
            command.add_argument("--output-dir", type=Path, required=True)
    inspect = commands.add_parser(
        "inspect-report", help="verify saved evidence; no backend execution"
    )
    inspect.add_argument("--report", type=Path, required=True)
    inspect.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "doctor":
            info = runtime(args.runtime_lock, args.runtime_sha256, args.backend)
            result = {
                **FLAGS,
                "ok": True,
                "status": "ready",
                "backend_version": info["version"],
                "executables_started": False,
                "runtime_files": len(info["files"]),
            }
        elif args.operation == "plan":
            require(
                args.instruction_budget is None or args.backend == esp_wasm.BACKEND,
                "instruction budget is only supported by ESP-EMU WASM",
            )
            require(
                (args.fault is not None)
                == (
                    args.profile
                    in (gpio_fault.PROFILE, sensor.PROFILE, spi_sensor.PROFILE, watchdog.PROFILE)
                ),
                "--fault is required only for the GPIO fault, sensor or watchdog profiles",
            )
            common = (
                args.runtime_lock,
                args.runtime_sha256,
                args.firmware,
                args.firmware_sha256,
                args.ready_line,
                args.build_line,
                args.heartbeat_prefix,
                (
                    16
                    if args.profile
                    in (
                        gpio_timer.PROFILE,
                        gpio_fault.PROFILE,
                        sensor.PROFILE,
                        spi_sensor.PROFILE,
                        watchdog.PROFILE,
                    )
                    else 3
                )
                if args.min_heartbeats is None
                else args.min_heartbeats,
            )
            if args.backend == esp_wasm.BACKEND:
                require(
                    args.virtual_ms is None and args.profile in (None, esp_wasm.PROFILE),
                    "profile/backend or time budget differ",
                )
                result = esp_wasm.make_plan(
                    *common,
                    args.timeout,
                    args.output_limit,
                    args.memory_mib,
                    50000000 if args.instruction_budget is None else args.instruction_budget,
                )
            elif args.backend in (qemu.BACKEND, qemu_s3.BACKEND):
                require(
                    args.virtual_ms is None, "QEMU profile does not enforce a virtual-time budget"
                )
                require(
                    args.profile in (None, qemu_s3.PROFILE)
                    if args.backend == qemu_s3.BACKEND
                    else args.profile in (None, qemu.PROFILE, qemu_flash.PROFILE),
                    "profile/backend differ",
                )
                planner = (
                    qemu_s3.make_plan
                    if args.backend == qemu_s3.BACKEND
                    else (
                        qemu_flash.make_plan
                        if args.profile == qemu_flash.PROFILE
                        else qemu.make_plan
                    )
                )
                result = planner(*common, args.timeout, args.output_limit, args.memory_mib)
            else:
                require(
                    args.profile
                    in (
                        None,
                        "nrf52840-uart-v1",
                        gpio_timer.PROFILE,
                        gpio_fault.PROFILE,
                        sensor.PROFILE,
                        spi_sensor.PROFILE,
                        watchdog.PROFILE,
                    ),
                    "profile/backend differ",
                )
                planner = (
                    watchdog.WatchdogProfile(args.fault).make_plan
                    if args.profile == watchdog.PROFILE
                    else spi_sensor.SPIProfile(args.fault).make_plan
                    if args.profile == spi_sensor.PROFILE
                    else sensor.SensorProfile(args.fault).make_plan
                    if args.profile == sensor.PROFILE
                    else gpio_fault.FaultProfile(args.fault).make_plan
                    if args.profile == gpio_fault.PROFILE
                    else gpio_timer.make_plan
                    if args.profile == gpio_timer.PROFILE
                    else make_plan
                )
                default_ms = (
                    82
                    if args.profile
                    in (
                        gpio_timer.PROFILE,
                        gpio_fault.PROFILE,
                        sensor.PROFILE,
                        spi_sensor.PROFILE,
                        watchdog.PROFILE,
                    )
                    else 250
                )
                result = planner(
                    *common,
                    default_ms if args.virtual_ms is None else args.virtual_ms,
                    args.timeout,
                    args.output_limit,
                    args.memory_mib,
                )
            write_json(args.output, result)
            result = {
                **FLAGS,
                "ok": True,
                "operation": "plan",
                "executables_started": False,
                "plan": file_record(args.output),
            }
        elif args.operation == "inspect-plan":
            schema = decode(bound(args.plan, args.sha256, 65536))["schema_version"]
            loader = {
                qemu.PLAN_SCHEMA: qemu.load_plan,
                qemu_flash.PLAN_SCHEMA: qemu_flash.load_plan,
                qemu_s3.PLAN_SCHEMA: qemu_s3.load_plan,
                gpio_timer.PLAN_SCHEMA: gpio_timer.load_plan,
                gpio_fault.PLAN_SCHEMA: gpio_fault.load_plan,
                sensor.PLAN_SCHEMA: sensor.load_plan,
                spi_sensor.PLAN_SCHEMA: spi_sensor.load_plan,
                watchdog.PLAN_SCHEMA: watchdog.load_plan,
                esp_wasm.PLAN_SCHEMA: esp_wasm.load_plan,
            }.get(schema, load_plan)
            result = loader(args.plan, args.sha256)
            result = {
                **FLAGS,
                "ok": True,
                "operation": "inspect-plan",
                "executables_started": False,
                "profile": result["profile"],
            }
        elif args.operation == "inspect-report":
            schema = decode(bound(args.report, args.sha256, 1024 * 1024))["schema_version"]
            inspector = {
                qemu.REPORT_SCHEMA: qemu.inspect_report,
                qemu_flash.REPORT_SCHEMA: qemu_flash.inspect_report,
                qemu_s3.REPORT_SCHEMA: qemu_s3.inspect_report,
                gpio_fault.REPORT_SCHEMA: gpio_fault.inspect_report,
                sensor.REPORT_SCHEMA: sensor.inspect_report,
                spi_sensor.REPORT_SCHEMA: spi_sensor.inspect_report,
                watchdog.REPORT_SCHEMA: watchdog.inspect_report,
                esp_wasm.REPORT_SCHEMA: esp_wasm.inspect_report,
            }.get(schema, inspect_report)
            result = inspector(
                args.report,
                args.sha256,
                **({"profile": gpio_timer} if schema == gpio_timer.REPORT_SCHEMA else {}),
            )
        else:
            require(sys.maxsize > 2**32, "64-bit Python required", "UNSUPPORTED_PLATFORM")
            schema = decode(bound(args.plan, args.sha256, 65536))["schema_version"]
            runner = {
                qemu.PLAN_SCHEMA: qemu.run,
                qemu_flash.PLAN_SCHEMA: qemu_flash.run,
                qemu_s3.PLAN_SCHEMA: qemu_s3.run,
                gpio_fault.PLAN_SCHEMA: gpio_fault.run,
                sensor.PLAN_SCHEMA: sensor.run,
                spi_sensor.PLAN_SCHEMA: spi_sensor.run,
                watchdog.PLAN_SCHEMA: watchdog.run,
                esp_wasm.PLAN_SCHEMA: esp_wasm.run,
            }.get(schema, run)
            result = runner(
                args.plan,
                args.sha256,
                args.output_dir.resolve(),
                **({"profile": gpio_timer} if schema == gpio_timer.PLAN_SCHEMA else {}),
            )
    except (SimError, OSError, ValueError, KeyError, TypeError) as error:
        result = {
            **FLAGS,
            "ok": False,
            "error": {"code": getattr(error, "code", "INVALID_INPUT"), "message": str(error)},
        }
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    return 0 if result.get("ok") else 1
