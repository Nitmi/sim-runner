from .common import require


def check_logs(stdout: bytes, stderr: bytes):
    require(
        b"SIM_RUNNER_COMPLETED_V1" in stdout,
        "script completion marker missing",
        "BACKEND_FAILED",
    )
    combined = (stdout + stderr).lower()
    for text in (
        b"[warning]",
        b"[error]",
        b"unhandled",
        b"unmapped",
        b"exception",
        b"could not",
        b"there was an error",
    ):
        require(
            text not in combined,
            "backend warning/error or unsupported access",
            "MODEL_COVERAGE_FAILED",
        )
