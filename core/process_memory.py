"""
Process and container memory readings.

Linux only (``/proc`` and cgroup v2); every reader returns ``None`` where the
information is unavailable, so callers can log or skip without branching on
the platform.
"""

import os
from typing import Optional, Tuple

try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):
    _PAGE_SIZE = 4096

_CGROUP_DIR = "/sys/fs/cgroup"


def rss_bytes() -> Optional[int]:
    """Current resident set size of this process."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return None


def peak_rss_bytes() -> Optional[int]:
    """Highest resident set size this process has reached (VmHWM)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def cgroup_memory_bytes() -> Tuple[Optional[int], Optional[int]]:
    """(usage, limit) of this container's cgroup v2; limit is None when unlimited."""
    try:
        with open(f"{_CGROUP_DIR}/memory.current") as f:
            current = int(f.read())
        with open(f"{_CGROUP_DIR}/memory.max") as f:
            raw = f.read().strip()
    except (OSError, ValueError):
        return None, None
    return current, None if raw == "max" else int(raw)
