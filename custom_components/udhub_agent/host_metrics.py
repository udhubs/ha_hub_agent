"""Collect host CPU / memory / disk for heartbeat (PRD §5.7A).

HAOS / Supervised: Supervisor /host/info (disk) + /core/stats (cpu/memory).
Container / no Supervisor: best-effort /proc; otherwise mark unsupported.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant

from .ssh_bypass import _supervisor_get, _supervisor_token

_LOGGER = logging.getLogger(__name__)


def _round(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _gb_from_bytes(raw: Any) -> float | None:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    # Supervisor disk_* is already GB; core memory_* is bytes
    if n > 10_000:
        return _round(n / (1024**3), 2)
    return _round(n, 2)


async def collect_host_metrics(hass: HomeAssistant) -> dict[str, Any]:
    """Return normalized host_metrics payload for agent.heartbeat."""
    if _supervisor_token():
        return await _from_supervisor(hass)
    return await hass.async_add_executor_job(_from_proc)


async def _from_supervisor(hass: HomeAssistant) -> dict[str, Any]:
    host = await _supervisor_get(hass, "/host/info")
    core_stats = await _supervisor_get(hass, "/core/stats")

    cpu = None
    mem_pct = None
    mem_used = None
    mem_total = None
    if isinstance(core_stats, dict):
        cpu = _round(core_stats.get("cpu_percent"))
        mem_pct = _round(core_stats.get("memory_percent"))
        usage = core_stats.get("memory_usage")
        limit = core_stats.get("memory_limit")
        mem_used = _gb_from_bytes(usage)
        mem_total = _gb_from_bytes(limit)
        if mem_pct is None and mem_used is not None and mem_total and mem_total > 0:
            mem_pct = _round((mem_used / mem_total) * 100)

    disk_pct = None
    disk_used = None
    disk_total = None
    if isinstance(host, dict):
        disk_used = _round(host.get("disk_used"), 2)
        disk_total = _round(host.get("disk_total"), 2)
        disk_free = _round(host.get("disk_free"), 2)
        if disk_total and disk_total > 0:
            if disk_used is not None:
                disk_pct = _round((disk_used / disk_total) * 100)
            elif disk_free is not None:
                disk_pct = _round(((disk_total - disk_free) / disk_total) * 100)
                disk_used = _round(disk_total - disk_free, 2)

    supported = any(v is not None for v in (cpu, mem_pct, disk_pct))
    return {
        "supported": supported,
        "source": "supervisor",
        "cpu_percent": cpu,
        "memory_percent": mem_pct,
        "memory_used_gb": mem_used,
        "memory_total_gb": mem_total,
        "disk_percent": disk_pct,
        "disk_used_gb": disk_used,
        "disk_total_gb": disk_total,
        "detail": None if supported else "supervisor_stats_unavailable",
    }


def _from_proc() -> dict[str, Any]:
    cpu = None
    mem_pct = None
    mem_used = None
    mem_total = None
    disk_pct = None
    disk_used = None
    disk_total = None

    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            load1 = float(fh.read().split()[0])
        cores = os.cpu_count() or 1
        cpu = _round(min(100.0, (load1 / cores) * 100))
    except Exception:
        _LOGGER.debug("UDHUB /proc/loadavg unavailable", exc_info=True)

    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    meminfo[parts[0][:-1]] = int(parts[1])
        total_kb = meminfo.get("MemTotal")
        avail_kb = meminfo.get("MemAvailable")
        if total_kb and total_kb > 0 and avail_kb is not None:
            used_kb = total_kb - avail_kb
            mem_total = _round(total_kb / (1024**2), 2)
            mem_used = _round(used_kb / (1024**2), 2)
            mem_pct = _round((used_kb / total_kb) * 100)
    except Exception:
        _LOGGER.debug("UDHUB /proc/meminfo unavailable", exc_info=True)

    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total > 0:
            used = total - free
            disk_total = _round(total / (1024**3), 2)
            disk_used = _round(used / (1024**3), 2)
            disk_pct = _round((used / total) * 100)
    except Exception:
        _LOGGER.debug("UDHUB statvfs unavailable", exc_info=True)

    supported = any(v is not None for v in (cpu, mem_pct, disk_pct))
    return {
        "supported": supported,
        "source": "proc" if supported else "unsupported",
        "cpu_percent": cpu,
        "memory_percent": mem_pct,
        "memory_used_gb": mem_used,
        "memory_total_gb": mem_total,
        "disk_percent": disk_pct,
        "disk_used_gb": disk_used,
        "disk_total_gb": disk_total,
        "detail": None if supported else "host_metrics_unavailable",
    }
