"""Fixed TWI0/TMP103 regression; model properties, guest reads and clocks stay distinct."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-i2c-sensor-v1"
PLAN_SCHEMA = "embedded-sim-runner.sensor-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.sensor-report.v1"
MODEL = ASSETS / "nrf52840-i2c-sensor.repl"
VIRTUAL_MS = 82
FAULTS = ("none", "temperature-stuck", "temperature-offset")
TEMPERATURES = (25, -10, 85, 25)
AT_MS = 22
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "sensor_accuracy_verified": False,
    "i2c_electrical_behavior_verified": False,
    "sensor_observation_semantics": "paused_model_property_and_register_snapshot_not_measurement",
    "sensor_sample_semantics": "guest_TWI0_register0_signed_byte_not_calibrated_temperature",
    "timer_observation_semantics": "guest_250kHz_TIMER0_IRQ_capture_not_host_time",
}


class SensorProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed sensor fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        phases = []
        for index, (start, observe, nominal) in enumerate(
            zip((0, 22, 42, 62), (20, 40, 60, 80), TEMPERATURES, strict=True)
        ):
            applied = nominal
            if index and fault == "temperature-stuck":
                applied = TEMPERATURES[0]
            if index and fault == "temperature-offset":
                applied = nominal + 10
            phases.append(
                dict(
                    input_at_ms=start,
                    observe_at_ms=observe,
                    nominal_celsius=nominal,
                    applied_celsius=applied,
                )
            )
        self.SCENARIO = dict(
            sensor="TMP103",
            controller="TWI0",
            i2c_address=0x48,
            register=0,
            representation="signed_int8_celsius_model",
            timer_frequency_hz=250000,
            period_ticks=1250,
            capture_tolerance_ticks=25,
            phases=phases,
            fault=dict(id=fault, requested_virtual_ms=None if fault == "none" else AT_MS),
            coverage_limits=[
                "no_bus_speed_or_electrical_model",
                "no_NACK_or_disconnect_coverage",
                "no_conversion_delay_or_accuracy",
                "no_EasyDMA_or_PPI_coverage",
            ],
        )

    def make_plan(
        self,
        lock,
        lock_sha,
        image,
        image_sha,
        ready,
        build,
        heartbeat,
        min_heartbeats=16,
        virtual_ms=82,
        timeout=30,
        output_limit=1048576,
        memory_mib=1024,
    ):
        require(
            type(virtual_ms) is int and virtual_ms == VIRTUAL_MS,
            "sensor scenario requires 82 virtual ms",
        )
        require(
            type(min_heartbeats) is int and min_heartbeats == 16,
            "sensor scenario requires 16 samples",
        )
        plan = uart_plan(
            lock,
            lock_sha,
            image,
            image_sha,
            ready,
            build,
            heartbeat,
            min_heartbeats,
            virtual_ms,
            timeout,
            output_limit,
            memory_mib,
        )
        plan.update(
            schema_version=PLAN_SCHEMA,
            profile=PROFILE,
            model_sha256=sha(payload(MODEL, 16384)),
            script_sha256=sha(self.script(virtual_ms).encode()),
            scenario=json.loads(json.dumps(self.SCENARIO)),
            **self.COVERAGE,
        )
        return plan

    def load_plan(self, path, expected):
        doc = decode(bound(path, expected, 65536))
        wanted = self.make_plan(
            Path(doc["runtime_lock"]["path"]),
            doc["runtime_lock"]["sha256"],
            Path(doc["firmware"]["path"]),
            doc["firmware"]["sha256"],
            doc["assertions"]["ready"],
            doc["assertions"]["build"],
            doc["assertions"]["heartbeat_prefix"],
            doc["assertions"]["minimum"],
            doc["virtual_ms"],
            doc["host_timeout_seconds"],
            doc["output_limit_bytes_per_file"],
            doc["job_memory_limit_mib"],
        )
        require(
            json.dumps(doc, sort_keys=True) == json.dumps(wanted, sort_keys=True),
            "sensor plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(
            type(virtual_ms) is int and virtual_ms == VIRTUAL_MS, "invalid sensor scenario duration"
        )
        lines = [
            'mach create "nrf52840-i2c-sensor"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            'emulation SetGlobalQuantum "0.0001"',
        ]
        cursor = 0
        for index, phase in enumerate(self.SCENARIO["phases"]):
            start = phase["input_at_ms"]
            if start > cursor:
                lines.append(f'emulation RunFor "{(start - cursor) / 1000:.3f}"')
            lines.extend(
                [
                    f"sysbus.twi0.temperature Temperature {phase['applied_celsius']}",
                    f'emulation RunFor "{(phase["observe_at_ms"] - start) / 1000:.3f}"',
                    "sysbus.timer0 WriteDoubleWord 0x4c 1",
                    f'console_log "SIM_SENSOR_OBS_{index}"',
                    "console_log `sysbus.twi0.temperature Temperature`",
                    "console_log `sysbus.twi0 ReadDoubleWord 0x588`",
                    "console_log `sysbus.timer0 ReadDoubleWord 0x54c`",
                ]
            )
            cursor = phase["observe_at_ms"]
        lines.extend(
            [
                f'emulation RunFor "{(virtual_ms - cursor) / 1000:.3f}"',
                "sysbus.uart0 CloseFileBackend $ORIGIN/uart.bin",
                'log "SIM_RUNNER_COMPLETED_V1"',
                "quit",
                "",
            ]
        )
        return "\n".join(lines)

    def evaluate(self, uart, assertions, stdout):
        evidence = dict(
            nominal_regression_passed=False,
            nominal_failure=None,
            fault=dict(id=self.fault, effect_observed=None, status="not_evaluated"),
            samples=[],
            model_observations=[],
            **self.COVERAGE,
        )

        def fail(message):
            evidence["nominal_failure"] = message
            raise AssertionFailure(message, evidence)

        try:
            observations = parse_observations(stdout)
        except (ValueError, AssertionFailure) as error:
            fail(str(error))
        evidence["model_observations"] = observations
        applied = [p["applied_celsius"] for p in self.SCENARIO["phases"]]
        actual = [o["temperature_celsius"] for o in observations]
        effect = (
            None if self.fault == "none" else actual == applied and actual != list(TEMPERATURES)
        )
        evidence["fault"].update(
            effect_observed=effect,
            status="not_requested"
            if self.fault == "none"
            else "observed"
            if effect
            else "not_observed",
        )
        try:
            lines = uart.decode("ascii", errors="strict").splitlines()
        except UnicodeError:
            fail("non-ASCII sensor UART")
        evidence["ready_matches"] = lines[:1] == [assertions["ready"]]
        evidence["build_matches"] = lines[1:2] == [assertions["build"]]
        evidence["sample_lines_seen"] = max(0, len(lines) - 2)
        evidence["sample_summary_truncated"] = len(lines) > 18
        pattern = (
            re.escape(assertions["heartbeat_prefix"])
            + r" ([0-9A-F]{8}) R=([0-9A-F]{2}) C=([0-9A-F]{8})"
            r" T=([0-9A-F]{8}) V=([0-9A-F]{8}) E=([0-9A-F]{8})"
        )
        nominal = True
        malformed = False
        for index, line in enumerate(lines[2:18], 1):
            match = re.fullmatch(pattern, line)
            if match is None:
                malformed = True
                continue
            sequence, raw, celsius, ticks, vector, error = (int(v, 16) for v in match.groups())
            celsius = celsius if celsius < 2**31 else celsius - 2**32
            phase = (index - 1) // 4
            evidence["samples"].append(
                dict(
                    sequence=sequence,
                    raw_byte=raw,
                    celsius=celsius,
                    timer_ticks=ticks,
                    irq_vector=vector,
                    error=error,
                )
            )
            nominal &= (
                sequence == index
                and error == 0
                and vector == 24
                and abs(ticks - 1250 * index) <= 25
                and raw == (TEMPERATURES[phase] & 255)
                and celsius == TEMPERATURES[phase]
            )
        model_nominal = all(
            o["temperature_celsius"] == t
            and o["i2c_address"] == 0x48
            and abs(o["timer_counter"] - (20 + 20 * i) * 250) <= 25
            for i, (o, t) in enumerate(zip(observations, TEMPERATURES, strict=True))
        )
        evidence["model_nominal"] = model_nominal
        if (
            not uart.endswith(b"\n")
            or not evidence["ready_matches"]
            or not evidence["build_matches"]
            or assertions["minimum"] != 16
            or len(lines) != 18
            or malformed
        ):
            fail("sensor READY/BUILD or complete 16-sample protocol mismatch")
        if not nominal or not model_nominal:
            fail("nominal TMP103 signed temperature, sequence, TWI0 or TIMER0 regression failed")
        evidence["nominal_regression_passed"] = True
        if self.fault != "none":
            fail("selected sensor fault did not violate the nominal regression")
        return evidence


def parse_observations(stdout):
    def check(condition, message):
        if not condition:
            raise AssertionFailure(message, {})

    check(
        re.findall(rb"(?m)^SIM_SENSOR_OBS_([^\r\n]*)", stdout) == [b"0", b"1", b"2", b"3"],
        "sensor model observation inventory/order differs",
    )
    result = []
    for index in range(4):
        pattern = (
            rb"(?m)^SIM_SENSOR_OBS_"
            + str(index).encode()
            + rb"\r?\n(-?[0-9]{1,3})\r?\n([0-9]{1,10})\r?\n([0-9]{1,10})\r?\n"
        )
        matches = list(re.finditer(pattern, stdout))
        check(len(matches) == 1, "sensor model snapshot missing/duplicate/partial")
        match = matches[0]
        temperature, address, counter = (int(v) for v in match.groups())
        check(
            -128 <= temperature <= 127 and 0 <= address <= 127 and 0 <= counter <= 0xFFFFFFFF,
            "sensor model snapshot out of range",
        )
        result.append(
            dict(
                phase=index,
                requested_virtual_ms=20 + index * 20,
                temperature_celsius=temperature,
                i2c_address=address,
                timer_counter=counter,
                stdout_offset=match.start(),
                stdout_bytes=len(match[0]),
                stdout_sha256=sha(match[0]),
            )
        )
    return result


def profile_from_plan(plan):
    return SensorProfile(plan["scenario"]["fault"]["id"])


def load_plan(path, expected):
    return profile_from_plan(decode(bound(path, expected, 65536))).load_plan(path, expected)


def run(path, expected, directory):
    from .run import run as native_run

    return native_run(
        path, expected, directory, profile=profile_from_plan(decode(bound(path, expected, 65536)))
    )


def inspect_report(path, expected):
    from .backend import check_logs
    from .inspect import inspect_report as native_inspect

    report = decode(bound(path, expected, 1024 * 1024))
    plan = decode(
        bound(path.parent / "plan.json", report["artifacts"]["plan.json"]["sha256"], 65536)
    )
    profile = profile_from_plan(plan)
    result = native_inspect(path, expected, profile=profile)
    saved = report["assertions"]
    if saved is None:
        require(
            report["ok"] is False and bool(report["errors"]), "missing sensor evidence cannot pass"
        )
        require(
            all(e["code"] != "ASSERTION_FAILED" for e in report["errors"]),
            "failed sensor analysis must preserve evidence",
        )
    else:
        process = report["process"]
        require(
            process["error"] is None
            and process["exit_code"] == 0
            and process["forced_termination"] is False
            and report["virtual_time"]["completed"] is True,
            "sensor analysis requires completed backend/schedule",
        )
        loaded = {
            n: bound(path.parent / n, report["artifacts"][n]["sha256"], 4 * 1024 * 1024)
            for n in ("backend.stdout.bin", "backend.stderr.bin", "uart.bin")
        }
        check_logs(loaded["backend.stdout.bin"], loaded["backend.stderr.bin"])
        try:
            evaluated = profile.evaluate(
                loaded["uart.bin"], plan["assertions"], loaded["backend.stdout.bin"]
            )
        except AssertionFailure as error:
            evaluated = error.evidence
            require(
                report["ok"] is False
                and bool(report["errors"])
                and report["errors"][0] == dict(code=error.code, message=str(error)),
                "failed sensor verdict differs",
            )
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "sensor evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
