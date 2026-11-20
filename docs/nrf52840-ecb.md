# nRF52840 ECB AES-128 known-answer regression

Local 0.16.0 adds `nrf52840-ecb-v1` for fixed Renode 1.17.0. It exercises
the ECB peripheral and firmware's public test-vector buffer handling. It is
not cryptographic certification, a secure storage test or a recommendation to
use ECB for application data. Runtime dependencies remain empty.

## Independent result and SRAM evidence

The four AES128 encryption vectors and public key come from
[NIST SP800-38A F.1.1](https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-38a.pdf).
They repeat twice, producing eight ordered samples. Expected ciphertext is
fixed independently of Renode and of guest output; the runner performs no AES
implementation or external cryptographic process at runtime.

Two aligned SRAM contexts at `0x20001004` and `0x20001104` each contain a
16-byte key, 16-byte plaintext and 16-byte output initially filled with `AA`.
Separate low/high guards surround each48-byte context. Both use the same
public key; the alternate context uses the next vector and stays unwritten
unless selected. A mailbox at `0x20001200` coordinates the fixed schedule.

The guest prepares both contexts and ECBDATAPTR, clears ENDECB and waits.
At virtual2/6/10/14/18/22/26/30ms the monitor records PRE, requests work and
runs1ms. The guest writes TASKS_START, polls ENDECB and waits for permission
to report. The monitor records POST, permits reporting, then runs3ms for
UART output and next preparation. Total duration40ms, global quantum100us.

Every snapshot has32 fields: ECBDATAPTR, ENDECB, INTENSET, mailbox, and both
contexts' key/plaintext/output words and guards. Sixteen snapshots contain512
numeric observations with exact stdout offsets, byte counts and hashes. The
UART0 protocol contains exact READY/BUILD and eight lines:

```text
SIM_NRF_ECB_SAMPLE 00000001 OUT=3AD77BB40D7A3660A89ECAF32466EF97 END=00000001 LO=13579BDF HI=2468ACE0
```

Byte order in OUT follows NIST bytes; SRAM words are little-endian. Partial,
extra, reordered or contradictory protocol/register fields fail. The source,
compiler and variant bind BUILD; wrong BUILD retains model snapshots without
asserting guest identity or fault effects. Only public fixtures are recorded;
never put application secrets in this profile or its evidence.

## Bounded faults

`--fault` is mandatory, including `none`. From the fifth transaction:

- `plaintext-stuck` replaces the main plaintext with vector4 while leaving
  the guest's requested sequence unchanged.
- `context-redirect` changes ECBDATAPTR to the valid alternate context. The
  alternate output changes and the intended output stays poisoned.

Each injection is applied before PRE, with one request marker at index4.
Fault effects require exact raw/model/guest evidence, not a marker alone.
Observed effects remain `status=failed`, `nominal_regression_passed=false`.
Guards detect corruption and do not prevent writes or establish protection.
Wrong BUILD keeps effects unknown. Unsupported MMIO is a coverage failure
with unevaluated assertions, not a failed known-answer assertion.

```powershell
python examples/nrf52840-ecb/build.py --output <new-build-directory>
sim-runner plan --profile nrf52840-ecb-v1 --fault none `
  --runtime-lock <lock.json> --runtime-sha256 <lock-sha256> `
  --firmware <firmware.bin> --firmware-sha256 <image-sha256> `
  --ready-line 'SIM_NRF_ECB_READY v1' --build-line 'SIM_NRF_ECB_BUILD <build-id>' `
  --heartbeat-prefix SIM_NRF_ECB_SAMPLE --output <new-plan.json>
sim-runner inspect-plan --plan <new-plan.json> --sha256 <plan-sha256>
sim-runner run --plan <new-plan.json> --sha256 <plan-sha256> --output-dir <new-run>
sim-runner inspect-report --report <new-run/report.json> --sha256 <report-sha256>
```

Schemas are `embedded-sim-runner.ecb-plan.v1` / `ecb-report.v1`. Each attempt
uses a new directory and current hash-bound inputs; no interactive per-run
confirmation or automatic retry is added. Saved inspection recomputes the
verdict and raw bindings without original runtime/firmware inputs. Integrity
checking does not authenticate the producer. HIL/Trace/Toolkit require their
own explicit schema integration; do not pass this schema to older adapters.

## Audited subset and cost

The pinned [ECB source](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Miscellaneous/NRF52840_ECB.cs)
encrypts synchronously on a START register write, reads key and plaintext
from ECBDATAPTR and writes ciphertext at pointer+32. Its callback ignores the
written bit value. STOP and ERROR have no implemented register definitions.
The profile uses START=1, polls only ENDECB, disables interrupts and does not
qualify those absent semantics, IRQ/PPI, error/abort, malformed pointers,
decryption, other modes, hardware crypto, confidentiality, physical timing,
power or side-channel resistance. All corresponding qualifications stay false.

Bounds stay one owned process, a30s host timeout,1MiB/capture and1024MiB
Windows Job ceiling. Known vectors add no runtime dependency. The32-field
snapshot/report work is bounded; record warm evaluation/inspection separately
from host startup/run cost, virtual time, RSS and cold I/O. Complete process,
Job and capture-thread cleanup is required for failed and passed attempts.
