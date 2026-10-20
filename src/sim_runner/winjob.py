"""Windows process containment. Assignment precedes execution of user code."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as w

from .common import SimError


class BasicLimit(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", w.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_limit", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class Counters(ctypes.Structure):
    _fields_ = [
        (key, ctypes.c_ulonglong)
        for key in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimit),
        ("io", Counters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")
    ]
    _fields_ += [(name, w.DWORD) for name in ("page_faults", "total", "active", "terminated")]


class ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD),
        ("usage", w.DWORD),
        ("thread_id", w.DWORD),
        ("owner_pid", w.DWORD),
        ("base_priority", w.LONG),
        ("delta_priority", w.LONG),
        ("flags", w.DWORD),
    ]


class Job:
    def __init__(self, memory_mib: int):
        if os.name != "nt":
            raise SimError("UNSUPPORTED_PLATFORM", "managed execution currently requires Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": (
                [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p],
                w.BOOL,
            ),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            self.fail("CreateJobObject")
        try:
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY; no breakaway.
            limits.job_memory = memory_mib * 1024 * 1024
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                self.fail("SetInformationJobObject")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def fail(action):
        raise SimError(
            "HOST_IO_ERROR", f"{action} failed (Windows error {ctypes.get_last_error()})"
        )

    def assign_and_resume(self, pid: int) -> None:
        process = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            self.fail("OpenProcess for managed child")
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                self.fail("AssignProcessToJobObject")
        finally:
            self.api.CloseHandle(process)
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            self.fail("CreateToolhelp32Snapshot")
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = []
            present = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.owner_pid == pid:
                    found.append(entry.thread_id)
                entry.size = ctypes.sizeof(entry)
                present = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        if len(found) != 1:
            raise SimError("HOST_IO_ERROR", "suspended child must have exactly one primary thread")
        thread = self.api.OpenThread(2, False, found[0])  # THREAD_SUSPEND_RESUME
        if not thread:
            self.fail("OpenThread")
        try:
            if self.api.ResumeThread(thread) != 1:
                self.fail("ResumeThread expected one suspend count")
        finally:
            self.api.CloseHandle(thread)

    def active(self) -> int:
        value = Accounting()
        if not self.api.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None
        ):
            self.fail("QueryInformationJobObject")
        return value.active

    def terminate(self) -> None:
        if not self.api.TerminateJobObject(self.handle, 1):
            self.fail("TerminateJobObject")

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                self.fail("CloseHandle for managed Job")
