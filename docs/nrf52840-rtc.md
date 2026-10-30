# nRF52840 RTC0 compare regression

Local candidate 0.11.0 adds the fixed `nrf52840-rtc-v1` profile with separate
`embedded-sim-runner.rtc-plan.v1` and `embedded-sim-runner.rtc-report.v1` schemas.
It reuses the pinned Renode 1.17.0 and owns no physical device, host bridge or
arbitrary monitor input. HIL/Trace require explicit later integration.

## Audited model and limits

The Renode 1.17 Infrastructure gitlink is
`066a7f13c052215632d469c995c89aea37c573b1`. Its fixed
[NRF52840_RTC implementation](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Timers/NRF52840_RTC.cs)
uses a 32768 Hz source, a PRESCALER+1 divider and compare timers. This profile
maps RTC0 at 0x4000B000 to NVIC IRQ11, uses CC0 and PRESCALER=31 (1024 Hz),
and programs absolute compare values 16 ticks apart. The guest checks the event
before and after clearing, the next CC0 value, prescaler and exception vector 27.

Only this documented subset is covered. The model's TICK/overflow paths,
TRIGOVRFLW and 24-bit wrap are not exercised. LFCLK selection/startup, oscillator
drift, PPI, sleep, power and physical wakeup are not qualified. Guest `wfi`
only waits for the emulated interrupt. No CLOCK peripheral is substituted with
a stub, and no warning or unhandled access is suppressed.

## Scenario and evidence

The fixed schedule lasts 162 virtual ms, with observations at 40/80/120/160 ms.
The nominal case requires exactly ten complete samples at counter values near
16..160, sequential numbering, event 1 before clearing and 0 after, correct
prescaler/vector and next compare values 32..176. Independent paused model
snapshots include COUNTER, PRESCALER, CC0, EVENTS_COMPARE0 and INTENSET.
Each snapshot retains its raw stdout offset, length and SHA-256.

| Required fault | Fixed operation at 42 virtual ms | Required independent effect |
| --- | --- | --- |
| `none` | No mutation | Ten valid IRQ samples and four advancing model snapshots |
| `rtc-stop` | Write RTC TASKS_STOP | Two valid samples, counter frozen near 43, CC0=48 and no pending event; interrupt remains enabled |
| `rtc-irq-mask` | Clear CC0 interrupt enable | Two valid samples, counter continues, CC0 stays 48, compare event becomes pending and interrupt mask is zero |

A fault marker is only a request. Model and guest evidence must establish the
effect separately. Both observed faults still return `ASSERTION_FAILED` and a
failed nominal regression. Missing/invalid snapshots leave effect unknown;
a marker with an otherwise normal run records effect=false and fails. Fault
observation cannot turn a failing nominal regression into success.

Counter tolerances of two ticks cover the fixed emulator/guest schedule only;
they are not physical oscillator tolerances or measured UART timestamps.
UART is raw ordered bytes. Paused snapshots, requested virtual boundaries and
host run windows remain distinct clock/evidence domains.

## Build and run

Use the installed Rust thumbv7em-none-eabihf target and LLVM tools. The fixture
builder binds source, compiler and variant into BUILD identity. Its vector table
and raw startup explicitly initialize sequence; it is not a physical-DK image.
Every build/run destination must be new.

```powershell
python examples/nrf52840-rtc/build.py --output D:/work/rtc-firmware
sim-runner plan --profile nrf52840-rtc-v1 --fault none --runtime-lock D:/tools/renode/runtime-lock.json --runtime-sha256 <lock-sha256> --firmware D:/work/rtc-firmware/firmware.bin --firmware-sha256 <image-sha256> --ready-line 'SIM_NRF_RTC_READY v1' --build-line 'SIM_NRF_RTC_BUILD <build-id>' --heartbeat-prefix SIM_NRF_RTC_SAMPLE --output D:/work/rtc-plan.json
sim-runner inspect-plan --plan D:/work/rtc-plan.json --sha256 <plan-sha256>
sim-runner run --plan D:/work/rtc-plan.json --sha256 <plan-sha256> --output-dir D:/work/rtc-run
sim-runner inspect-report --report D:/work/rtc-run/report.json --sha256 <report-sha256>
```

The sample count defaults to/must be 10 and virtual duration to/must be 162 ms.
The existing host timeout, capped output and Windows Job cleanup/memory bounds
apply. There is no per-run interactive confirmation, automatic retry or physical
fallback. The fixture's wrong_prescaler, missing_clear, stale_compare,
never_started and unmapped_write variants are development negatives, not open
monitor commands.

Saved evidence remains movable. Native `inspect-report` binds staged inputs,
script/model/scenario and raw files and recomputes both passing and
assertion-failed summaries without opening the original firmware/runtime.
Rehashed contradictory effect, sample, raw snapshot or cleanup claims fail.
This verifies integrity/consistency, not authenticity or physical qualification.

Record host startup/inventory cost, capture bytes and warm offline evaluation
cost separately from 162 virtual ms. No real-time SLA or measured RSS follows
from the 1 GiB Job memory cap. Build/cold-install/test evidence belongs outside
Git; local candidate qualification is not a published Release.
