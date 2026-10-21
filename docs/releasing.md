# Source-bound releases

Version [0.5.0](https://github.com/Nitmi/sim-runner/releases/tag/v0.5.0) is
published with verified build provenance for all four assets. The source tag
binds commit `b6e463df30c73e6cf5d81597bfda0c7af5c02baf`. The workflows prepare
artifacts and provenance; GitHub Release creation remains a separate action.

## Local preparation

Use Python 3.11+ and uv 0.11.7. The committed uv.lock fixes the development
tools. Runtime installation needs no dependencies. Keep build output outside
the checkout and commit source changes before a release build:

```powershell
uv sync --locked --extra dev
uv run --locked pytest -ra
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/release.py build --output-dir D:/work/sim-release-a
uv run --locked python scripts/release.py build --output-dir D:/work/sim-release-b
uv run --locked python scripts/release.py verify D:/work/sim-release-a --source-revision <commit>
uv run --locked python scripts/package_smoke.py --dist-dir D:/work/sim-release-a --output-dir D:/work/sim-package-smoke
```

Each build produces exactly a wheel, source archive, release-manifest.json and
SHA256SUMS. The verifier reads bounded archives in memory, checks wheel RECORD,
the 20 fixed runtime/model files, entry points, package identity and absence of
runtime dependencies. It compares wheel code and models with the source archive.
The builder additionally compares every source archive member with the clean
Git commit. All four output files must match between builds.

The smoke helper creates a fresh environment, installs only the exact wheel with
--no-index/--no-deps, verifies installed files and the CLI launcher, and exercises
offline doctor/plan/inspect plus hash and input-drift rejection with synthetic
files. An audit hook prohibits subprocess creation inside those offline CLI
calls. It never executes the synthetic runtime or starts Renode/QEMU.

## CI and provenance

CI runs Python 3.11/3.13 on Windows and Linux. Windows runs real process/Job
lifecycle tests. Linux tests portable offline behavior and skips Windows-only
execution tests; it does not establish Linux emulator execution support.
Neither CI nor the package smoke replaces the separately archived actual
Renode/QEMU acceptance runs.

The tag workflow calls CI, then requires refs/tags/v<project-version> to resolve
to both HEAD and the triggering commit. It builds twice on a GitHub-hosted
Windows runner, compares all four assets, smoke-installs the exact wheel and
uses actions/attest. External Actions are pinned to commit SHAs. The workflow
has no contents-write or PyPI publishing permission. Tag creation and release
publication are separate user-authorized actions.

For a subsequent release, push the reviewed source to Nitmi/sim-runner first.
Only after CI passes, create an immutable tag matching the new version. Verify successful tag
provenance and download the workflow's exact artifacts before creating a
GitHub Release. Verification should constrain repository, source ref, source
digest, signer workflow/digest and GitHub-hosted runners, for example:

```powershell
gh attestation verify embedded_sim_runner-0.5.0-py3-none-any.whl --repo Nitmi/sim-runner --source-ref refs/tags/v0.5.0 --source-digest <commit> --signer-workflow Nitmi/sim-runner/.github/workflows/python-release.yml --signer-digest <commit> --deny-self-hosted-runners
```

Repeat verification for the source archive, manifest and checksum file. Local
hash checks do not prove publisher authenticity. Cold-install the downloaded
Release wheel and run the package smoke plus the bounded actual-backend
acceptance profiles before updating downstream package pins. Runtime archives,
compiler toolchains, firmware images and local evidence are separate inputs;
none is bundled in the Python release.
