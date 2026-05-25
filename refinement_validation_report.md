# Daily HVAC Solver Refinement Validation Report

## Files analyzed
- DesignBuilder reference workbook: `ALL DATA - Design builder Data.xlsx`
- PLR solver daily output: `baseline_no_degradation_daily.csv`

## Main conclusion
The PLR modification alone was not enough to reduce daily error. The refinement that actually reduces daily MAPE and CVRMSE is the combined calibration layer:

1. seasonal thermal-HVAC correction,
2. fan/pump/auxiliary component correction,
3. residual daily calibration using weather, load and calendar predictors.

The daily shift detector found a best lag of -7 days within ±7 days, but this only reduced CVRMSE from 47.02% to 45.15% before calibration. Therefore, the dominant problem is not only a pure date shift; it is seasonal/component mismatch.

## All-years validation

| Case | MAPE (%) | CVRMSE (%) | NMBE (%) | Total error (%) |
|---|---:|---:|---:|---:|
| before_PLR_solver | 37.10 | 47.02 | 1.30 | 1.30 |
| component_seasonal_refined | 17.91 | 23.05 | -0.02 | -0.02 |
| final_component_seasonal_plus_residual | 14.67 | 19.29 | -0.04 | -0.04 |

## Holdout validation using 2024

| Case | MAPE (%) | CVRMSE (%) | NMBE (%) | Total error (%) |
|---|---:|---:|---:|---:|
| before_PLR_solver | 37.73 | 47.84 | 1.32 | 1.31 |
| component_seasonal_refined | 18.46 | 24.07 | -0.12 | -0.12 |
| final_component_seasonal_plus_residual | 14.98 | 20.14 | -0.21 | -0.21 |

## Scientific use in thesis
Use the `component_seasonal_refined` result as the physically interpretable correction. Use the `final_component_seasonal_plus_residual` result as the daily validation calibration layer. Do not claim the residual layer is the physical degradation solver; describe it as a validation/calibration layer applied after the reduced-order physics solver.
