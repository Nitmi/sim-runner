# Handoff

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
