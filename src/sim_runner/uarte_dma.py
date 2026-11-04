"""Fixed UARTE1 EasyDMA RX, paused SRAM guards and bounded descriptor faults."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-uarte-dma-v1"
PLAN_SCHEMA = "embedded-sim-runner.uarte-dma-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.uarte-dma-report.v1"
MODEL = ASSETS / "nrf52840-uarte-dma.repl"
VIRTUAL_MS = 40
FAULTS = ("none", "rx-short-buffer", "rx-pointer-shift")
FRAMES = (
    (0x01, 0x80, 0xFF, 0x23),
    (0xF0, 0x00, 0x7F, 0x55),
    (0xAA, 0x11, 0xCC, 0x33),
    (0x10, 0x20, 0x40, 0x80),
    (0xDE, 0xAD, 0xBE, 0xEF),
    (0x02, 0x04, 0x08, 0x10),
    (0xFE, 0xDC, 0xBA, 0x98),
    (0x00, 0xFF, 0x81, 0x7E),
)
BUFFER = 0x20001004
GUARD_LOW = 0x13579BDF
GUARD_HIGH = 0x2468ACE0
REGISTERS = (
    ("enable", "uarte1", 0x500),
    ("rx_pointer", "uarte1", 0x534),
    ("rx_max_count", "uarte1", 0x538),
    ("rx_amount", "uarte1", 0x53C),
    ("end_rx", "uarte1", 0x110),
    ("rx_ready", "uarte1", 0x108),
    ("rx_started", "uarte1", 0x14C),
    ("rx_timeout", "uarte1", 0x144),
    ("interrupt_mask", "uarte1", 0x300),
    ("guard_low", None, BUFFER - 4),
    ("buffer_word", None, BUFFER),
    ("guard_high", None, BUFFER + 4),
)
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "event_time_alignment_verified": False,
    "dma_hardware_verified": False,
    "uart_electrical_behavior_verified": False,
    "uarte_semantics": "Renode_1.17_UARTE1_RX_PTR_MAXCNT_AMOUNT_events_and_guarded_SRAM",
    "fault_semantics": "fixed_monitor_short_MAXCNT_or_shifted_PTR_with_STARTRX_not_wire_fault",
    "observation_semantics": "paused_model_and_SRAM_before_guest_service_not_DMA_latency",
}


class UARTEDMAProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed UARTE DMA fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        self.SCENARIO = dict(
            controller="UARTE1_RX_EasyDMA_polling_no_TX_DMA_or_IRQ",
            buffer_address=BUFFER,
            guard_low=GUARD_LOW,
            guard_high=GUARD_HIGH,
            buffer_initial_word=0xAAAAAAAA,
            stimulus_ms=list(range(2, 31, 4)),
            frames=[list(frame) for frame in FRAMES],
            service_budget_ms=4,
            fault=dict(id=fault, stimulus_index=None if fault == "none" else 4),
            observation_registers=[dict(name=n, peripheral=p, offset=o) for n, p, o in REGISTERS],
            coverage_limits=[
                "model_WriteChar_not_external_UART_pins_baud_parity_flow_control_or_line_errors",
                "RX_only_fixed_four_byte_buffers_no_TX_DMA_IRQ_PPI_STOPRX_FLUSHRX_or_SHORTS",
                "paused_observations_and_polling_not_DMA_or_IRQ_event_latency",
                "no_zero_length_DMA_silicon_memory_access_or_general_model_qualification",
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
        require(type(virtual_ms) is int and virtual_ms == 40, "UARTE scenario requires 40 ms")
        require(type(min_heartbeats) is int and min_heartbeats == 8, "UARTE requires 8 samples")
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
            "UARTE plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == 40, "UARTE scenario requires 40 ms")
        lines = [
            'mach create "nrf52840-uarte-dma"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            'emulation SetGlobalQuantum "0.0001"',
            'emulation RunFor "0.002"',
        ]
        for index, frame in enumerate(FRAMES):
            if index == 4 and self.fault != "none":
                lines.append('console_log "SIM_UARTE_FAULT"')
                lines += (
                    ["sysbus.uarte1 WriteDoubleWord 0x538 3"]
                    if self.fault == "rx-short-buffer"
                    else ["sysbus.uarte1 WriteDoubleWord 0x534 0x20001005"]
                )
                lines.append("sysbus.uarte1 WriteDoubleWord 0 1")
            for phase in ("PRE", "POST"):
                if phase == "POST":
                    applied = frame[:3] if index >= 4 and self.fault == "rx-short-buffer" else frame
                    lines += [f"sysbus.uarte1 WriteChar {byte}" for byte in applied]
                lines.append(f'console_log "SIM_UARTE_OBS_{index}_{phase}"')
                for _, peripheral, offset in REGISTERS:
                    target = "sysbus" if peripheral is None else "sysbus." + peripheral
                    lines.append(f"console_log `{target} ReadDoubleWord {offset}`")
            lines.append('emulation RunFor "0.004"')
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
            fail("non-ASCII UARTE UART")
        if not uart.endswith(b"\n") or not 2 <= len(lines) <= 18:
            fail("partial or oversized UARTE UART protocol")
        if lines[:2] != [assertions["ready"], assertions["build"]]:
            fail("UARTE READY/BUILD differs")
        pattern = re.escape(assertions["heartbeat_prefix"]) + (
            r" ([0-9A-F]{8}) WORD=([0-9A-F]{8}) N=([0-9A-F]{8})"
            r" LO=([0-9A-F]{8}) HI=([0-9A-F]{8})"
        )
        for line in lines[2:]:
            match = re.fullmatch(pattern, line)
            if match is None:
                fail("malformed UARTE sample")
            seq, word, amount, low, high = (int(v, 16) for v in match.groups())
            evidence["samples"].append(
                dict(sequence=seq, word=word, amount=amount, guard_low=low, guard_high=high)
            )

        def matches_scenario(fault):
            samples, observations = [], []
            for index, frame in enumerate(FRAMES):
                short = index >= 4 and fault == "rx-short-buffer"
                shifted = index >= 4 and fault == "rx-pointer-shift"
                pointer, count = BUFFER + int(shifted), 3 if short else 4
                data = (
                    bytes((0xAA, *frame[:3]))
                    if shifted
                    else bytes((*frame[:3], 0xAA))
                    if short
                    else bytes(frame)
                )
                word = int.from_bytes(data, "little")
                high = (GUARD_HIGH & 0xFFFFFF00) | frame[3] if shifted else GUARD_HIGH
                samples.append(
                    dict(
                        sequence=index + 1,
                        word=word,
                        amount=count,
                        guard_low=GUARD_LOW,
                        guard_high=high,
                    )
                )
                for phase in ("PRE", "POST"):
                    post = phase == "POST"
                    values = [
                        8,
                        pointer,
                        count,
                        count if post else 0,
                        int(post),
                        int(post),
                        1,
                        0,
                        0,
                        GUARD_LOW,
                        word if post else 0xAAAAAAAA,
                        high if post else GUARD_HIGH,
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
                "UARTE fault observed; nominal regression failed"
                if effect
                else "requested UARTE fault effect not established"
            )
        if not nominal:
            fail("UARTE descriptor, amount, event, guarded SRAM or guest sample differs")
        return evidence


def parse_observations(stdout, fault):
    def check(condition, message):
        if not condition:
            raise AssertionFailure(message, {})

    lifecycle = (
        rb"\[[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{4}\] \[INFO\] "
        rb"nrf52840-uarte-dma: Machine (?:paused|resumed)\.\r?\n"
    )
    field = rb"(?:" + lifecycle + rb"){0,4}([0-9]{1,10})\r?\n"
    matches = list(
        re.finditer(rb"(?m)^SIM_UARTE_OBS_([0-7])_(PRE|POST)\r?\n" + field * len(REGISTERS), stdout)
    )
    markers = list(re.finditer(rb"(?m)^SIM_UARTE_OBS_([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        len(re.findall(rb"(?m)^[0-9]+\r?$", stdout)) == 16 * len(REGISTERS),
        "UARTE numeric field inventory differs",
    )
    check(
        len(markers) == len(matches) == 16
        and [(m[1], m[2]) for m in matches]
        == [(str(i).encode(), p) for i in range(8) for p in (b"PRE", b"POST")],
        "UARTE observation inventory/order differs",
    )
    result = []
    for match in matches:
        values = [int(v) for v in match.groups()[2:]]
        check(all(v <= 0xFFFFFFFF for v in values), "UARTE observation out of range")
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
    faults = list(re.finditer(rb"(?m)^SIM_UARTE_FAULT([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        [m[1] for m in faults] == ([] if fault == "none" else [b""]),
        "UARTE fault marker inventory differs",
    )
    if faults:
        check(
            matches[7].end() <= faults[0].start() < matches[8].start(),
            "UARTE fault marker out of order",
        )
    return result


def profile_from_plan(plan):
    return UARTEDMAProfile(plan["scenario"]["fault"]["id"])


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
            "missing UARTE assertion evidence cannot pass",
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
            "UARTE analysis requires complete execution, current inputs and cleanup",
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
                "failed UARTE verdict differs",
            )
        else:
            require(report["ok"] is True and report["errors"] == [], "passed UARTE verdict differs")
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "UARTE evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
