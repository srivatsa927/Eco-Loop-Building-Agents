"""
Thin wrapper around the EnergyPlus Python API (pyenergyplus.api.EnergyPlusAPI) that runs a
single continuous simulation and, at a configurable control cadence, hands a compact state
snapshot to a caller-supplied decision function and writes the returned setpoints back into
the live run via EMS actuators - the "closed loop" the hackathon brief describes.

Two callbacks do the work, matching the standard EnergyPlus data-exchange pattern:
  - callback_begin_system_timestep_before_predictor: writes the current setpoints into the
    schedule actuators *before* the zone predictor computes loads for this timestep, every
    timestep (actuator overrides don't persist on their own - they must be reasserted).
  - callback_end_zone_timestep_after_zone_reporting: reads sensors after the timestep's HVAC
    simulation has settled, logs a row, and - once per `control_every_n_hours` - calls
    `decide_fn` and latches its (clamped) result as the new current setpoints.

Meters in the live Data Exchange API report instantaneous-per-call values, not a reliable
running total (see EnergyPlus API docs), so they're used here only to give the agent a rough
"recent power draw" signal. The authoritative kWh totals used for the savings dashboard come
from EnergyPlus's own hourly CSV output (Output:Meter + the `-r`/readvars CLI flag), produced
independently at the end of the run.
"""
import sys
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
EP_DIR = ROOT / "vendor" / "energyplus"
if str(EP_DIR) not in sys.path:
    sys.path.insert(0, str(EP_DIR))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pyenergyplus.api import EnergyPlusAPI  # noqa: E402

from safety import DEFAULT_SETPOINTS, clamp_setpoints  # noqa: E402

CONDITIONED_ZONES = [
    "CORE_ZN",
    "PERIMETER_ZN_1",
    "PERIMETER_ZN_2",
    "PERIMETER_ZN_3",
    "PERIMETER_ZN_4",
]


class EnergyPlusRunner:
    def __init__(
        self,
        idf_path: str,
        epw_path: str,
        out_dir: str,
        decide_fn: Optional[Callable[[dict], dict]] = None,
        control_every_n_hours: int = 2,
    ):
        self.idf_path = str(idf_path)
        self.epw_path = str(epw_path)
        self.out_dir = str(out_dir)
        self.decide_fn = decide_fn
        self.control_every_n_hours = control_every_n_hours

        self.api = EnergyPlusAPI()
        self.state = self.api.state_manager.new_state()

        self.handles = {}
        self._handles_ready = False
        self._pmv_ready = False
        self.current_setpoints = dict(DEFAULT_SETPOINTS)
        self.rows: list[dict] = []
        self.decisions: list[dict] = []
        self._day0 = None
        self._last_control_bucket = None

    def _request_variables(self):
        ex = self.api.exchange
        for zone in CONDITIONED_ZONES:
            ex.request_variable(self.state, "Zone Mean Air Temperature", zone)
            ex.request_variable(self.state, "Zone Air Relative Humidity", zone)
            ex.request_variable(self.state, "Zone Thermal Comfort Fanger Model PMV", f"{zone} PEOPLE")
        ex.request_variable(self.state, "Site Outdoor Air Drybulb Temperature", "Environment")
        ex.request_variable(self.state, "Facility Total Electricity Demand Rate", "Whole Building")

    def _on_get_input(self, _state):
        ex = self.api.exchange
        s = self.state
        h = self.handles
        for zone in CONDITIONED_ZONES:
            h[f"temp_{zone}"] = ex.get_variable_handle(s, "Zone Mean Air Temperature", zone)
            h[f"rh_{zone}"] = ex.get_variable_handle(s, "Zone Air Relative Humidity", zone)
        h["outdoor_temp"] = ex.get_variable_handle(s, "Site Outdoor Air Drybulb Temperature", "Environment")
        h["facility_power_w"] = ex.get_variable_handle(s, "Facility Total Electricity Demand Rate", "Whole Building")
        h["cooling_setpoint"] = ex.get_actuator_handle(s, "Schedule:Compact", "Schedule Value", "CLGSETP_SCH")
        h["heating_setpoint"] = ex.get_actuator_handle(s, "Schedule:Compact", "Schedule Value", "HTGSETP_SCH")

        bad = [k for k, v in h.items() if v == -1]
        if bad:
            raise RuntimeError(f"EnergyPlus API: could not resolve handles for: {bad}")
        self._handles_ready = True

    def _ensure_pmv_handles(self):
        # Comfort (PMV) output variables are keyed by the People *object* name, not the zone
        # name - in this model that's "<ZONE> People" (e.g. "CORE_ZN PEOPLE"), one per zone.
        if self._pmv_ready:
            return
        ex = self.api.exchange
        s = self.state
        pmv_handles = {
            f"pmv_{zone}": ex.get_variable_handle(s, "Zone Thermal Comfort Fanger Model PMV", f"{zone} PEOPLE")
            for zone in CONDITIONED_ZONES
        }
        if any(v == -1 for v in pmv_handles.values()):
            return  # try again next call
        self.handles.update(pmv_handles)
        self._pmv_ready = True

    def _on_before_predictor(self, _state):
        if not self._handles_ready:
            return
        ex = self.api.exchange
        s = self.state
        ex.set_actuator_value(s, self.handles["cooling_setpoint"], self.current_setpoints["cooling_c"])
        ex.set_actuator_value(s, self.handles["heating_setpoint"], self.current_setpoints["heating_c"])

    def _on_after_reporting(self, _state):
        if not self._handles_ready:
            return
        ex = self.api.exchange
        s = self.state
        if ex.warmup_flag(s):
            return
        self._ensure_pmv_handles()

        zone_temps = {z: ex.get_variable_value(s, self.handles[f"temp_{z}"]) for z in CONDITIONED_ZONES}
        zone_rh = {z: ex.get_variable_value(s, self.handles[f"rh_{z}"]) for z in CONDITIONED_ZONES}
        if self._pmv_ready:
            zone_pmv = {z: ex.get_variable_value(s, self.handles[f"pmv_{z}"]) for z in CONDITIONED_ZONES}
        else:
            zone_pmv = {z: float("nan") for z in CONDITIONED_ZONES}
        outdoor_temp = ex.get_variable_value(s, self.handles["outdoor_temp"])
        facility_power_w = ex.get_variable_value(s, self.handles["facility_power_w"])

        month, day, hour, minute = ex.month(s), ex.day_of_month(s), ex.hour(s), ex.minutes(s)

        row = {
            "month": month,
            "day": day,
            "hour": hour,
            "minute": minute,
            "outdoor_temp_c": outdoor_temp,
            "facility_power_w": facility_power_w,
            "cooling_setpoint_c": self.current_setpoints["cooling_c"],
            "heating_setpoint_c": self.current_setpoints["heating_c"],
        }
        for z in CONDITIONED_ZONES:
            row[f"temp_{z}"] = zone_temps[z]
            row[f"rh_{z}"] = zone_rh[z]
            row[f"pmv_{z}"] = zone_pmv[z]
        self.rows.append(row)

        if self.decide_fn is None:
            return
        # HVAC convergence sub-steps make zone-reporting callbacks fire at irregular, non-clean
        # minute marks (e.g. :13, :25, :65) rather than exactly on the hour, so cadence is
        # detected via bucket-crossing on elapsed simulated hours rather than exact equality.
        # Assumes the run stays within a single month (true for this 4-day window).
        if self._day0 is None:
            self._day0 = day
        elapsed_hours = (day - self._day0) * 24 + hour + minute / 60.0
        control_bucket = int(elapsed_hours // self.control_every_n_hours)
        if control_bucket != self._last_control_bucket:
            self._last_control_bucket = control_bucket
            snapshot = {
                "month": month,
                "day": day,
                "hour": hour,
                "outdoor_temp_c": outdoor_temp,
                "facility_power_w": facility_power_w,
                "zone_temps_c": zone_temps,
                "zone_rh_pct": zone_rh,
                "zone_pmv": zone_pmv,
                "current_cooling_c": self.current_setpoints["cooling_c"],
                "current_heating_c": self.current_setpoints["heating_c"],
            }
            try:
                decision = self.decide_fn(snapshot)
                new_setpoints = clamp_setpoints(decision["cooling_c"], decision["heating_c"])
                source = decision.get("source", "agent")
                rationale = decision.get("rationale", "")
            except Exception as exc:  # noqa: BLE001 - any agent failure must not crash the sim
                new_setpoints = dict(self.current_setpoints)
                source = "fallback_error"
                rationale = f"decide_fn failed: {exc}"
            self.decisions.append({**snapshot, **new_setpoints, "source": source, "rationale": rationale})
            self.current_setpoints = new_setpoints

    def run(self) -> list[dict]:
        self._request_variables()
        self.api.runtime.callback_after_component_get_input(self.state, self._on_get_input)
        self.api.runtime.callback_begin_system_timestep_before_predictor(self.state, self._on_before_predictor)
        self.api.runtime.callback_end_zone_timestep_after_zone_reporting(self.state, self._on_after_reporting)

        exit_code = self.api.runtime.run_energyplus(
            self.state,
            ["-w", self.epw_path, "-d", self.out_dir, "-r", self.idf_path],
        )
        if exit_code != 0:
            raise RuntimeError(f"EnergyPlus exited with code {exit_code}; see {self.out_dir}/eplusout.err")
        return self.rows
