# nRF52840 watchdog regression

Local candidate 0.10.0 adds `nrf52840-watchdog-v1` with distinct
`embedded-sim-runner.watchdog-plan.v1` and
`embedded-sim-runner.watchdog-report.v1` schemas. It uses the existing pinned
Renode **1.17.0**, without downloads, hardware discovery or interactive per-run
approval. Arbitrary monitor commands, bridges and emulator arguments are not
inputs. HIL/Trace need explicit future integration for these new schemas.

## Source and coverage

The audited Infrastructure revision is
**066a7f13c052215632d469c995c89aea37c573b1**, the Renode 1.17.0 gitlink.
See the fixed
[NRF52840_Watchdog implementation](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Timers/NRF52840_Watchdog.cs)
and [monitor reset handler](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Extensions/UserInterface/Monitor.cs).
Use this revision, not newer master semantics.

WDT at 0x40010000 uses CRV=1023 and RREN=3. The 32768 Hz model reload interval
is `(CRV+1)/32768`, or 31.25 virtual ms. Every 5 ms, TIMER0 IRQ8 writes magic
`6E524635` to RR0 then RR1. REQSTATUS must change from 2 after RR0 to 3 after
RR1 reloads the timer and restores the request flags. RUNSTATUS must be 1.
The normal fixture writes no WDT CONFIG or interrupt-enable registers.

The model's CONFIG sleep/halt bits are tags, not a verified implementation.
No low-power behavior, timeout IRQ/two-cycle delay, RESETREAS, retained RAM,
physical oscillator/reset, electrical protection or security claim is made.
Guest `wfi` is used only to await the modeled TIMER0 IRQ, not to qualify power
behavior. Mapped flash/RAM and UART are the same limited digital primitives
as other fixed profiles; there is no SoftDevice, radio or physical bridge.

The startup code has no C/Rust runtime initializer. It explicitly initializes
its sequence on **every reset entry**, so a restarted sequence does not depend
on RAM clearing or retention. The raw binary is simulation-only. Repeated
READY/BUILD markers establish a guest protocol reboot, not firmware authenticity.

## Schedule and verdicts

The run lasts 82 virtual ms. Paused snapshots at 20, 40, 60 and 80 ms record
RUNSTATUS, REQSTATUS, CRV, RREN, model countdown and TIMER0 capture. Each snapshot
retains its raw stdout offset, byte length and SHA-256. UART records sequence,
the two request states, RUNSTATUS, captured ticks and interrupt vector 24.
Normal acceptance requires one boot, 16 complete samples, the selected BUILD,
no resets, valid model countdowns and monotonically scheduled TIMER0 ticks.

| Required `--fault` | Fixed operation | Observed effect requirements |
| --- | --- | --- |
| `none` | No fault | Full nominal regression passes |
| `feed-timer-stop` | Stop TIMER0 at 22 ms | Timer frozen at about 5500 ticks, watchdog still running and counting down, exactly one raw WDT reset and callback, two valid guest boots with 4 then 6 samples, restarted timer snapshots |
| `watchdog-stop` | Set model `Enabled=false` at 22 ms | No reset, 16 uninterrupted valid timer samples, RUNSTATUS changes to 0 in guest and model, later reload writes set the disabled countdown to 1024 |

The monitor installs a **fixed reset macro containing only a log marker**.
It neither requests reset nor reloads the image, patches memory or resumes the
CPU. Renode invokes it after the WDT's own `RequestReset`. This avoids the
monitor's missing-reset-macro warning while preserving the native reset path.
Without it, strict diagnostic checking rejects the run. No warning allowlist
or log filtering was added.

The feeding fault normally resets near 51.3 ms, after the final feed near 20 ms.
The report does not label host timestamps or UART byte order as exact reset
timing. Countdown and counter tolerances cover the fixed emulator schedule;
they are not real hardware tolerances.

Both faults return **failed nominal regressions**, even when the selected effect
is observed. A request/marker alone cannot prove effect; missing evidence keeps
the effect unknown. A fault that fails to disturb an otherwise valid normal run
also fails qualification, with nominal pass and effect=false recorded separately.

## Commands

The fixture needs an existing Rust `thumbv7em-none-eabihf` target and LLVM tools.
Every output directory must be new. Its source/compiler/variant-bound BUILD ID
and hashes are in `manifest.json`.

```powershell
python examples/nrf52840-watchdog/build.py --output D:/work/wdt-firmware
sim-runner plan --profile nrf52840-watchdog-v1 --fault none --runtime-lock D:/tools/renode/runtime-lock.json --runtime-sha256 <runtime-lock-sha256> --firmware D:/work/wdt-firmware/firmware.bin --firmware-sha256 <firmware-sha256> --ready-line 'SIM_NRF_WDT_READY v1' --build-line 'SIM_NRF_WDT_BUILD <build-id>' --heartbeat-prefix SIM_NRF_WDT_SAMPLE --output D:/work/wdt-plan.json
sim-runner inspect-plan --plan D:/work/wdt-plan.json --sha256 <plan-sha256>
sim-runner run --plan D:/work/wdt-plan.json --sha256 <plan-sha256> --output-dir D:/work/wdt-run
sim-runner inspect-report --report D:/work/wdt-run/report.json --sha256 <report-sha256>
```

The sample budget and duration are fixed at 16 nominal samples / 82 ms. The
existing host timeout, per-file output limit and Windows Job memory/cleanup
bounds still apply. No automatic retries, physical fallback or persistent PATH
changes occur. Development negatives `wrong_reload_magic`,
`missing_second_channel`, `never_started` and `unmapped_write` are fixture build
variants, not extra runtime commands.

Offline `inspect-report` binds the staged model/script/scenario and recomputes
successful **and assertion-failed** summaries. Rehashed contradictory samples,
reset/effect claims and incomplete cleanup are rejected. Saved evidence is
movable and does not require the original runtime or source firmware. Integrity
and semantic consistency do not authenticate self-authored captures.

Record native host wall time, captured bytes and Python evaluation/inspection
cost separately from virtual time. Runtime inventory hashing and process startup
dominate this small scenario. A 1 GiB Job cap is not measured RSS or a real-time
performance guarantee. Runtime downloads, fixtures, reports and cold installs
belong outside Git; local package qualification is not a published release.
