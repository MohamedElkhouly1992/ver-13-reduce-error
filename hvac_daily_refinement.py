"""
Daily HVAC validation refinement module.

Purpose
-------
This module refines reduced-order HVAC daily outputs against DesignBuilder / EnergyPlus
reference data. It corrects three issues commonly responsible for high daily MAPE/CVRMSE:

1) calendar/daily shift mismatch,
2) seasonal thermal-load response mismatch,
3) component-energy misallocation between thermal HVAC, fans, pumps, and auxiliary energy.

It preserves the physical solver as the base model and adds a transparent calibration layer.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
import datetime as _dt
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd


XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass
class Metrics:
    n: int
    reference_total_MWh: float
    model_total_MWh: float
    total_difference_MWh: float
    overall_percentage_error_pct: float
    MAPE_pct: float
    WMAPE_pct: float
    CVRMSE_pct: float
    NMBE_pct: float
    MAE_kWh_day: float
    RMSE_kWh_day: float
    mean_daily_percentage_error_pct: float


def _excel_date(serial: float) -> pd.Timestamp:
    # Excel 1900 system with the standard 1899-12-30 origin.
    return pd.Timestamp(_dt.datetime(1899, 12, 30) + _dt.timedelta(days=float(serial)))


def _col_to_idx(col: str) -> int:
    idx = 0
    for ch in col:
        idx = idx * 26 + ord(ch) - 64
    return idx - 1


def _read_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.findall(XLSX_NS + "si"):
        out.append("".join(t.text or "" for t in si.iter(XLSX_NS + "t")))
    return out


def _sheet_name_to_xml(zf: zipfile.ZipFile) -> Dict[str, str]:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {}
    for rel in rels:
        rid_to_target[rel.attrib["Id"]] = "xl/" + rel.attrib["Target"].lstrip("/")
    result = {}
    for sh in wb.find(XLSX_NS + "sheets"):
        name = sh.attrib["name"]
        rid = sh.attrib.get(REL_NS + "id")
        result[name] = rid_to_target.get(rid, "")
    return result


def _read_xlsx_rows(zf: zipfile.ZipFile, sheet_xml: str) -> List[List[str]]:
    shared = _read_shared_strings(zf)
    root = ET.fromstring(zf.read(sheet_xml))
    rows = []
    for row in root.findall(".//" + XLSX_NS + "row"):
        vals = {}
        for cell in row.findall(XLSX_NS + "c"):
            ref = cell.attrib.get("r", "")
            match = re.match(r"([A-Z]+)(\d+)", ref)
            idx = _col_to_idx(match.group(1)) if match else len(vals)
            v = cell.find(XLSX_NS + "v")
            value = "" if v is None else v.text
            if cell.attrib.get("t") == "s" and value != "":
                value = shared[int(value)]
            vals[idx] = value
        if vals:
            rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    return rows


def read_designbuilder_reference(path: str | Path, sheet_name: Optional[str] = None) -> pd.DataFrame:
    """Read DesignBuilder daily reference data from the workbook used in this study.

    The function searches for a sheet that contains:
    System Fans, System Pumps, Auxiliary Energy, Heating (Gas), Cooling (Electricity), Total.

    Returns columns:
        date, db_fan, db_pump, db_aux, db_heat, db_cool, db_thermal, db_total,
        calendar_year, month, day_of_year
    """
    path = Path(path)
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
        return _standardize_reference_df(df)

    with zipfile.ZipFile(path) as zf:
        mapping = _sheet_name_to_xml(zf)
        candidate_names = [sheet_name] if sheet_name else list(mapping.keys())
        best_rows = None
        for name in candidate_names:
            if not name or name not in mapping:
                continue
            rows = _read_xlsx_rows(zf, mapping[name])
            if not rows:
                continue
            header = [str(x).strip() for x in rows[0]]
            joined = " | ".join(header)
            if all(k in joined for k in ["System Fans", "System Pumps", "Heating", "Cooling", "Total"]):
                best_rows = rows
                break
        if best_rows is None:
            # Fallback: scan all sheets.
            for name, xml in mapping.items():
                rows = _read_xlsx_rows(zf, xml)
                if not rows:
                    continue
                header = [str(x).strip() for x in rows[0]]
                joined = " | ".join(header)
                if all(k in joined for k in ["System Fans", "System Pumps", "Heating", "Cooling", "Total"]):
                    best_rows = rows
                    break

    if best_rows is None:
        raise ValueError("Could not find a DesignBuilder component-energy sheet in the workbook.")

    header = [str(x).strip() for x in best_rows[0]]
    idx = {name: i for i, name in enumerate(header)}

    def get(row: List[str], name: str, default: float = 0.0) -> float:
        i = idx.get(name)
        if i is None or i >= len(row) or row[i] == "":
            return default
        return float(row[i])

    recs = []
    for r in best_rows[2:]:  # row 2 is unit row in this workbook
        if not r or r[0] == "":
            continue
        fan = get(r, "System Fans")
        pump = get(r, "System Pumps")
        aux = get(r, "Auxiliary Energy")
        heat = get(r, "Heating (Gas)")
        cool = get(r, "Cooling (Electricity)")
        # Use the physical DesignBuilder component sum instead of the ambiguous duplicated
        # "Total" header, because this workbook also contains a second model-total column.
        total = fan + pump + aux + heat + cool
        recs.append(
            {
                "date": _excel_date(float(r[0])),
                "db_fan": fan,
                "db_pump": pump,
                "db_aux": aux,
                "db_heat": heat,
                "db_cool": cool,
                "db_total": total,
            }
        )
    out = pd.DataFrame(recs)
    out["db_thermal"] = out["db_heat"] + out["db_cool"]
    out["calendar_year"] = out["date"].dt.year
    out["month"] = out["date"].dt.month
    out["day_of_year"] = out["date"].dt.dayofyear
    return out


def _standardize_reference_df(df: pd.DataFrame) -> pd.DataFrame:
    lower = {c.lower().strip(): c for c in df.columns}
    def col(*names: str) -> Optional[str]:
        for name in names:
            if name.lower() in lower:
                return lower[name.lower()]
        return None
    date_col = col("date", "date/time", "datetime")
    total_col = col("db_total", "designbuilder_total", "reference_energy_kwh", "total")
    if date_col is None or total_col is None:
        raise ValueError("Reference CSV must contain a date/date-time column and a total energy column.")
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df[date_col])
    out["db_total"] = pd.to_numeric(df[total_col])
    out["db_fan"] = pd.to_numeric(df[col("db_fan", "system fans")], errors="coerce") if col("db_fan", "system fans") else 0.0
    out["db_pump"] = pd.to_numeric(df[col("db_pump", "system pumps")], errors="coerce") if col("db_pump", "system pumps") else 0.0
    out["db_aux"] = pd.to_numeric(df[col("db_aux", "auxiliary energy")], errors="coerce") if col("db_aux", "auxiliary energy") else 0.0
    out["db_heat"] = pd.to_numeric(df[col("db_heat", "heating (gas)")], errors="coerce") if col("db_heat", "heating (gas)") else 0.0
    out["db_cool"] = pd.to_numeric(df[col("db_cool", "cooling (electricity)")], errors="coerce") if col("db_cool", "cooling (electricity)") else 0.0
    out = out.fillna(0.0)
    out["db_thermal"] = out["db_heat"] + out["db_cool"]
    out["calendar_year"] = out["date"].dt.year
    out["month"] = out["date"].dt.month
    out["day_of_year"] = out["date"].dt.dayofyear
    return out


def load_solver_daily(path: str | Path) -> pd.DataFrame:
    """Load core-solver daily output and standardize component columns."""
    raw = pd.read_csv(path)
    df = raw.copy()

    def pick(*cols: str, required: bool = True) -> pd.Series:
        for c in cols:
            if c in raw.columns:
                return pd.to_numeric(raw[c], errors="coerce").fillna(0.0)
        if required:
            raise ValueError(f"Solver output is missing one of these columns: {cols}")
        return pd.Series(np.zeros(len(raw)), index=raw.index)

    df["model_total"] = pick("energy_kwh_day", "energy_kwh_period", "model_total")
    df["model_thermal"] = pick("thermal_hvac_kwh_period", "model_thermal")
    df["model_fan"] = pick("fan_kwh_period", "model_fan")
    df["model_pump"] = pick("pump_kwh_period", "model_pump")
    df["model_aux"] = pick("auxiliary_kwh_period", "model_aux")
    return df.reset_index(drop=True)


def attach_reference_calendar(model: pd.DataFrame, reference: pd.DataFrame) -> pd.DataFrame:
    out = model.copy().reset_index(drop=True)
    ref = reference.reset_index(drop=True)
    n = min(len(out), len(ref))
    out = out.iloc[:n].copy()
    out["date"] = ref.loc[: n - 1, "date"].values
    out["calendar_year"] = pd.to_datetime(out["date"]).dt.year
    out["month"] = pd.to_datetime(out["date"]).dt.month
    out["day_of_year"] = pd.to_datetime(out["date"]).dt.dayofyear
    return out


def compute_metrics(reference_kwh: Iterable[float], prediction_kwh: Iterable[float]) -> Metrics:
    ref = np.asarray(list(reference_kwh), dtype=float)
    pred = np.asarray(list(prediction_kwh), dtype=float)
    if len(ref) != len(pred):
        raise ValueError("Reference and prediction series must have equal length.")
    err = pred - ref
    mask = np.abs(ref) > 1e-9
    mean_ref = float(np.mean(ref)) if len(ref) else float("nan")
    return Metrics(
        n=int(len(ref)),
        reference_total_MWh=float(np.sum(ref) / 1000),
        model_total_MWh=float(np.sum(pred) / 1000),
        total_difference_MWh=float(np.sum(err) / 1000),
        overall_percentage_error_pct=float(100 * np.sum(err) / np.sum(ref)),
        MAPE_pct=float(100 * np.mean(np.abs(err[mask] / ref[mask]))),
        WMAPE_pct=float(100 * np.sum(np.abs(err)) / np.sum(np.abs(ref))),
        CVRMSE_pct=float(100 * np.sqrt(np.mean(err**2)) / mean_ref),
        NMBE_pct=float(100 * np.sum(err) / ((len(ref) - 1) * mean_ref)) if len(ref) > 1 else float("nan"),
        MAE_kWh_day=float(np.mean(np.abs(err))),
        RMSE_kWh_day=float(np.sqrt(np.mean(err**2))),
        mean_daily_percentage_error_pct=float(100 * np.mean(err[mask] / ref[mask])),
    )


def find_best_daily_shift(reference_kwh: Iterable[float], model_kwh: Iterable[float], max_lag_days: int = 7) -> Dict[str, object]:
    """Find best lag by CVRMSE.

    Convention: lag = +d compares reference[t] with model[t+d].
    If lag = -d, the model values are effectively shifted forward by d days to align with reference.
    """
    ref = np.asarray(list(reference_kwh), dtype=float)
    mod = np.asarray(list(model_kwh), dtype=float)
    best = None
    for lag in range(-max_lag_days, max_lag_days + 1):
        if lag < 0:
            r = ref[-lag:]
            p = mod[: len(mod) + lag]
        elif lag > 0:
            r = ref[: len(ref) - lag]
            p = mod[lag:]
        else:
            r = ref
            p = mod
        if len(r) < 30:
            continue
        met = compute_metrics(r, p)
        row = {"lag_days": lag, "metrics": asdict(met)}
        if best is None or met.CVRMSE_pct < best["metrics"]["CVRMSE_pct"]:
            best = row
    return best or {"lag_days": 0, "metrics": asdict(compute_metrics(ref, mod))}


def shift_model_components(model: pd.DataFrame, lag_days: int) -> pd.DataFrame:
    """Shift solver components to correct calendar misalignment.

    Uses edge forward/backward filling only at boundaries. Keep a copy of original columns if needed.
    """
    out = model.copy()
    if lag_days == 0:
        return out
    shift_amount = -lag_days
    cols = [c for c in ["model_total", "model_thermal", "model_fan", "model_pump", "model_aux"] if c in out]
    for c in cols:
        out[c] = out[c].shift(shift_amount).bfill().ffill()
    return out


def fit_component_seasonal_calibration(
    reference: pd.DataFrame,
    model: pd.DataFrame,
    train_years: Optional[List[int]] = None,
) -> Dict[str, object]:
    """Fit transparent calibration coefficients.

    Thermal energy is corrected with month-specific factors; fan/pump/auxiliary are corrected
    using global component scale factors to avoid excessive overfitting.
    """
    data = pd.concat(
        [
            reference.reset_index(drop=True),
            model[["model_total", "model_thermal", "model_fan", "model_pump", "model_aux"]].reset_index(drop=True),
        ],
        axis=1,
    )
    if train_years:
        train = data[data["calendar_year"].isin(train_years)].copy()
    else:
        train = data.copy()
    if train.empty:
        raise ValueError("No training rows selected for calibration.")

    eps = 1e-9
    thermal_month = {}
    for month in range(1, 13):
        sub = train[train["month"] == month]
        denominator = max(float(sub["model_thermal"].sum()), eps)
        thermal_month[str(month)] = float(sub["db_thermal"].sum() / denominator) if len(sub) else 1.0

    component_scale = {
        "fan_scale": float(train["db_fan"].sum() / max(train["model_fan"].sum(), eps)),
        "pump_scale": float(train["db_pump"].sum() / max(train["model_pump"].sum(), eps)),
        "aux_scale": float(train["db_aux"].sum() / max(train["model_aux"].sum(), eps)),
    }

    return {
        "method": "component_seasonal",
        "train_years": train_years if train_years else "all",
        "thermal_month_factor": thermal_month,
        "component_scale": component_scale,
    }


def apply_component_seasonal_calibration(model: pd.DataFrame, calibration: Dict[str, object]) -> pd.Series:
    thermal = calibration["thermal_month_factor"]
    scale = calibration["component_scale"]
    month_factor = model["month"].astype(int).astype(str).map(thermal).astype(float).fillna(1.0)
    corrected = (
        model["model_thermal"] * month_factor
        + model["model_fan"] * float(scale["fan_scale"])
        + model["model_pump"] * float(scale["pump_scale"])
        + model["model_aux"] * float(scale["aux_scale"])
    )
    return corrected.clip(lower=0.0)


def _make_residual_matrix(df: pd.DataFrame, base_col: str) -> Tuple[np.ndarray, List[str]]:
    doy = df["day_of_year"].astype(float).values
    features = [
        ("intercept", np.ones(len(df))),
        ("base_corrected_MWh", df[base_col].values / 1000.0),
        ("T_amb_C", pd.to_numeric(df.get("T_amb_C", 0), errors="coerce").fillna(0).values),
        ("T_amb_C_squared", pd.to_numeric(df.get("T_amb_C", 0), errors="coerce").fillna(0).values ** 2),
        ("T_max_C", pd.to_numeric(df.get("T_max_C", 0), errors="coerce").fillna(0).values),
        ("RH_mean_pct", pd.to_numeric(df.get("RH_mean_pct", 0), errors="coerce").fillna(0).values),
        ("GHI_mean_Wm2", pd.to_numeric(df.get("GHI_mean_Wm2", 0), errors="coerce").fillna(0).values),
        ("occ", pd.to_numeric(df.get("occ", 0), errors="coerce").fillna(0).values),
        ("Q_cool_kw", pd.to_numeric(df.get("Q_cool_kw", 0), errors="coerce").fillna(0).values),
        ("Q_heat_kw", pd.to_numeric(df.get("Q_heat_kw", 0), errors="coerce").fillna(0).values),
        ("sin_doy", np.sin(2 * np.pi * doy / 365.0)),
        ("cos_doy", np.cos(2 * np.pi * doy / 365.0)),
    ]
    for m in range(1, 12):
        features.append((f"month_{m}", (df["month"].values == m).astype(float)))
    names = [n for n, _ in features]
    X = np.vstack([v for _, v in features]).T
    return X, names


def fit_residual_correction(
    merged: pd.DataFrame,
    base_col: str = "corrected_component_seasonal_kwh",
    train_years: Optional[List[int]] = None,
    alpha: float = 100.0,
) -> Dict[str, object]:
    """Fit ridge residual correction on top of component-seasonal calibration."""
    train = merged[merged["calendar_year"].isin(train_years)].copy() if train_years else merged.copy()
    X, names = _make_residual_matrix(train, base_col)
    y = train["db_total"].values - train[base_col].values
    mu = X[:, 1:].mean(axis=0)
    sd = X[:, 1:].std(axis=0)
    sd[sd == 0] = 1.0
    Xs = X.copy()
    Xs[:, 1:] = (Xs[:, 1:] - mu) / sd
    A = Xs.T @ Xs + alpha * np.eye(Xs.shape[1])
    A[0, 0] -= alpha  # do not regularize intercept
    b = Xs.T @ y
    coef = np.linalg.solve(A, b)
    return {
        "method": "ridge_residual_on_component_seasonal",
        "alpha": float(alpha),
        "base_col": base_col,
        "feature_names": names,
        "feature_mean_except_intercept": mu.tolist(),
        "feature_std_except_intercept": sd.tolist(),
        "coef": coef.tolist(),
        "train_years": train_years if train_years else "all",
    }


def apply_residual_correction(merged: pd.DataFrame, residual_model: Dict[str, object]) -> pd.Series:
    base_col = residual_model["base_col"]
    X, names = _make_residual_matrix(merged, base_col)
    expected = residual_model["feature_names"]
    if names != expected:
        raise ValueError("Feature schema mismatch. Refit residual model for this dataset schema.")
    mu = np.asarray(residual_model["feature_mean_except_intercept"], dtype=float)
    sd = np.asarray(residual_model["feature_std_except_intercept"], dtype=float)
    coef = np.asarray(residual_model["coef"], dtype=float)
    Xs = X.copy()
    Xs[:, 1:] = (Xs[:, 1:] - mu) / sd
    pred = merged[base_col].values + Xs @ coef
    return pd.Series(np.maximum(pred, 0.0), index=merged.index)


def build_merged_dataset(reference: pd.DataFrame, model: pd.DataFrame) -> pd.DataFrame:
    n = min(len(reference), len(model))
    ref = reference.iloc[:n].reset_index(drop=True)
    mod = model.iloc[:n].reset_index(drop=True)
    keep_mod = [
        c
        for c in mod.columns
        if c not in {"date", "calendar_year", "month", "day_of_year"}
    ]
    merged = pd.concat([ref, mod[keep_mod]], axis=1)
    return merged


def monthly_bias_table(merged: pd.DataFrame, pred_cols: List[str]) -> pd.DataFrame:
    rows = []
    for month, sub in merged.groupby("month"):
        row = {
            "month": int(month),
            "reference_MWh": sub["db_total"].sum() / 1000.0,
        }
        for col in pred_cols:
            pred = sub[col]
            row[f"{col}_MWh"] = pred.sum() / 1000.0
            row[f"{col}_bias_pct"] = 100.0 * (pred.sum() - sub["db_total"].sum()) / sub["db_total"].sum()
        rows.append(row)
    return pd.DataFrame(rows).sort_values("month")


def run_pipeline(
    designbuilder_xlsx: str | Path,
    solver_daily_csv: str | Path,
    output_dir: str | Path,
    train_years: Optional[List[int]] = None,
    validate_years: Optional[List[int]] = None,
    max_lag_days: int = 7,
    apply_shift: bool = False,
    residual_alpha: float = 100.0,
) -> Dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reference = read_designbuilder_reference(designbuilder_xlsx)
    model = load_solver_daily(solver_daily_csv)
    model = attach_reference_calendar(model, reference)

    shift_info = find_best_daily_shift(reference["db_total"], model["model_total"], max_lag_days=max_lag_days)
    if apply_shift:
        model = shift_model_components(model, int(shift_info["lag_days"]))
    else:
        shift_info["applied"] = False
    if apply_shift:
        shift_info["applied"] = True

    merged = build_merged_dataset(reference, model)
    before = compute_metrics(merged["db_total"], merged["model_total"])

    comp_calib = fit_component_seasonal_calibration(reference, model, train_years=train_years)
    merged["corrected_component_seasonal_kwh"] = apply_component_seasonal_calibration(model, comp_calib).values
    comp_metrics = compute_metrics(merged["db_total"], merged["corrected_component_seasonal_kwh"])

    residual_model = fit_residual_correction(
        merged,
        base_col="corrected_component_seasonal_kwh",
        train_years=train_years,
        alpha=residual_alpha,
    )
    merged["corrected_final_kwh"] = apply_residual_correction(merged, residual_model)
    final_metrics = compute_metrics(merged["db_total"], merged["corrected_final_kwh"])

    metrics_rows = []
    for name, met in [
        ("before_PLR_solver", before),
        ("component_seasonal_refined", comp_metrics),
        ("final_component_seasonal_plus_residual", final_metrics),
    ]:
        row = {"case": name}
        row.update(asdict(met))
        metrics_rows.append(row)
    metrics_df = pd.DataFrame(metrics_rows)
    metrics_df.to_csv(output_dir / "validation_metrics_before_after.csv", index=False)

    if validate_years:
        val = merged[merged["calendar_year"].isin(validate_years)].copy()
        val_rows = []
        for name, col in [
            ("before_PLR_solver", "model_total"),
            ("component_seasonal_refined", "corrected_component_seasonal_kwh"),
            ("final_component_seasonal_plus_residual", "corrected_final_kwh"),
        ]:
            met = compute_metrics(val["db_total"], val[col])
            row = {"case": name, "subset": f"validation_years_{','.join(map(str, validate_years))}"}
            row.update(asdict(met))
            val_rows.append(row)
        pd.DataFrame(val_rows).to_csv(output_dir / "validation_metrics_holdout.csv", index=False)

    monthly = monthly_bias_table(
        merged,
        ["model_total", "corrected_component_seasonal_kwh", "corrected_final_kwh"],
    )
    monthly.to_csv(output_dir / "monthly_bias_before_after.csv", index=False)

    export_cols = [
        "date", "calendar_year", "month", "day_of_year",
        "db_total", "model_total", "corrected_component_seasonal_kwh", "corrected_final_kwh",
        "db_thermal", "model_thermal", "db_fan", "model_fan", "db_pump", "model_pump", "db_aux", "model_aux",
        "T_amb_C", "T_max_C", "RH_mean_pct", "GHI_mean_Wm2", "occ", "Q_cool_kw", "Q_heat_kw", "COP_eff", "mode",
    ]
    for c in export_cols:
        if c not in merged.columns:
            merged[c] = np.nan
    merged[export_cols].to_csv(output_dir / "corrected_daily_outputs.csv", index=False)

    coeff = {
        "component_seasonal_calibration": comp_calib,
        "residual_correction": residual_model,
        "daily_shift_detection": shift_info,
        "notes": [
            "Use component-seasonal calibration as the physically interpretable correction layer.",
            "Use residual correction only as a validation/calibration layer, not as a replacement for the physics solver.",
            "For future two-axis and three-axis runs, apply the same coefficients to clean/degraded strategy outputs only if building, weather, schedule, and HVAC system assumptions remain unchanged.",
        ],
    }
    with open(output_dir / "calibration_coefficients.json", "w", encoding="utf-8") as f:
        json.dump(coeff, f, indent=2)

    summary = {
        "before": asdict(before),
        "component_seasonal": asdict(comp_metrics),
        "final": asdict(final_metrics),
        "daily_shift_detection": shift_info,
        "output_files": [
            "validation_metrics_before_after.csv",
            "validation_metrics_holdout.csv" if validate_years else None,
            "monthly_bias_before_after.csv",
            "corrected_daily_outputs.csv",
            "calibration_coefficients.json",
        ],
    }
    summary["output_files"] = [x for x in summary["output_files"] if x]
    with open(output_dir / "run_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


def parse_years(text: Optional[str]) -> Optional[List[int]]:
    if text is None or str(text).strip() == "":
        return None
    return [int(x.strip()) for x in str(text).split(",") if x.strip()]


def main() -> None:
    p = argparse.ArgumentParser(description="Refine daily HVAC solver output against DesignBuilder reference data.")
    p.add_argument("--designbuilder_xlsx", required=True, help="Path to DesignBuilder reference workbook or CSV.")
    p.add_argument("--solver_daily_csv", required=True, help="Path to core solver daily CSV output.")
    p.add_argument("--output_dir", default="refined_daily_output", help="Output directory.")
    p.add_argument("--train_years", default=None, help="Comma-separated calibration years, e.g. 2020,2021,2022,2023. Default: all years.")
    p.add_argument("--validate_years", default=None, help="Comma-separated holdout years, e.g. 2024.")
    p.add_argument("--max_lag_days", type=int, default=7, help="Maximum lag for daily shift check.")
    p.add_argument("--apply_shift", action="store_true", help="Apply best detected lag before calibration.")
    p.add_argument("--residual_alpha", type=float, default=100.0, help="Ridge penalty for residual correction.")
    args = p.parse_args()

    summary = run_pipeline(
        designbuilder_xlsx=args.designbuilder_xlsx,
        solver_daily_csv=args.solver_daily_csv,
        output_dir=args.output_dir,
        train_years=parse_years(args.train_years),
        validate_years=parse_years(args.validate_years),
        max_lag_days=args.max_lag_days,
        apply_shift=args.apply_shift,
        residual_alpha=args.residual_alpha,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
