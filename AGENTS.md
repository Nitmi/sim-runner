# sim-runner

Use Python 3.11+ with no runtime dependencies. Keep simulation evidence distinct
from physical board acceptance. This project never discovers or opens hardware.
Use only fixed, audited profiles: Renode nRF52840 UART or Espressif QEMU
ESP32-C3 RAM/UART. Do not accept arbitrary monitor scripts, emulator arguments,
network resources or host bridges as inputs. Do not equate emulator support
with verified SDK boot, radio, peripheral completeness or physical timing.
Run pytest and Ruff after behavior changes. Keep runtime downloads, virtualenvs,
builds and acceptance evidence outside Git. No automatic publication.
