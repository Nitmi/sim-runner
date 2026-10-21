# nrf52840-uart-v1 coverage

For the separate input/interrupt/output scenario, use the explicitly selected
[GPIO/TIMER0 profile](nrf52840-gpio-timer.md). This default UART profile remains
unchanged and does not gain those peripherals implicitly.

This fixed profile is deliberately smaller than Renode's upstream nRF52840
platform. It instantiates upstream built-in models rather than inventing MMIO
stubs, and has no upstream platform includes or `ApplySVD` URL. Upstream model
reference: [Renode v1.17.0 nrf52840.repl](https://github.com/renode/renode/blob/v1.17.0/platforms/cpus/nrf52840.repl).

| Region | Purpose |
| --- | --- |
| 0x00000000–0x000FFFFF | Mapped memory for the raw firmware image, initially zero |
| 0x20000000–0x2003FFFF | Mapped RAM, initially zero |
| 0xE000E000 | Renode NVIC and Cortex-M4 system peripheral model |
| 0x40002000 | Renode NRF52840_UART, connected to NVIC interrupt 2 |

The fixture disables interrupts, uses the vector table at zero, only reads flash/
RAM and UART registers, and writes UART initialization/TX registers. UART accesses
are STARTTX +0x008, TXDRDY +0x11C, ENABLE +0x500, TXD +0x51C,
BAUDRATE +0x524, CONFIG +0x56C. The CPU performance setting is 100 MIPS,
not a statement about physical nRF52840 timing. The model explicitly uses
`easyDMA: false` for legacy UART. Unlike the physical DK fixture it makes no
PSEL pin-routing writes: Renode tags those fields but does not model their
electrical effect, and nonzero writes generate warnings. UARTE/EasyDMA firmware
needs a separate model/profile and is not covered by this candidate.

There is no clock, protection-register, flash-controller, RTC/timer, GPIO,
radio/BLE, UICR, electrical pin routing or analog model in this profile. The
fixture does not require initialized `.data`/`.bss` sections. General firmware
with different startup requirements may need a different reset/startup sequence.
The current raw image input starts at zero, includes its vector table and fits
within 1 MiB. HEX/ELF symbol loading is intentionally outside this first profile.

An optional `--unmapped-write` fixture writes 1 to 0x40000000 before the UART
protocol. Its UART may still look correct; the backend warning must make the
run fail. This tests that a successful-looking protocol does not hide missing
peripheral coverage. Logs are evidence of accesses observed during the bounded
execution, not proof that every possible future code path uses supported MMIO.

Outputs retain byte ordering but not per-byte virtual timestamps. We measure
repeatability by comparing UART hashes from fresh fixed-input executions. Host
UTC time, virtual elapsed time and physical time are separate domains. No claim
is made about general deterministic scheduling, physical UART baud timing,
electrical behavior, RF, power or temperature.
