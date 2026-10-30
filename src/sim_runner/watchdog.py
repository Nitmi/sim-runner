"""Fixed dual-reload watchdog regression with raw reset and model-state evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-watchdog-v1"
PLAN_SCHEMA = "embedded-sim-runner.watchdog-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.watchdog-report.v1"
MODEL = ASSETS / "nrf52840-watchdog.repl"
VIRTUAL_MS = 82
FAULTS = ("none", "feed-timer-stop", "watchdog-stop")
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "physical_reset_verified": False,
    "low_power_behavior_verified": False,
    "watchdog_semantics": "Renode_1.17_running_state_dual_RR_magic_and_RequestReset",
    "fault_semantics": "fixed_model_timer_stop_or_LimitTimer_disable_not_physical_fault",
    "reset_evidence_semantics": "raw_watchdog_log_plus_guest_reboot_and_paused_snapshots",
    "timer_observation_semantics": "guest_250kHz_TIMER0_IRQ_capture_not_host_time",
}


class WatchdogProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed watchdog fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        self.SCENARIO = dict(
            watchdog_frequency_hz=32768,
            reload_value=1023,
            reload_channels=[0, 1],
            reload_magic=0x6E524635,
            timer_frequency_hz=250000,
            period_ticks=1250,
            capture_tolerance_ticks=25,
            observations_ms=[20, 40, 60, 80],
            fault=dict(id=fault, requested_virtual_ms=None if fault == "none" else 22),
            coverage_limits=[
                "no_CONFIG_sleep_halt_or_physical_clock_semantics",
                "no_timeout_IRQ_or_two_cycle_reset_delay_coverage",
                "no_RESETREAS_retained_RAM_or_power_reset_proof",
                "no_physical_watchdog_protection_or_security_qualification",
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
        require(type(virtual_ms) is int and virtual_ms == 82, "watchdog scenario requires 82 ms")
        require(
            type(min_heartbeats) is int and min_heartbeats == 16,
            "watchdog scenario requires 16 nominal samples",
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
            "watchdog plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == 82, "watchdog scenario requires 82 ms")
        lines = [
            'mach create "nrf52840-watchdog"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            'emulation SetGlobalQuantum "0.0001"',
            "macro reset",
            '"""',
            'console_log "SIM_WDT_RESET_MACRO"',
            '"""',
        ]
        cursor = 0
        for index, observe in enumerate((20, 40, 60, 80)):
            if index == 1 and self.fault != "none":
                lines += [
                    'emulation RunFor "0.002"',
                    'console_log "SIM_WDT_FAULT"',
                    "sysbus.timer0 WriteDoubleWord 4 1"
                    if self.fault == "feed-timer-stop"
                    else "sysbus.watchdog Enabled false",
                ]
                cursor = 22
            lines += [
                f'emulation RunFor "{(observe - cursor) / 1000:.3f}"',
                "sysbus.timer0 WriteDoubleWord 0x4c 1",
                f'console_log "SIM_WDT_OBS_{index}"',
                *[
                    f"console_log `sysbus.watchdog ReadDoubleWord {offset}`"
                    for offset in (0x400, 0x404, 0x504, 0x508)
                ],
                "console_log `sysbus.watchdog Value`",
                "console_log `sysbus.timer0 ReadDoubleWord 0x54c`",
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
            boots=[],
            model_observations=[],
            reset_observations=[],
            **self.COVERAGE,
        )

        def fail(message):
            evidence["nominal_failure"] = message
            raise AssertionFailure(message, evidence)

        try:
            observations, resets = parse_observations(stdout, self.fault)
        except AssertionFailure as error:
            fail(str(error))
        evidence.update(model_observations=observations, reset_observations=resets)
        try:
            lines = uart.decode("ascii", errors="strict").splitlines()
        except UnicodeError:
            fail("non-ASCII watchdog UART")
        if not uart.endswith(b"\n") or len(lines) > 64:
            fail("partial or oversized watchdog UART protocol")
        pattern = (
            re.escape(assertions["heartbeat_prefix"])
            + r" ([0-9A-F]{8}) A=([0-9A-F]{8}) B=([0-9A-F]{8})"
            r" S=([0-9A-F]{8}) T=([0-9A-F]{8}) V=([0-9A-F]{8})"
        )
        index = 0
        while index < len(lines):
            if lines[index : index + 2] != [assertions["ready"], assertions["build"]]:
                fail("watchdog READY/BUILD or reboot protocol mismatch")
            index += 2
            boot = dict(samples=[])
            evidence["boots"].append(boot)
            while index < len(lines) and lines[index] != assertions["ready"]:
                match = re.fullmatch(pattern, lines[index])
                if match is None:
                    fail("malformed watchdog sample")
                seq, partial, complete, running, ticks, vector = (
                    int(v, 16) for v in match.groups()
                )
                boot["samples"].append(
                    dict(
                        sequence=seq,
                        partial_requests=partial,
                        complete_requests=complete,
                        running=running,
                        timer_ticks=ticks,
                        irq_vector=vector,
                    )
                )
                index += 1
        boots = evidence["boots"]
        samples_valid = bool(boots) and all(
            bool(b["samples"])
            and all(
                s["sequence"] == i
                and s["partial_requests"] == 2
                and s["complete_requests"] == 3
                and s["irq_vector"] == 24
                and abs(s["timer_ticks"] - i * 1250) <= 25
                for i, s in enumerate(b["samples"], 1)
            )
            for b in boots
        )
        state_valid = all(
            o["requests"] == 3
            and o["reload_value"] == 1023
            and o["reload_enabled"] == 3
            and 1 <= o["watchdog_counter"] <= 1024
            for o in observations
        )
        timer_nominal = all(
            abs(o["timer_counter"] - (20 + i * 20) * 250) <= 25 for i, o in enumerate(observations)
        )
        effect = None
        if self.fault == "feed-timer-stop":
            effect = (
                samples_valid
                and state_valid
                and len(resets) == 1
                and len(boots) == 2
                and [len(b["samples"]) for b in boots] == [4, 6]
                and all(s["running"] == 1 for b in boots for s in b["samples"])
                and all(o["running"] == 1 for o in observations)
                and abs(observations[0]["timer_counter"] - 5000) <= 25
                and abs(observations[1]["timer_counter"] - 5500) <= 25
                and abs(observations[0]["watchdog_counter"] - 860) <= 8
                and abs(observations[1]["watchdog_counter"] - 369) <= 8
                and abs(observations[2]["timer_counter"] - 2187) <= 125
                and abs(observations[3]["timer_counter"] - 7187) <= 125
                and observations[1]["stdout_offset"]
                < resets[0]["stdout_offset"]
                < observations[2]["stdout_offset"]
            )
        elif self.fault == "watchdog-stop":
            effect = (
                samples_valid
                and state_valid
                and timer_nominal
                and not resets
                and len(boots) == 1
                and len(boots[0]["samples"]) == 16
                and [s["running"] for s in boots[0]["samples"]] == [1] * 4 + [0] * 12
                and [o["running"] for o in observations] == [1, 0, 0, 0]
                and abs(observations[0]["watchdog_counter"] - 860) <= 8
                and all(o["watchdog_counter"] == 1024 for o in observations[1:])
            )
        evidence["fault"].update(
            effect_observed=effect,
            status="not_requested"
            if self.fault == "none"
            else "observed"
            if effect
            else "not_observed",
        )
        nominal = (
            assertions["minimum"] == 16
            and samples_valid
            and state_valid
            and timer_nominal
            and not resets
            and len(boots) == 1
            and len(boots[0]["samples"]) == 16
            and all(s["running"] == 1 for s in boots[0]["samples"])
            and all(o["running"] == 1 for o in observations)
            and all(abs(o["watchdog_counter"] - 860) <= 8 for o in observations)
        )
        if not nominal:
            fail("nominal dual-reload, watchdog state, reset or TIMER0 regression failed")
        evidence["nominal_regression_passed"] = True
        if self.fault != "none":
            fail("selected watchdog fault did not violate the nominal regression")
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
        re.findall(rb"(?m)^SIM_WDT_OBS_([^\r\n]*)", stdout) == [b"0", b"1", b"2", b"3"],
        "watchdog model observation inventory/order differs",
    )
    result = []
    for index in range(4):
        matches = list(
            re.finditer(
                rb"(?m)^SIM_WDT_OBS_"
                + str(index).encode()
                + rb"\r?\n"
                + rb"([0-9]{1,10})\r?\n" * 6,
                stdout,
            )
        )
        check(len(matches) == 1, "watchdog model snapshot missing/duplicate/partial")
        match = matches[0]
        running, requests, reload_value, enabled, counter, timer = map(int, match.groups())
        check(
            running in (0, 1)
            and requests <= 255
            and enabled <= 255
            and max(reload_value, counter, timer) <= 0xFFFFFFFF,
            "watchdog model snapshot out of range",
        )
        result.append(
            dict(
                phase=index,
                requested_virtual_ms=20 + index * 20,
                running=running,
                requests=requests,
                reload_value=reload_value,
                reload_enabled=enabled,
                watchdog_counter=counter,
                timer_counter=timer,
                **span(match),
            )
        )
    markers = list(re.finditer(rb"(?m)^SIM_WDT_FAULT([^\r\n]*)\r?\n", stdout))
    check(
        [m[1] for m in markers] == ([] if fault == "none" else [b""]),
        "watchdog fault marker inventory differs",
    )
    if markers:
        check(
            result[0]["stdout_offset"] < markers[0].start() < result[1]["stdout_offset"],
            "watchdog fault marker out of order",
        )
    resets = [
        dict(**span(m))
        for m in re.finditer(
            rb"(?m)^[^\r\n]*\[INFO\] watchdog: Reseting machine\r?\n",
            stdout,
        )
    ]
    check(len(resets) <= 3, "watchdog reset budget exceeded")
    macros = list(re.finditer(rb"(?m)^SIM_WDT_RESET_MACRO\r?\n", stdout))
    check(len(macros) == len(resets), "watchdog reset callback inventory differs")
    for reset, macro in zip(resets, macros, strict=True):
        check(reset["stdout_offset"] < macro.start(), "watchdog reset callback out of order")
        reset["callback"] = span(macro)
    return result, resets


def profile_from_plan(plan):
    return WatchdogProfile(plan["scenario"]["fault"]["id"])


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
            report["ok"] is False and bool(report["errors"]), "missing WDT evidence cannot pass"
        )
        require(
            all(e["code"] != "ASSERTION_FAILED" for e in report["errors"]),
            "failed WDT analysis must preserve evidence",
        )
    else:
        process = report["process"]
        require(
            process["error"] is None
            and process["exit_code"] == 0
            and process["forced_termination"] is False
            and report["virtual_time"]["completed"] is True,
            "WDT analysis requires completed backend/schedule",
        )
        require(
            process["cleanup"]
            == dict(process_exited=True, job_active_processes=0, capture_threads_stopped=True)
            and report["input_hashes_current"] is True
            and report["executables_started"] is True
            and report["evidence_origin"] == "renode_execution_attempt",
            "WDT analysis requires current inputs and complete cleanup",
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
                "failed WDT verdict differs",
            )
        else:
            require(report["ok"] is True and report["errors"] == [], "passed WDT verdict differs")
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "WDT evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
