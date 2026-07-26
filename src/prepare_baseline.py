"""
One-time data prep: copy the DOE Small Office reference building (Chicago) out of the
EnergyPlus ExampleFiles/WeatherData folders into data/, and shorten the RunPeriod from a
full year down to a 4-day representative summer window (Mon-Thu) so the closed-loop demo
runs in a reasonable amount of wall-clock time.

Run once: .venv/Scripts/python.exe src/prepare_baseline.py
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EP_DIR = ROOT / "vendor" / "energyplus"
SRC_IDF = EP_DIR / "ExampleFiles" / "RefBldgSmallOfficeNew2004_Chicago.idf"
SRC_EPW = EP_DIR / "WeatherData" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"

OUT_IDF = ROOT / "data" / "idf" / "baseline.idf"
OUT_EPW = ROOT / "data" / "weather" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"

# Representative 4-day summer window: forces weekday schedules so occupancy/comfort
# behavior is predictable regardless of what the TMY3's implied calendar says.
RUNPERIOD_OLD = """  RunPeriod,
    Annual,                  !- Name
    1,                       !- Begin Month
    1,                       !- Begin Day of Month
    ,                        !- Begin Year
    12,                      !- End Month
    31,                      !- End Day of Month
    ,                        !- End Year
    Sunday,                  !- Day of Week for Start Day"""

RUNPERIOD_NEW = """  RunPeriod,
    SummerWeek,              !- Name
    7,                       !- Begin Month
    14,                      !- Begin Day of Month
    ,                        !- Begin Year
    7,                       !- End Month
    17,                      !- End Day of Month
    ,                        !- End Year
    Monday,                  !- Day of Week for Start Day"""

# The shipped example disables weather-file run periods (it only exists to demo HVAC
# sizing on the two ASHRAE design days) - flip it on so our actual RunPeriod executes.
SIMCONTROL_OLD = """  SimulationControl,
    YES,                     !- Do Zone Sizing Calculation
    YES,                     !- Do System Sizing Calculation
    YES,                     !- Do Plant Sizing Calculation
    YES,                     !- Run Simulation for Sizing Periods
    NO,                      !- Run Simulation for Weather File Run Periods"""

SIMCONTROL_NEW = """  SimulationControl,
    YES,                     !- Do Zone Sizing Calculation
    YES,                     !- Do System Sizing Calculation
    YES,                     !- Do Plant Sizing Calculation
    NO,                      !- Run Simulation for Sizing Periods
    YES,                     !- Run Simulation for Weather File Run Periods"""


def main():
    OUT_IDF.parent.mkdir(parents=True, exist_ok=True)
    OUT_EPW.parent.mkdir(parents=True, exist_ok=True)

    text = SRC_IDF.read_text(encoding="latin-1")
    if RUNPERIOD_OLD not in text:
        raise SystemExit("RunPeriod block not found verbatim - source IDF may have changed; "
                          "check formatting before re-running.")
    if SIMCONTROL_OLD not in text:
        raise SystemExit("SimulationControl block not found verbatim - source IDF may have "
                          "changed; check formatting before re-running.")
    text = text.replace(RUNPERIOD_OLD, RUNPERIOD_NEW)
    text = text.replace(SIMCONTROL_OLD, SIMCONTROL_NEW)

    # Emit the .edd/.mdd dictionaries listing every EMS-actuatable object and readable
    # internal variable for this model - used to confirm exact sensor/actuator names.
    if "Output:EnergyManagementSystem," not in text:
        text += (
            "\n  Output:EnergyManagementSystem,\n"
            "    Verbose,                 !- Actuator Availability Dictionary Reporting\n"
            "    Verbose,                 !- Internal Variable Availability Dictionary Reporting\n"
            "    Verbose;                 !- EMS Runtime Language Debug Output Level\n"
        )

    # Per-zone comfort/temp variables in the standard CSV output, so both the unmodified
    # baseline run and the AI run produce directly comparable data through the same
    # reporting path (rather than only relying on the live Python API sensor reads, which
    # are used solely to feed the agent's decisions during the AI run).
    if "Zone Thermal Comfort Fanger Model PMV,Hourly" not in text:
        text += (
            "\n  Output:Variable,*,Zone Mean Air Temperature,Hourly;"
            "\n  Output:Variable,*,Zone Air Relative Humidity,Hourly;"
            "\n  Output:Variable,*,Zone Thermal Comfort Fanger Model PMV,Hourly;"
            "\n  Output:Variable,*,Site Outdoor Air Drybulb Temperature,Hourly;"
            "\n  Output:Variable,*,Facility Total Electricity Demand Rate,Hourly;\n"
        )
    OUT_IDF.write_text(text, encoding="latin-1")

    shutil.copyfile(SRC_EPW, OUT_EPW)
    print(f"Wrote {OUT_IDF} ({len(text)} bytes)")
    print(f"Copied {OUT_EPW}")


if __name__ == "__main__":
    main()
