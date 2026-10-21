"""Small fixed-format ESP32-C3 factory-image validator, independent of esptool."""

import functools
import hashlib
import operator
import struct

from .common import require, sha

FLASH_SIZE = 4 * 1024 * 1024
PARTITIONS = [
    (1, 2, 0x9000, 0x6000, b"nvs"),
    (1, 1, 0xF000, 0x1000, b"phy_init"),
    (0, 0, 0x10000, 0x100000, b"factory"),
]


def image_info(data: bytes, start: int, limit: int, *, chip_id: int = 5) -> dict:
    header = data[start : start + 24]
    require(len(header) == 24 and header[0] == 0xE9, "missing ESP image header")
    require(
        1 <= header[1] <= 16 and header[2] == 2 and header[3] >> 4 == 2,
        "requires unsigned 4 MiB DIO image",
    )
    require(
        struct.unpack_from("<H", header, 12)[0] == chip_id and header[23] == 1,
        "requires matching chip image with appended SHA-256",
    )
    require(header[19:23] == bytes(4), "unsupported reserved image fields")
    position = start + 24
    checksum = 0xEF
    segments = []
    for _ in range(header[1]):
        require(position + 8 <= limit, "truncated segment header")
        address, size = struct.unpack_from("<II", data, position)
        position += 8
        require(size % 4 == 0 and position + size <= limit, "invalid image segment size")
        content = data[position : position + size]
        checksum = functools.reduce(operator.xor, content, checksum)
        segments.append({"address": address, "size": size, "sha256": sha(content)})
        position += size
    # ESP checksum occupies the final byte of the next 16-byte boundary.
    checksum_at = ((position - start) // 16 + 1) * 16 + start - 1
    end = checksum_at + 33
    require(
        end <= limit and data[position:checksum_at] == bytes(checksum_at - position),
        "invalid image padding",
    )
    require(data[checksum_at] == checksum, "ESP image checksum mismatch")
    require(
        data[checksum_at + 1 : end] == hashlib.sha256(data[start : checksum_at + 1]).digest(),
        "ESP image SHA-256 mismatch",
    )
    return {
        "offset": start,
        "bytes": end - start,
        "sha256": sha(data[start:end]),
        "entry": struct.unpack_from("<I", header, 4)[0],
        "segments": segments,
    }


def flash_info(data: bytes, *, chip_id: int = 5) -> dict:
    require(len(data) == FLASH_SIZE, "requires a complete 4 MiB merged Flash image")
    require(chip_id in (5, 9), "unsupported Flash chip")
    boot = image_info(data, 0, 0x8000, chip_id=chip_id)
    app = image_info(data, 0x10000, 0x110000, chip_id=chip_id)
    position = 0x8000
    table = []
    for typ, subtype, offset, size, label in PARTITIONS:
        entry = data[position : position + 32]
        expected = struct.pack("<HBBII16sI", 0x50AA, typ, subtype, offset, size, label, 0)
        require(entry == expected, "requires unencrypted single-factory default partition table")
        table.append(
            {
                "type": typ,
                "subtype": subtype,
                "offset": offset,
                "size": size,
                "label": label.decode(),
            }
        )
        position += 32
    md5_record = data[position : position + 32]
    require(
        md5_record[:16] == b"\xeb\xeb" + b"\xff" * 14
        and md5_record[16:] == hashlib.md5(data[0x8000:position], usedforsecurity=False).digest(),
        "partition table MD5 mismatch",
    )
    for start, end in (
        (boot["bytes"], 0x8000),
        (position + 32, 0x10000),
        (0x10000 + app["bytes"], FLASH_SIZE),
    ):
        require(
            data[start:end] == b"\xff" * (end - start),
            "non-erased data outside bootloader/table/application is unsupported",
        )
    return {
        "bytes": len(data),
        "flash_size": FLASH_SIZE,
        "bootloader": boot,
        "partition_table": {"offset": 0x8000, "entries": table},
        "application": app,
        "initial_data_partitions": "erased_ff",
        "security_features_verified": False,
    }
