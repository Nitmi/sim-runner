# Handoff

## 2026-10-30 — local 0.9.0 SPI sensor profile

Branch: `feat/nrf52840-spi-sensor`, based on published 0.8.0 source
`7f542e0a8b4423b08b7ee0729102d261bb36e2a8`. User chose peripheral/fault expansion.
Adds an independent fixed legacy SPI0/LM74 profile, signed Q4 Rust fixture,
frozen-temperature and model-byte frame-slip controls. See
`docs/nrf52840-spi-sensor.md` for the exact 1.17 Infrastructure revision, protocol,
limits and commands. No remote writes, tags, releases, HIL/Trace/Toolkit edits,
plugin activation or workstation/runtime-lock switches are authorized here.

Source qualification: both Python 3.11/3.13 pass 339 tests; Ruff check/format
pass. Native nominal, two model faults, unsigned/stale/swapped/fraction-truncated
firmware, unmapped access and wrong BUILD were each captured once with complete
cleanup. Faults remain failed nominal regressions with observed effects. Package
and cold-install evidence belongs outside Git at
`D:/Code/tmp/sim-spi-090-20261030`; consult its final receipt for completion.

Pitfalls:

- Fixed 1.17 SPI chooses legacy/EasyDMA at construction, unlike newer master.
  It has no PSEL/FREQUENCY/CONFIG or CS routing, and does not call the sensor's
  `FinishTransmission`. LM74 alternates cached high/low bytes; after a slip the
  first sample following a temperature change contains the prior cached low
  byte. The independent literal test oracle must include those transition words.
- The slip is one fixed monitor `Transmit(0)`, not an electrical fault. Its
  marker/return alone is insufficient; model snapshots and guest words must
  establish effect separately from the nominal failure verdict.
- The initial native matrix helper expected a nonexistent BACKEND_DIAGNOSTIC
  code for unmapped MMIO; the real, correct code is MODEL_COVERAGE_FAILED. Only
  this host assertion was corrected; completed native outputs were preserved
  and no emulator case was replayed to hide the wrapper failure.
- Fixture source is directly `examples/.../main.rs`, not `src/main.rs`.
  Broad combined reads may truncate tool output; use bounded source regions.
  Updating wheel inventories adds one module and one model asset: 28 runtime
  payload files, 33 wheel members. Never confuse source or old installed imports.

Native startup/runtime inventory costs dominate 82 virtual ms. Record host
wall time and output bytes separately; the 1 GiB Job cap is not measured RSS or
a real-time/physical SPI throughput claim. Existing saved profiles remain
separate, and new schemas require explicit future HIL/Trace integration.

## 2026-10-29 — local 0.8.0 sensor profile

Branch: `feat/nrf52840-i2c-sensor`; based on published 0.7.0 plus its documentation
commit `8415a0a`. No remote writes, tags, releases, Toolkit/plugin edits or
workstation/runtime-lock switches are part of this change.

Adds the fixed Renode 1.17.0 TWI0/TMP103 sensor profile, signed-temperature Rust
fixture and two model faults. Review `docs/nrf52840-i2c-sensor.md` for protocol,
exact upstream source revision, limits and commands. New native schemas need
explicit HIL/Trace integration in a subsequent task. Do not route them through
old UART/GPIO validators or silently fall back to synthetic evidence.

Pitfalls:

- Renode master has newer NRF52840_I2C behavior. Audit the 1.17.0 Infrastructure
  gitlink, not master. This version lacks PSEL/FREQUENCY handling; TMP103's
  pointer must be consumed before STOP. TMP108 is a stub, not a separate
  validated sensor.
- A requested fault is not observed effect. Keep complete raw model snapshots,
  guest samples and the nominal failure verdict separate. Recompute saved
  assertion-failed reports as well as passed reports; hashes alone do not
  establish semantic consistency or authenticity.
- The reused Python 3.13 development venv contains an older copied sim-runner.
  Source tests must insert this checkout's `src` explicitly under `-I`, and cold
  installation must confirm installed import paths. Do not mistake old package
  tests for current source validation.
- Run Ruff formatting before checking E501 on newly authored files. Keep every
  attempt's raw output; formatting failures are development findings, not
  emulator or hardware failures.
- Host startup/inventory hashing dominates this 82 ms virtual scenario. Record
  actual wall time and memory/output cost; it is not real-time performance or
  an I2C/thermal timing measurement.
- Update `uv.lock`, release/test inventories and the independent package-smoke
  file count when adding modules/assets. The first development test run caught
  a stale expected count and a typo in a negative CLI test's GPIO profile name;
  both were corrected before cold installation.

Evidence is outside Git under `D:/Code/tmp/sim-sensor-080-20261029`. Published
Toolkit 0.35.0 and its selected component/environment locks remain unchanged.
