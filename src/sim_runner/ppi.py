"""Fixed TIMER0 -> PPI CH0/FORK -> GPIOTE task routing; no timing qualification."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-ppi-v1"
PLAN_SCHEMA = "embedded-sim-runner.ppi-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.ppi-report.v1"
MODEL = ASSETS / "nrf52840-ppi.repl"
VIRTUAL_MS = 40
FAULTS = ("none", "ppi-channel-disable", "ppi-fork-misroute")
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "event_time_alignment_verified": False,
    "ppi_routing_verified": False,
    "ppi_semantics": "Renode_1.17_TIMER0_COMPARE0_CH0_TEP_FORK_GPIOTE_OUT_subset",
    "fault_semantics": "fixed_monitor_CHENCLR_or_FORK_TEP_not_physical_fault",
    "observation_semantics": "paused_registers_and_guest_GPIO_poll_not_event_time_alignment",
}
# Fixed order binds every parsed value to its register and raw stdout span.
REGISTERS = (
    ("timer_event", "timer0", 0x140),
    ("timer_prescaler", "timer0", 0x510),
    ("timer_compare", "timer0", 0x540),
    ("channel_mask", "ppi", 0x500),
    ("event_endpoint", "ppi", 0x510),
    ("task_endpoint", "ppi", 0x514),
    ("fork_endpoint", "ppi", 0x910),
    ("config0", "gpiote", 0x510),
    ("config1", "gpiote", 0x514),
    ("config2", "gpiote", 0x518),
    ("gpio0_out", "gpio0", 0x4),
    ("gpio1_out", "gpio1", 0x4),
    ("gpio0_in", "gpio0", 0x10),
    ("gpio1_in", "gpio1", 0x10),
)


class PPIProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed PPI fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        self.SCENARIO = dict(
            event="TIMER0.EVENTS_COMPARE0",
            channel=0,
            task="GPIOTE.TASKS_OUT0_P0.11_toggle",
            fork="GPIOTE.TASKS_OUT1_P1.3_toggle",
            sentinel="GPIOTE.TASKS_OUT2_P0.13_toggle",
            stimulus_ms=list(range(2, 31, 4)),
            service_budget_ms=2,
            timer_prescaler=4,
            timer_compare=1000,
            fault=dict(id=fault, stimulus_index=None if fault == "none" else 4),
            observation_registers=[dict(name=n, peripheral=p, offset=o) for n, p, o in REGISTERS],
            coverage_limits=[
                "PPI_tasks_deferred_to_nearest_synced_state_no_event_time_alignment",
                "only_CH0_TEP_FORK_and_three_GPIOTE_OUT_channels_no_groups_or_fixed_channels",
                "monitor_TIMER_STOP_CLEAR_START_not_SHORTS_BITMODE_counter_mode_or_IRQ_timing",
                "inactive_RTC_RADIO_map_required_by_PPI_fixed_channel_subscription_not_radio_test",
                "GPIO_poll_and_model_snapshots_not_electrical_timing_or_full_silicon_coverage",
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
        min_heartbeats=8,
        virtual_ms=40,
        timeout=30,
        output_limit=1048576,
        memory_mib=1024,
    ):
        require(type(virtual_ms) is int and virtual_ms == 40, "PPI scenario requires 40 ms")
        require(
            type(min_heartbeats) is int and min_heartbeats == 8, "PPI requires 8 nominal samples"
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
            "PPI plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == 40, "PPI scenario requires 40 ms")
        lines = [
            'mach create "nrf52840-ppi"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            'emulation SetGlobalQuantum "0.0001"',
            'emulation RunFor "0.002"',
        ]
        for index in range(8):
            if index == 4 and self.fault != "none":
                lines += ['console_log "SIM_PPI_FAULT"']
                lines += (
                    ["sysbus.ppi WriteDoubleWord 0x508 1"]
                    if self.fault == "ppi-channel-disable"
                    else ["sysbus.ppi WriteDoubleWord 0x910 0x40006008"]
                )
            lines += [
                "sysbus.timer0 WriteDoubleWord 0x4 1",
                "sysbus.timer0 WriteDoubleWord 0xc 1",
                "sysbus.timer0 WriteDoubleWord 0x140 0",
            ]
            for phase in ("PRE", "POST"):
                if phase == "POST":
                    lines += [
                        "sysbus.timer0 WriteDoubleWord 0 1",
                        'emulation RunFor "0.002"',
                        "sysbus.timer0 WriteDoubleWord 0x4 1",
                    ]
                lines += [f'console_log "SIM_PPI_OBS_{index}_{phase}"']
                lines += [f"console_log `sysbus.{p} ReadDoubleWord {o}`" for _, p, o in REGISTERS]
            lines += ['emulation RunFor "0.002"']
        lines += [
            'emulation RunFor "0.006"',
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
            evidence["model_observations"] = parse_observations(stdout, self.fault)
        except AssertionFailure as error:
            fail(str(error))
        try:
            lines = uart.decode("ascii", errors="strict").splitlines()
        except UnicodeError:
            fail("non-ASCII PPI UART")
        if not uart.endswith(b"\n") or not 2 <= len(lines) <= 18:
            fail("partial or oversized PPI UART protocol")
        if lines[:2] != [assertions["ready"], assertions["build"]]:
            fail("PPI READY/BUILD differs")
        pattern = re.escape(assertions["heartbeat_prefix"]) + (
            r" ([0-9A-F]{8}) A=([0-9A-F]{8}) B=([0-9A-F]{8}) S=([0-9A-F]{8})"
        )
        for line in lines[2:]:
            match = re.fullmatch(pattern, line)
            if match is None:
                fail("malformed PPI sample")
            seq, a, b, sentinel = (int(v, 16) for v in match.groups())
            evidence["samples"].append(dict(sequence=seq, a=a, b=b, sentinel=sentinel))

        def matches_scenario(fault):
            a, b, sentinel, mask, fork = 0, 0, 0, 1, 0x40006004
            observations, samples = [], []
            for index in range(8):
                if index == 4 and fault == "ppi-channel-disable":
                    mask = 0
                if index == 4 and fault == "ppi-fork-misroute":
                    fork = 0x40006008
                for phase in ("PRE", "POST"):
                    if phase == "POST" and mask:
                        a ^= 1
                        if fork == 0x40006004:
                            b ^= 1
                        else:
                            sentinel ^= 1
                        samples.append(dict(sequence=len(samples) + 1, a=a, b=b, sentinel=sentinel))
                    values = [
                        int(phase == "POST"),
                        4,
                        1000,
                        mask,
                        0x40008140,
                        0x40006000,
                        fork,
                        0x30B03,
                        0x32303,
                        0x30D03,
                        (a << 11) | (sentinel << 13),
                        b << 3,
                        (a << 11) | (sentinel << 13),
                        b << 3,
                    ]
                    observations.append((index, phase, values))
            actual = [
                (o["index"], o["phase"], [o["registers"][n] for n, _, _ in REGISTERS])
                for o in evidence["model_observations"]
            ]
            return evidence["samples"] == samples and actual == observations

        nominal = matches_scenario("none")
        evidence["nominal_regression_passed"] = nominal
        if self.fault != "none":
            effect = matches_scenario(self.fault)
            evidence["fault"].update(
                effect_observed=effect, status="observed" if effect else "not_observed"
            )
            fail(
                "PPI fault observed; nominal regression failed"
                if effect
                else "requested PPI fault effect not established"
            )
        if not nominal:
            fail("PPI event, endpoints, channel, GPIO tasks or sample sequence differs")
        return evidence


def parse_observations(stdout, fault):
    def check(condition, message):
        if not condition:
            raise AssertionFailure(message, {})

    lifecycle = (
        rb"\[[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{4}\] \[INFO\] "
        rb"nrf52840-ppi: Machine (?:paused|resumed)\.\r?\n"
    )
    field = rb"(?:" + lifecycle + rb"){0,4}([0-9]{1,10})\r?\n"
    matches = list(
        re.finditer(rb"(?m)^SIM_PPI_OBS_([0-7])_(PRE|POST)\r?\n" + field * len(REGISTERS), stdout)
    )
    markers = list(re.finditer(rb"(?m)^SIM_PPI_OBS_([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        len(re.findall(rb"(?m)^[0-9]+\r?$", stdout)) == 16 * len(REGISTERS),
        "PPI numeric field inventory differs",
    )
    check(
        len(markers) == len(matches) == 16
        and [(m[1], m[2]) for m in matches]
        == [(str(i).encode(), p) for i in range(8) for p in (b"PRE", b"POST")],
        "PPI observation inventory/order differs",
    )
    result = []
    for match in matches:
        values = [int(v) for v in match.groups()[2:]]
        check(
            values[0] <= 1 and all(v <= 0xFFFFFFFF for v in values), "PPI observation out of range"
        )
        result.append(
            dict(
                index=int(match[1]),
                phase=match[2].decode(),
                registers=dict(zip((n for n, _, _ in REGISTERS), values, strict=True)),
                stdout_offset=match.start(),
                stdout_bytes=len(match[0]),
                stdout_sha256=sha(match[0]),
            )
        )
    faults = list(re.finditer(rb"(?m)^SIM_PPI_FAULT([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        [m[1] for m in faults] == ([] if fault == "none" else [b""]),
        "PPI fault marker inventory differs",
    )
    if faults:
        check(
            matches[7].end() <= faults[0].start() < matches[8].start(),
            "PPI fault marker out of order",
        )
    return result


def profile_from_plan(plan):
    return PPIProfile(plan["scenario"]["fault"]["id"])


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
            "missing PPI assertion evidence cannot pass",
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
            "PPI analysis requires complete execution, current inputs and cleanup",
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
                "failed PPI verdict differs",
            )
        else:
            require(report["ok"] is True and report["errors"] == [], "passed PPI verdict differs")
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "PPI evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
