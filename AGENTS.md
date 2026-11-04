# sim-runner

Use Python 3.11+ with no runtime dependencies. Keep simulation evidence distinct
from physical board acceptance. This project never discovers or opens hardware.
Use only fixed, audited profiles: Renode nRF52840 UART / GPIO-TIMER0 (including
explicit bounded model-fault controls) or fixed TWI0/TMP103 signed-temperature
reads with paused model snapshots and explicit frozen/offset faults.
The fixed legacy SPI0/LM74 profile adds signed Q4 two-byte frames, frozen values
and one monitor-byte frame-slip fault. It has no CS/electrical or clock-mode model;
the frame-slip control is a model Transmit call, not a physical bus glitch.
The fixed watchdog profile covers two reload channels, RUNSTATUS and model reset
with TIMER0 feeding or fixed timer-stop/LimitTimer-disable faults. It does not
cover CONFIG, sleep/halt, timeout IRQ, physical reset or protection guarantees.
The fixed RTC0 profile covers PRESCALER=31, CC0 compare interrupts and event
clearing, with TASKS_STOP/INTENCLR faults. It excludes LFCLK selection/startup,
TICK/overflow/TRIGOVRFLW/wrap, PPI and sleep/power/wakeup qualification.
The fixed EGU0 profile uses monitor TASKS_TRIGGER on 0/3/15 and observes event
latches/INTEN before and after guest IRQ service; faults mask channel 3 or route
one task to disabled sentinel channel 1. It excludes PPI, guest task-producer,
external routing, all-channel completeness, IRQ latency and physical timing.
The fixed GPIOTE input profile covers channels 0/1/2, P0.11 rising, P0.12 falling,
and P1.3 toggle edges with paused CONFIG/EVENTS_IN/INTEN/GPIO snapshots. Its two
faults mask channel 2 or change its polarity to falling. No TASKS_OUT/SET/CLR,
PORT/SENSE/LATCH/DETECTMODE, PPI, electrical behavior or physical timing is qualified.
The fixed PPI profile separately covers TIMER0 compare0 -> CH0 TEP/FORK ->
GPIOTE OUT0/1 and sentinel OUT2, with CHENCLR/fork-misroute faults. PPI defers
tasks to nearest synced state: never claim event-time alignment or precise
timing. Inactive RTC/RADIO models satisfy lazy fixed-channel subscriptions;
fixed channels remain disabled and no radio medium or bridge is created.
These sensor controls do not model electrical I2C, NACK/disconnect, conversion
delay, calibration or physical accuracy. Use
Renode 1.17 source semantics, not a newer model's registers. Espressif QEMU covers
ESP32-C3 RAM/UART or the bounded ESP-IDF C3/S3 Flash/UART profiles, or the
experimental fixed ESP32-C3 ESP-EMU 0.50.0 WASM/Node profile. The latter has no
browser, network or physical bridge; its UTF-8 UART strings and opaque built-in
ROM/eFuse defaults retain explicit model limitations. S3 uses a separate,
fixed Xtensa runtime, ROM and chip identity. Do not accept arbitrary monitor scripts, emulator arguments,
network resources or host bridges as inputs. Do not equate emulator support
with verified SDK boot, radio, peripheral completeness or physical timing.
Flash profile diagnostics must retain fixed, source-reviewed model limitations
in a passed_with_model_limits result; any unlisted or excessive diagnostic fails.
Run pytest and Ruff after behavior changes. Keep runtime downloads, virtualenvs,
builds and acceptance evidence outside Git. No automatic publication.
