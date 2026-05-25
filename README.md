# Daily HVAC Solver Refinement Bundle

This bundle applies the recommended refinements to reduce high daily MAPE and CVRMSE when validating the reduced-order HVAC solver against DesignBuilder.

## What it corrects

1. **Daily/calendar shift check**: detects whether the model and DesignBuilder daily series are shifted.
2. **Seasonal thermal correction**: corrects the thermal HVAC layer month-by-month so the model does not underpredict winter/autumn and overpredict early summer.
3. **Component correction**: corrects the physically inconsistent allocation between thermal energy, fan energy, pump energy, and auxiliary energy.
4. **Residual calibration layer**: optional final daily correction using weather, calendar, and load predictors. Use this as a calibration layer, not a replacement for the physics solver.

## Recommended command for your uploaded files

```bash
python run_refinement_pipeline.py \
  --designbuilder_xlsx "ALL DATA - Design builder Data.xlsx" \
  --solver_daily_csv "baseline_no_degradation_daily.csv" \
  --output_dir refined_daily_output \
  --train_years 2020,2021,2022,2023 \
  --validate_years 2024 \
  --max_lag_days 7 \
  --residual_alpha 100
```

To force the detected daily shift correction before calibration, add:

```bash
--apply_shift
```

For your current file, the main error source is not a pure calendar shift; it is seasonal/component mismatch. Therefore, first run without `--apply_shift`, inspect `run_summary.json`, then rerun with `--apply_shift` only if the lag report shows a strong improvement.

## Main outputs

- `validation_metrics_before_after.csv`
- `validation_metrics_holdout.csv`
- `monthly_bias_before_after.csv`
- `corrected_daily_outputs.csv`
- `calibration_coefficients.json`
- `run_summary.json`

## How to paste into the core solver

After your solver produces `daily_df`, import the patch:

```python
from core_solver_calibration_patch import apply_daily_calibration_patch

corrected_daily_df = apply_daily_calibration_patch(
    daily_df,
    calibration_json="calibration_coefficients.json",
    use_residual_layer=True,
)

# Use this column in validation reports:
corrected_daily_df["energy_kwh_day_refined"]
```

For manuscript-level reporting, present two levels:

1. `corrected_component_seasonal_kwh`: the physically interpretable correction layer.
2. `corrected_final_kwh`: the final calibration layer for daily validation.

## Important scientific note

The calibration coefficients are valid for the same building, weather, schedule, and HVAC configuration. If you change geometry, occupancy schedules, HVAC capacity, or weather file, refit the coefficients.
