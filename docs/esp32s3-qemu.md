# ESP32-S3 ESP-IDF Flash profile (0.5.0 local candidate)

`esp32s3-idf-flash-v1` uses a separate **Xtensa** distribution, executable,
ROM and chip-image ID. It boots a fixed unsigned 4 MiB DIO factory layout with
ESP-IDF 5.5.2, observes CPU0/CPU1 fixture markers and UART readiness, then exits
on the guest's restart request with `-no-reboot`. It does not flash hardware.
It shares the bounded process/private-storage implementation with C3 while
keeping profile schemas, backend identity, time semantics and diagnostics separate.

## Runtime and SDK

The Windows x64 runtime is Espressif QEMU `esp-develop-9.2.2-20260417`, source
`40edccac415693c5130f91c01d84176ae6008566`. Its official Xtensa archive is
`qemu-xtensa-softmmu-esp_develop_9.2.2_20260417-x86_64-w64-mingw32.tar.xz`,
35,996,720 bytes, SHA-256
`3c483d77f5350a568df1faf4d8dbc82c95d6bc2b826d0d4be910485e0a68ca2a`.
This is the reviewed later release, not the older QEMU recommended by IDF 5.5.2's
tools metadata. The archive hash matches the official GitHub asset digest;
this is not an attestation of upstream binaries or authentication of a runtime lock.

```powershell
python scripts/pin_qemu.py --architecture xtensa --archive '<downloaded-archive>' --destination '<new-tools-directory>' --output '<new-lock-path>'
sim-runner doctor --backend espressif-qemu-xtensa --runtime-lock '<lock>' --runtime-sha256 '<lock-sha256>'
```

Pinning checks the fixed archive size/hash and rejects links, duplicate or escaped
entries before extraction. Doctor rechecks the complete installed inventory;
neither starts QEMU. Use a separate directory from the C3 RISC-V distribution.
No persistent PATH, system feature or driver change is required. Runtime binaries
are trusted host software; this runner is not a security sandbox.

The SDK fixture uses ESP-IDF **v5.5.2** source commit
`30aaf64524299d3bde422ca9a2848090d1bc5d0f` and its required pinned submodules,
Xtensa GCC `esp-14.2.0_20251107`, an existing IDF Python environment, CMake and Ninja.
Use the SDK's `idf_tools.py install xtensa-esp-elf` with a selected process-local
`IDF_TOOLS_PATH` if the compiler is missing. Do not run device install/flash commands.

```powershell
python examples/esp32s3-idf-flash/build.py --idf-path '<idf-source>' --python '<idf-python.exe>' --tools-path '<idf-tools-root>' --output '<new-fixture>' --variant normal --build-tag a
```

The fixture selects a minimal component build, dual-core FreeRTOS, UART0 console,
no PSRAM/radio/USB, no task/interrupt watchdog, and a default single factory table.
BUILD binds its sources, shared build script, SDK commit, target, variant and tag;
the manifest also binds actual image/ELF/config files. Compiler/SDK build logs are
separate evidence, not a promise of reproducible firmware binaries. `hang`,
`panic` and `missing-core1` variants support negative acceptance.

## Fixed execution

```powershell
sim-runner plan --backend espressif-qemu-xtensa --profile esp32s3-idf-flash-v1 --runtime-lock '<lock>' --runtime-sha256 '<lock-sha256>' --firmware '<fixture>/flash.bin' --firmware-sha256 '<flash-sha256>' --ready-line 'SIM_IDF_S3_READY v1' --build-line 'SIM_IDF_S3_BUILD <build-id>' --heartbeat-prefix SIM_IDF_S3_HEARTBEAT --output '<new-plan.json>'
sim-runner inspect-plan --plan '<plan.json>' --sha256 '<plan-sha256>'
sim-runner run --plan '<plan.json>' --sha256 '<plan-sha256>' --output-dir '<new-run-directory>'
sim-runner inspect-report --report '<run-directory>/report.json' --sha256 '<report-sha256>'
```

Planning hashes files only. Run always stages fresh private Flash/eFuse copies
and the pinned `esp32s3_rev0_rom.bin`; it does not share writable state between
runs. eFuse initial bytes follow IDF's S3 revision 0.3 recipe: 1024 bytes,
byte38=12, otherwise zero. The work eFuse must remain unchanged for success.
The fixed `-m 32M` follows the IDF QEMU machine recipe; the fixture does not
enable or validate PSRAM. Timer watchdogs are disabled in the model as in IDF's
recipe, so success is not watchdog validation.

The fixed command disables default devices, host display/network/monitor and
user configuration; it opens only a private UART file. No GDB/QMP server,
semihosting, USB or physical serial bridge, arbitrary argv or scripts are exposed.
Windows starts the owned process suspended, assigns its kill-on-close Job and
then resumes it. Existing host timeout, output and memory limits apply.

The C3 `icount` setting is **not used** for this Xtensa SMP profile: the local
comparison stalled after second-stage boot with it, while the selected command
reached both application cores. `virtual_time.domain=qemu_device_clock_unmeasured`,
`elapsed_ns=null`, `budget_enforced=false`; `--virtual-ms` is rejected. Host
deadlines, SDK UART tick text and byte ordering are not measured model duration,
deterministic timing, per-byte timestamps or physical timing.

Success requires the S3 ROM marker, one IDF 5.5.2 second-stage boot, one factory
app load, ordered exact `SIM_IDF_S3_CORE0 v1` / `SIM_IDF_S3_CORE1 v1` before READY,
then exact READY/BUILD and consecutive heartbeats from1 (minimum3). The fixture
emits5. Panic, abort, error log, repeated boot or incomplete UART tails fail.
Core markers validate this fixture's scheduled tasks; they do not prove complete
SMP, memory-ordering, CPU or TIE instruction coverage.

## Reviewed limitations

The following **exact** backend diagnostics are accepted only within the shown
per-run bounds and retained with message/count/meaning in `model_diagnostics`:

| Diagnostic | Maximum | Limitation |
| --- | ---: | --- |
| `M25P80: Read id (command 0x90/0xAB) is not supported by device` | 1 | Legacy Flash ID unsupported |
| `M25P80: Unknown cmd 5a` | 1 | Flash SFDP unsupported |
| `M25P80: Unknown cmd 7a` | 1 | Flash resume unsupported |
| `Invalid read at addr 0x10200C, size 4, region 'er', reason: rejected` | 3 | Xtensa DSRSET debugger-presence query unmodeled |

The fixed QEMU `m25p80.c` implements those Flash limitations. Its Xtensa CPU
initializes a default external-register region without handlers; IDF defines
DSRSET=0x10200C and reads it in `xt_utils_dbgr_is_attached`. This exact debug query
does not validate debugger attachment. It is not an allowlist for other invalid
addresses, writes or read widths. Any unlisted/excess diagnostic fails; C3 PMP
messages are not accepted on S3. Models can also contain silent stubs.

Successful runs with these messages are `passed_with_model_limits`;
`model_coverage_complete=false`, `security_features_verified=false` and all
physical-evidence flags remain false. Flash identification/SFDP/suspend/resume,
debug attachment, secure boot/encryption, OTA, PSRAM, RF, USB, timing and complete
peripheral behavior are outside acceptance. Use another reviewed model or a
separately scoped physical test when those behaviors matter.

Saved reports use distinct `qemu-s3-flash-plan.v1` / `qemu-s3-flash-report.v1`
schemas. Inspect verifies movable saved artifact hashes, S3 identity/layout,
fixed model/argv, private storage, recorded assertions and successful cleanup.
It starts no process, does not authenticate the producer and cannot promote
failed evidence to success. Keep raw UART/stdout/stderr, input/work Flash,
initial/work eFuse, ROM, plan/lock/model, SDK manifest/logs and failed attempts.

`scripts/accept_s3.py` uses an installed CLI, explicit runtime/fixtures/C3
negative inputs, six actual backend runs and offline evidence rejection checks.
Older HIL0.5 / Trace0.5 / Toolkit0.28 still pin sim-runner0.4 and do not accept
this new profile; cross-component integration is the next change.

Sources: [IDF5.5.2 QEMU guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.2/esp32s3/api-guides/tools/qemu.html),
[IDF fixed QEMU target recipes](https://github.com/espressif/esp-idf/blob/30aaf64524299d3bde422ca9a2848090d1bc5d0f/tools/idf_py_actions/qemu_ext.py),
[official runtime release](https://github.com/espressif/qemu/releases/tag/esp-develop-9.2.2-20260417),
[fixed S3 machine](https://github.com/espressif/qemu/blob/40edccac415693c5130f91c01d84176ae6008566/hw/xtensa/esp32s3.c),
[Flash model](https://github.com/espressif/qemu/blob/40edccac415693c5130f91c01d84176ae6008566/hw/block/m25p80.c),
[external-register region](https://github.com/espressif/qemu/blob/40edccac415693c5130f91c01d84176ae6008566/target/xtensa/cpu.c),
[IDF debugger query](https://github.com/espressif/esp-idf/blob/30aaf64524299d3bde422ca9a2848090d1bc5d0f/components/xtensa/include/xt_utils.h).
