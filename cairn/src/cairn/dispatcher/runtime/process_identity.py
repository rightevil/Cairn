from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess


def process_identity(pid: int) -> str | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = w.HANDLE
        api.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        api.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = w.DWORD()
            times = [w.FILETIME() for _ in range(4)]
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            if not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            fields = stat.read_text().split(") ", 1)[1].split()
            return None if fields[0] == "Z" else fields[19]
        except (OSError, IndexError):
            return None
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return result.stdout.strip() or None


def stop_owned_process(pid: int, identity: str) -> bool:
    current = process_identity(pid)
    if current is None or current != identity:
        return True  # The recorded process is gone; never signal a recycled PID.
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    else:
        if os.getpgid(pid) != pid:
            return False
        os.killpg(pid, signal.SIGKILL)
    return process_identity(pid) != identity
