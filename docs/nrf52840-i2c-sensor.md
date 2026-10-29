# nRF52840 TWI0 / TMP103 sensor regression

Local candidate **0.8.0** adds the explicit `nrf52840-i2c-sensor-v1` profile.
It executes Cortex-M4 firmware against Renode 1.17.0's TWI0 and TMP103 models.
Use it to catch signed-byte conversion, stale reads and bounded temperature
response regressions before board testing. HIL/Trace support for these new
schemas is a subsequent integration step; existing Toolkit environments remain
unchanged. This candidate has not been published.

The model contains NVIC, mapped flash/RAM, UART0, TIMER0 and one TMP103 at I2C
address `0x48`. Firmware selects register zero with STARTTX/TXD followed by
STARTRX/RXD and STOP, and reports both the raw byte and signed Celsius value.
The fixed 250 kHz TIMER0 interrupt captures every 1250 ticks (5 virtual ms),
stops after 16 samples and reports exception vector 24. The timer starts after
READY and BUILD; its counter is a guest clock, not a host timestamp.

| Phase | Model input set at virtual ms | Pause / snapshot at virtual ms | Nominal Celsius |
| --- | --- | --- | --- |
| 0 | 0 | 20 | 25 |
| 1 | 22 | 40 | -10 |
| 2 | 42 | 60 | 85 |
| 3 | 62 | 80 | 25 |

The 2 ms gaps separate input transitions from sample boundaries. The schedule
always runs 82 virtual ms. Exactly four samples belong to each phase. A sample
must have its expected raw byte, signed value, sequence, vector and zero driver
error. TIMER0 sample/counter tolerance is 25 ticks (100 virtual microseconds),
not an accuracy claim about a physical clock. Paused snapshots separately read
the sensor's Temperature property, TWI0 address register and TIMER0 counter.
Each snapshot records its exact stdout byte span and SHA-256. UART claims alone
cannot satisfy these model observations.

## Build and run

The example needs the installed Rust `thumbv7em-none-eabihf` target and LLVM
tools. It has no SDK dependencies and is specifically a simulation fixture;
it is not a hardware-ready board image. Its BUILD ID binds source files,
compiler identity and the selected firmware variant.

```powershell
python examples/nrf52840-i2c-sensor/build.py --output D:/work/sensor-build
sim-runner plan --profile nrf52840-i2c-sensor-v1 --fault none --runtime-lock D:/work/renode-lock.json --runtime-sha256 <lock-sha256> --firmware D:/work/sensor-build/firmware.bin --firmware-sha256 <image-sha256> --ready-line 'SIM_NRF_SENSOR_READY v1' --build-line 'SIM_NRF_SENSOR_BUILD <build-id>' --heartbeat-prefix SIM_NRF_SENSOR_SAMPLE --output D:/work/sensor-plan.json
sim-runner inspect-plan --plan D:/work/sensor-plan.json --sha256 <plan-sha256>
sim-runner run --plan D:/work/sensor-plan.json --sha256 <plan-sha256> --output-dir D:/work/sensor-run
sim-runner inspect-report --report D:/work/sensor-run/report.json --sha256 <report-sha256>
```

Use a fresh directory per run. No per-run interactive confirmation or physical
access is required. The normal UART protocol has exact READY/BUILD lines followed
by 16 lines of the form:

```text
SIM_NRF_SENSOR_SAMPLE 00000005 R=F6 C=FFFFFFF6 T=0000186A V=00000018 E=00000000
```

`R` is unsigned byte storage, `C` is signed int32 represented in hexadecimal,
and `T` is a TIMER0 capture. `F6` must become -10, not 246. The build fixture's
`unsigned_temperature`, `stale_temperature` and `unmapped_write` variants are
negative controls; acceptance must reject them.

## Fixed model faults

`--fault` is mandatory for this profile, including the `none` control. The
firmware is unchanged across the three model scenarios:

| Selection | Model property at the four snapshots | Expected native verdict |
| --- | --- | --- |
| `none` | 25, -10, 85, 25 | passed / exit 0 |
| `temperature-stuck` | 25, 25, 25, 25 | failed / exit 1 |
| `temperature-offset` | 25, 0, 95, 35 | failed / exit 1 |

The requested fault starts at 22 virtual ms. A frozen property is implemented
by keeping it at the initial 25; offset adds exactly 10 to subsequent nominal
inputs. These are controlled model stimuli, not an electrical fault simulator.
`fault.effect_observed` is true only when all four complete snapshots show the
selected property schedule, false when complete snapshots disagree, and null
when snapshots are missing or invalid. It does not establish physical causality.

`nominal_regression_passed` remains separate. Observing a requested fault never
turns a failing normal regression into a pass. Selecting a fault that has no
observed effect also fails the run. Reports preserve bounded guest samples,
independent snapshots and failure evidence. Offline `inspect-report` re-evaluates
both passing and assertion-failed reports from hash-bound raw artifacts, even
when the original runtime/image is unavailable. It checks integrity, not origin
or authenticity. A valid inspection of a failed report does not mean the
simulated regression passed.

## Model audit and limits

The source reviewed is the exact Renode 1.17.0 Infrastructure revision
`066a7f13c052215632d469c995c89aea37c573b1`, rather than current master:

- [NRF52840_I2C](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/I2C/NRF52840_I2C.cs): ENABLE=5, ADDRESS, STARTTX/TXD, STARTRX/RXD and STOP. This version does not implement PSEL or FREQUENCY registers; the fixture does not write them.
- [TMP103](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Sensors/TMP103.cs): register zero returns a signed 8-bit integer Temperature value; finishing a transaction clears the register pointer. The fixture reads register zero before STOP.
- [TMP108](https://github.com/renode/renode-infrastructure/blob/066a7f13c052215632d469c995c89aea37c573b1/src/Emulator/Peripherals/Peripherals/Sensors/TMP108.cs) is a stub using TMP103 behavior in this release. This profile explicitly uses TMP103 and makes no TMP108 compatibility claim.

There is no bus-speed/electrical model, NACK/disconnect acceptance, conversion
delay, calibration, thermal dynamics, sensor accuracy, EasyDMA or PPI coverage.
GPIO pin routing, real power behavior, radio, UICR, flash-controller behavior
and complete nRF52840/SDK compatibility remain outside this profile. All
coverage and physical-verification flags stay false. Unexpected warnings,
errors or unmapped accesses fail; no new diagnostic allowlist is introduced.

Performance cost is bounded independently: default 30 host seconds, 1 MiB per
output file, 1024 MiB Windows Job cap, one backend process and zero retries.
Each plan/run validates the full runtime inventory; file hashing and emulator
startup cost much more host time than this 82 ms virtual scenario. Offline
report inspection hashes only bounded evidence and never starts the backend.
Native acceptance records host wall time, output sizes and cleanup separately
from the virtual-time assertions. Offline inspection can measure Python
allocation peaks separately; these are not the emulator's resident memory.
