# Implementation Guide

This document explains the technical design of the production pipeline, the confirmation strategy, and the optimization path that took the system below one minute.

## System Goal

The pipeline must do four things at the same time:

1. score every trade
2. keep the confirmation bar strict
3. include all relevant trades inside already confirmed suspicious events
4. finish in under one minute in production mode

That combination drives the architecture.

## Core Design

The pipeline is intentionally hybrid:

- statistical features create context
- explicit rules create interpretability
- strict second-pass confirmation controls false positives
- event expansion increases coverage only after confirmation

The system is therefore not a loose anomaly scraper. It is a confirmation-first surveillance pipeline.

## Data Flow

### 1. Market Preparation

For each symbol the pipeline reads only the columns it needs and computes:

- `mid`
- `ret_1m`
- `ret_5m`
- rolling volume and trade-count z-scores
- daily volume
- close-window markers
- precomputed pump and dump signals
- precomputed minute reversal

This work is done in [prepare_market](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py).

### 2. Trade Preparation

For each symbol the pipeline reads only the required trade columns and computes:

- `notional`
- `signed_qty`
- minute join to market context
- `price_dev_mid`
- `price_dev_close`
- robust quantity and notional z-scores
- rolling z-scores
- intraday z-scores
- wallet activity frequency

This work is done in [prepare_trades](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py).

### 3. Rule Detection

The detector layer uses explicit functions:

- `detect_usdc_peg_breaks`
- `detect_bat_hourly_volume`
- `detect_wash_patterns`
- `detect_structuring`
- `detect_ramping`
- `detect_pump_and_dump`
- `detect_marking_close`
- `detect_spoofing_proxy`
- `apply_isolation_forest`
- `apply_dbscan`

These create the raw behavioral flags and detector-specific scores.

### 4. Scoring

The pipeline combines statistical features and detector outputs into:

- `base_score`
- `final_score`
- raw `violation_type`
- raw `reason`

This happens in [score_symbol](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py).

### 5. First Pass

The first pass is deliberately broad enough to keep likely candidates alive. It uses high-signal shortcuts such as:

- rolling quantity anomalies
- round-trip flags
- peg-break flags
- BAT dead-hour flags

and a medium-signal composite for the next tier.

This happens in [first_pass_candidates](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py).

### 6. Second Pass Confirmation

This is the hard gate. The pipeline confirms only when detector-specific evidence is strong enough.

Examples:

- peg breaks need size and peg-break score
- ramping needs `ramping_score >= 0.90` and supportive intraday size
- wash-like flow needs round-trip or wash evidence with additional support
- coordinated pump needs pump-window evidence plus anomaly support

This happens in [second_pass_confirm](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py).

### 7. Event Expansion

After confirmation, the pipeline pulls in neighboring trades only from the same confirmed event. This is where recall increases without loosening the core gate.

Event expansion is used for:

- confirmed ramping sequences
- confirmed coordinated pump windows
- confirmed round-trip wash sequences
- confirmed structuring buckets
- confirmed wash-like wallet episodes

This happens in [expand_confirmed_sequences](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py).

### 8. Label Resolution

The final label is resolved from the confirmed event, not from the first raw rule that happened to fire.

This prevents mistakes such as:

- wash-confirmed trades staying labeled as ramping
- round-trip wash trades staying labeled as coordinated pump

This happens in:

- [resolve_confirmed_label](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py)
- [normalize_confirmed_candidates](C:/Users/ujjaw/Desktop/UJJAWAL/projects/aerial_view/BITS_Hack/problem3_pipeline.py)

## Why The Final Candidate Set Is Better Than The Earlier One

Earlier versions of the repo had two recurring problems:

1. label drift
2. over-expansion of ramping events

The cleaned production version fixes both:

- labels now match confirmed event context
- ramping events are split by time gap, so one wallet does not create a fake multi-hour ramping episode
- expanded rows now explicitly state that they are part of a confirmed event

Current production output:

- 43 candidate trades
- 30 flagged events
- 30 direct confirmations
- 13 event-expansion rows

## Performance Engineering Journey

The sub-minute result came from changing structure, not from weakening thresholds.

### Iteration 1: Identify the Real Bottlenecks

Profiling showed that the original main cost was not model fitting. It was Python-heavy event scanning:

- pump-and-dump minute scanning
- repeated rule logic over wide per-symbol windows
- expensive output writing

### Iteration 2: Precompute Market Events

Pump and dump context was moved into market preparation so the scoring pass could map trades to event windows instead of rescan minute bars repeatedly.

Effect:

- major reduction in symbol scoring time

### Iteration 3: Replace Loop-Heavy Rule Segments

Key detectors were tightened and restructured:

- ramping became event-based instead of rolling lambda heavy
- structuring became grouped aggregates instead of nested loops
- spoofing began using precomputed reversal context

Effect:

- lower compute cost with the same logic

### Iteration 4: Separate Confirmation From Expansion

Instead of flagging everything directly, the pipeline now:

1. confirms a seed trade or seed event
2. expands only within that event

Effect:

- more complete event coverage
- without changing the confirmation gate

### Iteration 5: Parallelize By Symbol

Each symbol is independent. The production path therefore uses per-symbol worker processes.

Effect:

- the scoring stage becomes bounded by the slowest symbol wave instead of the sum of all symbols

### Iteration 6: Stop Writing Heavy Audit Artifacts By Default

The largest remaining avoidable cost was writing `all_scored_trades.csv`.

That file is useful, but it is not required for the production review workflow. The default run now skips it, and the audit mode makes it opt-in through `--write-all-scored`.

Effect:

- final production runtime dropped well below one minute

## Production Runtime Result

Final production benchmark on this machine:

```bash
python problem3_pipeline.py --output-dir output_problem3 --workers 8
```

Observed runtime:

- 38.57 seconds

Observed outputs:

- `pair_stats.csv`
- `candidate_anomalies.csv`
- `flagged_events.csv`
- `submission.csv`
- `submission_with_labels.csv`

## Production vs Audit Mode

Production mode:

```bash
python problem3_pipeline.py --workers 8
```

- fastest
- enough for review and submission
- does not write `all_scored_trades.csv`

Audit mode:

```bash
python problem3_pipeline.py --workers 8 --write-all-scored
```

- same scoring logic
- slower
- useful when you want to inspect every scored trade

## Operational Recommendation

Use production mode for:

- regular reruns
- competition submission generation
- investigator workflow

Use audit mode only when:

- you are tuning thresholds
- you need the full scored population
- you are debugging event selection decisions

## Output Review Sequence

Recommended review order:

1. `flagged_events.csv`
2. `candidate_anomalies.csv`
3. `submission_with_labels.csv`
4. `submission.csv`

This keeps the human review focused on events first and individual rows second.
