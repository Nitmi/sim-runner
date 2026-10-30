"""Fixed 1.17 RTC0 compare IRQ regression, distinct from physical/low-power timing."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-rtc-v1"
PLAN_SCHEMA = "embedded-sim-runner.rtc-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.rtc-report.v1"
MODEL = ASSETS / "nrf52840-rtc.repl"
VIRTUAL_MS = 162
FAULTS = ("none", "rtc-stop", "rtc-irq-mask")
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "low_power_behavior_verified": False,
    "rtc_semantics": "Renode_1.17_32768Hz_divider_and_COMPARE0_IRQ_not_LFCLK_hardware",
    "fault_semantics": "fixed_RTC_TASKS_STOP_or_INTENCLR_not_physical_fault",
    "observation_semantics": "paused_model_registers_and_guest_IRQ_samples_not_host_time",
}


class RTCProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed RTC fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        self.SCENARIO = dict(
            source_frequency_hz=32768,
            prescaler=31,
            counter_frequency_hz=1024,
            compare_period_ticks=16,
            counter_tolerance_ticks=2,
            observations_ms=[40, 80, 120, 160],
            fault=dict(id=fault, requested_virtual_ms=None if fault == "none" else 42),
            coverage_limits=[
                "no_LFCLK_startup_selection_drift_or_physical_clock_measurement",
                "no_tick_overflow_TRIGOVRFLW_wrap_or_PPI_coverage",
                "no_sleep_power_wakeup_or_complete_RTC_qualification",
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
        min_heartbeats=10,
        virtual_ms=162,
        timeout=30,
        output_limit=1048576,
        memory_mib=1024,
    ):
        require(type(virtual_ms) is int and virtual_ms == 162, "RTC scenario requires 162 ms")
        require(
            type(min_heartbeats) is int and min_heartbeats == 10, "RTC requires 10 nominal samples"
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
            "RTC plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == 162, "RTC scenario requires 162 ms")
        lines = [
            'mach create "nrf52840-rtc"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            'emulation SetGlobalQuantum "0.0001"',
        ]
        cursor = 0
        for index, observe in enumerate((40, 80, 120, 160)):
            if index == 1 and self.fault != "none":
                lines += [
                    'emulation RunFor "0.002"',
                    'console_log "SIM_RTC_FAULT"',
                    "sysbus.rtc0 WriteDoubleWord 4 1"
                    if self.fault == "rtc-stop"
                    else "sysbus.rtc0 WriteDoubleWord 0x308 0x10000",
                ]
                cursor = 42
            lines += [
                f'emulation RunFor "{(observe - cursor) / 1000:.3f}"',
                f'console_log "SIM_RTC_OBS_{index}"',
                *[
                    f"console_log `sysbus.rtc0 ReadDoubleWord {offset}`"
                    for offset in (0x504, 0x508, 0x540, 0x140, 0x304)
                ],
            ]
            cursor = observe
        lines += [
            'emulation RunFor "0.002"',
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
            **self.COVERAGE,
        )

        def fail(message):
            evidence["nominal_failure"] = message
            raise AssertionFailure(message, evidence)

        try:
            observations = parse_observations(stdout, self.fault)
        except AssertionFailure as error:
            fail(str(error))
        evidence["model_observations"] = observations
        try:
            lines = uart.decode("ascii", errors="strict").splitlines()
        except UnicodeError:
            fail("non-ASCII RTC UART")
        if not uart.endswith(b"\n") or not 2 <= len(lines) <= 66:
            fail("partial or oversized RTC UART protocol")
        if lines[:2] != [assertions["ready"], assertions["build"]]:
            fail("RTC READY/BUILD differs")
        pattern = re.escape(assertions["heartbeat_prefix"]) + (
            r" ([0-9A-F]{8}) C=([0-9A-F]{8}) E=([0-9A-F]{8}) A=([0-9A-F]{8})"
            r" N=([0-9A-F]{8}) P=([0-9A-F]{8}) V=([0-9A-F]{8})"
        )
        for line in lines[2:]:
            match = re.fullmatch(pattern, line)
            if match is None:
                fail("malformed RTC sample")
            seq, counter, before, after, compare, prescaler, vector = (
                int(v, 16) for v in match.groups()
            )
            evidence["samples"].append(
                dict(
                    sequence=seq,
                    counter=counter,
                    event_before=before,
                    event_after=after,
                    next_compare=compare,
                    prescaler=prescaler,
                    irq_vector=vector,
                )
            )
        samples = evidence["samples"]
        sample_valid = bool(samples) and all(
            s["sequence"] == i
            and 16 * i <= s["counter"] <= 16 * i + 2
            and s["event_before"] == 1
            and s["event_after"] == 0
            and s["next_compare"] == 16 * (i + 1)
            and s["prescaler"] == 31
            and s["irq_vector"] == 27
            for i, s in enumerate(samples, 1)
        )
        clock_valid = all(
            abs(o["counter"] - ms * 1024 / 1000) <= 2
            for o, ms in zip(observations, (40, 80, 120, 160), strict=True)
        )
        nominal_model = clock_valid and all(
            o["prescaler"] == 31
            and o["compare"] == (o["counter"] // 16 + 1) * 16
            and o["event"] == 0
            and o["interrupt_mask"] == 65536
            for o in observations
        )
        nominal = sample_valid and len(samples) == 10 and nominal_model
        evidence["nominal_regression_passed"] = nominal
        if self.fault != "none":
            first, *later = observations
            common = (
                sample_valid
                and len(samples) == 2
                and first["prescaler"] == 31
                and abs(first["counter"] - 40.96) <= 2
                and first["compare"] == 48
                and first["event"] == 0
                and first["interrupt_mask"] == 65536
                and all(o["prescaler"] == 31 and o["compare"] == 48 for o in later)
            )
            if self.fault == "rtc-stop":
                effect = (
                    common
                    and abs(later[0]["counter"] - 43.008) <= 2
                    and all(
                        o["counter"] == later[0]["counter"]
                        and o["event"] == 0
                        and o["interrupt_mask"] == 65536
                        for o in later
                    )
                )
            else:
                effect = (
                    common
                    and clock_valid
                    and all(o["event"] == 1 and o["interrupt_mask"] == 0 for o in later)
                )
            evidence["fault"].update(
                effect_observed=effect, status="observed" if effect else "not_observed"
            )
            fail(
                "RTC fault observed; nominal regression failed"
                if effect
                else "requested RTC fault effect not established"
            )
        if not nominal:
            fail("RTC compare IRQ, divider, event clear, sample count or model state differs")
        return evidence


def parse_observations(stdout, fault):
    def check(condition, message):
        if not condition:
            raise AssertionFailure(message, {})

    pattern = rb"(?m)^SIM_RTC_OBS_([0-3])\r?\n" + rb"([0-9]{1,10})\r?\n" * 5
    matches = list(re.finditer(pattern, stdout))
    markers = list(re.finditer(rb"(?m)^SIM_RTC_OBS_([^\r\n]*)\r?\n", stdout))
    check(
        len(markers) == len(matches) == 4 and [m[1] for m in matches] == [b"0", b"1", b"2", b"3"],
        "RTC observation inventory/order differs",
    )
    result = []
    for match in matches:
        counter, prescaler, compare, event, mask = (int(x) for x in match.groups()[1:])
        check(
            counter < 2**24
            and prescaler < 4096
            and compare < 2**24
            and event <= 1
            and mask in (0, 65536),
            "RTC observation out of range",
        )
        result.append(
            dict(
                index=int(match[1]),
                counter=counter,
                prescaler=prescaler,
                compare=compare,
                event=event,
                interrupt_mask=mask,
                stdout_offset=match.start(),
                stdout_bytes=len(match[0]),
                stdout_sha256=sha(match[0]),
            )
        )
    faults = list(re.finditer(rb"(?m)^SIM_RTC_FAULT([^\r\n]*)\r?\n", stdout))
    check(
        [m[1] for m in faults] == ([] if fault == "none" else [b""]),
        "RTC fault marker inventory differs",
    )
    if faults:
        check(
            matches[0].end() <= faults[0].start() < matches[1].start(),
            "RTC fault marker out of order",
        )
    return result


def profile_from_plan(plan):
    return RTCProfile(plan["scenario"]["fault"]["id"])


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
            report["ok"] is False
            and bool(report["errors"])
            and all(e["code"] != "ASSERTION_FAILED" for e in report["errors"]),
            "missing RTC assertion evidence cannot pass",
        )
    else:
        process = report["process"]
        require(
            process["error"] is None
            and process["exit_code"] == 0
            and process["forced_termination"] is False
            and report["virtual_time"]["completed"] is True
            and process["cleanup"]
            == dict(process_exited=True, job_active_processes=0, capture_threads_stopped=True)
            and report["input_hashes_current"] is True
            and report["executables_started"] is True
            and report["evidence_origin"] == "renode_execution_attempt",
            "RTC analysis requires complete execution, current inputs and cleanup",
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
                and report["errors"] == [dict(code=error.code, message=str(error))],
                "failed RTC verdict differs",
            )
        else:
            require(report["ok"] is True and report["errors"] == [], "passed RTC verdict differs")
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "RTC evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
