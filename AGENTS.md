# sim-runner

Use Python 3.11+ with no runtime dependencies. Keep simulation evidence distinct
from physical board acceptance. This project never discovers or opens hardware.
Use only fixed, audited profiles: Renode nRF52840 UART / GPIO-TIMER0 or Espressif QEMU
ESP32-C3 RAM/UART or the bounded ESP-IDF C3/S3 Flash/UART profiles. S3 uses a separate,
fixed Xtensa runtime, ROM and chip identity. Do not accept arbitrary monitor scripts, emulator arguments,
network resources or host bridges as inputs. Do not equate emulator support
with verified SDK boot, radio, peripheral completeness or physical timing.
Flash profile diagnostics must retain fixed, source-reviewed model limitations
in a passed_with_model_limits result; any unlisted or excessive diagnostic fails.
Run pytest and Ruff after behavior changes. Keep runtime downloads, virtualenvs,
builds and acceptance evidence outside Git. No automatic publication.
