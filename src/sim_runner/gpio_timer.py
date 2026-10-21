"""Fixed Renode GPIO/TIMER0 IRQ scenario. No user monitor code or host bridges."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-gpio-timer-v1"
PLAN_SCHEMA = "embedded-sim-runner.gpio-timer-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.gpio-timer-report.v1"
MODEL = ASSETS / "nrf52840-gpio-timer.repl"
VIRTUAL_MS = 82
PHASES = [
    {"input_at_ms": start, "input": state, "observe_at_ms": observe}
    for start, state, observe in ((0, 0, 20), (22, 1, 40), (42, 0, 60), (62, 1, 80))
]
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "gpio_observation_semantics": "paused_model_register_snapshot_not_electrical_waveform",
    "timer_observation_semantics": "guest_reported_250kHz_capture_in_TIMER0_IRQ_not_host_time",
}
SCENARIO = {
    "gpio_port": 0,
    "input_pin": 11,
    "output_pin": 13,
    "loopback_pin": 14,
    "timer": "TIMER0",
    "timer_frequency_hz": 250000,
    "period_ticks": 1250,
    "capture_tolerance_ticks": 25,
    "irq_vector": 24,
    "phases": PHASES,
}


def make_plan(
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
        type(virtual_ms) is int and virtual_ms == VIRTUAL_MS, "GPIO scenario requires 82 virtual ms"
    )
    require(
        type(min_heartbeats) is int and min_heartbeats == 16, "GPIO scenario requires 16 samples"
    )
    doc = uart_plan(
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
    doc.update(
        schema_version=PLAN_SCHEMA,
        profile=PROFILE,
        model_sha256=sha(payload(MODEL, 16384)),
        script_sha256=sha(script(virtual_ms).encode()),
        scenario=json.loads(json.dumps(SCENARIO)),
        **COVERAGE,
    )
    return doc


def load_plan(path, expected):
    doc = decode(bound(path, expected, 65536))
    wanted = make_plan(
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
        "GPIO plan differs from fixed profile/current inputs",
        "INPUT_CHANGED",
    )
    return doc


def script(virtual_ms):
    require(virtual_ms == VIRTUAL_MS, "invalid GPIO scenario duration")
    lines = [
        'mach create "nrf52840-gpio-timer"',
        "machine LoadPlatformDescription $ORIGIN/model.repl",
        "sysbus LoadBinary $ORIGIN/firmware.bin 0",
        "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
        'emulation SetGlobalQuantum "0.0001"',
    ]
    cursor = 0
    for index, phase in enumerate(PHASES):
        if phase["input_at_ms"] > cursor:
            lines.append(f'emulation RunFor "{(phase["input_at_ms"] - cursor) / 1000:.3f}"')
        lines += [
            f"sysbus.gpio0 OnGPIO 11 {'true' if phase['input'] else 'false'}",
            f'emulation RunFor "{(phase["observe_at_ms"] - phase["input_at_ms"]) / 1000:.3f}"',
            f'console_log "SIM_GPIO_OBS_{index}"',
            "console_log `sysbus.gpio0 ReadDoubleWord 4`",
            "console_log `sysbus.gpio0 ReadDoubleWord 0x10`",
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


def evaluate(data, assertions, stdout):
    require(data.endswith(b"\n"), "incomplete GPIO UART", "ASSERTION_FAILED")
    lines = data.decode("ascii", errors="strict").splitlines()
    require(
        lines[:2] == [assertions["ready"], assertions["build"]],
        "GPIO READY/BUILD mismatch",
        "ASSERTION_FAILED",
    )
    require(
        len(lines) == 18 and assertions["minimum"] == 16,
        "expected exactly 16 TIMER0 IRQ samples",
        "ASSERTION_FAILED",
    )
    samples = []
    pattern = (
        re.escape(assertions["heartbeat_prefix"])
        + r" ([0-9A-F]{8}) I=([01]) O=([01]) L=([01]) T=([0-9A-F]{8}) V=([0-9A-F]{8})"
    )
    for index, line in enumerate(lines[2:], 1):
        match = re.fullmatch(pattern, line)
        require(match is not None, "invalid GPIO sample line", "ASSERTION_FAILED")
        seq, input_, output, loopback, ticks, vector = (int(v, 16) for v in match.groups())
        expected = PHASES[(index - 1) // 4]["input"]
        require(
            seq == index and input_ == output == loopback == expected,
            "GPIO input/output/loopback or sample sequence mismatch",
            "ASSERTION_FAILED",
        )
        require(
            vector == 24 and abs(ticks - index * 1250) <= 25,
            "TIMER0 IRQ/capture period mismatch",
            "ASSERTION_FAILED",
        )
        samples.append(
            {
                "sequence": seq,
                "input": input_,
                "output": output,
                "loopback": loopback,
                "timer_ticks": ticks,
                "irq_vector": vector,
            }
        )
    observations = []
    transcript = stdout.decode("utf-8", errors="strict")
    require(
        re.findall(r"(?m)^SIM_GPIO_OBS_([^\r\n]*)", transcript) == ["0", "1", "2", "3"],
        "GPIO model observation order/inventory differs",
        "ASSERTION_FAILED",
    )
    for index, phase in enumerate(PHASES):
        matches = re.findall(
            r"(?m)^SIM_GPIO_OBS_" + str(index) + r"\r?\n([0-9]{1,10})\r?\n([0-9]{1,10})\r?$",
            transcript,
        )
        require(len(matches) == 1, "missing/duplicate GPIO model observation", "ASSERTION_FAILED")
        out, inp = (int(v) for v in matches[0])
        require(
            0 <= out <= 0xFFFFFFFF
            and 0 <= inp <= 0xFFFFFFFF
            and (out >> 13) & 1 == (inp >> 14) & 1 == (inp >> 11) & 1 == phase["input"],
            "independent GPIO model state mismatch",
            "ASSERTION_FAILED",
        )
        observations.append(
            {
                "phase": index,
                "requested_virtual_ms": phase["observe_at_ms"],
                "out_register": out,
                "in_register": inp,
            }
        )
    return {
        "ready": True,
        "build": True,
        "samples": samples,
        "model_observations": observations,
        "period_ticks": 1250,
        "capture_tolerance_ticks": 25,
        "gpio_transitions": 3,
        **COVERAGE,
    }
