# GPIO / TIMER0 model fault inputs (local 0.6.0 candidate)

`nrf52840-gpio-timer-fault-v1` adds an explicit, fixed fault selection to the
Renode 1.17.0 [GPIO/TIMER0 scenario](nrf52840-gpio-timer.md). It reuses that
model, firmware protocol, 82 ms schedule, 16 nominal samples, 100 us quantum,
resource limits and process owner. No physical board or arbitrary monitor
command is accepted. The existing five profiles remain separate.

## Selection and verdict

`--fault` is required for this profile and rejected on every other profile.

| Fault | Fixed action at requested virtual 22 ms | Independent model observation |
| --- | --- | --- |
| `none` | No injection; normal control | GPIO follows the schedule; counter advances; COMPARE0 IRQ enabled |
| `gpio-stuck-low` | Hold P0.11 low for the remaining input phases | IN bit 11 remains low at the paused boundaries |
| `gpio-stuck-high` | Hold P0.11 high for the remaining input phases | IN bit 11 remains high at the paused boundaries |
| `timer-stop` | Write TASKS_STOP=1 on TIMER0 | Captured model count stays near 5,500; IRQ enable remains set |
| `timer-irq-mask` | Clear TIMER0 COMPARE0 interrupt enable | Counter advances; INTENSET bit 16 becomes zero |

These are digital model stimuli, not electrical stuck faults, a stopped silicon
clock or proof of a production root cause. The script applies the action while
paused. Schedule values are requested RunFor boundaries, not host timestamps.
There is no user-controlled address, pin, timing, register value, script or argv.

The firmware is still judged against the **normal** scenario. Observing an
injection does not make that regression pass. Normal controls pass; the four
faults against the supplied fixture produce native `failed` / `ASSERTION_FAILED`
and CLI exit 1. No `expected_failure` success status or automatic retry is added.
A selected fault that is not observed also cannot pass. An intentional negative
test controller may evaluate that expected exit; a normal HIL workflow must
retain the native failure and stop subsequent cases.

## Plan and run

Build the same raw firmware with `examples/nrf52840-gpio-timer/build.py`. Use its
hash-bound READY/BUILD/sample fields:

```powershell
sim-runner plan --backend renode --profile nrf52840-gpio-timer-fault-v1 `
  --fault timer-stop --runtime-lock '<lock.json>' --runtime-sha256 '<lock-sha>' `
  --firmware '<firmware.bin>' --firmware-sha256 '<firmware-sha>' `
  --ready-line '<manifest-ready>' --build-line '<manifest-build>' `
  --heartbeat-prefix SIM_NRF_IO_SAMPLE --output '<new-plan.json>'
sim-runner inspect-plan --plan '<plan.json>' --sha256 '<plan-sha>'
sim-runner run --plan '<plan.json>' --sha256 '<plan-sha>' --output-dir '<new-directory>'
sim-runner inspect-report --report '<directory>/report.json' --sha256 '<report-sha>'
```

Each operation emits JSON; do not add `--json`. There is no per-run approval
prompt. The new schemas are `embedded-sim-runner.gpio-fault-plan.v1` and
`embedded-sim-runner.gpio-fault-report.v1`; the plan binds the selected fixed
scenario, model and exact generated script. All previous budgets are retained.

## Evidence and uncertainty

At requested 20/40/60/80 ms boundaries, the paused monitor captures GPIO OUT/IN,
TIMER0's model counter and interrupt-enable state. The fixed observer writes
TASKS_CAPTURE3 and reads CC3; the fixture uses only channels 0/1 and never
enables channel 3 IRQ. This is an explicit model observation with a CC3 side
effect, not a passive physical measurement. Captures must be complete, ordered
ASCII protocol lines; surrounding Windows locale log bytes remain unchanged.

After a clean backend completion, `assertions` retains bounded guest samples,
all four independent snapshots, `model_timer_nominal`, `nominal_failure` and
the nominal verdict even when the regression fails. Guest UART cannot establish
the model effect. `assertions.fault.effect_observed` is true/false only after
the complete model snapshot protocol is parsed; it is null when not selected
or not evaluated. Missing/malformed snapshots, startup/host timeout or native
model errors never imply a verified injection. Original UART/stdout/stderr,
plan, model and firmware are retained under the usual artifact hashes.

Only the first 16 valid sample summaries are retained in JSON; the observed line
count and a truncation flag expose excess output, which fails the nominal
oracle. Raw capture limits are unchanged. Counter samples are not guest UART
timestamps, physical latency, electrical timing or proof of complete IRQ modes.
`model_coverage_complete=false` and `physical_timing_verified=false` remain explicit.

Saved inspection rechecks the fixed script/model/scenario and rederives fault
summaries for failed as well as successful evaluations. Rehashing a false
effect, counter, sample or nominal verdict is rejected. Inspection does not
start Renode, rerun the guest, authenticate the evidence author or establish a
physical firmware identity. Process cleanup remains a separate recorded result.

`scripts/accept_gpio_fault.py` runs two normal controls, all four faults, wrong
BUILD and a bounded host timeout through an installed CLI. Native failures keep
exit 1; the controller succeeds only when each declared expectation and cleanup
matches. HIL 0.6.0, Trace 0.6.0 and Toolkit 0.29.0 still pin sim-runner 0.5.0 and
do not accept this new schema/package. Native 0.6.0 remains a local candidate
until a separate integration and release step.

Model source review: [fixed TIMER0 START/STOP, capture and INTEN implementation](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Timers/NRF52840_Timer.cs)
and [fixed GPIO OnGPIO/IN implementation](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/GPIOPort/NRF52840_GPIO.cs).
