"""İşletim sistemine özgü süreç başlatma/durdurma adaptörleri.

Windows: CREATE_NEW_PROCESS_GROUP + Job Object (KILL_ON_JOB_CLOSE) → panel kapanınca
çocuk süreç sahipsiz kalmaz. Nazik durdurma CTRL_BREAK_EVENT ile yapılır.
POSIX: yeni oturum (process group) → SIGINT, SIGTERM, SIGKILL sırasıyla gruba gönderilir.
"""
from __future__ import annotations

import os
import signal
import subprocess

import psutil

IS_WINDOWS = os.name == "nt"


def popen_kwargs() -> dict:
    if IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


class _WindowsJob:
    """ctypes ile Job Object; pywin32 gerektirmez."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = self._k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject")

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k32.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")

    def assign(self, pid: int) -> None:
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        h = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if h:
            self._k32.AssignProcessToJobObject(self._handle, h)
            self._k32.CloseHandle(h)


_job: _WindowsJob | None = None


def attach_to_job(pid: int) -> None:
    global _job
    if not IS_WINDOWS:
        return
    try:
        if _job is None:
            _job = _WindowsJob()
        _job.assign(pid)
    except OSError:
        pass  # Job Object desteklenmiyorsa ağaç sonlandırma psutil ile yapılır


def request_graceful_stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_tree(pid: int, create_time: float | None, force: bool) -> None:
    """Süreç ağacını sonlandırır. PID yeniden kullanımına karşı create_time doğrulanır."""
    try:
        root = psutil.Process(pid)
        if create_time is not None and abs(root.create_time() - create_time) > 1.0:
            return  # bu PID artık bizim sürecimiz değil
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        return  # süreç doğrulanamıyor: PID yeniden kullanılmış olabilir, hiçbir şeye dokunma
    if not IS_WINDOWS:
        try:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for p in procs:
        try:
            p.kill() if force else p.terminate()
        except psutil.Error:
            pass
