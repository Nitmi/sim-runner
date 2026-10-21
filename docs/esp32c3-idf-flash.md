# ESP32-C3 ESP-IDF Flash boot (0.3.0 local candidate)

`esp32c3-idf-flash-v1` boots an actual ESP-IDF 5.5.2 image through the pinned
Espressif QEMU ROM, second-stage bootloader, default partition table and factory
application. Select it explicitly; the existing RAM ELF and Renode profiles keep
their formats and commands. This native candidate has not been published.

## Coverage and fixed inputs

- Same audited QEMU Windows distribution and complete runtime lock as the
  [RAM/UART profile](esp32c3-qemu.md): `esp-develop-9.2.2-20260417`.
- A complete **4 MiB DIO** merged image, bootloader at `0`, partition table at
  `0x8000`, nvs at `0x9000/0x6000`, phy_init at `0xF000/0x1000`, and one factory
  application at `0x10000/0x100000`. Partition flags must be zero. Initial data
  regions, unused Flash and padding outside images/table must be erased `0xFF`.
- Offline validation checks C3 image headers, segment bounds, XOR checksums,
  appended SHA-256, partition-table MD5 and fixed layout. It rejects other chips,
  sizes/layouts, encrypted/signed tails, OTA selections, populated data regions,
  truncation and drift. It does not prove every future instruction/MMIO access.
- Private `flash-input.bin` and writable `flash-work.bin` per run; the original
  image is never passed to QEMU. No storage is reused across runs.
- The private pinned ROM has SHA-256
  `0de1e65020e803bea0d7443dca149d61895e01fca3bb9c82d073234eebd73f99`.
  `-L .` selects that staged file; there is no `-kernel` direct entry shortcut.
- Fresh 1024-byte eFuse initial/work files: byte 38 is `0x0C`, all other bytes
  zero. This is the official IDF QEMU C3 revision-0.3 default. Passing runs must
  leave this eFuse state unchanged; no physical eFuse is accessed.
- The fixed QEMU timer watchdog disable follows the official IDF QEMU recipe.
  Thus watchdog functionality is not being tested. SDK fixture software watchdogs
  are disabled too. No secure boot, Flash encryption, OTA, RF, physical pin timing,
  production Flash behavior or complete peripheral coverage is claimed.

## Prepare the host and build the SDK fixture

Host setup is separate from runtime. Select trusted ESP-IDF **v5.5.2**, commit
`30aaf64524299d3bde422ca9a2848090d1bc5d0f`. Initialize the pinned `mbedtls`,
`micro-ecc`, `heap/tlsf` and `mqtt/esp-mqtt` submodules (the latter is required
by SDK configuration even though MQTT is not linked into this minimal fixture).
Install the SDK's official tools and Python requirements into dedicated
directories, verifying tools.json archive sizes/hashes. The tested tools are
GCC `esp-14.2.0_20251107`, CMake 3.30.2 and Ninja 1.12.1, with SDK Python 3.12.
No driver, physical port, WSL/Windows feature or persistent PATH change is needed.

```powershell
python examples/esp32c3-idf-flash/build.py `
  --idf-path D:/sdk/esp-idf-5.5.2 --tools-path D:/tools/idf-5.5.2 `
  --python D:/tools/idf-5.5.2/python_env/idf5.5_py3.12_env/Scripts/python.exe `
  --output D:/work/fixtures/normal-5
```

The builder requires a new output directory and the selected SDK/submodule Git
revisions. It temporarily selects tools/Python for its children, removes inherited
Python-home/path settings, disables the component manager and automatic global
submodule initialization, and runs only `idf.py build` plus `esptool merge_bin`.
It never executes `flash`, `write_flash`, monitor or device commands. SDK build
logs print suggested flashing commands; those are output data, not instructions.

The BUILD ID binds fixture source hashes, SDK commit, variant and optional
`--build-tag`. `fixture.json` records the resulting merged image, ELF, individual
bootloader/table/application, generated sdkconfig and flasher_args hashes.
The trusted compiler/SDK Python environment is not completely authenticated by
the BUILD ID. Preserve its setup metadata and logs separately. The fixture is a
simulation test, not evidence that a physical board runs that SDK artifact.

Variants are `normal` (five heartbeats then `esp_restart`), `hang` (same protocol,
then wait forever), and `panic` (same protocol, then abort). The latter two
exercise cleanup and full-capture failure detection. QEMU `-no-reboot` turns a
guest reset into native exit; continuous applications do not pass this profile
merely by printing READY before a timeout.

## Plan, run and inspect

```powershell
sim-runner plan --backend espressif-qemu --profile esp32c3-idf-flash-v1 `
  --runtime-lock '<qemu-runtime-lock.json>' --runtime-sha256 '<lock-sha256>' `
  --firmware '<merged-flash.bin>' --firmware-sha256 '<image-sha256>' `
  --ready-line 'SIM_IDF_C3_READY v1' --build-line 'SIM_IDF_C3_BUILD <build-id>' `
  --heartbeat-prefix SIM_IDF_C3_HEARTBEAT --output '<new-plan.json>'
sim-runner inspect-plan --plan '<plan.json>' --sha256 '<plan-sha256>'
sim-runner run --plan '<plan.json>' --sha256 '<plan-sha256>' --output-dir '<new-run-directory>'
sim-runner inspect-report --report '<run-directory>/report.json' --sha256 '<report-sha256>'
```

These native commands always emit JSON; do not add `--json`. Input hashes are
local bindings, with no manual confirmation per simulation run. Plans bind the
profile/model, ROM, eFuse initial state, complete merged image and runtime lock.
Plans bind runner versions: regenerate older plans with the new runner rather
than changing their version fields. Original older saved reports remain readable.

The fixed command retains disabled monitor/display/network, no host serial/USB,
no QMP/GDB/semihosting or user-supplied arguments. Host deadline, output limits,
Job memory cap and descendant cleanup are unchanged from RAM/UART. There is no
enforced/measured virtual-time budget or per-byte timestamp. The native runtime
remains trusted host software, not a security sandbox.

## Read the verdict and known limitations

Success requires an ordered single ROM/IDF-5.5.2 second-stage/factory-load/READY
boot chain; exact BUILD and at least three complete uppercase eight-hex-digit
heartbeats starting at 1; no gaps, partial tail, panic/abort or SDK error; native
unforced exit zero; unchanged inputs and eFuse; and verified host process cleanup.
The firmware's reason for native exit is not independently attested.

Actual SDK startup emits specific diagnostics in this QEMU build. The profile
does not suppress them or assert clean/full-chip compatibility:

| Exact diagnostic family | Bound per run | Interpretation |
| --- | --- | --- |
| `M25P80: Read id (command 0x90/0xAB) is not supported by device` | 1 | Legacy Flash ID probe unsupported |
| `M25P80: Unknown cmd 5a` | 1 | SFDP unsupported; not validated by this profile |
| `M25P80: Unknown cmd 7a` | 1 | Flash resume unsupported; not validated |
| `ignoring pmpcfg write - locked` | 256 | Writes to locked PMP configuration ignored |
| `ignoring pmpaddr write - pmpcfg + 1 locked` | 64 | Adjacent locked-PMP address writes ignored |
| `ignoring pmpaddr write - locked` | 64 | Locked-PMP address writes ignored |

Only these exact source-reviewed messages are recognized. Their counts and
meanings remain in `assertions.model_diagnostics`; **any unlisted message or
exceeded count fails**. A scoped successful run with any of them has
`ok=true,status=passed_with_model_limits`, never plain `passed`.
`model_coverage_complete=false` and `security_features_verified=false` remain
explicit. Do not use this profile to validate PMP protection, Flash identification,
SFDP/suspend/resume, security or physical behavior. Silent upstream stubs also
remain possible. A firmware task that requires those behaviors needs a different
validated model or physical test, not another allowlist entry.

`inspect-report` verifies movable saved artifact bytes, layout/bindings, private
storage sizes, recorded diagnostics, assertions and successful cleanup. It does
not execute QEMU, authenticate the producer or turn a saved failed run into a
pass. Its exit zero means integrity inspection succeeded; check `report_status`.
Keep raw UART/stdout/stderr, initial/work Flash and eFuse, ROM, plan/lock/model,
SDK build metadata, process results and failed attempts.

`scripts/accept_flash.py` accepts the installed executable, runtime lock, fixture
root (`normal-5`, `hang`, `panic`) and new output directory. It executes five
fresh models: two positives, wrong BUILD, post-protocol panic and host timeout;
then moved/tampered evidence and corrupt-image rejection. These are distinct from
synthetic unit tests. HIL 0.3.0, Trace 0.3.0 and Toolkit 0.26.0 currently pin/support
the older sim-runner 0.2.0 profiles; this new native schema is not silently passed
through those older integrations. That integration is a subsequent change.

Primary references: [IDF v5.5.2 QEMU recipe](https://github.com/espressif/esp-idf/blob/v5.5.2/tools/idf_py_actions/qemu_ext.py),
[pinned QEMU C3 machine/ROM/Flash loader](https://github.com/espressif/qemu/blob/esp-develop-9.2.2-20260417/hw/riscv/esp32c3.c),
[SPI diagnostics](https://github.com/espressif/qemu/blob/esp-develop-9.2.2-20260417/hw/block/m25p80.c),
[PMP semantics/diagnostics](https://github.com/espressif/qemu/blob/esp-develop-9.2.2-20260417/target/riscv/pmp.c),
[SDK image format](https://github.com/espressif/esp-idf/blob/v5.5.2/components/bootloader_support/include/esp_app_format.h).
