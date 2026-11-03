# nRF52840 EGU task/event/interrupt regression

Local candidate 0.12.0 adds `nrf52840-egu-v1` with separate
`embedded-sim-runner.egu-plan.v1` and `embedded-sim-runner.egu-report.v1` schemas.
It uses the existing pinned Renode 1.17.0. HIL/Trace need explicit subsequent
integration; this profile does not change Toolkit or workstation selections.

## Audited subset

Renode 1.17 pins Infrastructure commit
`066a7f13c052215632d469c995c89aea37c573b1`. Its
[NRF52840_EGU implementation](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Miscellaneous/NRF52840_EGU.cs)
sets per-channel event and interrupt-pending state when TASKS_TRIGGER is written
with 1. Writing EVENTS_TRIGGERED=0 clears the event and pending interrupt.
INTEN/INTENSET/INTENCLR control interrupt enables independently of event latches.
The fixed model maps EGU0 at 0x40014000 to NVIC IRQ20 (exception vector 36),
with Cortex-M4, mapped flash/RAM and legacy UART0 only.
Nordic's [nRF52840 product specification](https://docs-be.nordicsemi.com/bundle/nRF52840_PS_v1.10/raw/resource/enus/nRF52840_PS_v1.10.pdf?save_local=true)
also identifies this address and interrupt number; hardware behavior is not
established by that mapping or by simulation results.

The monitor generates nine fixed task writes on channels 0, 3 and 15. This checks
the guest interrupt consumer, not a guest task producer. Channel 1 is an explicit
disabled sentinel. PPI, SHORTS, radio and external routing are absent; no
complete 16-channel, interrupt-priority/preemption, latency, sleep/power or
physical timing qualification follows. No arbitrary monitor input, host bridge,
physical fallback or automatic retry is accepted. Unhandled MMIO and warnings
remain failures.

## Schedule and oracle

The scenario lasts 40 virtual ms. After 2 ms boot, channels 0/3/15 repeat three
times at 2/6/10/14/18/22/26/30/34 ms. Each stimulus has a paused pre-service
snapshot, 2 ms guest service budget, and a paused post-service snapshot. Every
snapshot includes EVENTS_TRIGGERED on channels 0/1/3/15 and INTEN, plus its raw
stdout offset, length and SHA-256. Requested virtual boundaries are distinct
from host wall time and the order of raw UART bytes.

The nominal oracle requires exactly nine sequential ISR samples, correct
channel order, event before=1/after=0, mask=0x8009 and exception vector 36.
Independent model snapshots require the corresponding event pending before
service and cleared after, with the sentinel clear throughout.

| Required fault | Mutation at the fifth stimulus (18 ms) | Independent required effect |
| --- | --- | --- |
| `none` | None | Nine samples and all expected pre/post event states |
| `egu-irq-mask` | INTENCLR bit 3 | Seven samples; channel 3 remains pending and masked, while 0/15 keep working |
| `egu-channel-misroute` | Substitute TASKS_TRIGGER1 for TASKS_TRIGGER3 once | Eight samples; sentinel event 1 remains pending without IRQ, and later channel 3 still works |

The ISR checks enables as well as events, so a masked pending channel remains
latched even when its siblings cause interrupts. A fault marker is only a
request. A fully observed fault still returns `ASSERTION_FAILED` and a failed
nominal regression. Invalid/missing observations preserve unknown effect; a
marker with otherwise nominal evidence records effect=false and fails.

## Usage and saved evidence

The example uses installed Rust thumbv7em-none-eabihf and LLVM tools. Its BUILD
ID binds source, compiler and variant. Raw startup initializes sequence
explicitly; it is a simulation fixture, not a physical-DK image.

```powershell
python examples/nrf52840-egu/build.py --output D:/work/egu-firmware
sim-runner plan --profile nrf52840-egu-v1 --fault none --runtime-lock D:/tools/renode/runtime-lock.json --runtime-sha256 <lock-sha256> --firmware D:/work/egu-firmware/firmware.bin --firmware-sha256 <image-sha256> --ready-line 'SIM_NRF_EGU_READY v1' --build-line 'SIM_NRF_EGU_BUILD <build-id>' --heartbeat-prefix SIM_NRF_EGU_SAMPLE --output D:/work/egu-plan.json
sim-runner inspect-plan --plan D:/work/egu-plan.json --sha256 <plan-sha256>
sim-runner run --plan D:/work/egu-plan.json --sha256 <plan-sha256> --output-dir D:/work/egu-run
sim-runner inspect-report --report D:/work/egu-run/report.json --sha256 <report-sha256>
```

Virtual duration and nominal sample count must equal 40 and 9. Existing timeout,
output cap and Windows Job cleanup/memory bounds apply. The negative fixture
variants are wrong_channel, missing_clear, wrong_mask, irq_disabled and
unmapped_write. The missing-clear fixture masks each handled channel after one
bad ISR sample to bound its log; uncleared event and A=1 remain visible.

Saved inspection verifies staged input/script/model hashes and recomputes both
passing and failed assertions from raw evidence without opening the original
runtime or firmware. It rejects rehashed contradictory samples, snapshots,
effect or cleanup summaries. Consistency is distinct from authenticity and
physical qualification. Record native startup/inventory wall time, UART bytes
and warm offline analysis separately from 40 virtual ms; the memory cap is not
an RSS measurement or real-time SLA. Build and acceptance outputs stay outside
Git; a locally qualified candidate is not a published release.
