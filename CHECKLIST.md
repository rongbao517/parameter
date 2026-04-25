# CHECKLIST

- [x] Inspect the current parameter-analysis implementation and confirm there is no existing fleet-size parameter.
- [x] Choose and document one explicit interpretation for "vehicle count".
- [x] Create/update the minimal control files required for this pass.
- [x] Patch `parameter.py` to add `NUM_VEHICLES` and its fleet-cap constraint.
- [x] Sync `sensitivity_analysis_plan.md` with the new vehicle-count branch.
- [x] Run a bounded smoke check.
- [x] Record the result and residual caveat.
