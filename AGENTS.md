# sim-runner

Use Python 3.11+ with no runtime dependencies. Keep simulation evidence distinct
from physical board acceptance. This project never discovers or opens hardware.
The initial backend is a fixed, audited Renode nRF52840 UART-only profile; do not
accept arbitrary monitor scripts, network resources or host bridges as inputs.
Run pytest and Ruff after behavior changes. Keep runtime downloads, virtualenvs,
builds and acceptance evidence outside Git. No automatic publication.
