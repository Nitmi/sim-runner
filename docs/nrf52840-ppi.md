# nRF52840 TIMER0 → PPI → GPIOTE functional regression

Local candidate 0.14.0 adds `nrf52840-ppi-v1`, using the fixed Renode 1.17.0
runtime. This is functional model routing, not event alignment or a timing test.
No physical board, network bridge, SDK or radio medium is opened.

## Fixed chain and evidence

| Producer | Route | Consumer |
| --- | --- | --- |
| TIMER0.EVENTS_COMPARE0, `0x40008140` | PPI channel 0 EEP / TEP | GPIOTE.TASKS_OUT0, `0x40006000`: P0.11 toggle |
| Same event | Channel 0 FORK.TEP | GPIOTE.TASKS_OUT1, `0x40006004`: P1.3 toggle |
| Fault-only fork destination | `0x40006008` | GPIOTE.TASKS_OUT2: sentinel P0.13 toggle |

The guest configures GPIOTE task mode, TIMER0 PRESCALER=4 / CC0=1000 and the
route; IRQs remain disabled. The fixed monitor starts eight timer intervals
at virtual 2/6/10/14/18/22/26/30 ms, using STOP/CLEAR/START and a 2 ms service
window per trigger. It does not emulate missing timer SHORTS with guest writes.
The total run is 40 virtual ms. A 100 µs global quantum only bounds this model
schedule; it does not establish event latency.

Sixteen paused PRE/POST snapshots each read 14 fields: timer event, prescaler,
CC0, channel mask, EEP, TEP, FORK.TEP, three GPIOTE configurations, both GPIO OUT
and both GPIO IN registers. PRE requires a cleared timer event; POST requires
the compare event plus exact routed states. Snapshot offsets, lengths and
SHA-256 bind raw stdout, including bounded asynchronous lifecycle messages.
Missing, duplicate, reordered, partial, excessive and contradictory fields fail.

UART requires exact READY and source/compiler/variant-bound BUILD lines followed
by exactly eight nominal `SAMPLE <sequence> A=<main> B=<fork> S=<sentinel>` lines
(all values eight uppercase hexadecimal digits). The guest polls the pins after
a fixed settling loop; the model register oracle is independent of those claims.
Wrong BUILD preserves model observations but cannot establish guest identity or
fault effect. UART/model contradictions cannot pass, including after rehashing
a changed saved report.

## Faults and commands

`--fault` is mandatory, including `none`. At trigger index 4:

- `ppi-channel-disable` writes CHENCLR=1. Compare events continue; both outputs
  stop changing. Only the first four guest samples are expected.
- `ppi-fork-misroute` changes FORK.TEP to the sentinel task. The main pin keeps
  toggling, the expected fork pin stops, and the sentinel toggles instead.

Observed faults retain `status=failed` and `nominal_regression_passed=false`.
`fault.effect_observed=true` is diagnostic evidence, never a passing regression.

```powershell
python examples/nrf52840-ppi/build.py --output <new-build-directory>
sim-runner plan --profile nrf52840-ppi-v1 --fault none `
  --runtime-lock <lock.json> --runtime-sha256 <lock-sha256> `
  --firmware <firmware.bin> --firmware-sha256 <image-sha256> `
  --ready-line 'SIM_NRF_PPI_READY v1' --build-line 'SIM_NRF_PPI_BUILD <build-id>' `
  --heartbeat-prefix SIM_NRF_PPI_SAMPLE --output <new-plan.json>
sim-runner inspect-plan --plan <new-plan.json> --sha256 <plan-sha256>
sim-runner run --plan <new-plan.json> --sha256 <plan-sha256> --output-dir <new-run>
sim-runner inspect-report --report <new-run/report.json> --sha256 <report-sha256>
```

Each run needs a new directory and rechecks exact input/runtime/model/script
hashes. No per-run manual confirmation is added. Saved inspection is offline,
movable and does not reread original runtime/firmware or launch subprocesses.
Schemas are `embedded-sim-runner.ppi-plan.v1` / `ppi-report.v1`; HIL/Trace and
Toolkit require separate integration and must not reinterpret these schemas.

## Audited model limitations and cost

The audited [PPI source](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Miscellaneous/NRF52840_PPI.cs)
defers both tasks to `ExecuteInNearestSyncedState`. Upstream
[issue 1016](https://github.com/renode/renode/issues/1016) documents delayed
triggering. This profile does not patch Renode or claim precise compare/task
timing. Broad PPI qualification, event alignment, physical timing and complete
model coverage flags remain false even on a passed functional regression.

The first PPI write lazily subscribes preprogrammed channels to TIMER0, RTC0
and RADIO events. Inactive RTC0 and RADIO are mapped so these subscriptions
succeed without hiding errors; fixed channels remain disabled, RTC is not
started and RADIO has no medium. This does not qualify radio or RTC behavior.
Groups, other configurable/preprogrammed channels, capture, counter mode,
SHORTS/BITMODE, GPIOTE SET/CLR/PORT/SENSE and all electrical/radio/timing
properties are outside the tested subset. Warnings and unsupported accesses fail.

Runtime dependencies remain empty. Host bounds stay 30 s, one owned process,
1 MiB per captured file and a 1024 MiB Windows Job memory ceiling. Record host
startup/inventory/run costs separately from virtual time; these ceilings are
not measured RSS or performance guarantees. Acceptance must check Job/process
and capture-thread cleanup for failed as well as passed runs.
