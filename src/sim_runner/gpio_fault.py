"""Fixed model fault inputs, observed effects and unchanged nominal verdict policy."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import gpio_timer
from .common import AssertionFailure, SimError, bound, decode, require, sha

PROFILE = "nrf52840-gpio-timer-fault-v1"
PLAN_SCHEMA = "embedded-sim-runner.gpio-fault-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.gpio-fault-report.v1"
FAULTS = ("none", "gpio-stuck-low", "gpio-stuck-high", "timer-stop", "timer-irq-mask")
VIRTUAL_MS = gpio_timer.VIRTUAL_MS
MODEL = gpio_timer.MODEL
AT_MS = 22


def scenario(fault):
    require(type(fault) is str and fault in FAULTS, "unsupported fixed fault scenario")
    phases = json.loads(json.dumps(gpio_timer.PHASES))
    for phase in phases:
        phase["applied_input"] = (
            int(fault == "gpio-stuck-high")
            if fault.startswith("gpio-stuck-") and phase["input_at_ms"] >= AT_MS
            else phase["input"]
        )
    return {
        **gpio_timer.SCENARIO,
        "phases": phases,
        "fault": {"id": fault, "requested_virtual_ms": None if fault == "none" else AT_MS},
        "model_counter_observation": "TASKS_CAPTURE3_then_CC3_while_paused_no_channel3_irq",
        "model_irq_observation": "TIMER0_INTENSET_COMPARE0_bit16_while_paused",
    }


class FaultProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    VIRTUAL_MS = VIRTUAL_MS
    MODEL = MODEL

    def __init__(self, fault):
        self.SCENARIO = scenario(fault)
        self.fault = fault
        self.COVERAGE = {
            **gpio_timer.COVERAGE,
            "fault_scenario": fault,
            "fault_scope": "fixed_paused_model_input_not_physical_fault_or_root_cause_proof",
        }

    def make_plan(self, *args, **kwargs):
        plan = gpio_timer.make_plan(*args, **kwargs)
        plan.update(
            schema_version=PLAN_SCHEMA,
            profile=PROFILE,
            scenario=json.loads(json.dumps(self.SCENARIO)),
            script_sha256=sha(self.script(plan["virtual_ms"]).encode()),
            **self.COVERAGE,
        )
        return plan

    def load_plan(self, path, expected):
        plan = decode(bound(path, expected, 65536))
        wanted = self.make_plan(
            Path(plan["runtime_lock"]["path"]),
            plan["runtime_lock"]["sha256"],
            Path(plan["firmware"]["path"]),
            plan["firmware"]["sha256"],
            plan["assertions"]["ready"],
            plan["assertions"]["build"],
            plan["assertions"]["heartbeat_prefix"],
            plan["assertions"]["minimum"],
            plan["virtual_ms"],
            plan["host_timeout_seconds"],
            plan["output_limit_bytes_per_file"],
            plan["job_memory_limit_mib"],
        )
        require(
            json.dumps(plan, sort_keys=True) == json.dumps(wanted, sort_keys=True),
            "fault plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return plan

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == VIRTUAL_MS, "invalid fault duration")
        lines = [
            'mach create "nrf52840-gpio-timer-fault"',
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
            if start == AT_MS and self.fault == "timer-stop":
                lines.append("sysbus.timer0 WriteDoubleWord 4 1")
            if start == AT_MS and self.fault == "timer-irq-mask":
                lines.append("sysbus.timer0 WriteDoubleWord 0x308 0x10000")
            lines.extend(
                [
                    f"sysbus.gpio0 OnGPIO 11 {'true' if phase['applied_input'] else 'false'}",
                    f'emulation RunFor "{(phase["observe_at_ms"] - start) / 1000:.3f}"',
                    # Channel 3 is unused by the fixed fixture and has no enabled IRQ.
                    "sysbus.timer0 WriteDoubleWord 0x4c 1",
                    f'console_log "SIM_FAULT_OBS_{index}"',
                    "console_log `sysbus.gpio0 ReadDoubleWord 4`",
                    "console_log `sysbus.gpio0 ReadDoubleWord 0x10`",
                    "console_log `sysbus.timer0 ReadDoubleWord 0x54c`",
                    "console_log `sysbus.timer0 ReadDoubleWord 0x304`",
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
        evidence = {
            "nominal_regression_passed": False,
            "fault": {"id": self.fault, "effect_observed": None, "status": "not_evaluated"},
            "samples": [],
            "model_observations": [],
            "nominal_failure": None,
            **self.COVERAGE,
        }
        # A requested injection or a printed command is not evidence of an effect.
        try:
            observations = parse_observations(stdout)
        except SimError as error:
            evidence["nominal_failure"] = str(error)
            raise AssertionFailure(str(error), evidence) from error
        evidence["model_observations"] = observations
        evidence["model_timer_nominal"] = all(
            abs(item["timer_counter"] - phase["observe_at_ms"] * 250) <= 25
            and item["interrupt_enable"] & 0x10000 == 0x10000
            for item, phase in zip(observations, gpio_timer.PHASES, strict=True)
        )
        observed = self.effect_observed(observations)
        evidence["fault"].update(
            effect_observed=observed,
            status="not_requested"
            if self.fault == "none"
            else "observed"
            if observed
            else "not_observed",
        )
        try:
            text = uart.decode("ascii", errors="strict")
        except UnicodeError as error:
            evidence["nominal_failure"] = "non-ASCII fault UART"
            raise AssertionFailure("non-ASCII fault UART", evidence) from error
        sample_pattern = (
            re.escape(assertions["heartbeat_prefix"])
            + r" ([0-9A-F]{8}) I=([01]) O=([01]) L=([01]) T=([0-9A-F]{8}) V=([0-9A-F]{8})"
        )
        evidence["sample_lines_seen"] = max(0, len(text.splitlines()) - 2)
        evidence["sample_summary_truncated"] = evidence["sample_lines_seen"] > 16
        for line in text.splitlines()[2:18]:
            match = re.fullmatch(sample_pattern, line)
            if match:
                values = [int(value, 16) for value in match.groups()]
                evidence["samples"].append(
                    dict(
                        zip(
                            (
                                "sequence",
                                "input",
                                "output",
                                "loopback",
                                "timer_ticks",
                                "irq_vector",
                            ),
                            values,
                            strict=True,
                        )
                    )
                )
        evidence["ready_matches"] = text.splitlines()[:1] == [assertions["ready"]]
        evidence["build_matches"] = text.splitlines()[1:2] == [assertions["build"]]
        normalized = b"".join(
            f"SIM_GPIO_OBS_{item['phase']}\n{item['out_register']}\n{item['in_register']}\n".encode()
            for item in observations
        )
        try:
            nominal = gpio_timer.evaluate(uart, assertions, normalized)
        except (SimError, UnicodeError) as error:
            evidence["nominal_failure"] = str(error)
            raise AssertionFailure(str(error), evidence) from error
        if not evidence["model_timer_nominal"]:
            evidence["nominal_failure"] = "independent TIMER0 model counter/IRQ state differs"
            raise AssertionFailure(evidence["nominal_failure"], evidence)
        evidence["nominal_regression_passed"] = True
        evidence["nominal_assertions"] = nominal
        # A fault selection whose effect is not observed can never produce a pass.
        if self.fault != "none":
            evidence["nominal_failure"] = "selected fault did not violate the nominal regression"
            raise AssertionFailure(evidence["nominal_failure"], evidence)
        return evidence

    def effect_observed(self, observations):
        if self.fault == "none":
            return None
        inputs = [(item["in_register"] >> 11) & 1 for item in observations]
        counts = [item["timer_counter"] for item in observations]
        irq = [(item["interrupt_enable"] >> 16) & 1 for item in observations]
        if self.fault.startswith("gpio-stuck-"):
            return inputs == [phase["applied_input"] for phase in self.SCENARIO["phases"]]
        if self.fault == "timer-stop":
            return (
                4975 <= counts[0] <= 5025
                and 5475 <= counts[1] <= 5525
                and counts[1:] == [counts[1]] * 3
                and irq == [1] * 4
            )
        return irq == [1, 0, 0, 0] and all(
            abs(count - phase["observe_at_ms"] * 250) <= 25
            for count, phase in zip(counts, self.SCENARIO["phases"], strict=True)
        )


def parse_observations(stdout):
    require(
        re.findall(rb"(?m)^SIM_FAULT_OBS_([^\r\n]*)", stdout) == [b"0", b"1", b"2", b"3"],
        "fault observation inventory/order differs",
        "ASSERTION_FAILED",
    )
    observations = []
    for index, phase in enumerate(gpio_timer.PHASES):
        matches = re.findall(
            rb"(?m)^SIM_FAULT_OBS_"
            + str(index).encode()
            + rb"\r?\n([0-9]{1,10})\r?\n([0-9]{1,10})\r?\n([0-9]{1,10})\r?\n([0-9]{1,10})\r?\n",
            stdout,
        )
        require(len(matches) == 1, "fault model snapshot missing/duplicate", "ASSERTION_FAILED")
        values = [int(value) for value in matches[0]]
        require(
            all(0 <= value <= 0xFFFFFFFF for value in values),
            "fault snapshot out of range",
            "ASSERTION_FAILED",
        )
        observations.append(
            {
                "phase": index,
                "requested_virtual_ms": phase["observe_at_ms"],
                **dict(
                    zip(
                        ("out_register", "in_register", "timer_counter", "interrupt_enable"),
                        values,
                        strict=True,
                    )
                ),
            }
        )
    return observations


def profile_from_plan(plan):
    return FaultProfile(plan["scenario"]["fault"]["id"])


def load_plan(path, expected):
    return profile_from_plan(decode(bound(path, expected, 65536))).load_plan(path, expected)


def run(path, expected, directory):
    from .run import run as native_run

    profile = profile_from_plan(decode(bound(path, expected, 65536)))
    return native_run(path, expected, directory, profile=profile)


def inspect_report(path, expected):
    from .backend import check_logs
    from .inspect import inspect_report as native_inspect

    report = decode(bound(path, expected, 1024 * 1024))
    plan_ref = report["artifacts"]["plan.json"]
    plan = decode(bound(path.parent / "plan.json", plan_ref["sha256"], 65536))
    profile = profile_from_plan(plan)
    result = native_inspect(path, expected, profile=profile)
    saved = report["assertions"]
    if saved is not None:
        process = report["process"]
        require(
            process["error"] is None
            and process["exit_code"] == 0
            and process["forced_termination"] is False,
            "fault analysis requires completed backend",
        )
        require(
            report["virtual_time"]["completed"] is True, "fault analysis requires complete schedule"
        )
        stdout = bound(
            path.parent / "backend.stdout.bin",
            report["artifacts"]["backend.stdout.bin"]["sha256"],
            4 * 1024 * 1024,
        )
        stderr = bound(
            path.parent / "backend.stderr.bin",
            report["artifacts"]["backend.stderr.bin"]["sha256"],
            4 * 1024 * 1024,
        )
        uart = bound(
            path.parent / "uart.bin", report["artifacts"]["uart.bin"]["sha256"], 4 * 1024 * 1024
        )
        check_logs(stdout, stderr)
        try:
            evaluated = profile.evaluate(uart, plan["assertions"], stdout)
        except AssertionFailure as error:
            evaluated = error.evidence
            require(
                report["ok"] is False
                and isinstance(report["errors"], list)
                and bool(report["errors"])
                and report["errors"][0] == {"code": error.code, "message": str(error)},
                "failed fault verdict differs",
            )
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "fault evidence summary differs",
        )
    else:
        require(
            report["ok"] is False and bool(report["errors"]), "missing fault evidence cannot pass"
        )
        require(
            all(error["code"] != "ASSERTION_FAILED" for error in report["errors"]),
            "failed fault analysis must preserve its evidence",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
