Equity Bonus Solver README
==========================

File covered by this README:
- equity_bonus_solver.py

Purpose
-------
`equity_bonus_solver.py` is a single Python file that works for both bonus equity problems in the hackathon.

It generates the two submission files:

- p1_alerts.csv for Problem 1
- p2_signals.csv for Problem 2

It is written to work directly with the student-pack folder structure:

- student-pack/equity/market_data.csv
- student-pack/equity/ohlcv.csv
- student-pack/equity/trade_data.csv

For Problem 2 it can also fetch SEC EDGAR 8-K filings live, or read a cached filings CSV if you pass --filings-csv.


What The Script Reads
---------------------
1. market_data.csv
   Used only for Problem 1.
   This contains per-minute order book snapshots with up to 10 bid/ask levels.

2. ohlcv.csv
   Used for both Problem 1 and Problem 2.
   This contains daily OHLCV data and also gives the script the ticker and company name for each sec_id.

3. trade_data.csv
   Used for both Problem 1 and Problem 2.
   This contains individual equity trades and order statuses such as FILLED and CANCELLED.

4. SEC EDGAR 8-K results
   Used only for Problem 2.
   The script either:
   - fetches filings live from https://efts.sec.gov/LATEST/search-index
   - or loads a local filings CSV passed with --filings-csv


What The Script Writes
----------------------
Each run creates a fresh numbered folder inside the output directory, for example:

- out/output_1/
- out/output_2/

Inside that run folder the script writes only the two submission CSV files so you can compare iterations side by side.

1. p1_alerts.csv
   Columns:
   - alert_id
   - sec_id
   - trade_date
   - time_window_start
   - anomaly_type
   - severity
   - remarks
   - time_to_run

2. p2_signals.csv
   Columns:
   - sec_id
   - event_date
   - event_type
   - headline
   - source_url
   - pre_drift_flag
   - suspicious_window_start
   - remarks
   - time_to_run


Runtime Logging
---------------
The script prints runtime to the console for:

- Problem 1
- Problem 2
- total command runtime

How to read the runtime correctly:

- `[P1]` is the Problem 1 runtime only.
- `[P2]` is the Problem 2 runtime only.
- `[TOTAL]` is just the full command runtime after both parts finish.

So the total line should not be confused with a separate scoring runtime for either problem.
It is simply the combined end-to-end time for that run.

On the intended submission workflow, each problem is meant to run individually under 60 seconds:

- Problem 1 is comfortably under 60 seconds on the provided files.
- Problem 2 is under 60 seconds when EDGAR data is reused from cache or supplied through `--filings-csv`.

Problem 2 uses live EDGAR results by default unless you pass `--filings-csv`.
Because EDGAR is an external service, first-run live fetch time can depend on SEC response speed and network conditions.

Example console output:

Wrote /path/to/p1_alerts.csv (4 rows)
[P1] Solver runtime: 1.92s | end-to-end: 1.92s
[P2] Solver runtime: 0.28s | end-to-end: 0.29s
[TOTAL] Runtime: 2.00s


How Problem 1 Works
-------------------
Problem 1 is treated as an order-book anomaly detection task with three main anomaly families:

- order_book_imbalance
- spread_dislocation
- unusual_cancel_pattern

Step 1: Market data preparation
The script reads market_data.csv and computes these order-book features:

- total_bid = sum of bid size levels 1-10
- total_ask = sum of ask size levels 1-10
- total_depth = total_bid + total_ask
- obi = (total_bid - total_ask) / total_depth
- spread_bps = (ask_price_level01 - bid_price_level01) / bid_price_level01 * 10000
- bid_concentration = bid_size_level01 / total_bid
- ask_concentration = ask_size_level01 / total_ask
- top2_bid_share
- top2_ask_share

Step 2: Local rolling baseline
For each sec_id, the script computes a 30-minute rolling median baseline for:

- total_bid
- total_ask
- spread_bps
- bid_concentration
- ask_concentration

Then it computes ratio-to-baseline values such as:

- total_bid_ratio30
- total_ask_ratio30
- spread_bps_ratio30

This lets the script compare a book snapshot to that ticker's recent normal behavior instead of comparing across different securities.

Step 3: Detect one-sided order book events
The script marks a BUY-side local event when all of these are true:

- OBI > 0.80
- bid depth is more than 2.0x its prior 30-minute median
- ask depth is below 1.0x its prior 30-minute median
- bid concentration > 0.55

It marks a SELL-side local event when the mirror image is true:

- OBI < -0.80
- ask depth is more than 2.0x its prior 30-minute median
- bid depth is below 1.0x its prior 30-minute median
- ask concentration > 0.55

Consecutive flagged minutes are grouped into runs.
Only runs of at least 4 minutes are considered.

These runs are labeled:
- order_book_imbalance

Step 4: Detect opening spread dislocations
The script also looks for abnormal spread widening near the market open.
It flags spread_event when all of these are true:

- timestamp is in the opening window: 09:00 to 09:45
- spread_bps is greater than max(250, 5 x that security's median positive spread)
- absolute OBI > 0.70
- max(level-1 bid concentration, level-1 ask concentration) > 0.55
- spread_bps > 0

Consecutive runs of at least 4 minutes are grouped and labeled:
- spread_dislocation

Step 5: Detect suspicious cancel bursts
The script reads trade_data.csv and uses:

- side
- quantity
- trader_id
- order_status
- timestamp

It creates:

- FILLED buy/sell quantities
- CANCELLED buy/sell quantities
- per-minute trade summaries

Then it searches for cancel bursts by (sec_id, trader_id, side).
Within a rolling 12-minute window it flags a candidate if:

- cancel_count >= 4
- and total cancelled quantity >= 1500 shares

Overlapping windows are deduplicated.

Each cancel burst is then checked against nearby order-book context:

- Does the book lean the same way during the cancel burst?
- Is there opposite-side FILLED volume soon after?
- Is there a nearby strong order-book signal already detected?

These are labeled:
- unusual_cancel_pattern

This is intended to catch spoofing-like behavior or manipulation around displayed liquidity.

Step 6: Score and rank Problem 1 candidates
Each candidate gets an internal score based on:

- run length
- OBI strength
- depth expansion relative to local baseline
- concentration at the top of the book
- spread magnitude
- cancel burst size and count
- supporting trade/book context

Severity is assigned as:

- HIGH
- MEDIUM
- LOW

The script then selects a small set of top alerts with type caps so it does not flood the submission with overlapping false positives.

Current caps:

- up to 2 unusual_cancel_pattern alerts
- up to 2 order_book_imbalance alerts
- up to 1 spread_dislocation alert

This is intentional because false positives are expensive in the hackathon scoring.


How Problem 2 Works
-------------------
Problem 2 is treated as an event-study plus trade-surveillance problem.

Goal:
- find material 8-K events
- then check whether unusual trading happened before the event became public

Step 1: Build the ticker universe
The script reads ohlcv.csv and gets:

- sec_id
- ticker
- name

This removes the need for a separate sec_id_map.csv in your current student pack because ticker and company name are already present inside ohlcv.csv.

Step 2: Get SEC 8-K filings
If you do not pass --filings-csv, the script fetches live EDGAR search results.

Use a real SEC-friendly user agent when you run it, for example:

- --user-agent "Your Name your_email@example.com"

For a fresh live pull, the script also uses multiple worker threads for the per-security EDGAR requests.
You can control that with:

- --edgar-workers

For each security it tries up to three search terms:

- cleaned company name
- company name
- ticker

The query is restricted to:

- forms = 8-K
- custom date range equal to the trade_data date range

Before keeping a filing, the script applies a company-match check against:

- ticker
- entity_name
- headline
- filing text

This is meant to reduce false positives from broad EDGAR search hits.

It parses:

- file_date
- source_url
- entity_name
- headline-like text
- form_type

If the SEC response does not contain a direct filing link, the script fills source_url with a valid SEC search URL for that ticker and filing date instead of leaving it blank.

Step 3: Classify event type
The script uses keyword matching, not heavy NLP.

Supported event classes:

- merger
- earnings
- leadership
- restatement
- bankruptcy
- other

Examples:

- "acquisition", "takeover", "merger agreement" -> merger
- "earnings", "eps", "guidance" -> earnings
- "CEO", "appoint", "board", "resign" -> leadership

When multiple filing hits exist for the same (sec_id, ticker, file_date), the script keeps the best event class using a fixed priority:

- merger
- restatement
- earnings
- leadership
- bankruptcy
- other

Step 4: Build the OHLCV baseline
The script sorts ohlcv.csv by sec_id and trade_date and computes:

- daily_return
- vol_15d_mean
- vol_15d_std
- ret_15d_std
- volume_z

This gives a rolling 15-day baseline for volume and return behavior.

Step 5: Define the suspicious pre-announcement window
For each filing date, the script looks at the previous 5 trading days before the filing.

It computes:

- suspicious_window_start = first day in that 5-day window
- suspicious_end = last trading day before the filing
- pre_drift = compounded return across the 5-day pre-event window
- drift_z = size of that drift relative to recent return volatility
- max volume z-score in that pre-event window

Step 6: Check trader-level evidence
The script then inspects FILLED trades from trade_data.csv inside the pre-event window.

For each trader_id and side:

- total_qty in the suspicious window
- max single fill
- number of fills
- prior same-side median quantity before the suspicious window

It scores traders more highly when:

- they suddenly trade much more than their own history
- they have no prior same-side history in that ticker
- the single largest fill is big relative to baseline

The strongest trader-side explanation is included in the final remarks.

Step 7: Score the event
Each filing gets an internal score from:

- event type bonus
- abnormal volume strength
- abnormal pre-drift strength
- trade-level evidence
- whether the dominant trade side matches the direction of the pre-event drift

The script sets:

- pre_drift_flag = 1

only when the total score is strong enough and at least two of these are meaningfully present:

- strong volume
- strong drift
- strong trade evidence

By default the script keeps only flagged events.
If you want to also output clean events with pre_drift_flag = 0, use:

--include-unflagged-events


Important Practical Notes
-------------------------
1. Problem 1 is deliberately conservative
The script prefers a smaller number of stronger alerts because false positives are costly.

2. Problem 2 depends on EDGAR availability
If SEC EDGAR is slow, blocked, or unavailable, use:

--filings-csv /path/to/filings.csv

For the fastest repeat runs, keep and reuse the EDGAR cache generated by the script.

3. The script uses the real schema in your student pack
In your trade_data.csv the useful fields are:

- sec_id
- timestamp
- side
- quantity
- trader_id
- order_status

Extra fields like manager_id and exchange_id are ignored.

4. The script can be tuned
If you want more aggressive or more conservative outputs, the easiest things to tune are:

- OBI thresholds
- baseline ratios
- minimum run length
- cancel burst thresholds
- max number of output rows


How To Run
----------
Run both problems from the same Python file:

python3 equity_bonus_solver.py --student-pack "/Users/chintanshah/Downloads/student-pack" --output-dir "./out"

Run only Problem 1:

python3 equity_bonus_solver.py --student-pack "/Users/chintanshah/Downloads/student-pack" --output-dir "./out" --problems p1

Run only Problem 2 with a cached filings file:

python3 equity_bonus_solver.py --student-pack "/Users/chintanshah/Downloads/student-pack" --output-dir "./out" --problems p2 --filings-csv "./filings.csv"

Recommended submission workflow:

1. Run Problem 2 once with live EDGAR and a real user agent so the cache gets created.
2. Use the same Python file for later comparison runs.
3. Read `[P1]` and `[P2]` as the individual runtimes.
4. Treat `[TOTAL]` only as the combined runtime of the full command.


Summary
-------
This single Python file solves both Problem 1 and Problem 2.

Problem 1 identifies anomalies from:

- one-sided order-book concentration
- spread dislocations near the open
- suspicious cancellation bursts with spoofing-like context

Problem 2 identifies anomalies from:

- 8-K event detection
- abnormal pre-announcement price drift
- abnormal pre-announcement volume
- trader-level abnormal fills before the event

The code is built to produce submission-ready CSVs quickly, explain its reasoning in the remarks field, and keep the individual problem runtimes within the sub-60-second target on the intended workflow.
