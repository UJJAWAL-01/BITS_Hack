# Problem 3 Runbook

This document is the operator guide for the crypto anomaly pipeline.

## Objective

Identify suspicious crypto trades from the challenge dataset and produce:

- a review file for investigators
- an event summary for analysts
- a clean submission file for scoring

## Production Command

Use this when you want the fastest operational run:

```bash
python problem3_pipeline.py --workers 8
```

Expected outputs in `output_problem3/`:

- `pair_stats.csv`
- `candidate_anomalies.csv`
- `flagged_events.csv`
- `submission.csv`
- `submission_with_labels.csv`

Expected runtime on this machine:

- about 39 seconds in the latest benchmark

## Audit Command

Use this only when you need the full per-trade scored dataset:

```bash
python problem3_pipeline.py --workers 8 --write-all-scored
```

Additional output:

- `all_scored_trades.csv`

This is slower because writing that file is expensive.

## Input Data

Market files:

- `student-pack/crypto-market/Binance_BATUSDT_2026_minute.csv`
- `student-pack/crypto-market/Binance_BTCUSDT_2026_minute.csv`
- `student-pack/crypto-market/Binance_DOGEUSDT_2026_minute.csv`
- `student-pack/crypto-market/Binance_ETHUSDT_2026_minute.csv`
- `student-pack/crypto-market/Binance_LTCUSDT_2026_minute.csv`
- `student-pack/crypto-market/Binance_SOLUSDT_2026_minute.csv`
- `student-pack/crypto-market/Binance_USDCUSDT_2026_minute.csv`
- `student-pack/crypto-market/Binance_XRPUSDT_2026_minute.csv`

Trade files:

- `student-pack/crypto-trades/BATUSDT_trades.csv`
- `student-pack/crypto-trades/BTCUSDT_trades.csv`
- `student-pack/crypto-trades/DOGEUSDT_trades.csv`
- `student-pack/crypto-trades/ETHUSDT_trades.csv`
- `student-pack/crypto-trades/LTCUSDT_trades.csv`
- `student-pack/crypto-trades/SOLUSDT_trades.csv`
- `student-pack/crypto-trades/USDCUSDT_trades.csv`
- `student-pack/crypto-trades/XRPUSDT_trades.csv`

## Operational Workflow

1. Read market data with only the required columns.
2. Read trade data with only the required columns.
3. Build market context:
   - returns
   - rolling volume and trade-count z-scores
   - close-window markers
   - precomputed pump/dump event windows
4. Build trade context:
   - notional
   - signed quantity
   - price deviation from minute context
   - rolling and intraday z-scores
   - wallet activity statistics
5. Run symbol-specific and cross-symbol detectors.
6. Apply strict second-pass confirmation.
7. Expand only within already confirmed events.
8. Resolve the final label from the confirmed event.
9. Write the outputs.

## Detection Logic

The pipeline combines fast statistical features with explicit surveillance rules:

- `peg_break`: stablecoin deviation from 1.0000 with size
- `ramping`: same-wallet monotonic same-direction sequences
- `wash_trading`: repeated near-zero net directional flow
- `round_trip_wash`: matched opposite-side trades in a short window
- `coordinated_pump`: trades inside confirmed pump-and-dump windows
- `spoofing`: price deviation followed by reversal

Some detectors are symbol-specific by design:

- `USDCUSDT`: peg-break logic
- `BATUSDT`: dead-hour volume context
- `DOGEUSDT`, `LTCUSDT`, `SOLUSDT`: additional Isolation Forest and DBSCAN support
- `BTCUSDT`, `ETHUSDT`: stronger intraday feature weighting

## Output File Meanings

`pair_stats.csv`

- one row per symbol
- baseline statistics for sanity checks and threshold context

`candidate_anomalies.csv`

- the operational review file
- one row per suspicious trade kept for review or submission
- includes:
  - `violation_type`
  - `final_score`
  - `selection_source`
  - `event_anchor_trade_id`
  - `confirmation_reason`
  - `reason`

`flagged_events.csv`

- event-level grouping of the candidate set
- useful for analyst review before final submission

`submission.csv`

- minimal scoring file: `symbol,date,trade_id`

`submission_with_labels.csv`

- same trade set plus the resolved `violation_type` and explanation

## Review Guidance

Recommended review order:

1. Start with `flagged_events.csv`.
2. Open the highest-score events first.
3. Cross-check the related trades in `candidate_anomalies.csv`.
4. Use `submission_with_labels.csv` as the final review artifact before submission.

## Current Output Snapshot

Current canonical output in `output_problem3/`:

- 43 candidate trades
- 30 flagged events
- 30 direct confirmed anchor events
- 13 event-expansion trades attached to already confirmed events

Current label mix:

- 23 `ramping`
- 8 `wash_trading`
- 6 `coordinated_pump`
- 3 `peg_break`
- 2 `round_trip_wash`
- 1 `spoofing`

## Troubleshooting

If runtime is above 1 minute:

1. Make sure you are not using `--write-all-scored`.
2. Use `--workers 8` if your machine has 8 logical cores available.
3. Close other CPU-heavy applications.
4. Keep the output directory on a local disk, not a network drive.

If you want a smaller submission:

1. Raise `--score-threshold`.
2. Lower `--max-per-symbol`.
3. Do not edit the confirmation logic first; adjust the final filters first.
