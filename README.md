# Eco-Loop Building Agents

A live, closed-loop Physical AI proof-of-concept: a local open-source LLM (`llama3.1:8b` via
Ollama) reads real-time state from a running EnergyPlus building simulation through an MCP
tool server, and writes heating/cooling setpoint decisions back into that same simulation via
EnergyPlus's EMS Python API — no restart, no human in the loop.

Building: DOE Small Office reference model (5 conditioned zones, packaged DX cooling + gas
heating), Chicago O'Hare TMY3 weather, a 4-day representative summer window.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full system design, prompt
strategy, latency handling, and safety net. See [`dashboard/dashboard.html`](dashboard/dashboard.html)
for the savings results.

## Repo layout

```
requirements.txt
vendor/energyplus/          # EnergyPlus 26.1.0 (downloaded, not committed - see Setup)
data/idf/baseline.idf       # prepared baseline building (4-day RunPeriod, EMS dictionary, output vars)
data/idf/ai_optimized_setpoints.idf   # baseline.idf with the AI's converged setpoint schedule baked in
data/weather/*.epw
src/
  ep_runner.py               # EnergyPlus Python API wrapper: EMS callbacks, sensors, actuators, safety clamp
  safety.py                  # shared hard setpoint bounds (used by ep_runner AND the MCP server)
  carbon_signal.py           # synthetic grid carbon-intensity proxy
  mcp_server.py              # MCP server: get_building_state / get_forecast / set_zone_setpoints
  agent_client.py            # MCP client + Ollama tool-calling agent loop
  baseline_runner.py         # runs the unmodified building (comparison baseline)
  closed_loop_main.py        # runs the AI-controlled closed loop end-to-end
  prepare_baseline.py        # one-time data prep (copies + patches the source IDF)
dashboard/
  build_dashboard.py         # reads both run CSVs -> dashboard.html
  dashboard.html
logs/
  baseline_run.csv           # EnergyPlus hourly output, unmodified building
  ai_run.csv                 # EnergyPlus hourly output, AI-controlled run
  ai_decisions.csv           # every control decision: state in, setpoints out, rationale, source
docs/ARCHITECTURE.md
```

## Setup

Prerequisites: Python 3.11+, [Ollama](https://ollama.com) installed and running.

```bash
ollama pull llama3.1:8b
```

Download EnergyPlus 26.1.0 (Windows x86_64, official NREL release, ~242MB) and extract it to
`vendor/energyplus/` (must contain `energyplus.exe` and the `pyenergyplus/` package at its
root):

```bash
curl -L -o vendor/energyplus.zip https://github.com/NREL/EnergyPlus/releases/download/v26.1.0/EnergyPlus-26.1.0-6f2e40d102-Windows-x86_64.zip
unzip vendor/energyplus.zip -d vendor/energyplus_extract
mv vendor/energyplus_extract/EnergyPlus-26.1.0-*/* vendor/energyplus/
```

Python environment:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

## Running it

```bash
# One-time: prepare the baseline IDF (shortens RunPeriod to a 4-day window, adds output vars)
.venv/Scripts/python src/prepare_baseline.py

# Baseline: the building's own stock ASHRAE setback schedule, no AI involved
.venv/Scripts/python src/baseline_runner.py

# AI closed loop: drives ~30 LLM decisions over the 4-day run (can take 15-45+ min depending
# on local inference speed - Ollama must be running)
.venv/Scripts/python src/closed_loop_main.py

# Build the dashboard from both runs
.venv/Scripts/python dashboard/build_dashboard.py
```

Open `dashboard/dashboard.html` in a browser to see the results.

## Results

Over the 4-day Chicago summer window:

| | Baseline (stock schedule) | AI closed-loop |
|---|---|---|
| Electricity | 1,053.5 kWh | 1,025.6 kWh (**-2.6%**) |
| Occupied-hours comfort compliance (\|PMV\| ≤ 0.5) | 97.5% | 90.9% |
| Control decisions | — | 49 (47 from the agent, 2 safety fallbacks, 0 crashes) |

Full charts in [`dashboard/dashboard.html`](dashboard/dashboard.html), decision-by-decision
detail (state in, setpoints out, rationale) in [`logs/ai_decisions.csv`](logs/ai_decisions.csv).
The comfort gap and why it happens — a morning thermal-recovery lag, not random error — is
explained in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#observed-results-and-behavior).

## Known limitations

Grid carbon intensity is a synthetic time-of-day proxy (no external API dependency by design);
the weather "forecast" tool assumes perfect knowledge of the same EPW being simulated. Both are
explained in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-simplifying-assumptions).
"# Eco-Loop-Building-Agents" 
