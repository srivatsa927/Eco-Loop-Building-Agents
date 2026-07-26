"""
MCP client + Ollama tool-calling agent loop. Exposes `AgentClient`, whose `.decide(snapshot)`
method is a synchronous `decide_fn` compatible with ep_runner.EnergyPlusRunner.

ep_runner's EMS callbacks are synchronous ctypes callbacks, but the MCP SDK is async, so a
single background thread runs one persistent asyncio event loop for the lifetime of the whole
closed-loop run: the MCP server subprocess is spawned once (not respawned per decision - with
~48 decisions across a run, that would be 48 subprocess spawns for no benefit) and each decide()
call is submitted to that loop via run_coroutine_threadsafe with a hard timeout, so a hung LLM
or tool call degrades to the last-known-good setpoints instead of ever stalling the simulation.
"""
import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Optional

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
LIVE_STATE_PATH = ROOT / "logs" / "live_state.json"

sys.path.insert(0, str(SRC_DIR))
from carbon_signal import carbon_intensity_g_per_kwh  # noqa: E402

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "llama3.1:8b"
LLM_TIMEOUT_S = 90.0
DECISION_TIMEOUT_S = 240.0  # generous ceiling for the whole multi-round tool-calling loop;
# observed single-round latency with llama3.1:8b varies roughly 20-90s call to call
MAX_TOOL_ROUNDS = 3
OCCUPIED_START_HOUR = 6
OCCUPIED_END_HOUR = 22

SYSTEM_PROMPT = """You are the automated energy control agent for a 5-zone small office \
building in Chicago, simulated in EnergyPlus. Each control step you choose one heating \
setpoint and one cooling setpoint (degrees C) applied uniformly to the whole building until \
your next decision.

Goal: minimize energy use and carbon impact while keeping occupants comfortable.

Rules of thumb:
- Occupied hours are 06:00-22:00. During occupied hours try to keep PMV within about -0.5 to \
+0.5, roughly corresponding to zone air temperatures of 21-25C.
- Outside occupied hours nobody is there - set back aggressively (e.g. cooling near 28C, \
heating near 16C) to save energy.
- IMPORTANT - the building has thermal mass and takes 2-4 HOURS to recover from a deep \
setback. If the current hour is 04:00 or 05:00 (i.e. occupancy starts at 06:00, within the \
next 1-2 hours), you MUST stop setting back and instead move the setpoints back toward the \
comfort band NOW (e.g. cooling ~24-25C, heating ~19-20C) so the zones are already comfortable \
by the time people arrive, even though nobody is there yet at 04:00/05:00 itself. Waiting \
until 06:00 to start warming/cooling back up is too late and will cause a multi-hour comfort \
violation every single day - pre-condition one to two control steps EARLY instead.
- The heating setpoint must stay meaningfully below the cooling setpoint (enforced \
automatically, but choose sane values yourselves too).
- Use the carbon-intensity forecast when it's useful: shifting pre-cooling/pre-heating to a \
lower-carbon hour reduces carbon impact even when it barely changes kWh.

You may call get_forecast if you want more information than what's already given. When ready, \
call set_zone_setpoints exactly once with cooling_c, heating_c, and a one-sentence rationale. \
Calling set_zone_setpoints is the ONLY way your decision takes effect - always finish by \
calling it, even if you're just confirming the current setpoints are still fine."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": "Get outdoor temperature and grid carbon-intensity forecast for the next N hours.",
            "parameters": {
                "type": "object",
                "properties": {"hours_ahead": {"type": "integer", "description": "How many hours ahead, e.g. 6"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_zone_setpoints",
            "description": "Commit the heating/cooling setpoints for the next control interval. Call exactly once, last.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cooling_c": {"type": "number", "description": "Cooling setpoint in Celsius"},
                    "heating_c": {"type": "number", "description": "Heating setpoint in Celsius"},
                    "rationale": {"type": "string", "description": "One sentence explaining the decision"},
                },
                "required": ["cooling_c", "heating_c", "rationale"],
            },
        },
    },
]


def _build_user_prompt(snapshot: dict) -> str:
    hour = snapshot["hour"]
    occupied = OCCUPIED_START_HOUR <= hour < OCCUPIED_END_HOUR
    carbon_now = carbon_intensity_g_per_kwh(hour)
    zone_temps = {z: round(v, 1) for z, v in snapshot["zone_temps_c"].items()}
    zone_pmv = {z: round(v, 2) for z, v in snapshot["zone_pmv"].items()}
    zone_rh = {z: round(v, 0) for z, v in snapshot["zone_rh_pct"].items()}
    return (
        f"Time: month {snapshot['month']} day {snapshot['day']}, {hour:02d}:00 "
        f"({'OCCUPIED' if occupied else 'unoccupied'})\n"
        f"Outdoor temp: {snapshot['outdoor_temp_c']:.1f} C\n"
        f"Zone temps (C): {zone_temps}\n"
        f"Zone PMV: {zone_pmv}\n"
        f"Zone RH (%): {zone_rh}\n"
        f"Facility power draw: {snapshot['facility_power_w']:.0f} W\n"
        f"Current setpoints: cooling {snapshot['current_cooling_c']} C / heating "
        f"{snapshot['current_heating_c']} C\n"
        f"Grid carbon intensity now: {carbon_now:.0f} gCO2/kWh\n\n"
        "Decide the setpoints for the next control interval."
    )


def _coerce_args(args) -> dict:
    if isinstance(args, str):
        args = json.loads(args)
    out = dict(args)
    for key in ("cooling_c", "heating_c", "hours_ahead"):
        if key in out and out[key] is not None:
            out[key] = float(out[key]) if key != "hours_ahead" else int(float(out[key]))
    return out


def _extract_tool_text(call_tool_result) -> str:
    parts = []
    for block in call_tool_result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else "{}"


class AgentClient:
    def __init__(self, model: str = MODEL_NAME):
        self.model = model
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[ClientSession] = None
        self._stdio_ctx = None
        self._session_ctx = None
        self._http = httpx.AsyncClient(timeout=LLM_TIMEOUT_S)

    # -- lifecycle -----------------------------------------------------------------
    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
        fut.result(timeout=30)

    async def _async_start(self):
        params = StdioServerParameters(command=sys.executable, args=[str(SRC_DIR / "mcp_server.py")])
        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    def stop(self):
        if self._loop is None or self._thread is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
        try:
            fut.result(timeout=15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    async def _async_stop(self):
        if self._session_ctx:
            await self._session_ctx.__aexit__(None, None, None)
        if self._stdio_ctx:
            await self._stdio_ctx.__aexit__(None, None, None)
        await self._http.aclose()

    # -- decision --------------------------------------------------------------
    def decide(self, snapshot: dict) -> dict:
        """Synchronous decide_fn for EnergyPlusRunner. Raises on failure/timeout so the
        caller's existing fallback-to-last-good-setpoints logic takes over."""
        if self._loop is None:
            raise RuntimeError("AgentClient.start() must be called before decide()")
        fut = asyncio.run_coroutine_threadsafe(self._decide_async(snapshot), self._loop)
        return fut.result(timeout=DECISION_TIMEOUT_S)

    async def _decide_async(self, snapshot: dict) -> dict:
        live_state = {**snapshot, "carbon_g_per_kwh": carbon_intensity_g_per_kwh(snapshot["hour"])}
        LIVE_STATE_PATH.write_text(json.dumps(live_state, default=str))

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(snapshot)},
        ]

        for _round in range(MAX_TOOL_ROUNDS):
            resp = await self._http.post(
                OLLAMA_URL,
                json={"model": self.model, "messages": messages, "tools": TOOLS, "stream": False, "keep_alive": "30m"},
            )
            resp.raise_for_status()
            msg = resp.json()["message"]
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                messages.append({"role": "assistant", "content": msg.get("content", "")})
                messages.append({"role": "user", "content": "Call set_zone_setpoints now to finish."})
                continue

            messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                args = _coerce_args(tc["function"]["arguments"])
                result = await self._session.call_tool(name, args)
                result_text = _extract_tool_text(result)
                messages.append({"role": "tool", "content": result_text})
                if name == "set_zone_setpoints":
                    decision = json.loads(result_text)
                    decision["source"] = "agent"
                    return decision

        raise TimeoutError(f"agent did not call set_zone_setpoints within {MAX_TOOL_ROUNDS} rounds")
