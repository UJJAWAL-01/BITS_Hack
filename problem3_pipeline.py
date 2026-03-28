from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import os
from pathlib import Path
from time import perf_counter
from typing import Iterable

import numpy as np
import pandas as pd

try:
    from sklearn.ensemble import IsolationForest
except Exception:  # pragma: no cover - fallback when sklearn is missing
    IsolationForest = None

try:
    from sklearn.cluster import DBSCAN, KMeans
    from sklearn.preprocessing import StandardScaler
except Exception:  # pragma: no cover - fallback when sklearn is missing
    DBSCAN = None
    KMeans = None
    StandardScaler = None


MARKET_FILES = {
    "BATUSDT": "student-pack/crypto-market/Binance_BATUSDT_2026_minute.csv",
    "BTCUSDT": "student-pack/crypto-market/Binance_BTCUSDT_2026_minute.csv",
    "DOGEUSDT": "student-pack/crypto-market/Binance_DOGEUSDT_2026_minute.csv",
    "ETHUSDT": "student-pack/crypto-market/Binance_ETHUSDT_2026_minute.csv",
    "LTCUSDT": "student-pack/crypto-market/Binance_LTCUSDT_2026_minute.csv",
    "SOLUSDT": "student-pack/crypto-market/Binance_SOLUSDT_2026_minute.csv",
    "USDCUSDT": "student-pack/crypto-market/Binance_USDCUSDT_2026_minute.csv",
    "XRPUSDT": "student-pack/crypto-market/Binance_XRPUSDT_2026_minute.csv",
}

TRADE_FILES = {
    "BATUSDT": "student-pack/crypto-trades/BATUSDT_trades.csv",
    "BTCUSDT": "student-pack/crypto-trades/BTCUSDT_trades.csv",
    "DOGEUSDT": "student-pack/crypto-trades/DOGEUSDT_trades.csv",
    "ETHUSDT": "student-pack/crypto-trades/ETHUSDT_trades.csv",
    "LTCUSDT": "student-pack/crypto-trades/LTCUSDT_trades.csv",
    "SOLUSDT": "student-pack/crypto-trades/SOLUSDT_trades.csv",
    "USDCUSDT": "student-pack/crypto-trades/USDCUSDT_trades.csv",
    "XRPUSDT": "student-pack/crypto-trades/XRPUSDT_trades.csv",
}

VIOLATION_PRIORITY = [
    "peg_break",
    "wash_trading",
    "round_trip_wash",
    "aml_structuring",
    "marking_close",
    "coordinated_pump",
    "ramping",
    "spoofing",
    "layering",
]


@dataclass
class PairData:
    symbol: str
    market: pd.DataFrame
    trades: pd.DataFrame


def format_seconds(seconds: float) -> str:
    return f"{seconds:.2f}s"


def robust_zscore(series: pd.Series) -> pd.Series:
    median = series.median()
    mad = (series - median).abs().median()
    if pd.isna(mad) or mad == 0:
        std = series.std(ddof=0)
        if pd.isna(std) or std == 0:
            return pd.Series(0.0, index=series.index)
        return (series - series.mean()) / std
    return 0.6745 * (series - median) / mad


def flag_by_zscore(series: pd.Series, threshold: float = 3.0) -> pd.Series:
    std = series.std(ddof=0)
    if pd.isna(std) or std == 0:
        return pd.Series(False, index=series.index)
    z = (series - series.mean()) / std
    return z.abs() > threshold


def flag_by_iqr(series: pd.Series, multiplier: float = 3.0) -> pd.Series:
    q1 = series.quantile(0.25)
    q3 = series.quantile(0.75)
    iqr = q3 - q1
    if pd.isna(iqr) or iqr == 0:
        return pd.Series(False, index=series.index)
    return (series < q1 - multiplier * iqr) | (series > q3 + multiplier * iqr)


def rolling_zscore(series: pd.Series, window: int, min_periods: int = 5) -> pd.Series:
    rolling_mean = series.rolling(window=window, min_periods=min_periods).mean()
    rolling_std = series.rolling(window=window, min_periods=min_periods).std(ddof=0)
    return ((series - rolling_mean) / rolling_std.replace(0, np.nan)).fillna(0.0)


def safe_rank(series: pd.Series) -> pd.Series:
    if series.empty:
        return series
    min_val = series.min()
    max_val = series.max()
    if pd.isna(min_val) or pd.isna(max_val) or min_val == max_val:
        return pd.Series(0.0, index=series.index)
    return (series - min_val) / (max_val - min_val)


def as_bool(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool)


def expand_seed_windows(seed_indices: np.ndarray, size: int, lookback: int, lookahead: int) -> np.ndarray:
    mask = np.zeros(size, dtype=bool)
    for idx in seed_indices:
        start = max(0, idx - lookback)
        end = min(size, idx + lookahead + 1)
        mask[start:end] = True
    return mask


def contiguous_event_ids(mask: np.ndarray | pd.Series, index: pd.Index) -> pd.Series:
    arr = np.asarray(mask, dtype=bool)
    starts = arr & ~np.concatenate(([False], arr[:-1]))
    ids = np.cumsum(starts)
    return pd.Series(ids * arr, index=index, dtype="int64")


def prepare_market(symbol: str, path: str) -> pd.DataFrame:
    base_asset = symbol.removesuffix("USDT")
    df = pd.read_csv(
        path,
        usecols=["Date", "High", "Low", "Close", f"Volume {base_asset}", "Volume USDT", "tradecount"],
        parse_dates=["Date"],
        memory_map=True,
    )
    df["timestamp"] = df["Date"]
    if not df["timestamp"].is_monotonic_increasing:
        df = df.sort_values("timestamp").reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
    df["symbol"] = symbol
    df = df.rename(
        columns={
            f"Volume {base_asset}": "volume_base",
            "Volume USDT": "volume_quote",
            "tradecount": "tradecount",
        }
    )
    df["mid"] = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["date"] = df["timestamp"].dt.date.astype(str)
    df["hour"] = df["timestamp"].dt.hour
    df["minute_of_day"] = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
    df["volume_quote_hour"] = df.groupby("date")["volume_quote"].transform(
        lambda s: s.rolling(60, min_periods=1).sum()
    )
    df["tradecount_hour"] = df.groupby("date")["tradecount"].transform(
        lambda s: s.rolling(60, min_periods=1).sum()
    )
    df["ret_1m"] = df["Close"].pct_change().fillna(0.0)
    df["ret_5m"] = df["Close"].pct_change(5).fillna(0.0)
    df["vol_z"] = robust_zscore(np.log1p(df["volume_quote"]))
    df["tradecount_z"] = robust_zscore(np.log1p(df["tradecount"]))
    df["volume_quote_rolling_mean"] = df["volume_quote"].rolling(window=60, min_periods=5).mean()
    df["volume_quote_rolling_std"] = df["volume_quote"].rolling(window=60, min_periods=5).std(ddof=0)
    df["volume_quote_rolling_z"] = (
        (df["volume_quote"] - df["volume_quote_rolling_mean"]) / df["volume_quote_rolling_std"].replace(0, np.nan)
    ).fillna(0.0)
    df["tradecount_rolling_z"] = rolling_zscore(np.log1p(df["tradecount"]), window=60)
    df["daily_volume_quote"] = df.groupby("date")["volume_quote"].transform("sum")
    df["is_close_window"] = df.groupby("date")["timestamp"].transform(lambda s: s == s.max()) | (df["minute_of_day"] >= 1430)
    df["ret_1m_std_60"] = df["ret_1m"].rolling(60, min_periods=10).std(ddof=0).fillna(0.0)
    df["ret_5m_std_60"] = df["ret_5m"].rolling(60, min_periods=10).std(ddof=0).fillna(0.0)
    df["pump_signal"] = (
        (df["ret_5m"] > df["ret_5m_std_60"] * 2.0)
        & (df["volume_quote_rolling_z"] > 1.5)
        & (df["tradecount_rolling_z"] > 1.5)
    )
    df["dump_signal"] = (
        (df["ret_1m"] < -df["ret_1m_std_60"] * 2.0)
        & (df["volume_quote_rolling_z"] > 1.5)
    )
    pump_seed = df["pump_signal"] & (
        df["dump_signal"].shift(-1, fill_value=False) | df["dump_signal"].shift(-2, fill_value=False)
    )
    pump_window_mask = expand_seed_windows(
        np.flatnonzero(pump_seed.to_numpy()),
        size=len(df),
        lookback=4,
        lookahead=2,
    )
    df["pump_dump_event_id"] = contiguous_event_ids(pump_window_mask, index=df.index)
    df["minute_reversal"] = ((df["Close"].shift(-1) - df["Close"]) / df["Close"].replace(0, np.nan)).fillna(0.0)
    return df


def prepare_trades(symbol: str, path: str, market: pd.DataFrame) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        usecols=["trade_id", "timestamp", "price", "quantity", "side", "trader_id"],
        parse_dates=["timestamp"],
        memory_map=True,
    )
    if not df["timestamp"].is_monotonic_increasing:
        df = df.sort_values("timestamp").reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
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
    daily_market = market[["date", "daily_volume_quote"]].drop_duplicates()
    df = df.merge(daily_market, on="date", how="left")
    df["price_dev_mid"] = (df["price"] - df["mid"]) / df["mid"].replace(0, np.nan)
    df["price_dev_close"] = (df["price"] - df["Close"]) / df["Close"].replace(0, np.nan)

    df["qty_z"] = robust_zscore(np.log1p(df["quantity"]))
    df["notional_z"] = robust_zscore(np.log1p(df["notional"]))
    df["price_dev_z"] = robust_zscore(df["price_dev_mid"].fillna(0.0))
    df["qty_rolling_mean"] = df["quantity"].rolling(window=20, min_periods=5).mean()
    df["qty_rolling_std"] = df["quantity"].rolling(window=20, min_periods=5).std(ddof=0)
    df["qty_rolling_z"] = ((df["quantity"] - df["qty_rolling_mean"]) / df["qty_rolling_std"].replace(0, np.nan)).fillna(0.0)
    df["notional_rolling_z"] = rolling_zscore(np.log1p(df["notional"]), window=20)
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
    df["qty_iqr_flag"] = flag_by_iqr(np.log1p(df["quantity"])).astype(int)
    df["notional_iqr_flag"] = flag_by_iqr(np.log1p(df["notional"])).astype(int)
    df["qty_z_flag"] = flag_by_zscore(np.log1p(df["quantity"])).astype(int)
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


def build_stats_row(pair: PairData) -> dict:
    return {
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
    out["round_trip_event_id"] = 0
    out["round_trip_event_size"] = 0

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
        flagged_positions = np.flatnonzero(local_flag)
        if len(flagged_positions) > 0:
            position_breaks = np.where(
                (np.diff(flagged_positions) > 1)
                | ((ts[flagged_positions[1:]] - ts[flagged_positions[:-1]]) / np.timedelta64(1, "m") > window_minutes)
            )[0] + 1
            for event_id, segment in enumerate(np.split(flagged_positions, position_breaks), start=1):
                segment_idx = [idx[pos] for pos in segment]
                out.loc[segment_idx, "round_trip_event_id"] = event_id
                out.loc[segment_idx, "round_trip_event_size"] = len(segment)

        wallet_net = group["signed_qty"].sum()
        wallet_turnover = group["quantity"].sum()
        if wallet_turnover > 0 and abs(wallet_net) / wallet_turnover < 0.1 and len(group) >= 6:
            out.loc[idx, "flag_wash_like"] = True

    return out


def detect_structuring(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["structuring_bucket"] = out["timestamp"].dt.floor("15min")
    out["round_distance"] = (out["notional"] % 1000).abs().clip(upper=1000)
    bucket_stats = (
        out.groupby(["trader_id", "structuring_bucket"], sort=False)
        .agg(
            trade_count=("trade_id", "count"),
            notional_max=("notional", "max"),
            notional_min=("notional", "min"),
            near_round=("round_distance", "mean"),
        )
        .reset_index()
    )
    bucket_stats["max_to_min"] = bucket_stats["notional_max"] / bucket_stats["notional_min"].clip(lower=1e-9)
    bucket_stats["flag_structuring"] = (
        (bucket_stats["trade_count"] >= 4)
        & ((bucket_stats["max_to_min"] <= 1.1) | (bucket_stats["near_round"] < 100))
    )
    bucket_stats["structuring_score"] = (
        np.minimum(1.0, bucket_stats["trade_count"] / 8.0) * 0.6
        + np.maximum(0.0, 1 - np.minimum(bucket_stats["max_to_min"] - 1, 0.2) / 0.2) * 0.4
    )
    out = out.merge(
        bucket_stats[
            [
                "trader_id",
                "structuring_bucket",
                "flag_structuring",
                "structuring_score",
                "trade_count",
            ]
        ],
        on=["trader_id", "structuring_bucket"],
        how="left",
    )
    out["flag_structuring"] = as_bool(out["flag_structuring"])
    out["structuring_score"] = out["structuring_score"].fillna(0.0)
    out["structuring_event_size"] = np.where(out["flag_structuring"], out["trade_count"].fillna(0), 0).astype(int)
    out = out.drop(columns=["round_distance", "trade_count"])
    return out


def apply_isolation_forest(df: pd.DataFrame, contamination: float = 0.01) -> pd.DataFrame:
    out = df.copy()
    out["iso_score"] = 0.0
    out["flag_iso"] = False
    if IsolationForest is None or len(out) < 50:
        return out

    features = out[["qty_z", "price_dev_z", "wallet_freq_z"]].replace([np.inf, -np.inf], 0.0).fillna(0.0)
    features = features.clip(lower=-10, upper=10)
    # PHASE 5 BALANCED: Reduce n_estimators from 200 to 100 for 2x speedup (minimal accuracy loss)
    clf = IsolationForest(
        n_estimators=100,
        contamination=contamination,
        random_state=42,
        n_jobs=1,
    )
    clf.fit(features)
    raw = -clf.score_samples(features)
    out["iso_score"] = safe_rank(pd.Series(raw, index=out.index)).fillna(0.0)
    out["flag_iso"] = clf.predict(features) == -1
    return out


def apply_dbscan(df: pd.DataFrame, eps: float = 0.9, min_samples: int = 15) -> pd.DataFrame:
    # PHASE 5 BALANCED: Increase min_samples from 8 to 15 for faster clustering
    out = df.copy()
    out["dbscan_noise"] = False
    if DBSCAN is None or StandardScaler is None or len(out) < min_samples * 2:
        return out
    features = out[["qty_rolling_z", "price_dev_z", "wallet_freq_z"]].replace([np.inf, -np.inf], 0.0).fillna(0.0)
    features = features.clip(lower=-10, upper=10)
    X = StandardScaler().fit_transform(features)
    labels = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(X)
    out["dbscan_noise"] = labels == -1
    return out


def apply_kmeans_distance(df: pd.DataFrame, n_clusters: int = 4) -> pd.DataFrame:
    # PHASE 5 BALANCED: Reduce clusters from 5 to 4 and n_init from 10 to 5 for faster convergence
    out = df.copy()
    out["kmeans_far"] = False
    out["kmeans_distance_score"] = 0.0
    if KMeans is None or StandardScaler is None or len(out) < max(20, n_clusters * 5):
        return out
    features = out[["qty_rolling_z", "price_dev_z", "wallet_freq_z"]].replace([np.inf, -np.inf], 0.0).fillna(0.0)
    features = features.clip(lower=-10, upper=10)
    X = StandardScaler().fit_transform(features)
    model = KMeans(n_clusters=n_clusters, random_state=42, n_init=5)
    model.fit(X)
    distances = model.transform(X).min(axis=1)
    distance_series = pd.Series(distances, index=out.index)
    threshold = distance_series.quantile(0.95)
    out["kmeans_distance_score"] = safe_rank(distance_series).fillna(0.0)
    out["kmeans_far"] = distance_series > threshold
    return out


def detect_ramping(df: pd.DataFrame, max_gap_minutes: int = 90) -> pd.DataFrame:
    out = df.copy()
    out["flag_ramping"] = False
    out["ramping_score"] = 0.0
    out["ramping_event_id"] = 0
    out["ramping_event_size"] = 0
    for trader_id, group in out.groupby("trader_id", sort=False):
        if len(group) < 4:
            continue
        g = group.sort_values("timestamp")
        side = g["side_num"].to_numpy()
        price = g["price"].to_numpy()
        same_side_window = (
            (side[3:] == side[2:-1])
            & (side[2:-1] == side[1:-2])
            & (side[1:-2] == side[:-3])
        )
        price_diff = np.diff(price)
        monotonic_window = (
            ((price_diff[:-2] >= 0) & (price_diff[1:-1] >= 0) & (price_diff[2:] >= 0))
            | ((price_diff[:-2] <= 0) & (price_diff[1:-1] <= 0) & (price_diff[2:] <= 0))
        )
        qualifying_window_ends = np.flatnonzero(same_side_window & monotonic_window) + 3
        if len(qualifying_window_ends) == 0:
            continue
        trade_flags = np.zeros(len(g), dtype=bool)
        for end_idx in qualifying_window_ends:
            trade_flags[end_idx - 3 : end_idx + 1] = True
        out.loc[g.index[trade_flags], "flag_ramping"] = True
        out.loc[g.index[trade_flags], "ramping_score"] = 1.0

        flagged_positions = np.flatnonzero(trade_flags)
        flagged_ts = g["timestamp"].to_numpy()[flagged_positions]
        position_breaks = np.where(
            (np.diff(flagged_positions) > 1)
            | ((flagged_ts[1:] - flagged_ts[:-1]) / np.timedelta64(1, "m") > max_gap_minutes)
        )[0] + 1
        for event_id, segment in enumerate(np.split(flagged_positions, position_breaks), start=1):
            segment_idx = g.index[segment]
            out.loc[segment_idx, "ramping_event_id"] = event_id
            out.loc[segment_idx, "ramping_event_size"] = len(segment)
    return out


def detect_pump_and_dump(df: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["flag_pump_dump"] = False
    out["pump_dump_score"] = 0.0
    event_id_map = market.set_index("timestamp")["pump_dump_event_id"]
    out["pump_dump_event_id"] = out["minute"].map(event_id_map).fillna(0).astype(int)
    out["flag_pump_dump"] = out["pump_dump_event_id"] > 0
    out["pump_dump_event_size"] = 0
    active_event_mask = out["pump_dump_event_id"] > 0
    if active_event_mask.any():
        out.loc[active_event_mask, "pump_dump_event_size"] = (
            out.loc[active_event_mask]
            .groupby("pump_dump_event_id")["trade_id"]
            .transform("count")
            .astype(int)
        )
    out["pump_dump_score"] = (
        safe_rank(out["qty_z"].clip(lower=0)).fillna(0.0) * 0.4
        + safe_rank(out["notional_z"].clip(lower=0)).fillna(0.0) * 0.3
        + safe_rank(out["wallet_freq_z"].clip(lower=0)).fillna(0.0) * 0.3
    )
    return out


def detect_marking_close(df: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["flag_marking_close"] = False
    out["marking_close_score"] = 0.0
    close_minutes = set(market.loc[market["is_close_window"], "timestamp"])
    out["flag_marking_close"] = out["minute"].isin(close_minutes)

    close_window_notional = out.loc[out["flag_marking_close"]].groupby("date")["notional"].transform("sum")
    out["close_window_share"] = 0.0
    close_mask = out["flag_marking_close"] & close_window_notional.notna()
    out.loc[close_mask, "close_window_share"] = (
        out.loc[close_mask, "notional"] / close_window_notional.loc[close_mask].replace(0, np.nan)
    ).fillna(0.0)
    out.loc[:, "day_volume_share"] = (out["notional"] / out["daily_volume_quote"].replace(0, np.nan)).fillna(0.0)
    out["flag_marking_close"] = out["flag_marking_close"] & (
        (out["close_window_share"] > 0.20) | (out["day_volume_share"] > 0.05)
    ) & (out["price_dev_close"].abs() > 0.001)
    out["marking_close_score"] = (
        safe_rank(out["close_window_share"]).fillna(0.0) * 0.5
        + safe_rank(out["day_volume_share"]).fillna(0.0) * 0.3
        + safe_rank(out["price_dev_close"].abs()).fillna(0.0) * 0.2
    )
    return out


def detect_spoofing_proxy(df: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["flag_spoofing_proxy"] = False
    out["flag_layering_proxy"] = False
    out["spoofing_score"] = 0.0
    out["minute_reversal"] = out["minute"].map(market.set_index("timestamp")["minute_reversal"]).fillna(0.0)
    out["flag_spoofing_proxy"] = (
        (out["qty_z"] > 1.8)
        & (out["price_dev_close"].abs() > 0.0015)
        & (out["minute_reversal"].abs() > 0.0015)
        & (np.sign(out["price_dev_close"].fillna(0.0)) != np.sign(out["minute_reversal"].fillna(0.0)))
    )

    burst = (
        out.groupby(["trader_id", "minute"])["trade_id"]
        .count()
        .rename("trades_in_minute")
        .reset_index()
    )
    out = out.merge(burst, on=["trader_id", "minute"], how="left")
    out["flag_layering_proxy"] = out["flag_spoofing_proxy"] & (out["trades_in_minute"] >= 3)
    out["spoofing_score"] = (
        safe_rank(out["qty_z"].clip(lower=0)).fillna(0.0) * 0.4
        + safe_rank(out["price_dev_close"].abs()).fillna(0.0) * 0.3
        + safe_rank(out["minute_reversal"].abs()).fillna(0.0) * 0.3
    )
    return out


def build_flagged_events(scored: pd.DataFrame) -> pd.DataFrame:
    event_rows: list[dict] = []
    for violation in [
        "peg_break",
        "wash_trading",
        "round_trip_wash",
        "aml_structuring",
        "marking_close",
        "coordinated_pump",
        "ramping",
        "spoofing",
        "layering",
    ]:
        subset = scored[scored["violation_type"] == violation].copy()
        if subset.empty:
            continue

        if "event_anchor_trade_id" in subset.columns and subset["event_anchor_trade_id"].notna().any():
            group_cols = ["symbol", "violation_type", "event_anchor_trade_id"]
        elif violation in {"wash_trading", "round_trip_wash", "aml_structuring", "ramping", "spoofing", "layering"}:
            group_cols = ["symbol", "date", "trader_id"]
        else:
            group_cols = ["symbol", "date"]

        for group_key, group in subset.groupby(group_cols, sort=False):
            event_rows.append(
                {
                    "symbol": group["symbol"].iloc[0],
                    "date": group["date"].iloc[0],
                    "event_start": group["timestamp"].min(),
                    "event_end": group["timestamp"].max(),
                    "violation_type": violation,
                    "trade_count": len(group),
                    "trader_count": group["trader_id"].nunique(),
                    "max_score": group["final_score"].max(),
                    "trade_ids": "|".join(group["trade_id"].head(10)),
                    "remarks": group["reason"].iloc[0],
                }
            )
    if not event_rows:
        return pd.DataFrame(
            columns=[
                "symbol",
                "date",
                "event_start",
                "event_end",
                "violation_type",
                "trade_count",
                "trader_count",
                "max_score",
                "trade_ids",
                "remarks",
            ]
        )
    return pd.DataFrame(event_rows).sort_values(["max_score", "symbol"], ascending=[False, True]).reset_index(drop=True)


def first_pass_candidates(scored: pd.DataFrame) -> pd.DataFrame:
    out = scored.copy()
    out["pass1_candidate"] = False
    out["pass1_reason"] = ""

    high_signal = (
        (out["qty_rolling_z"] >= 2.5)
        | as_bool(out.get("flag_round_trip", pd.Series(False, index=out.index)))
        | as_bool(out.get("flag_peg_break", pd.Series(False, index=out.index)))
        | as_bool(out.get("flag_bat_volume", pd.Series(False, index=out.index)))
    )
    medium_signal = (
        (out["wallet_freq_z"] >= 1.5)
        | (out["notional_intraday_z"] >= 2.0)
        | as_bool(out.get("flag_ramping", pd.Series(False, index=out.index)))
        | as_bool(out.get("flag_marking_close", pd.Series(False, index=out.index)))
        | as_bool(out.get("flag_pump_dump", pd.Series(False, index=out.index)))
    )

    out.loc[high_signal | ((out["final_score"] >= 0.52) & medium_signal), "pass1_candidate"] = True
    out.loc[as_bool(out["flag_peg_break"]), "pass1_reason"] = "high_signal_usdc_peg_break"
    out.loc[as_bool(out["flag_round_trip"]) & out["pass1_reason"].eq(""), "pass1_reason"] = "high_signal_round_trip"
    out.loc[as_bool(out["flag_bat_volume"]) & out["pass1_reason"].eq(""), "pass1_reason"] = "high_signal_bat_dead_hour"
    out.loc[(out["qty_rolling_z"] >= 2.5) & out["pass1_reason"].eq(""), "pass1_reason"] = "high_signal_qty_rolling_z"
    out.loc[out["pass1_candidate"] & out["pass1_reason"].eq(""), "pass1_reason"] = "medium_signal_composite"
    return out


def second_pass_confirm(candidates: pd.DataFrame) -> pd.DataFrame:
    out = candidates.copy()
    out["confirmed"] = False
    out["confirmation_reason"] = ""

    # Strongest, lowest false-positive classes first.
    peg_mask = as_bool(out["flag_peg_break"]) & (out["score_peg_break"] >= 0.55) & (out["notional_z"] >= 0.5)
    out.loc[peg_mask, ["confirmed", "confirmation_reason"]] = [True, "strict_peg_break"]

    bat_mask = (
        as_bool(out["flag_bat_volume"])
        & (out["qty_rolling_z"] >= 1.8)
        & (out["notional_z"] >= 0.8)
    )
    out.loc[bat_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_bat_dead_hour"]

    wash_mask = (
        (
            as_bool(out["flag_round_trip"]) & (out["round_trip_score"] >= 0.75)
        ) | (
            as_bool(out["flag_wash_like"]) & (out["final_score"] >= 0.60) & (out["wallet_freq_z"] >= 0.5)
        )
    )
    out.loc[wash_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_wash"]

    aml_mask = (
        as_bool(out["flag_structuring"])
        & (out["structuring_score"] >= 0.65)
        & (out["wallet_freq_z"] >= 0.5)
    )
    out.loc[aml_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_aml_structuring"]

    marking_close_mask = (
        as_bool(out["flag_marking_close"])
        & (out["marking_close_score"] >= 0.55)
        & (out["day_volume_share"] >= 0.03)
    )
    out.loc[marking_close_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_marking_close"]

    ramping_mask = (
        as_bool(out["flag_ramping"])
        & (out["ramping_score"] >= 0.90)
        & (out["qty_intraday_z"] >= 1.0)
    )
    out.loc[ramping_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_ramping"]

    pump_mask = (
        as_bool(out["flag_pump_dump"])
        & (
            (out["iso_score"] >= 0.55)
            | as_bool(out.get("dbscan_noise", pd.Series(False, index=out.index)))
            | (out["tradecount"] >= out["tradecount"].median())
        )
        & (out["qty_rolling_z"] >= 1.5)
    )
    out.loc[pump_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_coordinated_pump"]

    spoof_mask = (
        as_bool(out["flag_spoofing_proxy"])
        & (out["spoofing_score"] >= 0.60)
        & (out["price_dev_close"].abs() >= 0.0015)
    )
    out.loc[spoof_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_spoofing_proxy"]

    layer_mask = (
        as_bool(out.get("flag_layering_proxy", pd.Series(False, index=out.index)))
        & (out["spoofing_score"] >= 0.55)
        & (out["trades_in_minute"] >= 3)
    )
    out.loc[layer_mask & ~out["confirmed"], ["confirmed", "confirmation_reason"]] = [True, "strict_layering_proxy"]

    # Final safeguard: only allow high composite scores through if a named rule did not already confirm.
    fallback_mask = (out["final_score"] >= 0.72) & ~out["confirmed"]
    out.loc[fallback_mask, ["confirmed", "confirmation_reason"]] = [True, "strict_high_composite"]
    return out


def resolve_confirmed_label(row: pd.Series) -> str:
    confirmation = row.get("confirmation_reason", "")
    if confirmation == "strict_peg_break":
        return "peg_break"
    if confirmation == "strict_coordinated_pump":
        return "coordinated_pump"
    if confirmation == "strict_spoofing_proxy":
        return "spoofing"
    if confirmation == "strict_layering_proxy":
        return "layering"
    if confirmation == "strict_ramping":
        return "ramping"
    if confirmation == "strict_wash":
        if bool(row.get("flag_round_trip", False)):
            return "round_trip_wash"
        return "wash_trading"
    if confirmation == "strict_aml_structuring":
        return "aml_structuring"
    if confirmation == "strict_marking_close":
        return "marking_close"
    if confirmation == "strict_bat_dead_hour":
        if bool(row.get("flag_ramping", False)):
            return "ramping"
        if bool(row.get("flag_spoofing_proxy", False)):
            return "spoofing"
        if bool(row.get("flag_structuring", False)):
            return "aml_structuring"
        if bool(row.get("flag_round_trip", False)):
            return "round_trip_wash"
        if bool(row.get("flag_wash_like", False)):
            return "wash_trading"
        if bool(row.get("flag_pump_dump", False)):
            return "coordinated_pump"
    return row.get("raw_violation_type", row.get("violation_type", "spoofing"))


def build_candidate_reason(row: pd.Series) -> str:
    label = row.get("violation_type", "spoofing")
    selection_source = row.get("selection_source", "direct_confirmation")
    if selection_source == "event_expansion":
        if label == "ramping":
            return "Part of a confirmed ramping event from the same wallet with monotonic price progression"
        if label == "coordinated_pump":
            return "Part of a confirmed coordinated pump event inside the same pump-and-dump window"
        if label == "round_trip_wash":
            return "Part of a confirmed round-trip wash sequence with matched opposite-side trades"
        if label == "wash_trading":
            return "Part of a confirmed wash-trading sequence with near-zero net directional flow"
        if label == "aml_structuring":
            return "Part of a confirmed structuring sequence with repeated similar-sized trades"
        return f"Part of a confirmed {label} event"

    if label == "peg_break":
        return (
            f"Confirmed peg break: price={row['price']:.6f}, deviation={abs(row['price'] - 1.0) * 100:.2f}%, "
            f"score={row.get('score_peg_break', 0.0):.2f}"
        )
    if label == "ramping":
        return (
            f"Confirmed ramping: monotonic same-direction sequence, ramping_score={row.get('ramping_score', 0.0):.2f}, "
            f"qty_intraday_z={row.get('qty_intraday_z', 0.0):.2f}"
        )
    if label == "coordinated_pump":
        return (
            f"Confirmed pump event: trade inside pump-and-dump window, pump_score={row.get('pump_dump_score', 0.0):.2f}, "
            f"qty_rolling_z={row.get('qty_rolling_z', 0.0):.2f}"
        )
    if label == "round_trip_wash":
        return (
            f"Confirmed round-trip wash: opposite-side match within {15} minutes, "
            f"round_trip_score={row.get('round_trip_score', 0.0):.2f}"
        )
    if label == "wash_trading":
        return (
            f"Confirmed wash-like flow: repeated near-zero net directional activity, "
            f"wallet_freq_z={row.get('wallet_freq_z', 0.0):.2f}"
        )
    if label == "aml_structuring":
        return (
            f"Confirmed structuring: repeated similar-size trades in 15-minute bucket, "
            f"structuring_score={row.get('structuring_score', 0.0):.2f}"
        )
    if label == "spoofing":
        return (
            f"Confirmed spoofing proxy: price deviation={abs(row.get('price_dev_close', 0.0)) * 100:.2f}%, "
            f"next-minute reversal={abs(row.get('minute_reversal', 0.0)) * 100:.2f}%"
        )
    if label == "layering":
        return (
            f"Confirmed layering proxy: burst activity with reversal, trades_in_minute={int(row.get('trades_in_minute', 0) or 0)}"
        )
    if label == "marking_close":
        return (
            f"Confirmed marking-the-close: close_window_share={row.get('close_window_share', 0.0):.2f}, "
            f"day_volume_share={row.get('day_volume_share', 0.0):.2f}"
        )
    return row.get("raw_reason", row.get("reason", "Confirmed suspicious trade"))


def normalize_confirmed_candidates(confirmed: pd.DataFrame) -> pd.DataFrame:
    out = confirmed.copy()
    out["violation_type"] = out.apply(resolve_confirmed_label, axis=1)
    out["selection_source"] = "direct_confirmation"
    out["event_anchor_trade_id"] = out["trade_id"]
    out["reason"] = out.apply(build_candidate_reason, axis=1)
    return out


def expand_confirmed_sequences(candidate_pool: pd.DataFrame, confirmed: pd.DataFrame) -> pd.DataFrame:
    if confirmed.empty:
        out = confirmed.copy()
        out["selection_source"] = pd.Series(dtype="object")
        out["event_anchor_trade_id"] = pd.Series(dtype="object")
        return out

    pool = candidate_pool.copy()
    if "confirmed" not in pool.columns:
        pool["confirmed"] = False
    if "confirmation_reason" not in pool.columns:
        pool["confirmation_reason"] = ""

    selected_parts = [confirmed.assign(selection_source="direct_confirmation")]

    def expand_from_keys(mask: pd.Series, key_columns: list[str]) -> None:
        confirmed_subset = (
            confirmed.loc[mask, key_columns + ["event_anchor_trade_id", "violation_type", "final_score"]]
            .sort_values("final_score", ascending=False)
            .drop_duplicates(subset=key_columns)
        )
        if confirmed_subset.empty:
            return
        pool_keys = pd.MultiIndex.from_frame(pool[key_columns])
        confirmed_keys = pd.MultiIndex.from_frame(confirmed_subset[key_columns])
        expanded = pool.loc[pool_keys.isin(confirmed_keys)].copy()
        if expanded.empty:
            return
        expanded = expanded.merge(confirmed_subset, on=key_columns, how="left", suffixes=("", "_anchor"))
        expanded["confirmed"] = True
        expanded["confirmation_reason"] = "expanded_from_confirmed_event"
        expanded["selection_source"] = "event_expansion"
        expanded["event_anchor_trade_id"] = expanded["event_anchor_trade_id"]
        expanded["violation_type"] = expanded["violation_type_anchor"]
        expanded = expanded.drop(columns=["violation_type_anchor", "final_score_anchor"])
        selected_parts.append(expanded)

    expand_from_keys(
        (confirmed["violation_type"] == "ramping") & (confirmed["ramping_event_id"] > 0),
        ["symbol", "date", "trader_id", "ramping_event_id"],
    )
    expand_from_keys(
        (confirmed["violation_type"] == "coordinated_pump") & (confirmed["pump_dump_event_id"] > 0),
        ["symbol", "date", "pump_dump_event_id"],
    )
    expand_from_keys(
        (confirmed["violation_type"] == "round_trip_wash") & (confirmed["round_trip_event_id"] > 0),
        ["symbol", "date", "trader_id", "round_trip_event_id"],
    )
    expand_from_keys(
        (confirmed["violation_type"] == "aml_structuring") & confirmed["structuring_bucket"].notna(),
        ["symbol", "date", "trader_id", "structuring_bucket"],
    )
    expand_from_keys(
        (confirmed["violation_type"] == "wash_trading") & as_bool(confirmed["flag_wash_like"]),
        ["symbol", "date", "trader_id"],
    )

    expanded = pd.concat(selected_parts, ignore_index=True)
    expanded = expanded.sort_values(["symbol", "timestamp", "trade_id", "selection_source"])
    expanded = expanded.drop_duplicates(subset=["symbol", "date", "trade_id"], keep="first")
    expanded["reason"] = expanded.apply(build_candidate_reason, axis=1)
    return expanded.reset_index(drop=True)


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
    df = detect_pump_and_dump(df, pair.market)
    df = detect_marking_close(df, pair.market)
    df = detect_spoofing_proxy(df, pair.market)

    if pair.symbol in {"DOGEUSDT", "LTCUSDT", "SOLUSDT"}:
        df = apply_isolation_forest(df, contamination=0.012)
        df = apply_dbscan(df)
        df["kmeans_far"] = False
        df["kmeans_distance_score"] = 0.0
    else:
        df["iso_score"] = 0.0
        df["flag_iso"] = False
        df["dbscan_noise"] = False
        df["kmeans_far"] = False
        df["kmeans_distance_score"] = 0.0

    if pair.symbol in {"BTCUSDT", "ETHUSDT"}:
        df["btc_eth_feature_score"] = (
            safe_rank(df["qty_intraday_z"].clip(lower=0))
            + safe_rank(df["notional_intraday_z"].clip(lower=0))
            + safe_rank(df["round_trip_score"].clip(lower=0))
        ) / 3.0
    else:
        df["btc_eth_feature_score"] = 0.0

    df["base_score"] = (
        safe_rank(df["qty_z"].clip(lower=0)).fillna(0.0) * 0.12
        + safe_rank(df["qty_rolling_z"].clip(lower=0)).fillna(0.0) * 0.10
        + safe_rank(df["notional_z"].clip(lower=0)).fillna(0.0) * 0.12
        + safe_rank(df["notional_rolling_z"].clip(lower=0)).fillna(0.0) * 0.08
        + safe_rank(df["price_dev_z"].abs()).fillna(0.0) * 0.10
        + safe_rank(df["wallet_freq_z"].clip(lower=0)).fillna(0.0) * 0.05
        + df["iso_score"].fillna(0.0) * 0.10
        + df["kmeans_distance_score"].fillna(0.0) * 0.05
        + df["btc_eth_feature_score"].fillna(0.0) * 0.10
        + df["score_peg_break"].fillna(0.0) * 0.15
        + df["score_bat_volume"].fillna(0.0) * 0.10
        + df["qty_iqr_flag"].fillna(0.0) * 0.02
        + df["notional_iqr_flag"].fillna(0.0) * 0.02
        + df["qty_z_flag"].fillna(0.0) * 0.01
    )

    df["violation_type"] = ""
    df.loc[df["flag_peg_break"], "violation_type"] = "peg_break"
    df.loc[df["flag_layering_proxy"], "violation_type"] = "layering"
    df.loc[df["flag_spoofing_proxy"], "violation_type"] = "spoofing"
    df.loc[df["flag_round_trip"], "violation_type"] = "round_trip_wash"
    df.loc[df["flag_wash_like"], "violation_type"] = "wash_trading"
    df.loc[df["flag_structuring"], "violation_type"] = "aml_structuring"
    df.loc[df["flag_marking_close"], "violation_type"] = "marking_close"
    df.loc[df["flag_pump_dump"], "violation_type"] = "coordinated_pump"
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
    df.loc[df["flag_marking_close"], "final_score"] += 0.15
    df.loc[df["flag_pump_dump"], "final_score"] += 0.20
    df.loc[df["flag_spoofing_proxy"], "final_score"] += 0.12
    df.loc[df["flag_layering_proxy"], "final_score"] += 0.08
    df.loc[df["flag_ramping"], "final_score"] += 0.12
    df.loc[df["dbscan_noise"], "final_score"] += 0.04
    df.loc[df["kmeans_far"], "final_score"] += 0.03

    df["reason"] = np.select(
        [
            df["flag_peg_break"],
            df["flag_layering_proxy"],
            df["flag_spoofing_proxy"],
            df["flag_round_trip"],
            df["flag_wash_like"],
            df["flag_structuring"],
            df["flag_marking_close"],
            df["flag_pump_dump"],
            df["flag_bat_volume"],
            df["flag_ramping"],
            df["flag_iso"],
        ],
        [
            "USDC deviated materially from 1.0000 with meaningful trade size",
            "Repeated same-minute bursts with fast reversal look like a layering proxy",
            "Large trade moved price away from close and the next minute reversed sharply",
            "Same wallet appears to round-trip buy and sell at similar price/size",
            "Wallet shows near-zero net directional flow across repeated trades",
            "Wallet placed many similar-sized trades in a tight time window",
            "Large trade in the final close window had outsized impact on end-of-day pricing",
            "Trade occurred inside a pump-then-fast-dump market window with elevated activity",
            "Trade landed in an abnormally active BAT hour relative to its dead baseline",
            "Wallet traded in one direction with monotonic price progression",
            "IsolationForest flagged unusual quantity/price/wallet-frequency combination",
        ],
        default="Composite anomaly score exceeded threshold",
    )
    df["raw_violation_type"] = df["violation_type"]
    df["raw_reason"] = df["reason"]
    df["date"] = df["timestamp"].dt.date.astype(str)
    return df


def choose_candidates(scored: pd.DataFrame, max_per_symbol: int, score_threshold: float) -> pd.DataFrame:
    pass1 = first_pass_candidates(scored)
    selected = pass1[pass1["pass1_candidate"] & (pass1["final_score"] >= score_threshold)].copy()
    selected = second_pass_confirm(selected)
    selected = selected[selected["confirmed"]].copy()
    selected = normalize_confirmed_candidates(selected)
    selected = expand_confirmed_sequences(pass1, selected)

    # Keep the strategy conservative because false positives are expensive.
    selected = selected.sort_values(["symbol", "final_score"], ascending=[True, False])
    selected = selected.groupby("symbol", as_index=False, group_keys=False).head(max_per_symbol)

    # Prefer clearer labels when multiple rows have nearly identical scores.
    selected["violation_rank"] = selected["violation_type"].apply(
        lambda x: VIOLATION_PRIORITY.index(x) if x in VIOLATION_PRIORITY else len(VIOLATION_PRIORITY)
    )
    selected = selected.sort_values(
        ["symbol", "violation_rank", "final_score", "timestamp"], ascending=[True, True, False, True]
    ).reset_index(drop=True)
    return selected


def build_submission(candidates: pd.DataFrame) -> pd.DataFrame:
    submission = candidates[["symbol", "date", "trade_id"]].copy()
    submission = submission.rename(columns={"symbol": "symbol", "date": "date", "trade_id": "trade_id"})
    return submission.drop_duplicates().reset_index(drop=True)


def load_all_pairs() -> list[PairData]:
    pairs: list[PairData] = []
    for symbol in MARKET_FILES:
        symbol_start = perf_counter()
        market = prepare_market(symbol, MARKET_FILES[symbol])
        trades = prepare_trades(symbol, TRADE_FILES[symbol], market)
        pairs.append(PairData(symbol=symbol, market=market, trades=trades))
        elapsed = perf_counter() - symbol_start
        print(
            f"[LOAD] {symbol}: market_rows={len(market)} trades_rows={len(trades)} "
            f"time={format_seconds(elapsed)}"
        )
    return pairs


def _process_symbol_worker(symbol: str) -> tuple[str, dict, pd.DataFrame, int, int, float, float]:
    load_start = perf_counter()
    market = prepare_market(symbol, MARKET_FILES[symbol])
    trades = prepare_trades(symbol, TRADE_FILES[symbol], market)
    load_elapsed = perf_counter() - load_start
    pair = PairData(symbol=symbol, market=market, trades=trades)
    score_start = perf_counter()
    scored = score_symbol(pair)
    score_elapsed = perf_counter() - score_start
    return (
        symbol,
        build_stats_row(pair),
        scored,
        len(market),
        len(trades),
        load_elapsed,
        score_elapsed,
    )


def run_pipeline(
    output_dir: Path,
    max_per_symbol: int,
    score_threshold: float,
    workers: int,
    write_all_scored: bool,
) -> None:
    total_start = perf_counter()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[RUN] output_dir={output_dir} max_per_symbol={max_per_symbol} "
        f"score_threshold={score_threshold:.2f} workers={workers} "
        f"write_all_scored={write_all_scored}"
    )

    symbol_results: list[tuple[str, dict, pd.DataFrame]] = []
    processing_start = perf_counter()
    if workers <= 1:
        for symbol in MARKET_FILES:
            symbol_name, stats_row, scored, market_rows, trades_rows, load_elapsed, score_elapsed = _process_symbol_worker(symbol)
            symbol_results.append((symbol_name, stats_row, scored))
            print(
                f"[PROCESS] {symbol_name}: market_rows={market_rows} trades_rows={trades_rows} "
                f"load={format_seconds(load_elapsed)} score={format_seconds(score_elapsed)}"
            )
    else:
        completed: dict[str, tuple[str, dict, pd.DataFrame]] = {}
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process_symbol_worker, symbol): symbol for symbol in MARKET_FILES}
            for future in as_completed(futures):
                symbol_name, stats_row, scored, market_rows, trades_rows, load_elapsed, score_elapsed = future.result()
                completed[symbol_name] = (symbol_name, stats_row, scored)
                print(
                    f"[PROCESS] {symbol_name}: market_rows={market_rows} trades_rows={trades_rows} "
                    f"load={format_seconds(load_elapsed)} score={format_seconds(score_elapsed)}"
                )
        for symbol in MARKET_FILES:
            symbol_results.append(completed[symbol])
    processing_elapsed = perf_counter() - processing_start
    print(f"[TIME] symbol_processing={format_seconds(processing_elapsed)}")

    stats_start = perf_counter()
    stats_df = pd.DataFrame([stats_row for _, stats_row, _ in symbol_results]).sort_values("symbol")
    stats_df.to_csv(output_dir / "pair_stats.csv", index=False, float_format="%.8g")
    stats_elapsed = perf_counter() - stats_start
    print(f"[TIME] pair_stats={format_seconds(stats_elapsed)}")

    scored_parts = [scored for _, _, scored in symbol_results]
    concat_start = perf_counter()
    scored_df = pd.concat(scored_parts, ignore_index=True)
    if write_all_scored:
        scored_export_columns = [
            "symbol",
            "date",
            "timestamp",
            "trade_id",
            "trader_id",
            "side",
            "price",
            "quantity",
            "notional",
            "price_dev_mid",
            "price_dev_close",
            "qty_z",
            "qty_rolling_z",
            "notional_z",
            "notional_rolling_z",
            "price_dev_z",
            "wallet_freq_z",
            "qty_intraday_z",
            "notional_intraday_z",
            "score_peg_break",
            "score_bat_volume",
            "round_trip_score",
            "structuring_score",
            "ramping_score",
            "pump_dump_score",
            "marking_close_score",
            "spoofing_score",
            "iso_score",
            "dbscan_noise",
            "flag_peg_break",
            "flag_bat_volume",
            "flag_round_trip",
            "flag_wash_like",
            "flag_structuring",
            "flag_ramping",
            "flag_pump_dump",
            "flag_marking_close",
            "flag_spoofing_proxy",
            "flag_layering_proxy",
            "violation_type",
            "final_score",
            "reason",
        ]
        scored_df[scored_export_columns].to_csv(
            output_dir / "all_scored_trades.csv",
            index=False,
            float_format="%.8g",
        )
    concat_elapsed = perf_counter() - concat_start
    timing_label = "concat_and_write_all_scored" if write_all_scored else "concat_scored"
    print(f"[TIME] {timing_label}={format_seconds(concat_elapsed)}")

    candidate_start = perf_counter()
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
            "selection_source",
            "event_anchor_trade_id",
            "pass1_reason",
            "confirmation_reason",
            "reason",
        ]
    ].to_csv(output_dir / "candidate_anomalies.csv", index=False, float_format="%.8g")
    candidate_elapsed = perf_counter() - candidate_start
    print(
        f"[TIME] candidate_selection={format_seconds(candidate_elapsed)} "
        f"candidate_rows={len(candidates)}"
    )

    events_start = perf_counter()
    events = build_flagged_events(candidates)
    events.to_csv(output_dir / "flagged_events.csv", index=False, float_format="%.8g")
    events_elapsed = perf_counter() - events_start
    print(f"[TIME] flagged_events={format_seconds(events_elapsed)} events_rows={len(events)}")

    submission_start = perf_counter()
    submission = build_submission(candidates)
    submission.to_csv(output_dir / "submission.csv", index=False)
    candidates[["symbol", "date", "trade_id", "violation_type", "reason"]].to_csv(
        output_dir / "submission_with_labels.csv",
        index=False,
        float_format="%.8g",
    )
    submission_elapsed = perf_counter() - submission_start
    print(
        f"[TIME] submission_files={format_seconds(submission_elapsed)} "
        f"submission_rows={len(submission)}"
    )

    print(f"Wrote {len(stats_df)} pair stats rows to {output_dir / 'pair_stats.csv'}")
    if write_all_scored:
        print(f"Wrote {len(scored_df)} scored trades to {output_dir / 'all_scored_trades.csv'}")
    print(f"Wrote {len(candidates)} candidate anomalies to {output_dir / 'candidate_anomalies.csv'}")
    print(f"Wrote {len(events)} flagged events to {output_dir / 'flagged_events.csv'}")
    print(f"Wrote {len(submission)} submission rows to {output_dir / 'submission.csv'}")
    total_elapsed = perf_counter() - total_start
    print(f"[TOTAL TIME] pipeline_completed_in={format_seconds(total_elapsed)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Problem 3 crypto anomaly pipeline")
    parser.add_argument(
        "--output-dir",
        default="output_problem3",
        help="Directory where the production output files will be written.",
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
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(len(MARKET_FILES), os.cpu_count() or 1)),
        help="Number of worker processes to use for per-symbol loading and scoring.",
    )
    parser.add_argument(
        "--write-all-scored",
        action="store_true",
        help="Also write all_scored_trades.csv. Leave off for the fastest production run.",
    )
    args = parser.parse_args()
    run_pipeline(
        Path(args.output_dir),
        max_per_symbol=args.max_per_symbol,
        score_threshold=args.score_threshold,
        workers=args.workers,
        write_all_scored=args.write_all_scored,
    )


if __name__ == "__main__":
    main()
