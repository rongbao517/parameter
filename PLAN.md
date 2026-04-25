# PLAN

## Objective

Add a vehicle-count sensitivity branch to the existing one-at-a-time UAM parameter analysis in `/Users/wf24018/home/parameter`, parallel to the existing cost, speed, and grid-size analyses.

## Evidence And Files

- `parameter.py`
- `sensitivity_analysis_plan.md`
- Active user requirement: add a vehicle-count sensitivity analysis parallel to the existing flow

## Constraints

- Keep durable files inside the project root.
- Preserve the existing output table structure in `scenario_summary.csv`.
- Keep the analysis as one-at-a-time sensitivity rather than a multi-factor sweep.
- Do not treat seat capacity or vertiport takeoff/landing capacity as the same concept as fleet size.

## Chosen Route

- Interpret `NUM_VEHICLES` as the maximum total number of active UAM vehicles per time interval.
- Implement it as a fleet-cap constraint `sum z[t,p,q] <= NUM_VEHICLES` for each time interval.
- Keep `MIN_AIR_DISTANCE` and `MAX_AIR_DISTANCE` as fixed calibration parameters.
- Remove `SEAT_CAPACITY`, `DEFAULT_DEPARTURE_CAPACITY`, and `DEFAULT_ARRIVAL_CAPACITY` from `SENSITIVITY_VALUES`.
- Add `GRID_CELL_SIZE_KM`, extend `price_air` to include `11.0`, and add `NUM_VEHICLES` with baseline/test values `[10, 20, 40]` around a baseline of `20`.

## Success Criteria

- `parameter.py` generates scenarios for the vehicle-count group.
- The model includes a fleet-size constraint without changing the existing summary-column contract.
- Planning docs reflect the new group and updated scenario count.
- A bounded smoke check passes.

## Main Risk

This fleet constraint is an approximation because the current model does not include aircraft circulation or repositioning across time intervals.

## Downgrade Criterion

If the new constraint creates an inconsistent formulation or cannot be verified cheaply, stop at a documented design patch instead of claiming a trusted run result.
