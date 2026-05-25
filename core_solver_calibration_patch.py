"""
Paste/import this patch after your core solver exports the daily dataframe.

Usage inside your project:

    from core_solver_calibration_patch import apply_daily_calibration_patch

    daily_df = run_scenario_model(...)
    corrected_df = apply_daily_calibration_patch(
        daily_df,
        calibration_json="calibration_coefficients.json"
    )

This patch assumes your daily_df contains the same component columns produced by your solver:
thermal_hvac_kwh_period, fan_kwh_period, pump_kwh_period, auxiliary_kwh_period,
plus date/month/day_of_year or year/day_of_year.
"""

from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd


def _ensure_calendar(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
        out["calendar_year"] = out["date"].dt.year
        out["month"] = out["date"].dt.month
        out["day_of_year"] = out["date"].dt.dayofyear
    else:
        if "month" not in out.columns:
            # If only day_of_year is available, approximate month using a dummy non-leap calendar.
            if "day_of_year" not in out.columns:
                raise ValueError("daily_df must contain date, month, or day_of_year.")
            dummy = pd.to_datetime("2021-01-01") + pd.to_timedelta(out["day_of_year"].astype(int) - 1, unit="D")
            out["month"] = dummy.dt.month
        if "day_of_year" not in out.columns and "date" in out.columns:
            out["day_of_year"] = pd.to_datetime(out["date"]).dt.dayofyear
    return out


def _standardize_solver_columns(df: pd.DataFrame) -> pd.DataFrame:
    raw = df.copy()
    out = raw.copy()

    def pick(*cols: str, required: bool = True) -> pd.Series:
        for c in cols:
            if c in raw.columns:
                return pd.to_numeric(raw[c], errors="coerce").fillna(0.0)
        if required:
            raise ValueError(f"Missing one of required columns: {cols}")
        return pd.Series(np.zeros(len(raw)), index=raw.index)

    out["model_thermal"] = pick("thermal_hvac_kwh_period", "model_thermal")
    out["model_fan"] = pick("fan_kwh_period", "model_fan")
    out["model_pump"] = pick("pump_kwh_period", "model_pump")
    out["model_aux"] = pick("auxiliary_kwh_period", "model_aux")
    out["model_total"] = pick("energy_kwh_day", "energy_kwh_period", "model_total", required=False)
    if float(out["model_total"].abs().sum()) == 0.0:
        out["model_total"] = out[["model_thermal", "model_fan", "model_pump", "model_aux"]].sum(axis=1)
    return out


def _feature_matrix(df: pd.DataFrame, base_col: str, feature_names: list[str]) -> np.ndarray:
    doy = pd.to_numeric(df.get("day_of_year", 1), errors="coerce").fillna(1).astype(float).values
    values = {
        "intercept": np.ones(len(df)),
        "base_corrected_MWh": df[base_col].values / 1000.0,
        "T_amb_C": pd.to_numeric(df.get("T_amb_C", 0), errors="coerce").fillna(0).values,
        "T_amb_C_squared": pd.to_numeric(df.get("T_amb_C", 0), errors="coerce").fillna(0).values ** 2,
        "T_max_C": pd.to_numeric(df.get("T_max_C", 0), errors="coerce").fillna(0).values,
        "RH_mean_pct": pd.to_numeric(df.get("RH_mean_pct", 0), errors="coerce").fillna(0).values,
        "GHI_mean_Wm2": pd.to_numeric(df.get("GHI_mean_Wm2", 0), errors="coerce").fillna(0).values,
        "occ": pd.to_numeric(df.get("occ", 0), errors="coerce").fillna(0).values,
        "Q_cool_kw": pd.to_numeric(df.get("Q_cool_kw", 0), errors="coerce").fillna(0).values,
        "Q_heat_kw": pd.to_numeric(df.get("Q_heat_kw", 0), errors="coerce").fillna(0).values,
        "sin_doy": np.sin(2 * np.pi * doy / 365.0),
        "cos_doy": np.cos(2 * np.pi * doy / 365.0),
    }
    for m in range(1, 12):
        values[f"month_{m}"] = (df["month"].values == m).astype(float)
    return np.vstack([values[name] for name in feature_names]).T


def apply_daily_calibration_patch(
    daily_df: pd.DataFrame,
    calibration_json: str | Path,
    use_residual_layer: bool = True,
) -> pd.DataFrame:
    """Apply final daily calibration to a core-solver daily dataframe."""
    with open(calibration_json, "r", encoding="utf-8") as f:
        calib = json.load(f)
    out = _standardize_solver_columns(daily_df)
    out = _ensure_calendar(out)

    comp = calib["component_seasonal_calibration"]
    thermal = comp["thermal_month_factor"]
    scale = comp["component_scale"]
    month_factor = out["month"].astype(int).astype(str).map(thermal).astype(float).fillna(1.0)
    out["corrected_component_seasonal_kwh"] = (
        out["model_thermal"] * month_factor
        + out["model_fan"] * float(scale["fan_scale"])
        + out["model_pump"] * float(scale["pump_scale"])
        + out["model_aux"] * float(scale["aux_scale"])
    ).clip(lower=0.0)

    if use_residual_layer and "residual_correction" in calib:
        residual = calib["residual_correction"]
        base_col = residual["base_col"]
        X = _feature_matrix(out, base_col, residual["feature_names"])
        mu = np.asarray(residual["feature_mean_except_intercept"], dtype=float)
        sd = np.asarray(residual["feature_std_except_intercept"], dtype=float)
        coef = np.asarray(residual["coef"], dtype=float)
        X[:, 1:] = (X[:, 1:] - mu) / sd
        out["corrected_final_kwh"] = np.maximum(out[base_col].values + X @ coef, 0.0)
    else:
        out["corrected_final_kwh"] = out["corrected_component_seasonal_kwh"]

    # Keep original solver total for traceability and add replacement column for reports.
    out["energy_kwh_day_original_solver"] = out["model_total"]
    out["energy_kwh_day_refined"] = out["corrected_final_kwh"]
    return out
