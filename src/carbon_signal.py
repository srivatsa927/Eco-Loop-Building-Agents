"""
Synthetic local grid carbon-intensity signal, standing in for a real-time API (electricityMap,
WattTime, etc.) so the demo has zero external network dependency and cannot fail during judging.

Shaped like a typical mid-latitude "duck curve": low overnight (baseload generation), a morning
ramp, a solar-driven midday dip, and a sharp evening peak when demand is high and solar is gone
and gas peaker plants pick up the load. Values are illustrative gCO2/kWh, not measured data.
"""

# (hour, intensity_g_per_kwh) anchor points; interpolated between them.
_ANCHORS = [
    (0, 380), (3, 350), (6, 380), (8, 460),
    (11, 330), (13, 300), (15, 320), (17, 480),
    (19, 590), (21, 520), (23, 430), (24, 380),
]


def carbon_intensity_g_per_kwh(hour: float) -> float:
    hour = hour % 24
    for (h0, v0), (h1, v1) in zip(_ANCHORS, _ANCHORS[1:]):
        if h0 <= hour <= h1:
            frac = (hour - h0) / (h1 - h0)
            return round(v0 + frac * (v1 - v0), 1)
    return _ANCHORS[-1][1]  # pragma: no cover - unreachable, anchors span 0-24


if __name__ == "__main__":
    for h in range(24):
        print(f"{h:02d}:00  {carbon_intensity_g_per_kwh(h):>5.1f} gCO2/kWh")
