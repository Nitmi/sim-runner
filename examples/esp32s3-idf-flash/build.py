"""Build the fixed S3 fixture using the same pinned SDK-only build flow."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    source = Path(__file__).resolve().parent
    builder = runpy.run_path(str(source.parent / "esp32c3-idf-flash/build.py"))
    builder["main"](source=source, target="esp32s3")
