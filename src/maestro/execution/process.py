"""Runner process identity and process-group termination.

A PID alone can be reused; the identity adds the kernel start time and boot id,
so restart reconciliation only signals the process group it actually started.
"""

import contextlib
import os
import signal
from pathlib import Path


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _start_ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    if fields[0] == "Z":
        return None
    return fields[19]


def identity(pid: int) -> dict:
    return {"pid": pid, "pgid": pid, "start_ticks": _start_ticks(pid), "boot_id": _boot_id()}


def matches(recorded: dict | None) -> bool:
    """True only if the recorded leader is the same live process (same boot, same start)."""
    if not recorded or not recorded.get("pid") or not recorded.get("start_ticks"):
        return False
    if recorded.get("boot_id") != _boot_id():
        return False
    return _start_ticks(recorded["pid"]) == recorded["start_ticks"]


def signal_group(pgid: int, sig: int) -> bool:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)
        return True
    return False


def kill_recorded(recorded: dict | None) -> bool:
    """SIGKILL a leftover Runner group from an earlier service process, if it is still ours."""
    if not matches(recorded):
        return False
    return signal_group(recorded["pgid"], signal.SIGKILL)
