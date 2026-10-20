from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

FLAGS = {
    "execution_kind": "simulation",
    "hardware_access": False,
    "physical_board_verified": False,
    "physical_firmware_identity_verified": False,
}


class SimError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition, message, code="INVALID_INPUT"):
    if not condition:
        raise SimError(code, message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: str) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value), "invalid SHA-256")
    return value.lower()


def payload(path: Path, limit: int) -> bytes:
    require(path.is_file() and not path.is_symlink(), f"not a regular file: {path}")
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        require(0 <= size <= limit, f"file exceeds {limit} bytes: {path}")
        value = stream.read(size + 1)
    require(len(value) == size, f"file changed while reading: {path}", "INPUT_CHANGED")
    return value


def bound(path: Path, expected: str, limit: int) -> bytes:
    value = payload(path, limit)
    require(sha(value) == digest(expected), f"SHA-256 mismatch: {path}", "INPUT_CHANGED")
    return value


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(data: bytes):
    return json.loads(data, object_pairs_hook=no_duplicates)


def write_json(path: Path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")


def file_record(path: Path) -> dict:
    value = payload(path, 512 * 1024 * 1024)
    return {"path": str(path.resolve()), "sha256": sha(value), "bytes": len(value)}
