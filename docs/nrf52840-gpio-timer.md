# nRF52840 GPIO / TIMER0 profile (local 0.4.0 candidate)

`nrf52840-gpio-timer-v1` runs a fixed digital scenario on the existing hash-bound
Renode 1.17.0 Windows x64 runtime. It adds GPIO0, GPIO1, GPIOTE and TIMER0 to the
restricted Cortex-M4 / NVIC / RAM / flash / non-DMA UART model. It does not load
the full DK description, SVD downloads, register stubs or physical bridges.
GPIOTE is present because this runtime's GPIO PinChanged event requires a
subscriber; GPIOTE channels / sense / latch / PPI are not acceptance coverage.

The intended use is a small firmware regression for input processing, output
state and periodic interrupt handling before a physical test. The first profile
has a fixed scenario; it is not an arbitrary pin, monitor script or board model
runner. Unsupported protocols and timings require another audited profile.

## Fixed scenario

| GPIO0 input P0.11 change | State | Model snapshot after running to |
| --- | --- | --- |
| Start, 0 ms | low | 20 ms |
| 22 ms | high | 40 ms |
| 42 ms | low | 60 ms |
| 62 ms | high | 80 ms |

The fixed monitor script applies input changes while paused, runs the indicated
virtual durations, reads GPIO OUT / IN directly at each boundary and continues
to a total of 82 ms. Global quantum is 100 us. Boundary labels are the requested
RunFor schedule, not sampled wall clock, UART timestamps or electrical edges.
Raw register observations are emitted by the pinned runtime's `console_log`
command to the native stdout; ordinary monitor result / echo output is not
captured by headless mode. The exact script and its SHA-256 are bound in the plan.

Firmware uses TIMER0 timer mode, prescaler 6 (16 MHz / 64 = 250 kHz) and CC0
interrupts at absolute 1,250-tick intervals. Captures remain below 16-bit rollover;
the profile avoids unmodeled BITMODE / SHORTS registers and verifies no counter
width or overflow modes. It captures
CC1 on entry and reports the active exception number (24 = external IRQ 8).
The ISR reads P0.11, drives P0.13 and reads its OUT bit plus modeled loopback
P0.14. The built-in model connects output 13 to input 14. UART produces READY,
the exact build line, then exactly 16 sequenced lines:

```text
SIM_NRF_IO_SAMPLE 00000001 I=0 O=0 L=0 T=000004E2 V=00000018
```

The expected input / output / loopback values are four low, four high, four low,
four high. Each captured timer value must be within 25 ticks of sequence ×
1,250. This tolerance bounds the supported model/fixture's virtual IRQ entry;
it is not a real hardware latency guarantee. Four independent model snapshots
must show matching input, output and loopback states, in the exact order. UART
claims alone cannot satisfy the snapshot checks. Any unexpected/incomplete UART,
wrong build, missing sample, wrong state, vector or period fails. Native warnings,
unsupported accesses, missing completion, nonzero exits and forced cleanup fail.

## Build and run

The example requires installed Rust `thumbv7em-none-eabihf` and llvm-tools. It
does not install targets or use the Nordic SDK. It builds a raw vector-table
image and records compiler / source / variant hashes in a manifest. Its build ID
is generated from those inputs, not a physical firmware identity. Use the
manifest's current fields after each source/variant change.

```powershell
python examples/nrf52840-gpio-timer/build.py --output 'D:\work\gpio-fixture'
sim-runner plan --backend renode --profile nrf52840-gpio-timer-v1 `
  --runtime-lock '<runtime-lock.json>' --runtime-sha256 '<lock-sha256>' `
  --firmware '<gpio-fixture\firmware.bin>' --firmware-sha256 '<firmware-sha256>' `
  --ready-line 'SIM_NRF_IO_READY v1' --build-line 'SIM_NRF_IO_BUILD <build-id>' `
  --heartbeat-prefix SIM_NRF_IO_SAMPLE --output '<new-plan.json>'
sim-runner inspect-plan --plan '<plan.json>' --sha256 '<plan-sha256>'
sim-runner run --plan '<plan.json>' --sha256 '<plan-sha256>' --output-dir '<new-run>'
sim-runner inspect-report --report '<run\report.json>' --sha256 '<report-sha256>'
```

All commands always emit JSON. Do not add `--json`. For this profile
`--min-heartbeats` means IRQ samples and defaults to / must be 16; `--virtual-ms`
defaults to / must be 82. Host timeout, output and memory caps retain their native
bounds. Inputs remain local hash-bound regular files; no per-run confirmation,
retry, hardware discovery or physical fallback is added.

`--variant invert_output`, `slow_timer`, `missing_irq` and `unmapped_write` build
negative fixtures. `scripts/accept_gpio_timer.py` drives an installed Python CLI,
owns child trees with the existing Job containment, records every call and tests
two positives, those four negatives, wrong BUILD and a one-second host deadline.
Unexpected failures stop the controller; intentional negatives must return their
specified failures. Old UART/RAM/Flash profiles remain separately supported.

## Evidence and limits

New schemas are `embedded-sim-runner.gpio-timer-plan.v1` and
`embedded-sim-runner.gpio-timer-report.v1`. Artifacts use the existing Renode
directory layout: plan, runtime lock, model, raw firmware, exact monitor script,
empty config, stdout, stderr, UART and report. All bytes/hashes are retained even
on failure. Inspect is movable/offline and checks hashes, fixed scenario/script,
false coverage claims, native cleanup and saved assertions on successful runs;
it does not reexecute Renode or authenticate a report's author.

`model_coverage_complete=false` and `physical_timing_verified=false` remain
explicit even on `passed`. These tests cover the described digital behavior,
not electrical drive/pull characteristics, bounce, propagation delays, power,
clock accuracy, IRQ jitter on silicon, every timer mode, radio, GPIOTE/PPI,
SDK startup, DMA or hardware acceptance. A deliberately dishonest guest could
fabricate UART timer/vector claims; the runner is a regression tool, not an
attestation of guest code semantics. Model GPIO snapshots verify state at four
boundaries, not a complete waveform or every transient.

HIL 0.4.0 / Trace 0.4.0 / Toolkit 0.27.0 currently pin sim-runner 0.3.0 and do not
accept this new schema/inventory. Use native sim-runner until a separately tested
integration update. No formal station or plugin switch is implied by this candidate.

Source references used for this runtime:

- [Pinned platform definitions](https://github.com/renode/renode/blob/v1.17.0/platforms/cpus/nrf52840.repl)
- [Pinned GPIO model](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/GPIOPort/NRF52840_GPIO.cs)
- [Pinned TIMER model](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Timers/NRF52840_Timer.cs)
- [Pinned GPIOTE dependency](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/GPIOPort/NRF52840_GPIOTasksEvents.cs)
- [Monitor command substitution](https://renode.readthedocs.io/en/latest/basic/monitor-syntax.html)
