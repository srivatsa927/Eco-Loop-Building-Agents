"""
Outdoor-temperature "forecast" for the agent's get_forecast tool: reads ahead in the same EPW
file being simulated. This is a deliberate "perfect forecast" simplification (see
docs/ARCHITECTURE.md) rather than a live weather API - common in building-control research and
enough to let the agent reason about pre-cooling/pre-heating ahead of a temperature swing.

EPW hourly data rows start on line 9; columns are Year, Month, Day, Hour(1-24), Minute, Data
Source flags, Dry Bulb Temperature (C), ... (see the EnergyPlus Auxiliary Programs
documentation for the full column layout).
"""
from pathlib import Path

_cache: dict[tuple, float] = {}
_cached_path: Path | None = None


def _load(epw_path: Path):
    global _cached_path
    if _cached_path == epw_path:
        return
    _cache.clear()
    lines = epw_path.read_text(encoding="latin-1").splitlines()[8:]
    for line in lines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        month, day, hour = int(parts[1]), int(parts[2]), int(parts[3])
        _cache[(month, day, hour % 24)] = float(parts[6])
    _cached_path = epw_path


def outdoor_temp_forecast(epw_path: Path, month: int, day: int, hour: int, hours_ahead: int) -> list[dict]:
    _load(Path(epw_path))
    out = []
    for i in range(1, hours_ahead + 1):
        total_hour = hour + i
        d, h = day + total_hour // 24, total_hour % 24
        out.append({"hour_offset": i, "outdoor_temp_c": _cache.get((month, d, h))})
    return out
