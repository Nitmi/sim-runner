# nRF52840 legacy SPI / LM74

Local candidate 0.9.0 adds `nrf52840-spi-sensor-v1`, using the existing hash-bound
Renode **1.17.0** runtime. No physical devices, SDK, runtime download, persistent
configuration or interactive per-run confirmation is required. All model commands
are fixed by the selected enum; arbitrary scripts/arguments/bridges are rejected.
The new plan/report schemas are `embedded-sim-runner.spi-sensor-plan.v1` and
`embedded-sim-runner.spi-sensor-report.v1`. They are intentionally distinct from
I2C; existing HIL/Trace versions do not consume them yet.

## Audited behavior and limits

The [Renode 1.17.0 Infrastructure gitlink](https://github.com/renode/renode/tree/v1.17.0/src/Infrastructure)
is **066a7f13c052215632d469c995c89aea37c573b1**. This profile audits the exact
[NRF52840_SPI](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/SPI/NRF52840_SPI.cs)
and [TI_LM74](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Sensors/TI_LM74.cs)
implementations. Do not use newer master register semantics.

SPI0 is at 0x40003000, with `easyDMA=false` fixed at model construction. ENABLE=1
selects the legacy controller. Each TXD write immediately clocks one model byte;
the fixture clears READY first, polls a bounded 10,000 iterations, then consumes
RXD. There are no PSEL/FREQUENCY/CONFIG writes. No SPI IRQ is enabled. TIMER0's
250 kHz IRQ schedules 16 reads at 5 ms intervals; UART0 records their order.

LM74 alternates a cached high and low byte. Its model word is
`((signed_Q4 << 3) | 7) & 0xffff`, with Q4 units of **1/16 °C**. The normal
protocol must preserve both bytes, all three low bits and arithmetic sign shift.
The four temperatures are 25.0625, -10.125, 85.5 and 0.0625 °C, giving model
words **0C8F, FAF7, 2AC7, 000F**. These are model values, not calibrated readings.

The controller does not signal chip-select or call `FinishTransmission`. The
fixture starts aligned and reads exactly two bytes per sample. Frame alignment
is therefore part of this bounded model regression, not proof of a hardware SPI
driver. Bus frequency/mode, pin routing, electrical behavior, real CS framing,
conversion delay, calibration, EasyDMA/PPI and physical timing are not verified.
Unexpected warnings/errors/unhandled MMIO still fail; no new diagnostic allowlist.

## Fixed schedule and faults

Inputs change while paused at 0, 22, 42 and 62 virtual ms. Independent paused
observations at 20, 40, 60 and 80 ms record model Temperature, SPI ENABLE and a
TIMER0 capture. The run completes at 82 ms. Model observations retain exact raw
stdout offsets, lengths and hashes; UART retains guest raw words and signed Q4.

| `--fault` | Model control | Evidence needed for observed effect |
| --- | --- | --- |
| `none` | Four temperatures, two-byte guest frames | Normal protocol, raw words, Q4, sequence, IRQ/counter and four model snapshots all pass |
| `temperature-stuck` | Hold the initial model Temperature after 22 ms | Four snapshots show the frozen value; nominal regression still fails |
| `frame-slip` | At 22 ms consume exactly one byte via the model's `Transmit(0)` | Marker plus returned high byte, unchanged nominal temperatures, ENABLE/counter observations and all 16 guest words match the slipped model stream |

`frame-slip` is a monitor model operation, **not an injected wire bit error or
electrical fault**. Its discarded byte must be FA. Following words are F7FA;
at the next input transition the first word is F72A (cached old low byte), then
C72A. The final transition gives C700, then 0F00. Merely selecting a fault or
seeing the monitor marker never proves the guest effect. A missing observation
leaves the effect unknown. Fault cases return a failed nominal regression, not
a converted pass.

`inspect-report` launches no backend: it binds the fixed model/script/schedule
and recomputes both successful and assertion-failed analyses from raw evidence.
Changing summary fields or rehashing contradictory snapshots is rejected. Local
hashes establish integrity/consistency, not authenticity of self-authored evidence.

## Run

Build the no-dependency Rust fixture using an installed `thumbv7em-none-eabihf`
target and LLVM tools. Each output directory must be new.

```powershell
python examples/nrf52840-spi-sensor/build.py --output D:/work/spi-firmware
sim-runner plan --profile nrf52840-spi-sensor-v1 --fault none --runtime-lock D:/tools/renode/runtime-lock.json --runtime-sha256 <lock-sha256> --firmware D:/work/spi-firmware/firmware.bin --firmware-sha256 <firmware-sha256> --ready-line 'SIM_NRF_SPI_READY v1' --build-line 'SIM_NRF_SPI_BUILD <build-id>' --heartbeat-prefix SIM_NRF_SPI_SAMPLE --output D:/work/spi-plan.json
sim-runner inspect-plan --plan D:/work/spi-plan.json --sha256 <plan-sha256>
sim-runner run --plan D:/work/spi-plan.json --sha256 <plan-sha256> --output-dir D:/work/spi-run
sim-runner inspect-report --report D:/work/spi-run/report.json --sha256 <report-sha256>
```

`--fault` is mandatory; cross-profile controls and mixed backends fail offline.
The sample count/duration are fixed at 16/82 ms. Existing host timeout, output,
memory, Windows Job ownership and cleanup bounds apply unchanged. The fixture
supports `unsigned_temperature`, `swapped_bytes`, `truncated_fraction`,
`stale_temperature` and `unmapped_write` development-negative variants, selected
only during fixture build. Each gets a separate source/compiler-bound BUILD ID.

Measure host wall time, output bytes and memory separately from virtual time.
Runtime inventory hashing and process startup dominate this small scenario;
82 virtual ms is not a host real-time promise or a SPI throughput measurement.
