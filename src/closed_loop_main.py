"""
Entry point for the AI-controlled closed-loop run: wires the LLM agent (agent_client) into the
live EnergyPlus EMS callback loop (ep_runner) and produces everything the dashboard needs.

Run: .venv/Scripts/python.exe src/closed_loop_main.py
"""
import csv
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_client import AgentClient  # noqa: E402
from ep_runner import EnergyPlusRunner  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
IDF_PATH = ROOT / "data" / "idf" / "baseline.idf"
EPW_PATH = ROOT / "data" / "weather" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
OUT_DIR = ROOT / "logs" / "ai_run_raw"
FINAL_CSV = ROOT / "logs" / "ai_run.csv"
DECISIONS_CSV = ROOT / "logs" / "ai_decisions.csv"
EXPORT_IDF = ROOT / "data" / "idf" / "ai_optimized_setpoints.idf"

CONTROL_EVERY_N_HOURS = 2
EXPORT_DAY = 16  # a full mid-week day (avoids the partial edge days 14/17) for the exported schedule


def export_decision_schedule_idf(decisions: list[dict]):
    """Bakes the AI's converged setpoint choices for one representative day into a standalone
    IDF (baseline.idf with HTGSETP_SCH/CLGSETP_SCH replaced by Schedule:Compact objects
    reflecting what the agent actually decided), satisfying the 'modified .idf' deliverable
    without requiring the full run to be re-executed to inspect the AI's behavior."""
    day_decisions = sorted((d for d in decisions if d["day"] == EXPORT_DAY), key=lambda d: d["hour"])
    if not day_decisions:
        day_decisions = sorted(decisions, key=lambda d: (d["day"], d["hour"]))[:8]

    def build_schedule(name: str, key: str) -> str:
        lines = [f"  Schedule:Compact,\n    {name},\n    Temperature,\n    Through: 12/31,\n    For: AllDays,"]
        for i, dec in enumerate(day_decisions):
            end_hour = day_decisions[i + 1]["hour"] if i + 1 < len(day_decisions) else 24
            terminator = ";" if i == len(day_decisions) - 1 else ","
            lines.append(f"    Until: {end_hour:02d}:00,{dec[key]}{terminator}")
        return "\n".join(lines) + "\n"

    text = IDF_PATH.read_text(encoding="latin-1")
    for name, key in (("HTGSETP_SCH", "heating_c"), ("CLGSETP_SCH", "cooling_c")):
        # Field lines carry trailing inline comments (e.g. "26.7,       !- Field 7"), so the
        # terminating ";" is never immediately followed by "\n" - match through to end-of-line
        # after the semicolon instead, or this silently swallows everything up to the next
        # object whose last field happens to have no inline comment.
        pattern = re.compile(rf"  Schedule:Compact,\n    {name},.*?;[^\n]*\n", re.DOTALL)
        text, n = pattern.subn(build_schedule(name, key), text, count=1)
        if n == 0:
            raise RuntimeError(f"Could not find {name} Schedule:Compact block to replace")

    EXPORT_IDF.write_text(text, encoding="latin-1")
    print(f"Exported AI-optimized schedule -> {EXPORT_IDF} (from day {EXPORT_DAY} decisions)")


def main():
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    agent = AgentClient()
    print("Starting agent (MCP server + Ollama session)...")
    agent.start()

    runner = EnergyPlusRunner(
        idf_path=str(IDF_PATH),
        epw_path=str(EPW_PATH),
        out_dir=str(OUT_DIR),
        decide_fn=agent.decide,
        control_every_n_hours=CONTROL_EVERY_N_HOURS,
    )

    t0 = time.time()
    try:
        print("Running closed-loop simulation (this drives ~30 LLM decisions - can take a while)...")
        runner.run()
    finally:
        agent.stop()
    elapsed = time.time() - t0
    print(f"Simulation complete in {elapsed / 60:.1f} min. {len(runner.decisions)} agent decisions made.")

    shutil.copyfile(OUT_DIR / "eplusout.csv", FINAL_CSV)
    print(f"Copied EnergyPlus CSV output -> {FINAL_CSV}")

    if runner.decisions:
        fieldnames = list(runner.decisions[0].keys())
        with open(DECISIONS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(runner.decisions)
        print(f"Wrote decision log -> {DECISIONS_CSV}")

        n_fallback = sum(1 for d in runner.decisions if d["source"] != "agent")
        print(f"Decisions from agent: {len(runner.decisions) - n_fallback}/{len(runner.decisions)} "
              f"(fallbacks: {n_fallback})")
        export_decision_schedule_idf(runner.decisions)
    else:
        print("WARNING: no decisions were recorded - check control cadence / decide_fn wiring.")


if __name__ == "__main__":
    main()
