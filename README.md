# sim-runner

Local CLI for bounded firmware simulation and saved UART evidence. The 0.4.0
local candidate supports Windows x64 with four fixed profiles: Renode 1.17.0 nRF52840
UART and [GPIO/TIMER0 IRQ](docs/nrf52840-gpio-timer.md), Espressif QEMU ESP32-C3
RAM/UART and actual ESP-IDF 5.5.2 Flash boot.
None enumerates physical devices. The new
[Flash boot profile](docs/esp32c3-idf-flash.md) requires explicit selection and
retains reviewed QEMU limitations as `passed_with_model_limits`; it does not
establish complete chip, PMP, Flash command, security or physical compatibility.
The following Renode commands remain compatible; regenerate older plans with
the current runner because plans bind the runner version.

For QEMU setup, executable ELF constraints and reproducible smoke commands, see
[ESP32-C3 profile](docs/esp32c3-qemu.md). It is direct RAM ELF execution, not an
ESP-IDF flash-boot or ESP32-S3 profile. Use `--backend espressif-qemu` on doctor
and plan; run and inspectors route from the hash-bound saved schema.

The default Renode UART model includes Cortex-M4/NVIC, 1 MiB mapped flash, 256 KiB RAM and UART0.
Flash and RAM start zero-filled in each new process; the raw firmware image is
copied to address zero. This is not Nordic erased-flash behavior. There is no
SoftDevice, radio, GPIO, timer, UICR, flash-controller or board electrical model.
The explicitly selected GPIO/TIMER0 profile adds only its documented digital
subset, fixed input schedule and output loopback. Its 16 IRQ samples and four
model snapshots do not establish complete model coverage or physical timing.
Warnings, errors and unhandled MMIO fail acceptance. UART output is byte-order
evidence, not an electrical baud-rate or pin-routing measurement.

## Setup

Use the official Renode **v1.17.0 Windows portable ZIP**, whose GitHub release
SHA-256 is `18bcf145422039e8702c3e5f9864c7787561bfd5deb7a3ee6e168ecf19e2b9db`.
Extract to an absolute tools directory. `scripts/pin_runtime.py` verifies every
extracted file against that archive before producing a runtime lock. No driver,
system feature or persistent PATH changes are needed. Renode includes third
party licenses; keep the extracted distribution intact.

```powershell
python scripts/pin_runtime.py --archive D:/downloads/renode.zip --root D:/tools/renode --output D:/work/renode-lock.json
pip install .
sim-runner doctor --runtime-lock D:/work/renode-lock.json --runtime-sha256 <lock-sha256>
```

Doctor and plan/inspect hash files only; they never launch the backend.
Treat the pinned runtime and its dependencies as trusted host software. This
runner and the emulator are not a sandbox for hostile firmware/runtime files.

## Run a fixture

`examples/nrf52840-uart/build.py --output <new-directory>` uses an installed Rust
`thumbv7em-none-eabihf` target and LLVM tools to produce raw firmware, ELF and a
source/compiler-bound BUILD ID. Runtime users may instead provide their own raw
image with a Cortex-M vector table and code supported by this restricted model.
The example differs from the historical physical-DK HIL fixture.

```powershell
sim-runner plan --runtime-lock D:/work/renode-lock.json --runtime-sha256 <lock-sha256> --firmware D:/work/build/firmware.bin --firmware-sha256 <image-sha256> --ready-line 'SIM_NRF52840_READY v1' --build-line 'SIM_NRF52840_BUILD <build-id>' --heartbeat-prefix SIM_NRF52840_HEARTBEAT --output D:/work/plan.json
sim-runner inspect-plan --plan D:/work/plan.json --sha256 <plan-sha256>
sim-runner run --plan D:/work/plan.json --sha256 <plan-sha256> --output-dir D:/work/run-001
sim-runner inspect-report --report D:/work/run-001/report.json --sha256 <report-sha256>
```

No interactive confirmation is requested. A plan binds exact firmware/runtime/
model hashes and bounded settings. Each run requires a fresh directory, rechecks
inputs, stages private firmware/model/script/config and starts one headless
Renode process. No monitor TCP, Robot server or physical bridge is configured.

Assertions require READY and exact BUILD as the first two complete lines,
followed only by at least three HEARTBEAT lines with eight uppercase hexadecimal
digits, starting at 1 and incrementing without gaps. A partial final line fails.
The default virtual budget is 250 ms, host timeout 30 s, each output file 1 MiB
and the Windows Job memory cap 1024 MiB. Timeout, cancellation or output overflow
terminates the owned process tree. Windows Job limits contain lifecycle/resources;
they do not restrict filesystem/network permissions. The fixed model and script
have no downloaded resources, peripherals bridged to the host or arbitrary user
monitor commands. Runtime scripts and native libraries remain trusted inputs.
The child uses Renode's CI mode to disable shared cache/config writes and a
private config/temp directory. Renode can still initialize its per-user directory;
this is not a guarantee that a trusted runtime cannot access other host files.

Stdout/stderr capture is capped at the stated byte count. The UART file is
monitored every 20 ms; it can briefly exceed its threshold before termination.
A failed/oversized capture is preserved and cannot receive a passing result.

## Evidence

`report.json` records process status, cleanup, input hashes, UART assertions and
artifact hashes; raw UART/stdout/stderr and the exact script/model/image remain
in the run directory, including on a failed run. Renode may return exit 0 after
monitor errors, so the runner also checks logs, completion and UART assertions.
`inspect-report` rechecks the saved hashes and successful-run assertions without
the original firmware/runtime paths; a copied evidence directory remains usable.
This is an integrity check, not a signature or independent proof of origin.
Reports always say `execution_kind=simulation`, `hardware_access=false`, and
`physical_board_verified=false`. UTC timestamps describe host recording;
`virtual_ms` belongs to the emulator domain. Per-byte virtual timestamps are not
available in this first profile. A virtual pass never proves physical firmware
identity, peripheral timing, power, RF, temperature or actual board operation.

## Development

Run pytest and Ruff in an isolated Python environment. Unit/lifecycle tests use
explicit synthetic fixtures; `scripts/accept_local.py` runs actual pinned Renode
and archives separate positive/negative execution evidence. `scripts/accept_qemu.py`
does the same with the actual Espressif QEMU backend. No hardware is used.

Upstream documentation: [Renode installation](https://github.com/renode/renode),
[UART file backend](https://renode.readthedocs.io/en/latest/host-integration/uart.html),
[headless modes](https://renode.readthedocs.io/en/latest/basic/running.html).
