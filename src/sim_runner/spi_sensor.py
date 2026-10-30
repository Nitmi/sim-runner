"""Fixed legacy SPI/LM74 frames, signed Q4 values and bounded model faults."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-spi-sensor-v1"
PLAN_SCHEMA = "embedded-sim-runner.spi-sensor-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.spi-sensor-report.v1"
MODEL = ASSETS / "nrf52840-spi-sensor.repl"
VIRTUAL_MS = 82
FAULTS = ("none", "temperature-stuck", "frame-slip")
TEMPERATURES_Q4 = (401, -162, 1368, 1)
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "sensor_accuracy_verified": False,
    "spi_electrical_behavior_verified": False,
    "sensor_observation_semantics": "paused_LM74_property_SPI_enable_and_TIMER0_snapshot",
    "sensor_sample_semantics": "guest_SPI_two_byte_word_and_signed_Q4_not_calibrated_temperature",
    "frame_slip_semantics": "one_monitor_Transmit_consumes_model_byte_not_wire_fault",
    "timer_observation_semantics": "guest_250kHz_TIMER0_IRQ_capture_not_host_time",
}


def wire_word(q4):
    return ((q4 << 3) | 7) & 65535


def expected_words(fault):
    """Audit oracle for LM74's cached high/low alternating-byte model, not a bus model."""
    words = [wire_word(t) for t in TEMPERATURES_Q4 for _ in range(4)]
    if fault == "temperature-stuck":
        return [words[0]] * 16
    if fault == "frame-slip":
        # At 22 ms the monitor consumes the new phase's high byte once. At later
        # phase changes the first low byte still belongs to the prior cached word.
        nominal = words.copy()
        for index in range(4, 16):
            low_from = nominal[index - 1] if index in (8, 12) else nominal[index]
            words[index] = ((low_from & 255) << 8) | (nominal[index] >> 8)
    return words


class SPIProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed SPI sensor fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        self.SCENARIO = dict(
            sensor="TI_LM74",
            controller="SPI0_legacy_no_EasyDMA",
            representation="signed_int13_Q4_in_big_endian_word_with_three_low_status_bits",
            timer_frequency_hz=250000,
            period_ticks=1250,
            capture_tolerance_ticks=25,
            phases=[
                dict(
                    input_at_ms=start,
                    observe_at_ms=observe,
                    nominal_q4=nominal,
                    applied_q4=TEMPERATURES_Q4[0]
                    if index and fault == "temperature-stuck"
                    else nominal,
                )
                for index, (start, observe, nominal) in enumerate(
                    zip((0, 22, 42, 62), (20, 40, 60, 80), TEMPERATURES_Q4, strict=True)
                )
            ],
            fault=dict(id=fault, requested_virtual_ms=None if fault == "none" else 22),
            coverage_limits=[
                "no_SPI_clock_mode_pin_routing_or_electrical_model",
                "no_chip_select_or_FinishTransmission_from_controller",
                "no_conversion_delay_calibration_or_accuracy",
                "no_EasyDMA_SPI_IRQ_or_PPI_coverage",
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
        require(type(virtual_ms) is int and virtual_ms == 82, "SPI scenario requires 82 ms")
        require(
            type(min_heartbeats) is int and min_heartbeats == 16, "SPI scenario requires 16 samples"
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
            "SPI plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == 82, "SPI scenario requires 82 ms")
        lines = [
            'mach create "nrf52840-spi-sensor"',
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
            temperature = Decimal(phase["applied_q4"]) / 16
            lines.append(f"sysbus.spi0.temperature Temperature {temperature}")
            if index == 1 and self.fault == "frame-slip":
                lines += [
                    'console_log "SIM_SPI_SLIP"',
                    "console_log `sysbus.spi0.temperature Transmit 0`",
                ]
            lines += [
                f'emulation RunFor "{(phase["observe_at_ms"] - start) / 1000:.3f}"',
                "sysbus.timer0 WriteDoubleWord 0x4c 1",
                f'console_log "SIM_SPI_OBS_{index}"',
                "console_log `sysbus.spi0.temperature Temperature`",
                "console_log `sysbus.spi0 ReadDoubleWord 0x500`",
                "console_log `sysbus.timer0 ReadDoubleWord 0x54c`",
            ]
            cursor = phase["observe_at_ms"]
        lines += [
            f'emulation RunFor "{(virtual_ms - cursor) / 1000:.3f}"',
            "sysbus.uart0 CloseFileBackend $ORIGIN/uart.bin",
            'log "SIM_RUNNER_COMPLETED_V1"',
            "quit",
            "",
        ]
        return "\n".join(lines)

    def evaluate(self, uart, assertions, stdout):
        evidence = dict(
            nominal_regression_passed=False,
            nominal_failure=None,
            fault=dict(id=self.fault, effect_observed=None, status="not_evaluated"),
            samples=[],
            model_observations=[],
            frame_slip_observation=None,
            **self.COVERAGE,
        )

        def fail(message):
            evidence["nominal_failure"] = message
            raise AssertionFailure(message, evidence)

        try:
            observations, slip = parse_observations(stdout, self.fault)
        except (ValueError, AssertionFailure) as error:
            fail(str(error))
        evidence.update(model_observations=observations, frame_slip_observation=slip)
        try:
            lines = uart.decode("ascii", errors="strict").splitlines()
        except UnicodeError:
            fail("non-ASCII SPI sensor UART")
        evidence["ready_matches"] = lines[:1] == [assertions["ready"]]
        evidence["build_matches"] = lines[1:2] == [assertions["build"]]
        evidence["sample_lines_seen"] = max(0, len(lines) - 2)
        evidence["sample_summary_truncated"] = len(lines) > 18
        pattern = (
            re.escape(assertions["heartbeat_prefix"])
            + r" ([0-9A-F]{8}) R=([0-9A-F]{4}) Q=([0-9A-F]{8})"
            r" T=([0-9A-F]{8}) V=([0-9A-F]{8}) E=([0-9A-F]{8})"
        )
        nominal = True
        malformed = False
        for index, line in enumerate(lines[2:18], 1):
            match = re.fullmatch(pattern, line)
            if match is None:
                malformed = True
                continue
            sequence, raw, q4, ticks, vector, error = (int(v, 16) for v in match.groups())
            q4 = q4 if q4 < 2**31 else q4 - 2**32
            evidence["samples"].append(
                dict(
                    sequence=sequence,
                    raw_word=raw,
                    temperature_q4=q4,
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
                and raw == wire_word(TEMPERATURES_Q4[(index - 1) // 4])
                and q4 == TEMPERATURES_Q4[(index - 1) // 4]
            )
        actual = [o["temperature_q4"] for o in observations]
        model_nominal = actual == list(TEMPERATURES_Q4) and all(
            o["spi_enabled"] == 1 and abs(o["timer_counter"] - (20 + 20 * i) * 250) <= 25
            for i, o in enumerate(observations)
        )
        evidence["model_nominal"] = model_nominal
        effect = None
        if self.fault == "temperature-stuck":
            effect = actual == [TEMPERATURES_Q4[0]] * 4
        elif self.fault == "frame-slip" and len(evidence["samples"]) == 16:
            effect = (
                model_nominal
                and slip["discarded_byte"] == (wire_word(TEMPERATURES_Q4[1]) >> 8)
                and [s["raw_word"] for s in evidence["samples"]] == expected_words("frame-slip")
                and all(
                    s["sequence"] == i and s["error"] == 0
                    for i, s in enumerate(evidence["samples"], 1)
                )
            )
        evidence["fault"].update(
            effect_observed=effect,
            status="not_requested"
            if self.fault == "none"
            else "observed"
            if effect
            else "unknown"
            if effect is None
            else "not_observed",
        )
        if (
            not uart.endswith(b"\n")
            or not evidence["ready_matches"]
            or not evidence["build_matches"]
            or assertions["minimum"] != 16
            or len(lines) != 18
            or malformed
        ):
            fail("SPI READY/BUILD or complete 16-sample protocol mismatch")
        if not nominal or not model_nominal:
            fail("nominal LM74 frame, signed Q4, sequence, SPI0 or TIMER0 regression failed")
        evidence["nominal_regression_passed"] = True
        if self.fault != "none":
            fail("selected SPI sensor fault did not violate the nominal regression")
        return evidence


def parse_observations(stdout, fault):
    def check(condition, message):
        if not condition:
            raise AssertionFailure(message, {})

    def span(match):
        return dict(
            stdout_offset=match.start(), stdout_bytes=len(match[0]), stdout_sha256=sha(match[0])
        )

    check(
        re.findall(rb"(?m)^SIM_SPI_OBS_([^\r\n]*)", stdout) == [b"0", b"1", b"2", b"3"],
        "SPI model observation inventory/order differs",
    )
    result = []
    for index in range(4):
        pattern = (
            rb"(?m)^SIM_SPI_OBS_"
            + str(index).encode()
            + rb"\r?\n(-?[0-9]{1,3}(?:\.[0-9]{1,4})?)\r?\n([0-9]{1,10})\r?\n([0-9]{1,10})\r?\n"
        )
        matches = list(re.finditer(pattern, stdout))
        check(len(matches) == 1, "SPI model snapshot missing/duplicate/partial")
        match = matches[0]
        q4 = Decimal(match[1].decode()) * 16
        enabled, counter = (int(v) for v in match.groups()[1:])
        check(
            -880 <= q4 <= 2400
            and q4 == int(q4)
            and enabled in (0, 1)
            and 0 <= counter <= 0xFFFFFFFF,
            "SPI model snapshot out of range/non-Q4",
        )
        result.append(
            dict(
                phase=index,
                requested_virtual_ms=20 + index * 20,
                temperature_q4=int(q4),
                spi_enabled=enabled,
                timer_counter=counter,
                **span(match),
            )
        )
    markers = re.findall(rb"(?m)^SIM_SPI_SLIP([^\r\n]*)", stdout)
    check(markers == ([b""] if fault == "frame-slip" else []), "SPI slip marker differs")
    slip = None
    if fault == "frame-slip":
        matches = list(re.finditer(rb"(?m)^SIM_SPI_SLIP\r?\n([0-9]{1,3})\r?\n", stdout))
        check(len(matches) == 1, "SPI slip observation missing/duplicate/partial")
        match = matches[0]
        check(int(match[1]) <= 255, "SPI slip byte out of range")
        check(
            result[0]["stdout_offset"] < match.start() < result[1]["stdout_offset"],
            "SPI slip observation out of order",
        )
        slip = dict(requested_virtual_ms=22, discarded_byte=int(match[1]), **span(match))
    return result, slip


def profile_from_plan(plan):
    return SPIProfile(plan["scenario"]["fault"]["id"])


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
            report["ok"] is False and bool(report["errors"]), "missing SPI evidence cannot pass"
        )
        require(
            all(e["code"] != "ASSERTION_FAILED" for e in report["errors"]),
            "failed SPI analysis must preserve evidence",
        )
    else:
        process = report["process"]
        require(
            process["error"] is None
            and process["exit_code"] == 0
            and process["forced_termination"] is False
            and report["virtual_time"]["completed"] is True,
            "SPI analysis requires completed backend/schedule",
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
                "failed SPI verdict differs",
            )
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "SPI evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
