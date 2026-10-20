"""Windows lifecycle fixture: an exclusively opened output file, no hardware."""

import ctypes
import sys
import time
from ctypes import wintypes as w

api = ctypes.WinDLL("kernel32", use_last_error=True)
api.CreateFileW.argtypes = [
    w.LPCWSTR,
    w.DWORD,
    w.DWORD,
    ctypes.c_void_p,
    w.DWORD,
    w.DWORD,
    w.HANDLE,
]
api.CreateFileW.restype = w.HANDLE
api.WriteFile.argtypes = [
    w.HANDLE,
    ctypes.c_void_p,
    w.DWORD,
    ctypes.POINTER(w.DWORD),
    ctypes.c_void_p,
]
api.WriteFile.restype = w.BOOL
api.FlushFileBuffers.argtypes = [w.HANDLE]
api.FlushFileBuffers.restype = w.BOOL
handle = api.CreateFileW(sys.argv[1], 0x40000000, 0, None, 2, 0x80, None)
if handle == ctypes.c_void_p(-1).value:
    raise ctypes.WinError(ctypes.get_last_error())
data = ctypes.create_string_buffer(b"X" * 65536)
count = w.DWORD()
if not api.WriteFile(handle, data, 65536, ctypes.byref(count), None):
    raise ctypes.WinError(ctypes.get_last_error())
if not api.FlushFileBuffers(handle):
    raise ctypes.WinError(ctypes.get_last_error())
print("EXCLUSIVE_WRITER_READY", flush=True)
time.sleep(60)
