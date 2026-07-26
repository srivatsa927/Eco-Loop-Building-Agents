# System Architecture — Eco-Loop Building Agents

## Overview

A single continuous EnergyPlus simulation (DOE Small Office reference building, Chicago,
4-day summer window) runs under the **EnergyPlus Python API** (`pyenergyplus.api`), not the
CLI. An EMS-style callback fires every zone timestep; at a coarser cadence (every 3 simulated
hours) it hands a compact state snapshot to an **LLM agent** (Ollama, `llama3.1:8b`, local),
which calls **MCP tools** to inspect state/forecast and commit new heating/cooling setpoints.
Those setpoints are written back into the *same running simulation* via EMS actuators — true
forward injection, no restart.

```
EnergyPlus (live process, pyenergyplus.api)
  │  every zone timestep: write current setpoints -> actuators (callback_begin_system_timestep_before_predictor)
  │  every zone timestep: read sensors, log a row       (callback_end_zone_timestep_after_zone_reporting)
  │  every 3 sim-hours:  -> decide_fn(snapshot) ────────┐
  │                                                       ▼
  │                                          AgentClient.decide()  (src/agent_client.py)
  │                                                       │  writes logs/live_state.json
  │                                                       │  Ollama /api/chat, tools=[get_forecast, set_zone_setpoints]
  │                                                       ▼
  │                                          MCP session -> mcp_server.py (child process, stdio)
  │                                                       │  get_building_state / get_forecast: read live_state.json
  │                                                       │  set_zone_setpoints: clamp + return
  │◄──────────────────────────────────────────────────────
  │  latched (clamped) setpoints become "current setpoints" for the next N hours
```

## Tool-calling architecture

Three MCP tools (`src/mcp_server.py`, built on the official `mcp` Python SDK's `FastMCP`),
spawned as a real child process over stdio and called through a genuine `ClientSession` —
not an in-process function call dressed up as a tool:

- `get_building_state` — current time, outdoor weather, per-zone temp/RH/PMV, facility power,
  active setpoints.
- `get_forecast(hours_ahead)` — outdoor temp + synthetic carbon-intensity forecast.
- `set_zone_setpoints(cooling_c, heating_c, rationale)` — the only action tool; clamps to a
  safe band server-side and returns what was actually applied.

**The state-handoff problem.** The MCP server is a separate OS process from the EnergyPlus
driver, but it needs to see *live* simulation state it has no direct access to. Rather than
building bidirectional IPC under a hackathon time budget, the driver writes the latest
snapshot to a small `logs/live_state.json` immediately before asking the agent to decide, and
the server's read tools simply load whatever is there. This keeps the server a genuine,
independently-callable tool boundary (it could be pointed at any live building, or called by a
different client entirely) while sidestepping custom IPC. The *write* path needs no such
trick: `set_zone_setpoints`'s return value flows back to the caller through the normal MCP
`CallToolResult`.

**Agent loop** (`AgentClient._decide_async`): system prompt + a pre-filled compact state
summary in the first user turn (so the model isn't forced to call `get_building_state` just to
learn values it already has), tools offered for anything it wants to check further, capped at
3 tool-calling rounds. The loop ends the instant `set_zone_setpoints` is called; anything else
triggers a "now call set_zone_setpoints" nudge on the next round.

## Prompt engineering

The system prompt is rule-based rather than open-ended: it states the objective (minimize
energy/carbon while keeping comfort), the occupied window (06:00-22:00), a target PMV band
(±0.5), permission to set back aggressively when unoccupied, and the carbon-forecast angle
(shift pre-conditioning to lower-carbon hours). The user turn is a fixed-format snapshot
(time, outdoor temp, per-zone temp/PMV/RH, power draw, current setpoints, carbon intensity)
so the model reasons over consistent, compact structure rather than free text. `llama3.1:8b`'s
tool-calling returns argument values with inconsistent types (e.g. setpoints as strings) and,
observed during testing, occasionally an invalid ordering (heating above cooling) — so every
tool argument is coerced/validated defensively, and setpoints are clamped and deadband-checked
in **two places**: inside the `set_zone_setpoints` MCP tool, and again as a hard floor in
`ep_runner.py` immediately before the value ever reaches an EnergyPlus actuator. Neither layer
trusts the model's raw output.

## Latency management

Single-call latency with a locally-hosted 8B model is real and variable (observed ~20-90s per
round-trip once the model is warm; ~60s one-time load if it had unloaded). Three decisions
follow directly from that:

1. **Control cadence is decoupled from the physics timestep.** The simulation steps every 10
   minutes; the agent is only consulted every 3 simulated hours. This is both a latency
   necessity (consulting an LLM every 10 minutes would make a multi-day run take hours) and a
   reasonable modeling choice (HVAC setpoints don't need to change faster than that).
2. **`keep_alive: "30m"` on every Ollama call** so the model stays resident between decisions
   instead of reloading (a ~60s cost) each time.
3. **Hard timeout with safe fallback**: each decision is bounded (240s ceiling for up to 3
   tool-calling rounds). If the agent errors, times out, or returns something unparsable,
   `ep_runner.py` catches it, keeps the *last known-good setpoints*, and logs the fallback with
   its cause — the simulation is never blocked or crashed by a slow or broken model call. This
   is the main defense behind "runs reliably over an extended time horizon."

## Handling simulation logs

EnergyPlus's native output (`.err`, `.eso`, `.mtr`) is verbose and file-based — not something
to feed an LLM directly. The live loop never touches those files: it reads exactly the
sensor/meter values it needs in-memory through the Python API's Data Exchange interface
(`get_variable_value`/`get_meter_value`), so the agent's context stays small and fixed-size no
matter how long the run is. Those raw log files are retained on disk purely for human
debugging. For the *authoritative* energy/comfort numbers used in the savings dashboard,
`Output:Meter`/`Output:Variable` objects declared in the IDF produce EnergyPlus's own standard
hourly CSV (via the `-r`/readvars flag) for both the baseline and AI runs — this avoids a
subtlety in the live API (`get_meter_value` returns an instantaneous per-call reading, not a
guaranteed running total) by keeping the live API for agent-facing signals only and using
EnergyPlus's own accounting for anything the dashboard claims as a result.

## Safety net (why this doesn't crash)

- Every actuator write is clamped to `[15, 22]`°C heating / `[22, 29]`°C cooling with a minimum
  1.5°C deadband, unconditionally, in `src/safety.py` — shared by both the MCP server and the
  EnergyPlus runner so the same rule applies wherever a setpoint could originate.
- Every agent decision is wrapped in try/except at the point it's consumed
  (`EnergyPlusRunner._on_after_reporting`); any failure logs `source=fallback_error` and keeps
  the previous setpoints rather than propagating.
- The baseline run never goes through the agent or EMS actuator path at all — it's a plain,
  unmodified CLI run of the stock IDF — so the comparison reflects the building's real stock
  control strategy, not an artifact of our own plumbing.

## Observed results and behavior

Over the 4-day run: **1,053.5 kWh baseline vs. 1,025.6 kWh AI-controlled (2.6% reduction)**,
with occupied-hours comfort compliance (|PMV| ≤ 0.5) at **97.5% baseline vs. 90.9% AI**. 49
control decisions were made, 47 from the live agent and 2 from the safety fallback (a slow
model call hitting the timeout) — the simulation never crashed or stalled.

The comfort gap is worth being honest about rather than hiding: it comes entirely from a
predictable morning-recovery window (roughly 07:00-08:00), not random noise. The building's
thermal mass takes 2-4 hours to recover from the deep overnight setback the agent correctly
applies to save energy, so the setpoint decision has to *lead* the 06:00 occupancy start by a
couple of hours, not react to it. An earlier version of the system prompt didn't say this
explicitly, and occupied-hours comfort compliance was only 79.4% - a first sign of the agent
setting back too late. Making the pre-conditioning instruction explicit and concrete (name the
exact hour, name the exact reason) recovered most of that gap (79.4% -> 90.9%) at essentially
the same energy savings. The remaining gap is the model applying that instruction
*inconsistently* run to run - a realistic limitation of a small (8B) local model following a
multi-step conditional rule every time, not a bug in the control loop itself (the decision log
in `logs/ai_decisions.csv` shows the correct reasoning appearing on some mornings and reverting
to plain "aggressive setback" on others). A larger model, a dedicated optimal-start
calculation, or a rule-based override for that specific 2-hour window would likely close the
rest of the gap - noted here as the clearest next improvement rather than engineered under
this time budget.

## Known simplifying assumptions

- **Carbon intensity** is a synthetic time-of-day proxy (documented duck-curve shape), not a
  live grid API — a deliberate choice to remove an external network dependency from the demo.
- **Weather forecast** used by `get_forecast` reads ahead in the same EPW file being simulated
  (a "perfect forecast" assumption, common in building-control research) rather than a
  real forecast API.
- **Uniform building-wide setpoints**: the reference building's 5 conditioned zones already
  share one heating and one cooling schedule object, so the agent controls both schedules
  rather than 5 independent zone setpoints — matches the model's actual control structure.
- Scoped to a representative 4-day summer window (cooling-dominated) rather than a full year,
  to keep total LLM-decision count and wall-clock demo time practical.
