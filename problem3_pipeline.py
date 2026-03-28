from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

try:
    from sklearn.ensemble import IsolationForest
except Exception:  # pragma: no cover - fallback when sklearn is missing
    IsolationForest = None


MARKET_FILES = {
    "BATUSDT": "student-pack\\crypto-market\\Binance_BATUSDT_2026_minute.csv",
    "BTCUSDT": "student-pack\\crypto-market\\Binance_BTCUSDT_2026_minute.csv",
    "DOGEUSDT": "student-pack\\crypto-market\\Binance_DOGEUSDT_2026_minute.csv",
    "ETHUSDT": "student-pack\\crypto-market\\Binance_ETHUSDT_2026_minute.csv",
    "LTCUSDT": "student-pack\\crypto-market\\Binance_LTCUSDT_2026_minute.csv",
    "SOLUSDT": "student-pack\\crypto-market\\Binance_SOLUSDT_2026_minute.csv",
    "USDCUSDT": "student-pack\\crypto-market\\Binance_USDCUSDT_2026_minute.csv",
    "XRPUSDT": "student-pack\\crypto-market\\Binance_XRPUSDT_2026_minute.csv",
}

TRADE_FILES = {
    "BATUSDT": "student-pack\\crypto-trades\\BATUSDT_trades.csv",
    "BTCUSDT": "student-pack\\crypto-trades\\BTCUSDT_trades.csv",
    "DOGEUSDT": "student-pack\\crypto-trades\\DOGEUSDT_trades.csv",
    "ETHUSDT": "student-pack\\crypto-trades\\ETHUSDT_trades.csv",
    "LTCUSDT": "student-pack\\crypto-trades\\LTCUSDT_trades.csv",
    "SOLUSDT": "student-pack\\crypto-trades\\SOLUSDT_trades.csv",
    "USDCUSDT": "student-pack\\crypto-trades\\USDCUSDT_trades.csv",
    "XRPUSDT": "student-pack\\crypto-trades\\XRPUSDT_trades.csv",
}

VIOLATION_PRIORITY = [
    "peg_break",
    "wash_trading",
    "round_trip_wash",
    "aml_structuring",
    "coordinated_pump",
    "ramping",
    "spoofing",
]


@dataclass
class PairData:
    symbol: str
    market: pd.DataFrame
    trades: pd.DataFrame


def robust_zscore(series: pd.Series) -> pd.Series:
    median = series.median()
    mad = (series - median).abs().median()
    if pd.isna(mad) or mad == 0:
        std = series.std(ddof=0)
        if pd.isna(std) or std == 0:
            return pd.Series(0.0, index=series.index)
        return (series - series.mean()) / std
    return 0.6745 * (series - median) / mad


def safe_rank(series: pd.Series) -> pd.Series:
    if series.empty:
        return series
    min_val = series.min()
    max_val = series.max()
    if pd.isna(min_val) or pd.isna(max_val) or min_val == max_val:
        return pd.Series(0.0, index=series.index)
    return (series - min_val) / (max_val - min_val)


def prepare_market(symbol: str, path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["Date"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["symbol"] = symbol
    base_asset = next(col for col in df.columns if col.startswith("Volume ") and not col.endswith("USDT"))
    quote_asset = next(col for col in df.columns if col.endswith("USDT"))
    df = df.rename(
        columns={
            base_asset: "volume_base",
            quote_asset: "volume_quote",
            "tradecount": "tradecount",
        }
    )
    df["mid"] = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["date"] = df["timestamp"].dt.date.astype(str)
    df["hour"] = df["timestamp"].dt.hour
    df["volume_quote_hour"] = df.groupby("date")["volume_quote"].transform(
        lambda s: s.rolling(60, min_periods=1).sum()
    )
    df["tradecount_hour"] = df.groupby("date")["tradecount"].transform(
        lambda s: s.rolling(60, min_periods=1).sum()
    )
    return df


def prepare_trades(symbol: str, path: str, market: pd.DataFrame) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["symbol"] = symbol
    df["date"] = df["timestamp"].dt.date.astype(str)
    df["hour"] = df["timestamp"].dt.hour
    df["minute"] = df["timestamp"].dt.floor("min")
    df["notional"] = df["price"] * df["quantity"]
    df["signed_qty"] = np.where(df["side"].str.upper().eq("BUY"), df["quantity"], -df["quantity"])
    df["side_num"] = np.where(df["side"].str.upper().eq("BUY"), 1, -1)

    minute_market = market[["timestamp", "mid", "Close", "volume_quote", "tradecount"]].rename(
        columns={"timestamp": "minute"}
    )
    df = df.merge(minute_market, on="minute", how="left")
    df["price_dev_mid"] = (df["price"] - df["mid"]) / df["mid"].replace(0, np.nan)
    df["price_dev_close"] = (df["price"] - df["Close"]) / df["Close"].replace(0, np.nan)

    df["qty_z"] = robust_zscore(np.log1p(df["quantity"]))
    df["notional_z"] = robust_zscore(np.log1p(df["notional"]))
    df["price_dev_z"] = robust_zscore(df["price_dev_mid"].fillna(0.0))
    df["wallet_trade_count"] = df.groupby("trader_id")["trade_id"].transform("count")
    df["wallet_freq_z"] = robust_zscore(df["wallet_trade_count"])

    intraday_baseline = (
        df.groupby("hour")[["quantity", "notional"]]
        .agg(["mean", "std"])
        .reset_index()
    )
    intraday_baseline.columns = ["hour", "qty_hour_mean", "qty_hour_std", "notional_hour_mean", "notional_hour_std"]
    df = df.merge(intraday_baseline, on="hour", how="left")
    df["qty_intraday_z"] = (
        (df["quantity"] - df["qty_hour_mean"]) / df["qty_hour_std"].replace(0, np.nan)
    ).fillna(0.0)
    df["notional_intraday_z"] = (
        (df["notional"] - df["notional_hour_mean"]) / df["notional_hour_std"].replace(0, np.nan)
    ).fillna(0.0)
    return df


def build_stats(pair_data: Iterable[PairData]) -> pd.DataFrame:
    rows: list[dict] = []
    for pair in pair_data:
        rows.append(
            {
                "symbol": pair.symbol,
                "market_close_mean": pair.market["Close"].mean(),
                "market_close_std": pair.market["Close"].std(),
                "market_close_min": pair.market["Close"].min(),
                "market_close_max": pair.market["Close"].max(),
                "trade_price_mean": pair.trades["price"].mean(),
                "trade_price_std": pair.trades["price"].std(),
                "trade_price_min": pair.trades["price"].min(),
                "trade_price_max": pair.trades["price"].max(),
                "trade_qty_mean": pair.trades["quantity"].mean(),
                "trade_qty_std": pair.trades["quantity"].std(),
                "trade_qty_min": pair.trades["quantity"].min(),
                "trade_qty_max": pair.trades["quantity"].max(),
                "trade_notional_mean": pair.trades["notional"].mean(),
                "trade_notional_std": pair.trades["notional"].std(),
                "trade_count": len(pair.trades),
            }
        )
    return pd.DataFrame(rows).sort_values("symbol")


def detect_usdc_peg_breaks(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["peg_abs_deviation"] = (out["price"] - 1.0).abs()
    out["flag_peg_break"] = out["peg_abs_deviation"] > 0.005
    out["score_peg_break"] = (
        safe_rank(out["peg_abs_deviation"]).fillna(0.0) * 0.7
        + safe_rank(out["notional_z"].clip(lower=0)).fillna(0.0) * 0.3
    )
    return out


def detect_bat_hourly_volume(df: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    hourly = (
        market.assign(hour_bucket=market["timestamp"].dt.floor("h"))
        .groupby("hour_bucket", as_index=False)
        .agg(volume_quote=("volume_quote", "sum"))
    )
    median_hourly = hourly["volume_quote"].median()
    threshold = median_hourly * 5 if median_hourly > 0 else hourly["volume_quote"].quantile(0.95)
    hot_hours = set(hourly.loc[hourly["volume_quote"] > threshold, "hour_bucket"])
    out = df.copy()
    out["hour_bucket"] = out["timestamp"].dt.floor("h")
    out["flag_bat_volume"] = out["hour_bucket"].isin(hot_hours)
    out["score_bat_volume"] = (
        safe_rank(out["notional_z"].clip(lower=0)).fillna(0.0) * 0.6
        + safe_rank(out["qty_z"].clip(lower=0)).fillna(0.0) * 0.4
    )
    return out


def detect_wash_patterns(df: pd.DataFrame, window_minutes: int = 15) -> pd.DataFrame:
    out = df.copy()
    out["flag_round_trip"] = False
    out["flag_wash_like"] = False
    out["round_trip_score"] = 0.0

    for trader_id, group in out.groupby("trader_id", sort=False):
        if len(group) < 2:
            continue
        idx = group.index.to_list()
        ts = group["timestamp"].to_numpy()
        side = group["side_num"].to_numpy()
        qty = group["quantity"].to_numpy()
        price = group["price"].to_numpy()

        local_score = np.zeros(len(group))
        local_flag = np.zeros(len(group), dtype=bool)
        for i in range(len(group)):
            for j in range(i + 1, min(i + 8, len(group))):
                dt_minutes = (ts[j] - ts[i]) / np.timedelta64(1, "m")
                if dt_minutes > window_minutes:
                    break
                if side[i] == side[j]:
                    continue
                qty_ratio = min(qty[i], qty[j]) / max(qty[i], qty[j]) if max(qty[i], qty[j]) > 0 else 0
                price_gap = abs(price[i] - price[j]) / max(price[i], price[j]) if max(price[i], price[j]) > 0 else 0
                if qty_ratio >= 0.9 and price_gap <= 0.0025:
                    score = 0.6 * qty_ratio + 0.4 * (1 - min(price_gap / 0.0025, 1))
                    local_score[i] = max(local_score[i], score)
                    local_score[j] = max(local_score[j], score)
                    local_flag[i] = True
                    local_flag[j] = True
        out.loc[idx, "flag_round_trip"] = local_flag
        out.loc[idx, "round_trip_score"] = local_score

        wallet_net = group["signed_qty"].sum()
        wallet_turnover = group["quantity"].sum()
        if wallet_turnover > 0 and abs(wallet_net) / wallet_turnover < 0.1 and len(group) >= 6:
            out.loc[idx, "flag_wash_like"] = True

    return out


def detect_structuring(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["flag_structuring"] = False
    out["structuring_score"] = 0.0
    for trader_id, group in out.groupby("trader_id", sort=False):
        if len(group) < 6:
            continue
        g = group.copy()
        g["minute_bucket"] = g["timestamp"].dt.floor("15min")
        for _, window in g.groupby("minute_bucket"):
            if len(window) < 4:
                continue
            notional = window["notional"]
            max_to_min = notional.max() / max(notional.min(), 1e-9)
            near_round = ((notional % 1000).abs().clip(upper=1000)).mean()
            if max_to_min <= 1.1 or near_round < 100:
                score = min(1.0, len(window) / 8.0) * 0.6 + max(0.0, 1 - min(max_to_min - 1, 0.2) / 0.2) * 0.4
                out.loc[window.index, "flag_structuring"] = True
                out.loc[window.index, "structuring_score"] = np.maximum(
                    out.loc[window.index, "structuring_score"], score
                )
    return out


def apply_isolation_forest(df: pd.DataFrame, contamination: float = 0.01) -> pd.DataFrame:
    out = df.copy()
    out["iso_score"] = 0.0
    out["flag_iso"] = False
    if IsolationForest is None or len(out) < 50:
        return out

    features = out[["qty_z", "price_dev_z", "wallet_freq_z"]].replace([np.inf, -np.inf], 0.0).fillna(0.0)
    clf = IsolationForest(
        n_estimators=200,
        contamination=contamination,
        random_state=42,
        n_jobs=1,
    )
    clf.fit(features)
    raw = -clf.score_samples(features)
    out["iso_score"] = safe_rank(pd.Series(raw, index=out.index)).fillna(0.0)
    out["flag_iso"] = clf.predict(features) == -1
    return out


def detect_ramping(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["flag_ramping"] = False
    out["ramping_score"] = 0.0
    for trader_id, group in out.groupby("trader_id", sort=False):
        if len(group) < 4:
            continue
        g = group.sort_values("timestamp")
        same_side = g["side_num"].rolling(4).apply(lambda x: 1.0 if len(set(x)) == 1 else 0.0, raw=False)
        monotonic = g["price"].rolling(4).apply(
            lambda x: 1.0 if pd.Series(x).is_monotonic_increasing or pd.Series(x).is_monotonic_decreasing else 0.0,
            raw=False,
        )
        score = ((same_side.fillna(0.0) + monotonic.fillna(0.0)) / 2.0).to_numpy()
        flagged = score >= 1.0
        out.loc[g.index, "flag_ramping"] = np.logical_or(out.loc[g.index, "flag_ramping"], flagged)
        out.loc[g.index, "ramping_score"] = np.maximum(out.loc[g.index, "ramping_score"], score)
    return out


def score_symbol(pair: PairData) -> pd.DataFrame:
    df = pair.trades.copy()
    df["reason"] = ""

    if pair.symbol == "USDCUSDT":
        df = detect_usdc_peg_breaks(df)
    else:
        df["flag_peg_break"] = False
        df["score_peg_break"] = 0.0

    if pair.symbol == "BATUSDT":
        df = detect_bat_hourly_volume(df, pair.market)
    else:
        df["flag_bat_volume"] = False
        df["score_bat_volume"] = 0.0

    df = detect_wash_patterns(df)
    df = detect_structuring(df)
    df = detect_ramping(df)

    if pair.symbol in {"DOGEUSDT", "LTCUSDT", "SOLUSDT"}:
        df = apply_isolation_forest(df, contamination=0.012)
    else:
        df["iso_score"] = 0.0
        df["flag_iso"] = False

    if pair.symbol in {"BTCUSDT", "ETHUSDT"}:
        df["btc_eth_feature_score"] = (
            safe_rank(df["qty_intraday_z"].clip(lower=0))
            + safe_rank(df["notional_intraday_z"].clip(lower=0))
            + safe_rank(df["round_trip_score"].clip(lower=0))
        ) / 3.0
    else:
        df["btc_eth_feature_score"] = 0.0

    df["base_score"] = (
        safe_rank(df["qty_z"].clip(lower=0)).fillna(0.0) * 0.20
        + safe_rank(df["notional_z"].clip(lower=0)).fillna(0.0) * 0.20
        + safe_rank(df["price_dev_z"].abs()).fillna(0.0) * 0.10
        + safe_rank(df["wallet_freq_z"].clip(lower=0)).fillna(0.0) * 0.05
        + df["iso_score"].fillna(0.0) * 0.10
        + df["btc_eth_feature_score"].fillna(0.0) * 0.10
        + df["score_peg_break"].fillna(0.0) * 0.15
        + df["score_bat_volume"].fillna(0.0) * 0.10
    )

    df["violation_type"] = ""
    df.loc[df["flag_peg_break"], "violation_type"] = "peg_break"
    df.loc[df["flag_round_trip"], "violation_type"] = "round_trip_wash"
    df.loc[df["flag_wash_like"], "violation_type"] = "wash_trading"
    df.loc[df["flag_structuring"], "violation_type"] = "aml_structuring"
    df.loc[df["flag_ramping"], "violation_type"] = "ramping"
    df.loc[df["flag_iso"] & df["violation_type"].eq(""), "violation_type"] = "coordinated_pump"
    df.loc[df["violation_type"].eq(""), "violation_type"] = "spoofing"

    # Encourage high-confidence rule-based detections over generic isolation-forest flags.
    df["final_score"] = df["base_score"]
    df.loc[df["flag_round_trip"], "final_score"] += 0.25
    df.loc[df["flag_wash_like"], "final_score"] += 0.20
    df.loc[df["flag_structuring"], "final_score"] += 0.18
    df.loc[df["flag_peg_break"], "final_score"] += 0.35
    df.loc[df["flag_bat_volume"], "final_score"] += 0.18
    df.loc[df["flag_ramping"], "final_score"] += 0.12

    df["reason"] = np.select(
        [
            df["flag_peg_break"],
            df["flag_round_trip"],
            df["flag_wash_like"],
            df["flag_structuring"],
            df["flag_bat_volume"],
            df["flag_ramping"],
            df["flag_iso"],
        ],
        [
            "USDC deviated materially from 1.0000 with meaningful trade size",
            "Same wallet appears to round-trip buy and sell at similar price/size",
            "Wallet shows near-zero net directional flow across repeated trades",
            "Wallet placed many similar-sized trades in a tight time window",
            "Trade landed in an abnormally active BAT hour relative to its dead baseline",
            "Wallet traded in one direction with monotonic price progression",
            "IsolationForest flagged unusual quantity/price/wallet-frequency combination",
        ],
        default="Composite anomaly score exceeded threshold",
    )
    df["date"] = df["timestamp"].dt.date.astype(str)
    return df


def choose_candidates(scored: pd.DataFrame, max_per_symbol: int, score_threshold: float) -> pd.DataFrame:
    selected = scored[scored["final_score"] >= score_threshold].copy()
    selected = selected.sort_values(["symbol", "final_score"], ascending=[True, False])
    selected = selected.groupby("symbol", as_index=False, group_keys=False).head(max_per_symbol)

    # Prefer clearer labels when multiple rows have nearly identical scores.
    selected["violation_rank"] = selected["violation_type"].apply(
        lambda x: VIOLATION_PRIORITY.index(x) if x in VIOLATION_PRIORITY else len(VIOLATION_PRIORITY)
    )
    selected = selected.sort_values(
        ["symbol", "final_score", "violation_rank", "timestamp"], ascending=[True, False, True, True]
    ).reset_index(drop=True)
    return selected


def build_submission(candidates: pd.DataFrame) -> pd.DataFrame:
    submission = candidates[["symbol", "date", "trade_id"]].copy()
    submission = submission.rename(columns={"symbol": "symbol", "date": "date", "trade_id": "trade_id"})
    return submission.drop_duplicates().reset_index(drop=True)


def load_all_pairs() -> list[PairData]:
    pairs: list[PairData] = []
    for symbol in MARKET_FILES:
        market = prepare_market(symbol, MARKET_FILES[symbol])
        trades = prepare_trades(symbol, TRADE_FILES[symbol], market)
        pairs.append(PairData(symbol=symbol, market=market, trades=trades))
    return pairs


def run_pipeline(output_dir: Path, max_per_symbol: int, score_threshold: float) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pairs = load_all_pairs()

    stats_df = build_stats(pairs)
    stats_df.to_csv(output_dir / "pair_stats.csv", index=False)

    scored_parts = []
    for pair in pairs:
        scored = score_symbol(pair)
        scored_parts.append(scored)

    scored_df = pd.concat(scored_parts, ignore_index=True)
    scored_df.sort_values(["symbol", "final_score"], ascending=[True, False]).to_csv(
        output_dir / "all_scored_trades.csv",
        index=False,
    )

    candidates = choose_candidates(scored_df, max_per_symbol=max_per_symbol, score_threshold=score_threshold)
    candidates[
        [
            "symbol",
            "date",
            "trade_id",
            "timestamp",
            "price",
            "quantity",
            "trader_id",
            "violation_type",
            "final_score",
            "reason",
        ]
    ].to_csv(output_dir / "candidate_anomalies.csv", index=False)

    submission = build_submission(candidates)
    submission.to_csv(output_dir / "submission.csv", index=False)

    print(f"Wrote {len(stats_df)} pair stats rows to {output_dir / 'pair_stats.csv'}")
    print(f"Wrote {len(scored_df)} scored trades to {output_dir / 'all_scored_trades.csv'}")
    print(f"Wrote {len(candidates)} candidate anomalies to {output_dir / 'candidate_anomalies.csv'}")
    print(f"Wrote {len(submission)} submission rows to {output_dir / 'submission.csv'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Problem 3 crypto anomaly pipeline")
    parser.add_argument(
        "--output-dir",
        default="output_problem3",
        help="Directory where pair_stats.csv, candidate_anomalies.csv, all_scored_trades.csv, and submission.csv will be written.",
    )
    parser.add_argument(
        "--max-per-symbol",
        type=int,
        default=40,
        help="Maximum number of flagged trades to keep per symbol after scoring.",
    )
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=0.55,
        help="Minimum final score required before a trade is added to the candidate set.",
    )
    args = parser.parse_args()
    run_pipeline(Path(args.output_dir), max_per_symbol=args.max_per_symbol, score_threshold=args.score_threshold)


if __name__ == "__main__":
    main()
