# Experimental ESP-EMU WASM / ESP32-C3 Flash profile

Local sim-runner 0.7.0 adds backend `espressif-esp-emulator-wasm`, profile
`esp32c3-wasm-flash-v1`. It runs the official ESP-EMU **0.50.0** WASM distribution
inside a hash-bound **Node 24.15.0** process on Windows x64. It is separate from
Espressif QEMU, a browser session and a native Windows ESP-EMU binary. This does
not install WSL or start a browser, server, proxy or physical bridge.

The upstream release has Linux/macOS native binaries and a browser WASM bundle.
The tested headless host uses its fixed WasmEmulator API directly; upstream does
not document this exact Node workflow. Core source is not included in the public
distribution repository. The host is an experimental adapter, not full-chip,
radio, security, electrical or physical timing qualification.

## Pin software before execution

Download `esp-emu-0.50.0-wasm.tar.gz` and `SHA256SUMS` from the
[official release](https://github.com/espressif/esp-emulator/releases/tag/v0.50.0).
Archive SHA-256 is
`ecaf1bed6c085434fd119c320ea2dab5c1890bada7675cea4b1c15e240ec8001`.
Extract the archive to a new directory and retain licenses. The runtime root
must contain `www/` and exactly the ten files in the packaged model inventory.
Do not execute the upstream installer, serve.py, page or browser worker.

```powershell
python scripts/pin_esp_wasm.py --archive '<official-wasm.tar.gz>' `
  --root '<extracted-root-containing-www>' --node '<absolute-node.exe>' `
  --node-sha256 '<trusted-node-executable-sha256>' --output '<new-runtime-lock.json>'
```

This checks the entire archive, extracted inventory and exact Node bytes, then
writes a new lock. It never extracts archives, runs Node, downloads or discovers
devices. Node's version in this lock is a declaration; actual execution checks
`process.versions.node` against 24.15.0. Obtain Node from a trusted distribution.
Hashing its executable does not authenticate its publisher, adjacent libraries
or the host OS. The upstream archive digest/checksum/API identity is integrity
evidence, not a claim that upstream supplied a GitHub artifact attestation.

## Plan, run and inspect

Use the same complete unsigned 4 MiB C3 DIO factory image accepted by the QEMU
[ESP-IDF 5.5.2 Flash profile](esp32c3-idf-flash.md). The bootloader is at zero,
the fixed partition table at 0x8000, and the application at 0x10000, with erased
data partitions. Other chips, ELF/RAM execution, secure/encrypted or OTA layouts
are unsupported. Build `examples/esp32c3-idf-flash` or select its bound image.

```powershell
sim-runner doctor --backend espressif-esp-emulator-wasm `
  --runtime-lock '<lock.json>' --runtime-sha256 '<lock-sha256>'
sim-runner plan --backend espressif-esp-emulator-wasm `
  --profile esp32c3-wasm-flash-v1 `
  --runtime-lock '<lock.json>' --runtime-sha256 '<lock-sha256>' `
  --firmware '<flash.bin>' --firmware-sha256 '<flash-sha256>' `
  --ready-line 'SIM_IDF_C3_READY v1' --build-line '<complete BUILD line>' `
  --heartbeat-prefix SIM_IDF_C3_HEARTBEAT --min-heartbeats 5 `
  --instruction-budget 50000000 --timeout 30 --output '<new-plan.json>'
sim-runner inspect-plan --plan '<plan.json>' --sha256 '<plan-sha256>'
sim-runner run --plan '<plan.json>' --sha256 '<plan-sha256>' --output-dir '<new-run>'
sim-runner inspect-report --report '<run/report.json>' --sha256 '<report-sha256>'
```

Commands emit JSON; do not add `--json`. Doctor/plan/inspect are file-only.
Plan and report use separate `embedded-sim-runner.esp-wasm-plan.v1` and
`embedded-sim-runner.esp-wasm-report.v1` schemas. No per-run confirmation or
automatic retry is added. HIL 0.7, Trace 0.7 and Toolkit 0.30 still select the
published sim-runner 0.6 and reject this new package/schema; integration is a
separate step, not an implicit station upgrade.

## Execution and verdict

Each run stages a private image, exact official glue/WASM/package/license,
fixed host script, model and lock. The fixed Node command enables its permission
model for the new run directory, disables addons and does not allow child
processes or workers. Ambient NODE_OPTIONS/NODE_PATH/preloads are removed.
The Windows Job owns the process from suspended startup and bounds host time,
memory and capture sizes. This is not a security sandbox for hostile host
software. No network/serial APIs are imported into the WASM module; no WiFi,
WebSocket, host UART input or physical bridge is configured.

The host runs batches of at most 10,000 requested instructions, up to the bound
(10,000–100,000,000, in multiples of 10,000). It stops at a guest restart request
without performing that restart, or at budget exhaustion. The fixture deliberately
requests restart after five heartbeats. A pass requires ROM, SDK second-stage,
factory load, exact READY/BUILD, complete contiguous heartbeats, no panic/error,
guest restart request, clean host exit and unchanged inputs. Budget exhaustion,
timeout, malformed output and unexpected diagnostics fail. Partial files remain.

All successes are `passed_with_model_limits`. Each report retains fixed limits:
opaque core/ROM/eFuse defaults, UART string representation, unmeasured clock and
incomplete chip/security/physical coverage. Fresh WASM memory holds a private
Flash copy; no physical Flash/eFuse is accessed and no storage copy is persisted
by the emulator. Default ROM/eFuse state is not independently dumped or verified.

## Evidence semantics

`run_batch` returns a JavaScript string. `uart.bin` preserves the UTF-8 encoding
of those returned strings, restricted to ASCII for this profile; it is **not**
claimed as the original UART byte stream. Non-ASCII/replacement output fails.
`raw_uart_bytes_verified=false` and
`uart_representation=ascii_subset_of_wasm_strings_encoded_utf8` remain explicit.

`runtime_observation` preserves batch count, requested instruction upper bound,
upstream cycle counter / cycles-per-us claim, chip/hart identity and stop reason.
Requested batches are not retired instruction counts: sleep handling or early
stop can change actual progress. The counter and frequency claim are not a
measured elapsed duration. Virtual time remains
`esp_emulator_unmeasured_device_clock`, elapsed=null, budget_enforced=false;
the instruction bound and host deadline are separate budgets.

Inspection works on copied evidence without live runtime/firmware paths. It
checks staged software/model/host/image hashes, rederives boot and nominal UART
assertions, and preserves failed verdicts and unknown observations. It never
executes Node or authenticates an evidence author. Rehashing false summary,
clock, coverage, cleanup or nominal success claims cannot make them valid.

Primary sources: [upstream distribution README](https://github.com/espressif/esp-emulator/tree/v0.50.0),
[official browser API use](https://github.com/espressif/esp-emulator/blob/v0.50.0/docs/guides/BROWSER.md),
and the exact release bundle's `www/worker.js` / `www/pkg/esp_emu.js`.
