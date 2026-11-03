# nRF52840 GPIOTE input-event and interrupt regression

The `nrf52840-gpiote-v1` profile uses the fixed Renode 1.17.0 runtime. The
audited Infrastructure revision is `066a7f13c052215632d469c995c89aea37c573b1`:
[GPIOTE model](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/GPIOPort/NRF52840_GPIOTasksEvents.cs)
and [GPIO model](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/GPIOPort/NRF52840_GPIO.cs).
The model maps GPIOTE at 0x40006000 to IRQ6, exception vector22.

## Fixed scenario

Three event channels select different pins: channel0=P0.11 rising,
channel1=P0.12 falling and channel2=P1.3 toggle. Input P0.12 starts high;
the other selected inputs start low. INTEN is7, with PORT interrupt disabled.
The monitor produces nine fixed input changes at2/6/10/14/18/22/26/30/34 virtual
ms: P0.11 high/low, P0.12 low/high, P1.3 high/low, P0.11 high, P0.12 low,
P1.3 high. Two nonmatching edges must produce no channel IRQ.

Each change has paused PRE and POST model observations separated by a2ms
guest-service budget. Eighteen snapshots retain EVENTS_IN0/1/2, INTENSET,
CONFIG0/1/2 and both GPIO IN registers as raw stdout offset/length/SHA-256.
Seven nominal ISR samples must show channels0/1/2/2/0/1/2, selected pin level,
event before1/after0, mask7 and vector22. Guest fields cannot substitute for
independent model observations. The raw Rust fixture initializes its own sequence.

Renode's CONFIG bit20 reads the channel's *current* state, which changes with
input edges; it is not a constant copy of the initial OUTINIT write. Its edge
handler assigns EventPending from each changed input instead of implementing
every silicon latch guarantee. These are explicit model semantics, not claims
of complete Nordic hardware behavior.

| Fault at stimulus index4 | Independent effect | Nominal verdict |
| --- | --- | --- |
| `none` | Seven ISR samples, correct register and pin states | Passed only when all evidence agrees |
| `gpiote-irq-mask` | INTENCLR masks channel2; its event remains pending while channels0/1 run; four ISR samples | Failed even when effect is observed |
| `gpiote-wrong-polarity` | CONFIG2 changes toggle to falling; two rising events disappear, one falling event remains; five ISR samples | Failed even when effect is observed |

A request marker is distinct from proof of effect. Wrong BUILD, incomplete
observations, contradictory evidence and failed execution retain unknown/null
where evaluation could not occur. Saved inspection recomputes the same policy
from bounded local artifacts, including failed reports, and rejects rehashed
contradictions. It never starts Renode or reopens original runtime/firmware.
Bounded exact asynchronous machine paused/resumed INFO stays inside each raw
snapshot span; warnings, unrelated INFO, extra markers and excess lines fail.

## Use

Build the simulation-only fixture with
`python examples/nrf52840-gpiote/build.py --output <new-directory>`. The manifest
binds compiler, source bytes, variant, BUILD and image hash. Negative variants
are wrong channel claims, wrong channel0 polarity, missing channel2 enable,
disabled NVIC and unmapped MMIO. They are separate native attempts, never retries.

```powershell
sim-runner plan --backend renode --profile nrf52840-gpiote-v1 --fault none `
  --runtime-lock <lock.json> --runtime-sha256 <lock-sha256> `
  --firmware <firmware.bin> --firmware-sha256 <image-sha256> `
  --ready-line 'SIM_NRF_GPIOTE_READY v1' --build-line 'SIM_NRF_GPIOTE_BUILD <build-id>' `
  --heartbeat-prefix SIM_NRF_GPIOTE_SAMPLE --output <new-plan.json>
sim-runner inspect-plan --plan <new-plan.json> --sha256 <plan-sha256>
sim-runner run --plan <new-plan.json> --sha256 <plan-sha256> --output-dir <new-run>
sim-runner inspect-report --report <new-run/report.json> --sha256 <report-sha256>
```

Fault selection is explicit, including `none`. Fixed time is40 virtual ms and
minimum is seven nominal samples; arbitrary schedules or monitor scripts are
not accepted. GPIOTE has distinct `embedded-sim-runner.gpiote-plan.v1` and
`embedded-sim-runner.gpiote-report.v1` schemas; older HIL/Trace require explicit
future integration and must not treat them as another profile's evidence.

## Limits and cost

TASKS_OUT/SET/CLR, PORT/SENSE/LATCH/DETECTMODE, PPI, debounce, pulls, drive,
all-channel/pin completeness, electrical events, IRQ latency/preemption,
sleep/power and physical timing remain unqualified. Model coverage, PPI and
physical timing flags stayfalse. No hardware access or physical fallback exists.
Host bounds remain30s,1MiB/output, one backend process and1024MiB Windows Job
memory, with complete process/capture-thread cleanup. The host window, virtual
schedule and guest ISR order are distinct clocks; no verified synchronization
or physical throughput follows. Record startup/inventory/host time and Python
allocation separately from model time and backend RSS, without an SLA claim.
