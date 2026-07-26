"""
Runs the *unmodified* baseline building (its own original ASHRAE occupied/unoccupied setback
schedule, no agent involved at all) so the AI-controlled run has something meaningful to beat.
Deliberately does not go through the Python API/EMS layer - a plain CLI run guarantees this is
exactly the building's stock control strategy, not something incidentally shaped by our own
actuator-override plumbing.

Run: .venv/Scripts/python.exe src/baseline_runner.py
"""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENERGYPLUS_EXE = ROOT / "vendor" / "energyplus" / "energyplus.exe"
IDF_PATH = ROOT / "data" / "idf" / "baseline.idf"
EPW_PATH = ROOT / "data" / "weather" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
OUT_DIR = ROOT / "logs" / "baseline_raw"
FINAL_CSV = ROOT / "logs" / "baseline_run.csv"


def main():
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    result = subprocess.run(
        [str(ENERGYPLUS_EXE), "-w", str(EPW_PATH), "-d", str(OUT_DIR), "-r", str(IDF_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        raise SystemExit(f"EnergyPlus baseline run failed with code {result.returncode}")

    shutil.copyfile(OUT_DIR / "eplusout.csv", FINAL_CSV)
    print(f"Baseline run complete -> {FINAL_CSV}")


if __name__ == "__main__":
    main()
