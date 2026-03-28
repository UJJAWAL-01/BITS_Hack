Problem 3 runner:

`python3 problem3_pipeline.py`

Outputs go to `output_problem3/` by default:

- `pair_stats.csv`: quick per-symbol market/trade stats
- `all_scored_trades.csv`: every trade with engineered scores
- `candidate_anomalies.csv`: filtered suspicious trades with reasons and violation labels
- `submission.csv`: final file with `symbol,date,trade_id`

Useful options:

`python3 problem3_pipeline.py --score-threshold 0.60 --max-per-symbol 25`

Notes:

- The script is tuned around the workflow in the problem statement:
  - `USDCUSDT`: peg-break checks
  - `BATUSDT`: dead-hour / abnormal-hour activity
  - `DOGEUSDT`, `LTCUSDT`, `SOLUSDT`: Isolation Forest support
  - `BTCUSDT`, `ETHUSDT`: intraday z-scores and wallet round-trip logic
- Review `candidate_anomalies.csv` before final submission. The competition penalizes false positives, so you may want to raise `--score-threshold` if the file is too large.
