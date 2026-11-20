"""Fixed AES128 ECB known-answer, descriptor and guarded SRAM evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import AssertionFailure, bound, decode, payload, require, sha
from .plan import ASSETS
from .plan import make_plan as uart_plan

PROFILE = "nrf52840-ecb-v1"
PLAN_SCHEMA = "embedded-sim-runner.ecb-plan.v1"
REPORT_SCHEMA = "embedded-sim-runner.ecb-report.v1"
MODEL = ASSETS / "nrf52840-ecb.repl"
VIRTUAL_MS = 40
FAULTS = ("none", "plaintext-stuck", "context-redirect")
CONTEXT = 0x20001004
ALTERNATE = 0x20001104
MAILBOX = 0x20001200
GUARD_LOW = 0x13579BDF
GUARD_HIGH = 0x2468ACE0
KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
PLAINTEXTS = tuple(
    bytes.fromhex(v)
    for v in (
        "6bc1bee22e409f96e93d7e117393172a",
        "ae2d8a571e03ac9c9eb76fac45af8e51",
        "30c81c46a35ce411e5fbc1191a0a52ef",
        "f69f2445df4f9b17ad2b417be66c3710",
    )
)
CIPHERTEXTS = tuple(
    bytes.fromhex(v)
    for v in (
        "3ad77bb40d7a3660a89ecaf32466ef97",
        "f5d3d58503b9699de785895a96fdbaaf",
        "43b1cd7f598ece23881b00e3ed030688",
        "7b0c785e27e8ad3f8223207104725dd4",
    )
)
REGISTERS = (
    ("data_pointer", "ecb", 0x504),
    ("end", "ecb", 0x100),
    ("interrupt_mask", "ecb", 0x304),
    ("mailbox", None, MAILBOX),
) + tuple(
    (prefix + name, None, base + offset)
    for prefix, base in (("", CONTEXT), ("alternate_", ALTERNATE))
    for name, offset in (
        ("guard_low", -4),
        *(("key" + str(i), i * 4) for i in range(4)),
        *(("plaintext" + str(i), 16 + i * 4) for i in range(4)),
        *(("ciphertext" + str(i), 32 + i * 4) for i in range(4)),
        ("guard_high", 48),
    )
)
COVERAGE = {
    "model_coverage_complete": False,
    "physical_timing_verified": False,
    "event_time_alignment_verified": False,
    "crypto_hardware_verified": False,
    "cryptographic_security_qualified": False,
    "side_channel_resistance_verified": False,
    "ecb_semantics": "Renode_1.17_fixed_AES128_block_NIST_F1_1_not_security_certification",
    "fault_semantics": "fixed_monitor_plaintext_repeat_or_valid_alternate_context_pointer",
    "observation_semantics": "paused_model_SRAM_guest_handshake_not_encryption_latency",
}


def words(data):
    return [int.from_bytes(data[i : i + 4], "little") for i in range(0, 16, 4)]


class ECBProfile:
    PROFILE = PROFILE
    PLAN_SCHEMA = PLAN_SCHEMA
    REPORT_SCHEMA = REPORT_SCHEMA
    MODEL = MODEL
    VIRTUAL_MS = VIRTUAL_MS

    def __init__(self, fault):
        require(type(fault) is str and fault in FAULTS, "unsupported fixed ECB fault")
        self.fault = fault
        self.COVERAGE = {**COVERAGE, "fault_scenario": fault}
        self.SCENARIO = dict(
            controller="ECB_AES128_polled_with_guest_START",
            context_address=CONTEXT,
            alternate_context=ALTERNATE,
            mailbox=MAILBOX,
            key=KEY.hex(),
            plaintexts=[v.hex() for v in PLAINTEXTS],
            ciphertexts=[v.hex() for v in CIPHERTEXTS],
            vector_source="NIST_SP_800_38A_F1_1",
            stimulus_ms=list(range(2, 31, 4)),
            encryption_service_budget_ms=1,
            guest_report_budget_ms=3,
            fault=dict(id=fault, stimulus_index=None if fault == "none" else 4),
            observation_registers=[dict(name=n, peripheral=p, offset=o) for n, p, o in REGISTERS],
            coverage_limits=[
                "public_test_keys_only_no_secrets",
                "fixed_AES128_encrypt_not_decrypt_modes_or_crypto_certification",
                "model_START_synchronous_ignores_written_bit_no_STOP_or_ERROR_register_model",
                "no_physical_timing_power_side_channel_protection_IRQ_or_PPI_qualification",
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
        require(type(virtual_ms) is int and virtual_ms == 40, "ECB scenario requires 40 ms")
        require(type(min_heartbeats) is int and min_heartbeats == 8, "ECB requires 8 samples")
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
            "ECB plan differs from fixed scenario/current inputs",
            "INPUT_CHANGED",
        )
        return doc

    def script(self, virtual_ms):
        require(type(virtual_ms) is int and virtual_ms == 40, "ECB requires 40 ms")
        lines = [
            'mach create "nrf52840-ecb"',
            "machine LoadPlatformDescription $ORIGIN/model.repl",
            "sysbus LoadBinary $ORIGIN/firmware.bin 0",
            "sysbus.uart0 CreateFileBackend $ORIGIN/uart.bin true",
            'emulation SetGlobalQuantum "0.0001"',
            'emulation RunFor "0.002"',
        ]
        for index in range(8):
            if index >= 4 and self.fault != "none":
                if index == 4:
                    lines.append('console_log "SIM_ECB_FAULT"')
                if self.fault == "context-redirect":
                    lines.append(f"sysbus.ecb WriteDoubleWord 0x504 {ALTERNATE}")
                else:
                    lines += [
                        f"sysbus WriteDoubleWord {CONTEXT + 16 + i * 4} {v}"
                        for i, v in enumerate(words(PLAINTEXTS[3]))
                    ]
            for phase in ("PRE", "POST"):
                if phase == "POST":
                    lines += [f"sysbus WriteDoubleWord {MAILBOX} 1", 'emulation RunFor "0.001"']
                lines.append(f'console_log "SIM_ECB_OBS_{index}_{phase}"')
                for _, peripheral, offset in REGISTERS:
                    target = "sysbus" if peripheral is None else "sysbus." + peripheral
                    lines.append(f"console_log `{target} ReadDoubleWord {offset}`")
            lines += [f"sysbus WriteDoubleWord {MAILBOX} 3", 'emulation RunFor "0.003"']
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
            fail("non-ASCII ECB UART")
        if not uart.endswith(b"\n") or not 2 <= len(lines) <= 18:
            fail("partial or oversized ECB UART protocol")
        if lines[:2] != [assertions["ready"], assertions["build"]]:
            fail("ECB READY/BUILD differs")
        pattern = re.escape(assertions["heartbeat_prefix"]) + (
            r" ([0-9A-F]{8}) OUT=([0-9A-F]{32}) END=([0-9A-F]{8})"
            r" LO=([0-9A-F]{8}) HI=([0-9A-F]{8})"
        )
        for line in lines[2:]:
            match = re.fullmatch(pattern, line)
            if match is None:
                fail("malformed ECB sample")
            sequence, output, end, low, high = match.groups()
            evidence["samples"].append(
                dict(
                    sequence=int(sequence, 16),
                    ciphertext=output.lower(),
                    end=int(end, 16),
                    guard_low=int(low, 16),
                    guard_high=int(high, 16),
                )
            )

        def matches_scenario(fault):
            samples, observations = [], []
            for index in range(8):
                stuck = index >= 4 and fault == "plaintext-stuck"
                redirected = index >= 4 and fault == "context-redirect"
                vector = 3 if stuck else index % 4
                alt_vector = (index + 1) % 4
                output = bytes([0xAA]) * 16 if redirected else CIPHERTEXTS[vector]
                samples.append(
                    dict(
                        sequence=index + 1,
                        ciphertext=output.hex(),
                        end=1,
                        guard_low=GUARD_LOW,
                        guard_high=GUARD_HIGH,
                    )
                )
                for phase in ("PRE", "POST"):
                    post = phase == "POST"
                    values = [ALTERNATE if redirected else CONTEXT, int(post), 0, 2 if post else 0]
                    for alternate in (False, True):
                        clear = PLAINTEXTS[alt_vector if alternate else vector]
                        cipher = CIPHERTEXTS[alt_vector if alternate else vector]
                        written = post and (alternate == redirected)
                        values += [
                            GUARD_LOW,
                            *words(KEY),
                            *words(clear),
                            *words(cipher if written else bytes([0xAA]) * 16),
                            GUARD_HIGH,
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
                "ECB fault observed; nominal regression failed"
                if effect
                else "requested ECB fault effect not established"
            )
        if not nominal:
            fail(
                "ECB known answer, descriptor, key/plaintext/output, event or guest sample differs"
            )
        return evidence


def parse_observations(stdout, fault):
    def check(condition, message):
        if not condition:
            raise AssertionFailure(message, {})

    lifecycle = (
        rb"\[[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{4}\] \[INFO\] "
        rb"nrf52840-ecb: Machine (?:paused|resumed)\.\r?\n"
    )
    field = rb"(?:" + lifecycle + rb"){0,4}([0-9]{1,10})\r?\n"
    matches = list(
        re.finditer(rb"(?m)^SIM_ECB_OBS_([0-7])_(PRE|POST)\r?\n" + field * len(REGISTERS), stdout)
    )
    markers = list(re.finditer(rb"(?m)^SIM_ECB_OBS_([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        len(re.findall(rb"(?m)^[0-9]+\r?$", stdout)) == 16 * len(REGISTERS),
        "ECB numeric field inventory differs",
    )
    check(
        len(markers) == len(matches) == 16
        and [(m[1], m[2]) for m in matches]
        == [(str(i).encode(), p) for i in range(8) for p in (b"PRE", b"POST")],
        "ECB observation inventory/order differs",
    )
    result = []
    for match in matches:
        values = [int(v) for v in match.groups()[2:]]
        check(all(v <= 0xFFFFFFFF for v in values), "ECB observation out of range")
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
    faults = list(re.finditer(rb"(?m)^SIM_ECB_FAULT([^\r\n]*)(?:\r?\n|$)", stdout))
    check(
        [m[1] for m in faults] == ([] if fault == "none" else [b""]),
        "ECB fault marker inventory differs",
    )
    if faults:
        check(
            matches[7].end() <= faults[0].start() < matches[8].start(),
            "ECB fault marker out of order",
        )
    return result


def profile_from_plan(plan):
    return ECBProfile(plan["scenario"]["fault"]["id"])


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
            "missing ECB assertion evidence cannot pass",
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
            "ECB analysis requires complete execution, current inputs and cleanup",
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
                "failed ECB verdict differs",
            )
        else:
            require(report["ok"] is True and report["errors"] == [], "passed ECB verdict differs")
        require(
            json.dumps(evaluated, sort_keys=True) == json.dumps(saved, sort_keys=True),
            "ECB evidence summary differs",
        )
    return {
        **result,
        "fault_requested": profile.fault,
        "fault_effect_observed": None if saved is None else saved["fault"]["effect_observed"],
        "nominal_regression_passed": None if saved is None else saved["nominal_regression_passed"],
    }
