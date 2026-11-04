# nRF52840 UARTE1 EasyDMA RX functional regression

Local candidate 0.15.0 adds `nrf52840-uarte-dma-v1` for fixed Renode 1.17.0.
The monitor supplies model bytes to UARTE1; the guest polls ENDRX and reports
received SRAM, RXD.AMOUNT and guard words through separate legacy UART0.
No physical device, host UART bridge, network or SDK is used.

## Fixed buffers and independent evidence

| Address / register | Purpose |
| --- | --- |
| UARTE1 `0x40028000` | ENABLE=8; RX PTR/MAXCNT/AMOUNT and RX events |
| `0x20001000` | Low guard `0x13579BDF` |
| `0x20001004` | Four-byte receive buffer, initialized to `0xAAAAAAAA` |
| `0x20001008` | High guard `0x2468ACE0` |

Eight fixed four-byte frames include zero, high-bit and mixed-order values.
At virtual 2/6/10/14/18/22/26/30 ms, the monitor records PRE state, supplies
bytes with model `WriteChar`, records POST state while guest execution remains
paused, then gives the guest 4 ms to report and rearm. The complete schedule is
40 virtual ms with a 100 us global quantum. There is no per-byte timestamp.

Each of 16 snapshots contains 12 fields: ENABLE, RXD.PTR, MAXCNT, AMOUNT,
ENDRX, RXDRDY, RXSTARTED, RXTO, INTEN, both guards and the buffer word. Exact
raw stdout offsets, lengths and SHA-256 include bounded known machine lifecycle
messages. Missing, extra, partial, reordered or contradictory fields fail.
The model/SRAM oracle is checked independently of the guest's printed claims.

The guest clears events, restores poison/guards and issues STARTRX after every
report. It deliberately preserves PTR/MAXCNT so a descriptor fault persists.
READY and source/compiler/variant-bound BUILD must precede exactly eight lines:

```text
SIM_NRF_UARTE_SAMPLE 00000001 WORD=23FF8001 N=00000004 LO=13579BDF HI=2468ACE0
```

All numbers are eight uppercase hexadecimal digits; sequence starts at one.
Wrong BUILD retains available model observations but cannot establish guest
identity or a fault effect. Partial final lines and guest/model disagreement
fail. A guard checks for corruption; it does not prevent an out-of-bounds write.

## Descriptor faults

`--fault` is mandatory, including `none`. Immediately before frame index 4:

- `rx-short-buffer` changes MAXCNT to 3 and restarts RX. The monitor supplies
  three bytes per remaining frame. AMOUNT=3 and the buffer's last byte remains
  poison. This tests a shortened descriptor, not a dropped byte on a wire.
- `rx-pointer-shift` changes PTR to `0x20001005` and restarts RX. Four bytes are
  supplied; the first buffer byte stays poison and the fourth input byte
  overwrites the low byte of the high guard. This exposes corruption in the
  mapped SRAM model; it does not qualify silicon memory protection.

Both expected fault effects retain `status=failed` and
`nominal_regression_passed=false`. `fault.effect_observed=true` is diagnostic
evidence and cannot turn a failing nominal regression into a pass.

```powershell
python examples/nrf52840-uarte-dma/build.py --output <new-build-directory>
sim-runner plan --profile nrf52840-uarte-dma-v1 --fault none `
  --runtime-lock <lock.json> --runtime-sha256 <lock-sha256> `
  --firmware <firmware.bin> --firmware-sha256 <image-sha256> `
  --ready-line 'SIM_NRF_UARTE_READY v1' --build-line 'SIM_NRF_UARTE_BUILD <build-id>' `
  --heartbeat-prefix SIM_NRF_UARTE_SAMPLE --output <new-plan.json>
sim-runner inspect-plan --plan <new-plan.json> --sha256 <plan-sha256>
sim-runner run --plan <new-plan.json> --sha256 <plan-sha256> --output-dir <new-run>
sim-runner inspect-report --report <new-run/report.json> --sha256 <report-sha256>
```

Each attempt uses a new directory and exact current runtime/firmware/model/
script hashes. No interactive per-run confirmation is added. Saved inspection
recomputes the verdict and raw observation bindings offline; original runtime
and firmware are unnecessary. This is integrity checking, not proof of origin.
Schemas are `embedded-sim-runner.uarte-dma-plan.v1` / `uarte-dma-report.v1`.
HIL/Trace and Toolkit adoption require their own explicit integration.

## Audited model subset and cost

The pinned [NRF52840 UART source](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/UART/NRF52840_UART.cs)
latches PTR and clears AMOUNT on STARTRX; each model byte writes mapped SRAM
and increments AMOUNT. Reaching MAXCNT sets ENDRX. RXSTARTED and RXDRDY are
separate event latches. The profile exercises this RX subset only; UART TX has
different model completion behavior and is excluded from DMA qualification.

TX DMA, interrupts, PPI, STOPRX/FLUSHRX, SHORTS, zero-length descriptors,
flow control, baud/parity/line errors and complete EasyDMA semantics are not
qualified. Model input is not UART pin activity. Complete model coverage,
event-time alignment, physical timing, DMA hardware and UART electrical
verification flags remain false, including on a passed regression. Warnings
and unsupported MMIO remain failures; no diagnostic allowlist is added.

Runtime dependencies stay empty. Bounds remain one owned process, 30 s host
timeout, 1 MiB per captured file and a 1024 MiB Windows Job ceiling. Sixteen
snapshots carry 192 numeric observations; analysis uses bounded captured data.
Record native host startup/run time and warm Python evaluation/inspection
costs separately from virtual time, RSS and cold I/O. Process, Job and capture
thread cleanup must hold for passed and failed attempts alike.
