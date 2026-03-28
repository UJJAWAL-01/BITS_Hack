from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
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


def detect_pump_and_dump(df: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["flag_pump_dump"] = False
    out["pump_dump_score"] = 0.0

    minute = market[
        [
            "timestamp",
            "ret_1m",
            "ret_5m",
            "vol_z",
            "tradecount_z",
            "volume_quote_rolling_z",
            "tradecount_rolling_z",
            "Close",
        ]
    ].copy()
    minute["pump_signal"] = (
        (minute["ret_5m"] > minute["ret_5m"].rolling(60, min_periods=10).std().fillna(0) * 2.0)
        & (minute["volume_quote_rolling_z"] > 1.5)
        & (minute["tradecount_rolling_z"] > 1.5)
    )
    minute["dump_signal"] = (
        (minute["ret_1m"] < -minute["ret_1m"].rolling(60, min_periods=10).std().fillna(0) * 2.0)
        & (minute["volume_quote_rolling_z"] > 1.5)
    )

    event_minutes: set[pd.Timestamp] = set()
    for i in range(len(minute) - 2):
        if not bool(minute.iloc[i]["pump_signal"]):
            continue
        next_slice = minute.iloc[i + 1 : i + 3]
        if next_slice["dump_signal"].any():
            start = minute.iloc[max(0, i - 4)]["timestamp"]
            end = next_slice.iloc[-1]["timestamp"]
            event_minutes.update(pd.date_range(start, end, freq="min"))

    out["flag_pump_dump"] = out["minute"].isin(event_minutes)
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

    minute = market[["timestamp", "Close", "vol_z", "tradecount_z"]].copy()
    minute["next_close"] = minute["Close"].shift(-1)
    minute["reversal"] = (minute["next_close"] - minute["Close"]) / minute["Close"].replace(0, np.nan)
    reversal_map = minute.set_index("timestamp")["reversal"]

    out["minute_reversal"] = out["minute"].map(reversal_map).fillna(0.0)
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

        if violation in {"wash_trading", "round_trip_wash", "aml_structuring", "ramping", "spoofing", "layering"}:
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
    df["date"] = df["timestamp"].dt.date.astype(str)
    return df


def choose_candidates(scored: pd.DataFrame, max_per_symbol: int, score_threshold: float) -> pd.DataFrame:
    pass1 = first_pass_candidates(scored)
    selected = pass1[pass1["pass1_candidate"] & (pass1["final_score"] >= score_threshold)].copy()
    selected = second_pass_confirm(selected)
    selected = selected[selected["confirmed"]].copy()

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


def run_pipeline(output_dir: Path, max_per_symbol: int, score_threshold: float) -> None:
    total_start = perf_counter()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[RUN] output_dir={output_dir} max_per_symbol={max_per_symbol} "
        f"score_threshold={score_threshold:.2f}"
    )

    load_start = perf_counter()
    pairs = load_all_pairs()
    load_elapsed = perf_counter() - load_start
    print(f"[TIME] loading_all_pairs={format_seconds(load_elapsed)}")

    stats_start = perf_counter()
    stats_df = build_stats(pairs)
    stats_df.to_csv(output_dir / "pair_stats.csv", index=False)
    stats_elapsed = perf_counter() - stats_start
    print(f"[TIME] pair_stats={format_seconds(stats_elapsed)}")

    # PHASE 6: Parallel symbol scoring using 4 worker processes
    print(f"[PARALLEL] Starting symbol scoring with 4 workers")
    score_start = perf_counter()
    
    with ProcessPoolExecutor(max_workers=4) as executor:
        # Submit all jobs
        futures_map = {executor.submit(_score_symbol_worker, pair): pair for pair in pairs}
        scored_results = {}
        
        # Collect results as they complete (order may vary)
        completed = 0
        for future in as_completed(futures_map):
            pair = futures_map[future]
            try:
                symbol, scored = future.result(timeout=300)
                scored_results[symbol] = scored
                completed += 1
                print(
                    f"[SCORE] {symbol}: scored_rows={len(scored)} "
                    f"(parallel {completed}/8)",
                    flush=True
                )
            except Exception as e:
                print(f"[ERROR] {pair.symbol}: {str(e)}", flush=True)
                raise
    
    # Restore original order
    scored_parts = [scored_results[pair.symbol] for pair in pairs]
    score_elapsed = perf_counter() - score_start
    print(f"[TIME] parallel_scoring_phase={format_seconds(score_elapsed)}")

    concat_start = perf_counter()
    scored_df = pd.concat(scored_parts, ignore_index=True)
    scored_df.sort_values(["symbol", "final_score"], ascending=[True, False]).to_csv(
        output_dir / "all_scored_trades.csv",
        index=False,
    )
    concat_elapsed = perf_counter() - concat_start
    print(f"[TIME] concat_and_write_all_scored={format_seconds(concat_elapsed)}")

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
            "pass1_reason",
            "confirmation_reason",
            "reason",
        ]
    ].to_csv(output_dir / "candidate_anomalies.csv", index=False)
    candidate_elapsed = perf_counter() - candidate_start
    print(
        f"[TIME] candidate_selection={format_seconds(candidate_elapsed)} "
        f"candidate_rows={len(candidates)}"
    )

    events_start = perf_counter()
    events = build_flagged_events(candidates)
    events.to_csv(output_dir / "flagged_events.csv", index=False)
    events_elapsed = perf_counter() - events_start
    print(f"[TIME] flagged_events={format_seconds(events_elapsed)} events_rows={len(events)}")

    submission_start = perf_counter()
    submission = build_submission(candidates)
    submission.to_csv(output_dir / "submission.csv", index=False)
    candidates[["symbol", "date", "trade_id", "violation_type", "reason"]].to_csv(
        output_dir / "submission_with_labels.csv",
        index=False,
    )
    submission_elapsed = perf_counter() - submission_start
    print(
        f"[TIME] submission_files={format_seconds(submission_elapsed)} "
        f"submission_rows={len(submission)}"
    )

    print(f"Wrote {len(stats_df)} pair stats rows to {output_dir / 'pair_stats.csv'}")
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
