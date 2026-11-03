"""Fixed Renode 1.17 GPIOTE input-event/IRQ subset; no PPI or physical timing claim."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-gpiote-v1"
PLAN_SCHEMA = "embedded-sim-runner.gpiote-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.gpiote-report.v1"
MODEL = ASSETS / "nrf52840-gpiote.repl"
VIRTUAL_MS = 40
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "ppi_routing_verified": False,
    "gpiote_semantics": "Renode_1.17_CONFIG_pin_edges_EVENTS_IN_INTEN_IRQ6",
    "fault_semantics": "fixed_monitor_INTENCLR_or_CONFIG_polarity_not_physical_fault",
    "observation_semantics": "paused_pre_post_registers_and_guest_ISR_not_electrical_time",
}
STIMULI = (
    (0, 11, 1),
    (0, 11, 0),
    (0, 12, 0),
    (0, 12, 1),
    (1, 3, 1),
    (1, 3, 0),
    (0, 11, 1),
    (0, 12, 0),
    (1, 3, 1),
)
FAULTS = ("none", "gpiote-irq-mask", "gpiote-wrong-polarity")


class GPIOTEProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed GPIOTE fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        self.SCENARIO = dict(
            channels=[
                dict(channel=0, port=0, pin=11, polarity="rising"),
                dict(channel=1, port=0, pin=12, polarity="falling"),
                dict(channel=2, port=1, pin=3, polarity="toggle"),
            ],
            initial_gpio_inputs=[4096, 0],
            stimuli=[list(s) for s in STIMULI],
            stimulus_ms=list(range(2, 35, 4)),
            service_budget_ms=2,
            interrupt_mask=7,
            fault=dict(id=fault, stimulus_index=None if fault == "none" else 4),
            coverage_limits=[
                "no_TASKS_OUT_SET_CLR_PORT_SENSE_LATCH_DETECTMODE_or_PPI_qualification",
                "only_three_channels_and_fixed_input_edges_not_all_pin_combinations",
                "monitor_inputs_not_electrical_pulls_debounce_IRQ_latency_or_physical_timing",
                "model_CONFIG_OUTINIT_reads_current_channel_state_not_constant_initial_bit",
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
        min_heartbeats=7,
        virtual_ms=40,
        timeout=30,
        output_limit=1048576,
        memory_mib=1024,
    ):
        require(type(virtual_ms) is int and virtual_ms == 40, "GPIOTE scenario requires 40 ms")
        require(
            type(min_heartbeats) is int and min_heartbeats == 7, "GPIOTE requires 7 nominal samples"
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
            "GPIOTE plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == 40, "GPIOTE scenario requires 40 ms")
        lines = [
            'mach create "nrf52840-gpiote"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            'emulation SetGlobalQuantum "0.0001"',
            "sysbus.gpio0 OnGPIO 12 true",
            'emulation RunFor "0.002"',
        ]
        for index, (port, pin, value) in enumerate(STIMULI):
            if index == 4 and self.fault != "none":
                lines += ['console_log "SIM_GPIOTE_FAULT"']
                lines += (
                    ["sysbus.gpiote WriteDoubleWord 0x308 4"]
                    if self.fault == "gpiote-irq-mask"
                    else ["sysbus.gpiote WriteDoubleWord 0x518 140033"]
                )
            lines += [f"sysbus.gpio{port} OnGPIO {pin} {'true' if value else 'false'}"]
            for phase in ("PRE", "POST"):
                if phase == "POST":
                    lines += ['emulation RunFor "0.002"']
                lines += [f'console_log "SIM_GPIOTE_OBS_{index}_{phase}"']
                lines += [
                    f"console_log `sysbus.gpiote ReadDoubleWord {offset}`"
                    for offset in (0x100, 0x104, 0x108, 0x304, 0x510, 0x514, 0x518)
                ]
                lines += [
                    "console_log `sysbus.gpio0 ReadDoubleWord 0x10`",
                    "console_log `sysbus.gpio1 ReadDoubleWord 0x10`",
                ]
            if index != 8:
                lines += ['emulation RunFor "0.002"']
        lines += [
            'emulation RunFor "0.004"',
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
            fail("non-ASCII GPIOTE UART")
        if not uart.endswith(b"\n") or not 2 <= len(lines) <= 34:
            fail("partial or oversized GPIOTE UART protocol")
        if lines[:2] != [assertions["ready"], assertions["build"]]:
            fail("GPIOTE READY/BUILD differs")
        pattern = re.escape(assertions["heartbeat_prefix"]) + (
            r" ([0-9A-F]{8}) C=([0-9A-F]{8}) P=([0-9A-F]{8}) E=([0-9A-F]{8}) A=([0-9A-F]{8})"
            r" M=([0-9A-F]{8}) V=([0-9A-F]{8})"
        )
        for line in lines[2:]:
            match = re.fullmatch(pattern, line)
            if match is None:
                fail("malformed GPIOTE sample")
            seq, channel, level, before, after, mask, vector = (int(v, 16) for v in match.groups())
            evidence["samples"].append(
                dict(
                    sequence=seq,
                    channel=channel,
                    pin_level=level,
                    event_before=before,
                    event_after=after,
                    interrupt_mask=mask,
                    irq_vector=vector,
                )
            )

        def matches_scenario(fault):
            # Source-audited fixed input schedule; model expectations never use guest claims.
            levels, events, mask = [0, 1, 0], [0, 0, 0], 7
            configs = [0x10B01, 0x20C01, 0x32301]
            expected_samples, expected_observations = [], []
            schedule = [(0, 1), (0, 0), (1, 0), (1, 1), (2, 1), (2, 0), (0, 1), (1, 0), (2, 1)]

            def observation(index, phase, mask):
                registers = [c | (level << 20) for c, level in zip(configs, levels, strict=True)]
                inputs = [(levels[0] << 11) | (levels[1] << 12), levels[2] << 3]
                return (index, phase, *events, mask, *registers, *inputs)

            for index, (channel, value) in enumerate(schedule):
                if index == 4 and fault == "gpiote-irq-mask":
                    mask = 3
                if index == 4 and fault == "gpiote-wrong-polarity":
                    configs[2] = 0x22301
                old = levels[channel]
                polarity = (configs[channel] >> 16) & 3
                events[channel] = int(
                    (polarity == 1 and old == 0 and value == 1)
                    or (polarity == 2 and old == 1 and value == 0)
                    or (polarity == 3 and old != value)
                )
                levels[channel] = value

                expected_observations.append(observation(index, "PRE", mask))
                for served in range(3):
                    if events[served] and mask & (1 << served):
                        events[served] = 0
                        expected_samples.append(
                            dict(
                                sequence=len(expected_samples) + 1,
                                channel=served,
                                pin_level=levels[served],
                                event_before=1,
                                event_after=0,
                                interrupt_mask=mask,
                                irq_vector=22,
                            )
                        )
                expected_observations.append(observation(index, "POST", mask))
            observed = [
                (
                    o["index"],
                    o["phase"],
                    *o["events"],
                    o["interrupt_mask"],
                    *o["configurations"],
                    *o["gpio_inputs"],
                )
                for o in evidence["model_observations"]
            ]
            return evidence["samples"] == expected_samples and observed == expected_observations

        nominal = matches_scenario("none")
        evidence["nominal_regression_passed"] = nominal
        if self.fault != "none":
            effect = matches_scenario(self.fault)
            evidence["fault"].update(
                effect_observed=effect, status="observed" if effect else "not_observed"
            )
            fail(
                "GPIOTE fault observed; nominal regression failed"
                if effect
                else "requested GPIOTE fault effect not established"
            )
        if not nominal:
            fail("GPIOTE channel IRQ, event clear, mask, sample count or model state differs")
        return evidence


def parse_observations(stdout, fault):
    def check(condition, message):
        if not condition:
            raise AssertionFailure(message, {})

    # Monitor Console.WriteLine and Renode's asynchronous INFO logger share stdout.
    # Retain the raw span, allowing only bounded, exact lifecycle lines between
    # fields. Never filter general INFO, warnings/errors, missing or extra values.
    lifecycle = (
        rb"\[[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{4}\] \[INFO\] "
        rb"nrf52840-gpiote: Machine (?:paused|resumed)\.\r?\n"
    )
    field = rb"(?:" + lifecycle + rb"){0,4}([0-9]{1,10})\r?\n"
    pattern = rb"(?m)^SIM_GPIOTE_OBS_([0-8])_(PRE|POST)\r?\n" + field * 9
    matches = list(re.finditer(pattern, stdout))
    markers = list(re.finditer(rb"(?m)^SIM_GPIOTE_OBS_([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        len(markers) == len(matches) == 18
        and [(m[1], m[2]) for m in matches]
        == [(str(i).encode(), p) for i in range(9) for p in (b"PRE", b"POST")],
        "GPIOTE observation inventory/order differs",
    )
    result = []
    for match in matches:
        values = [int(x) for x in match.groups()[2:]]
        check(
            all(v <= 1 for v in values[:3])
            and values[3] <= 255
            and all(v <= 0xFFFFFFFF for v in values[4:]),
            "GPIOTE observation out of range",
        )
        result.append(
            dict(
                index=int(match[1]),
                phase=match[2].decode(),
                events=values[:3],
                interrupt_mask=values[3],
                configurations=values[4:7],
                gpio_inputs=values[7:9],
                stdout_offset=match.start(),
                stdout_bytes=len(match[0]),
                stdout_sha256=sha(match[0]),
            )
        )
    faults = list(re.finditer(rb"(?m)^SIM_GPIOTE_FAULT([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        [m[1] for m in faults] == ([] if fault == "none" else [b""]),
        "GPIOTE fault marker inventory differs",
    )
    if faults:
        check(
            matches[7].end() <= faults[0].start() < matches[8].start(),
            "GPIOTE fault marker out of order",
        )
    return result


def profile_from_plan(plan):
    return GPIOTEProfile(plan["scenario"]["fault"]["id"])


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
            "missing GPIOTE assertion evidence cannot pass",
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
            "GPIOTE analysis requires complete execution, current inputs and cleanup",
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
                "failed GPIOTE verdict differs",
            )
        else:
            require(
                report["ok"] is True and report["errors"] == [], "passed GPIOTE verdict differs"
            )
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "GPIOTE evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
