# ESP32-C3 RAM/UART profile

`esp32c3-ram-uart-v1` runs actual RV32IMC machine code in Espressif QEMU
`esp-develop-9.2.2-20260417`, using its fixed `esp32c3` machine. This candidate
validates direct ELF startup, RAM loads, UART0 TX and guest software reset.
Upstream implements additional devices; their existence is not acceptance of
those devices here. ESP32/ESP32-S3, ESP-IDF/Zephyr flash boot, SPI flash, eFuses,
radio/Wi-Fi/BLE, USB, GPIO and electrical/baud/timing behavior are not verified.

## Runtime setup

Download the official [Windows RISC-V archive](https://github.com/espressif/qemu/releases/download/esp-develop-9.2.2-20260417/qemu-riscv32-softmmu-esp_develop_9.2.2_20260417-x86_64-w64-mingw32.tar.xz).
Its size is 38,496,020 bytes and SHA-256 is
`697aa4800a1f52be0b1693b30e22a684f7ea93c46c489e619384cae7b0e9b87b`,
as published in [Espressif tools.json](https://github.com/espressif/esp-idf/blob/master/tools/tools.json).
`pin_qemu.py` verifies the complete archive, rejects links and unsafe paths,
extracts to a fresh directory, verifies extracted bytes and creates a lock of
all 11 distribution files. No runtime is bundled in the runner wheel. QEMU and
its dependencies remain third party software under their upstream licenses.

```powershell
python scripts/pin_qemu.py --archive D:/downloads/qemu.tar.xz --destination D:/tools/qemu-c3 --output D:/work/qemu-lock.json
sim-runner doctor --backend espressif-qemu --runtime-lock D:/work/qemu-lock.json --runtime-sha256 <lock-sha256>
```

Doctor is static: exact runtime version/executable, complete inventory and
file hashes. It does not start QEMU or discover devices. No admin privileges,
driver, ESP-IDF, Docker, WSL, Windows feature or persistent PATH change is needed.
The native runtime is trusted software; this is not a security sandbox.

## Firmware and execution

Install the runner into a dedicated Python 3.11+ environment. The fixture builder
uses an already installed Rust `riscv32imc-unknown-none-elf` target. It downloads
nothing, records compiler/source hashes, sets a BUILD ID and emits an ELF.

```powershell
python examples/esp32c3-uart/build.py --output D:/work/c3-fixture
sim-runner plan --backend espressif-qemu --runtime-lock D:/work/qemu-lock.json --runtime-sha256 <lock-sha256> --firmware D:/work/c3-fixture/firmware.elf --firmware-sha256 <elf-sha256> --ready-line 'SIM_ESP32C3_READY v1' --build-line 'SIM_ESP32C3_BUILD <build-id>' --heartbeat-prefix SIM_ESP32C3_HEARTBEAT --output D:/work/c3-plan.json
sim-runner inspect-plan --plan D:/work/c3-plan.json --sha256 <plan-sha256>
sim-runner run --plan D:/work/c3-plan.json --sha256 <plan-sha256> --output-dir 'D:/work/c3 run 001'
sim-runner inspect-report --report 'D:/work/c3 run 001/report.json' --sha256 <report-sha256>
```

No confirmation is requested per run. Hashes bind inputs, not user approvals.
The output directory must be new. Plans bind runtime, ELF, profile, markers and
budgets, rejecting unknown fields and changed inputs before starting QEMU.

Only ELF32 little-endian RISC-V ET_EXEC, soft-float RV32I/RVC ABI is accepted.
PT_LOAD virtual/physical addresses must match; all loaded data/BSS must fit
IRAM `0x4037C000–0x403DFFFF` or DRAM `0x3FC80000–0x3FCDFFFF`. These ranges alias:
DRAM starts at IRAM offset `0x4000`, so overlapping alias segments are rejected.
The entry must lie in executable load data. Dynamic/interpreted files, ELF64,
other CPUs, flash-mapped segments and raw flash images fail offline. This checks
layout/ABI, not every instruction or future code path's RV32IMC compatibility.

The sample uses disjoint code and stack/data regions, initializes SP/mtvec/BSS,
prints READY and exact BUILD followed by five consecutive HEARTBEAT lines, then
writes `SW_SYS_RST` at RTC_CNTL `0x60008000`. Fixed `-no-reboot` converts the guest
reset to a native exit. There is no boot ROM or flash boot path: QEMU loads the
ELF directly and uses its entry as reset vector. RAM starts fresh in each
process. No flash/eFuse backing file or state is reused. The sample is for this
simulation profile, not a claim that the ELF has passed on physical hardware.

The completion contract is exact UART assertions **and** unforced native exit
zero. It does not independently prove why QEMU exited. General infinite-loop
firmware needs a future profile/control scheme; correct UART followed by a hang
still fails this profile. Wrong BUILD or any incomplete/gapped UART also fails.

## Limits and failure evidence

The fixed command uses TCG single-threaded with a 16 MiB translation cache,
`-icount shift=0,align=off,sleep=on`, `-no-user-config`, `-nodefaults`, display and
monitor disabled, `-nic none`, and only a private UART output file. There are no
QMP/GDB sockets, user-supplied arguments/scripts, semihosting, USB pass-through
or host serial bridges. No host input is delivered to UART. This controls the
runner's configuration; a trusted native emulator still has host OS permissions.

Default host deadline is 30 s, outputs 1 MiB each, Windows Job memory 1024 MiB.
The host deadline is not a virtual-time budget. QEMU's icount clock is named in
the report, with `elapsed_ns=null`, `budget_enforced=false`; per-byte virtual
timestamps are unavailable. `--virtual-ms` is rejected for QEMU. Identical UART
hashes from fresh fixed-input runs establish repeatability of that output only.

Stdout/stderr have a strict startup-line allowlist. `-d guest_errors,unimp`
diagnostics, warnings, invalid/unimplemented accesses, nonzero exits, timeout,
capture limits, input drift or incomplete process cleanup prevent passing.
Upstream has silent peripheral stubs: absence of diagnostics cannot establish
complete MMIO coverage. Raw file output is monitored every 20 ms and can briefly
overshoot its cap before termination, as with the Renode backend.

`--variant unmapped-write` builds a negative fixture that writes `0x70000000`.
Its store-fault handler skips that 16/32-bit instruction so UART and native exit
can still look successful; the saved QEMU diagnostic must make acceptance fail.
`--variant hang` emits valid UART then loops, testing deadline/tree cleanup.
`scripts/accept_qemu.py` runs two fresh positives, wrong BUILD, unmapped write,
hang, movable evidence, hash tampering and rejection before backend start using
the installed CLI. Synthetic unit transcripts remain separately identified.

Failed runs retain ELF/profile/plan/lock, raw stdout/stderr/UART, process cleanup
and hashes. `inspect-report` works on moved evidence without launching QEMU or
requiring the original runtime path. It checks integrity, not a digital signature
or independent proof of execution. Every report remains simulation evidence.

Primary sources: [Espressif QEMU guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-guides/tools/qemu.html),
[pinned C3 loader/memory map](https://github.com/espressif/qemu/blob/esp-develop-9.2.2-20260417/hw/riscv/esp32c3.c),
[RTC reset model](https://github.com/espressif/qemu/blob/esp-develop-9.2.2-20260417/hw/misc/esp32c3_rtc_cntl.c),
[QEMU invocation](https://www.qemu.org/docs/master/system/invocation.html).
