"""
Hard safety bounds for HVAC setpoints, applied regardless of what any upstream decision logic
(LLM or otherwise) requests. Kept dependency-free (no EnergyPlus import) so both the EMS runner
process and the standalone MCP server process can share the exact same clamping logic.
"""

ABSOLUTE_MIN_HEATING_C = 15.0
ABSOLUTE_MAX_HEATING_C = 22.0
ABSOLUTE_MIN_COOLING_C = 22.0
ABSOLUTE_MAX_COOLING_C = 29.0
MIN_DEADBAND_C = 1.5  # cooling setpoint must be at least this far above heating setpoint

DEFAULT_SETPOINTS = {"cooling_c": 24.0, "heating_c": 20.0}


def clamp_setpoints(cooling_c: float, heating_c: float) -> dict:
    cooling_c = max(ABSOLUTE_MIN_COOLING_C, min(ABSOLUTE_MAX_COOLING_C, cooling_c))
    heating_c = max(ABSOLUTE_MIN_HEATING_C, min(ABSOLUTE_MAX_HEATING_C, heating_c))
    if cooling_c - heating_c < MIN_DEADBAND_C:
        mid = (cooling_c + heating_c) / 2
        cooling_c = mid + MIN_DEADBAND_C / 2
        heating_c = mid - MIN_DEADBAND_C / 2
    return {"cooling_c": round(cooling_c, 2), "heating_c": round(heating_c, 2)}
