"""
MCP server exposing the building's live state and control surface as tools for the LLM agent.

Runs as its own process (spawned over stdio by agent_client.py), which is genuine MCP - but it
still needs to see live simulation state that only exists inside the EnergyPlus driver process.
Rather than building a custom bidirectional IPC channel under a hackathon time budget, the two
processes agree on a tiny JSON handoff file: the driver (ep_runner's decide_fn, via
agent_client.py) writes the latest snapshot to LIVE_STATE_PATH immediately before asking the
agent to decide, and this server's read-only tools just load whatever is there. This keeps the
server a genuine, independently-callable tool boundary while sidestepping custom IPC - see
docs/ARCHITECTURE.md for the full rationale.
"""
import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from carbon_signal import carbon_intensity_g_per_kwh  # noqa: E402
from safety import clamp_setpoints  # noqa: E402
from weather_forecast import outdoor_temp_forecast  # noqa: E402

ROOT = SRC_DIR.parent
LIVE_STATE_PATH = ROOT / "logs" / "live_state.json"
EPW_PATH = ROOT / "data" / "weather" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"

mcp = FastMCP("eco-loop-building")


@mcp.tool()
def get_building_state() -> dict:
    """Get the current live building state: time, outdoor weather, per-zone temperature/
    humidity/PMV comfort index, whole-facility power draw, and the currently active
    heating/cooling setpoints."""
    if not LIVE_STATE_PATH.exists():
        return {"error": "no live state available yet"}
    return json.loads(LIVE_STATE_PATH.read_text())


@mcp.tool()
def get_forecast(hours_ahead: int = 6) -> dict:
    """Get a forecast of outdoor temperature and local grid carbon intensity for the next
    `hours_ahead` hours, to help decide whether to pre-cool/pre-heat or coast."""
    state = json.loads(LIVE_STATE_PATH.read_text()) if LIVE_STATE_PATH.exists() else {}
    month, day, hour = state.get("month", 7), state.get("day", 15), state.get("hour", 12)
    temp_forecast = outdoor_temp_forecast(EPW_PATH, month, day, hour, hours_ahead)
    for row in temp_forecast:
        row["carbon_g_per_kwh"] = carbon_intensity_g_per_kwh(hour + row["hour_offset"])
    return {"forecast": temp_forecast}


@mcp.tool()
def set_zone_setpoints(cooling_c: float, heating_c: float, rationale: str) -> dict:
    """Commit new heating and cooling setpoints for the building (applied uniformly across
    all conditioned zones). Values are clamped to a safe comfort/equipment band server-side
    regardless of what is requested. `rationale` should briefly explain the reasoning (e.g.
    referencing occupancy, forecast, or carbon intensity) for the decision log."""
    clamped = clamp_setpoints(float(cooling_c), float(heating_c))
    return {**clamped, "rationale": rationale}


if __name__ == "__main__":
    mcp.run()
