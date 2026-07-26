"""
Builds the quantitative savings dashboard: reads logs/baseline_run.csv and logs/ai_run.csv
(both standard EnergyPlus hourly CSV output, produced by baseline_runner.py and
closed_loop_main.py respectively), computes energy savings and comfort-compliance metrics, and
renders a single self-contained dashboard.html (charts embedded as base64 PNGs, no CDN
dependency, no network access needed to view it).

Run: .venv/Scripts/python.exe dashboard/build_dashboard.py
"""
import base64
import io
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASELINE_CSV = ROOT / "logs" / "baseline_run.csv"
AI_CSV = ROOT / "logs" / "ai_run.csv"
OUT_HTML = ROOT / "dashboard" / "dashboard.html"

ZONES = ["CORE_ZN", "PERIMETER_ZN_1", "PERIMETER_ZN_2", "PERIMETER_ZN_3", "PERIMETER_ZN_4"]
OCCUPIED_START_HOUR, OCCUPIED_END_HOUR = 6, 22
PMV_COMFORT_BAND = 0.5

# Palette from the project's dataviz skill reference (validated, colorblind-safe ordering).
COLOR_BASELINE = "#2a78d6"
COLOR_AI = "#eb6834"
COLOR_GOOD = "#0ca30c"
COLOR_GRID = "#e1e0d9"
COLOR_TEXT = "#0b0b0b"
COLOR_MUTED = "#52514e"
COLOR_SURFACE = "#fcfcfb"


def _parse_datetime(df: pd.DataFrame) -> pd.DataFrame:
    # EnergyPlus reports "24:00:00" for the last timestep of a day instead of rolling over to
    # 00:00:00 of the next day, which pandas can't parse directly - normalize it first.
    raw = df["Date/Time"].str.strip()
    is_midnight_24 = raw.str.contains("24:00:00")
    normalized = raw.str.replace("24:00:00", "00:00:00", regex=False)
    dt = pd.to_datetime("2024/" + normalized, format="%Y/%m/%d  %H:%M:%S")
    dt.loc[is_midnight_24] += pd.Timedelta(days=1)
    df = df.copy()
    df["dt"] = dt
    df["hour"] = df["dt"].dt.hour
    return df


def _load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    return _parse_datetime(df)


def _total_kwh(df: pd.DataFrame, meter_col: str) -> float:
    return df[meter_col].sum() / 3.6e6 if meter_col in df.columns else float("nan")


def _pmv_columns(df: pd.DataFrame) -> dict:
    return {
        z: next(c for c in df.columns if c.upper().startswith(f"{z} PEOPLE") and "PMV" in c.upper())
        for z in ZONES
    }


def _comfort_compliance_pct(df: pd.DataFrame) -> float:
    occ = df[(df["hour"] >= OCCUPIED_START_HOUR) & (df["hour"] < OCCUPIED_END_HOUR)]
    pmv_cols = _pmv_columns(df)
    within = 0
    total = 0
    for z, col in pmv_cols.items():
        vals = occ[col]
        within += (vals.abs() <= PMV_COMFORT_BAND).sum()
        total += len(vals)
    return 100.0 * within / total if total else float("nan")


def _fig_to_base64() -> str:
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=COLOR_SURFACE)
    plt.close()
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _style_axes(ax):
    ax.set_facecolor(COLOR_SURFACE)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(COLOR_GRID)
    ax.tick_params(colors=COLOR_MUTED, labelsize=9)
    ax.yaxis.grid(True, color=COLOR_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.title.set_color(COLOR_TEXT)
    ax.xaxis.label.set_color(COLOR_MUTED)
    ax.yaxis.label.set_color(COLOR_MUTED)


def chart_energy_bar(baseline_kwh: float, ai_kwh: float) -> str:
    fig, ax = plt.subplots(figsize=(5, 4))
    bars = ax.bar(["Baseline", "AI closed-loop"], [baseline_kwh, ai_kwh],
                   color=[COLOR_BASELINE, COLOR_AI], width=0.55)
    for b, v in zip(bars, [baseline_kwh, ai_kwh]):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.0f} kWh", ha="center", va="bottom",
                 fontsize=10, color=COLOR_TEXT)
    pct = 100 * (baseline_kwh - ai_kwh) / baseline_kwh
    ax.set_title(f"Total facility electricity — {pct:.1f}% reduction", fontsize=11)
    ax.set_ylabel("kWh over 4-day run")
    _style_axes(ax)
    return _fig_to_base64()


def chart_power_timeseries(base_df: pd.DataFrame, ai_df: pd.DataFrame) -> str:
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(base_df["dt"], base_df["Electricity:Facility [J](Hourly)"] / 3.6e6, label="Baseline",
            color=COLOR_BASELINE, linewidth=1.6)
    ax.plot(ai_df["dt"], ai_df["Electricity:Facility [J](Hourly)"] / 3.6e6, label="AI closed-loop",
            color=COLOR_AI, linewidth=1.6)
    ax.set_title("Hourly facility electricity use", fontsize=11)
    ax.set_ylabel("kWh")
    ax.legend(frameon=False, labelcolor=COLOR_TEXT)
    _style_axes(ax)
    fig.autofmt_xdate()
    return _fig_to_base64()


def chart_comfort_timeseries(base_df: pd.DataFrame, ai_df: pd.DataFrame) -> str:
    base_pmv_col = _pmv_columns(base_df)["CORE_ZN"]
    ai_pmv_col = _pmv_columns(ai_df)["CORE_ZN"]
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.axhspan(-PMV_COMFORT_BAND, PMV_COMFORT_BAND, color=COLOR_GOOD, alpha=0.08, label="Comfort band")
    ax.plot(base_df["dt"], base_df[base_pmv_col], label="Baseline", color=COLOR_BASELINE, linewidth=1.6)
    ax.plot(ai_df["dt"], ai_df[ai_pmv_col], label="AI closed-loop", color=COLOR_AI, linewidth=1.6)
    ax.axhline(0, color=COLOR_MUTED, linewidth=0.8)
    ax.set_title("Core zone thermal comfort (PMV)", fontsize=11)
    ax.set_ylabel("PMV")
    ax.legend(frameon=False, labelcolor=COLOR_TEXT)
    _style_axes(ax)
    fig.autofmt_xdate()
    return _fig_to_base64()


def chart_comfort_bar(base_pct: float, ai_pct: float) -> str:
    fig, ax = plt.subplots(figsize=(5, 4))
    bars = ax.bar(["Baseline", "AI closed-loop"], [base_pct, ai_pct],
                   color=[COLOR_BASELINE, COLOR_AI], width=0.55)
    for b, v in zip(bars, [base_pct, ai_pct]):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.1f}%", ha="center", va="bottom",
                 fontsize=10, color=COLOR_TEXT)
    ax.set_title("Occupied-hours comfort compliance (|PMV| ≤ 0.5)", fontsize=11)
    ax.set_ylabel("% of occupied zone-hours")
    ax.set_ylim(0, 105)
    _style_axes(ax)
    return _fig_to_base64()


def build():
    base_df = _load(BASELINE_CSV)
    ai_df = _load(AI_CSV)

    baseline_kwh = _total_kwh(base_df, "Electricity:Facility [J](Hourly)")
    ai_kwh = _total_kwh(ai_df, "Electricity:Facility [J](Hourly)")
    pct_reduction = 100 * (baseline_kwh - ai_kwh) / baseline_kwh

    baseline_gas_kwh = _total_kwh(base_df, "NaturalGas:Facility [J](Hourly)")
    ai_gas_kwh = _total_kwh(ai_df, "NaturalGas:Facility [J](Hourly)")

    base_comfort = _comfort_compliance_pct(base_df)
    ai_comfort = _comfort_compliance_pct(ai_df)

    img_energy = chart_energy_bar(baseline_kwh, ai_kwh)
    img_power_ts = chart_power_timeseries(base_df, ai_df)
    img_comfort_ts = chart_comfort_timeseries(base_df, ai_df)
    img_comfort_bar = chart_comfort_bar(base_comfort, ai_comfort)

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Eco-Loop Building Agents — Savings Dashboard</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: #f9f9f7;
          color: {COLOR_TEXT}; margin: 0; padding: 32px; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .sub {{ color: {COLOR_MUTED}; margin-bottom: 24px; font-size: 14px; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
  .card {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_GRID}; border-radius: 10px;
           padding: 16px 20px; min-width: 180px; }}
  .card .label {{ font-size: 12px; color: {COLOR_MUTED}; text-transform: uppercase; letter-spacing: .04em; }}
  .card .value {{ font-size: 26px; font-weight: 600; margin-top: 4px; }}
  .card .value.good {{ color: {COLOR_GOOD}; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .grid img {{ width: 100%; height: auto; border-radius: 8px; border: 1px solid {COLOR_GRID}; }}
  .full {{ grid-column: 1 / -1; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style></head>
<body>
  <h1>Eco-Loop Building Agents — Quantitative Savings Dashboard</h1>
  <div class="sub">DOE Small Office reference building, Chicago O'Hare TMY3, 4-day summer window (Jul 14-17) &mdash;
    baseline (stock ASHRAE setback schedule) vs. LLM closed-loop control</div>

  <div class="cards">
    <div class="card"><div class="label">Electricity savings</div><div class="value good">{pct_reduction:.1f}%</div></div>
    <div class="card"><div class="label">Baseline electricity</div><div class="value">{baseline_kwh:,.0f} kWh</div></div>
    <div class="card"><div class="label">AI-controlled electricity</div><div class="value">{ai_kwh:,.0f} kWh</div></div>
    <div class="card"><div class="label">Baseline comfort compliance</div><div class="value">{base_comfort:.1f}%</div></div>
    <div class="card"><div class="label">AI comfort compliance</div><div class="value">{ai_comfort:.1f}%</div></div>
    <div class="card"><div class="label">Gas use (baseline / AI)</div><div class="value" style="font-size:18px">{baseline_gas_kwh:,.0f} / {ai_gas_kwh:,.0f} kWh-th</div></div>
  </div>

  <div class="grid">
    <div><img src="data:image/png;base64,{img_energy}"></div>
    <div><img src="data:image/png;base64,{img_comfort_bar}"></div>
    <div class="full"><img src="data:image/png;base64,{img_power_ts}"></div>
    <div class="full"><img src="data:image/png;base64,{img_comfort_ts}"></div>
  </div>
</body></html>
"""
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"Dashboard written -> {OUT_HTML}")
    print(f"Electricity: baseline={baseline_kwh:.1f} kWh, ai={ai_kwh:.1f} kWh, reduction={pct_reduction:.1f}%")
    print(f"Comfort compliance: baseline={base_comfort:.1f}%, ai={ai_comfort:.1f}%")


if __name__ == "__main__":
    build()
