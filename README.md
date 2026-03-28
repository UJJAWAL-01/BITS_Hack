# BITS Hack Trade Surveillance Pipeline

This repository contains a productionized pipeline for Problem 3 of the hackathon: identify suspicious crypto trades from minute-level market data and synthetic trade logs, then generate a reviewable candidate set and a submission file.

The repository is intentionally cleaned down to one canonical output directory, `output_problem3/`, and one canonical execution path, [problem3_pipeline.py](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py).

## Current Production Result

Using the bundled dataset and the production command below, the pipeline currently:

- scores all 19,254 trades across 8 symbols
- confirms and expands to 43 suspicious trades across 30 flagged events
- writes only the operational output files by default
- completes in 38.57 seconds on this machine with 8 workers

## Quick Start

Prerequisites:

- Python 3.9+
- `pandas`
- `numpy`
- `scikit-learn`

Install dependencies:

```bash
pip install pandas numpy scikit-learn
```

Run the production workflow:

```bash
python problem3_pipeline.py --workers 8
```

This writes the latest production outputs to `output_problem3/`:

- `pair_stats.csv`
- `candidate_anomalies.csv`
- `flagged_events.csv`
- `submission.csv`
- `submission_with_labels.csv`

Run the slower audit workflow if you need the full scored trade dump:

```bash
python problem3_pipeline.py --workers 8 --write-all-scored
```

That additional CSV is useful for offline analysis, but it is intentionally disabled by default because it pushes runtime up.

## Step-By-Step Workflow

1. Load the 8 market files and 8 trade files from `student-pack/`.
2. Build market context once per symbol: returns, rolling volume, trade count, pump/dump windows, close-window markers.
3. Build trade context once per trade: notional, intraday z-scores, wallet frequency, price deviation from market context.
4. Run rule detectors for peg breaks, BAT dead-hour activity, wash-like flow, round-trip behavior, structuring, ramping, pump windows, close manipulation, and spoofing/layering proxies.
5. Confirm only the trades that survive strict second-pass checks.
6. Expand only inside already confirmed events so related trades are included without loosening the gate.
7. Resolve the final label from the confirmed event, not from whichever raw rule fired first.
8. Write the review and submission outputs.

## Why The Pipeline Looks Like This

The design goal was not to maximize raw anomaly count. The design goal was:

- high signal quality
- explicit reasoning per trade
- event-level grouping for review
- sub-minute production runtime

That led to two operating modes:

- production mode: fast, review-ready, no full scored dump
- audit mode: same scoring, extra `all_scored_trades.csv`, slower

## Repository Layout

- [problem3_pipeline.py](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py): production pipeline
- [output_problem3/](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/output_problem3): canonical latest outputs
- [README_problem3.md](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/README_problem3.md): operator runbook
- [README_IMPLEMENTATION.md](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/README_IMPLEMENTATION.md): technical implementation details
- [DOCUMENTATION_INDEX.md](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/DOCUMENTATION_INDEX.md): documentation map
- [student-pack/](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/student-pack): provided challenge data and problem statements

## Canonical Output Policy

`output_problem3/` is the only tracked output directory in the cleaned repository.

Experimental directories such as `output_robust_*`, `output_label_audit_*`, and other trial runs are intentionally removed from versioned state. If you need to run experiments locally, use a temporary output directory; it will be ignored by git.
