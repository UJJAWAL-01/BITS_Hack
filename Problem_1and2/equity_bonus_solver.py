#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


DEFAULT_STUDENT_PACK = Path("/Users/chintanshah/Downloads/student-pack")
MARKET_START_HOUR = 9
MARKET_END_HOUR = 16
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

EVENT_KEYWORDS = {
    "merger": [
        "merger",
        "acquisition",
        "acquired",
        "acquire",
        "takeover",
        "combination",
        "combine",
        "merger agreement",
    ],
    "earnings": [
        "earnings",
        "quarterly results",
        "quarterly",
        "revenue",
        "guidance",
        "eps",
        "results of operations",
    ],
    "leadership": [
        "chief executive",
        "chief financial",
        "ceo",
        "cfo",
        "president",
        "director",
        "board",
        "resign",
        "appointment",
        "appoint",
    ],
    "restatement": [
        "restatement",
        "restate",
        "material weakness",
        "correction",
        "non-reliance",
        "accounting",
    ],
    "bankruptcy": [
        "bankruptcy",
        "chapter 11",
        "chapter 7",
        "insolvency",
        "reorganization",
    ],
}

EVENT_PRIORITY = {
    "merger": 0,
    "restatement": 1,
    "earnings": 2,
    "leadership": 3,
    "bankruptcy": 4,
    "other": 5,
}

def format_elapsed(seconds: float) -> str:
    return f"{seconds:.2f}s"


def build_edgar_search_url(query_term: str, start_date: str, end_date: str) -> str:
    params = {
        "q": f'"{query_term}"',
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
    }
    return f"{EDGAR_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def resolve_sec_source_url(src: dict, query_term: str, start_date: str, end_date: str) -> str:
    direct_fields = [
        "file_path",
        "filing_url",
        "linkToHtml",
        "linkToFilingDetails",
        "linkToTxt",
        "url",
    ]
    for field in direct_fields:
        value = src.get(field, "")
        if not value:
            continue
        value = str(value)
        if value.startswith("http"):
            return value
        if value.startswith("/"):
            return "https://www.sec.gov" + value
        if value.startswith("Archives/") or value.startswith("archives/"):
            return "https://www.sec.gov/" + value.lstrip("/")
    return build_edgar_search_url(query_term=query_term, start_date=start_date, end_date=end_date)


def create_run_output_dir(base_dir: Path) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    existing_numbers: list[int] = []
    for child in base_dir.iterdir():
        if not child.is_dir() or not child.name.startswith("output_"):
            continue
        suffix = child.name.removeprefix("output_")
        if suffix.isdigit():
            existing_numbers.append(int(suffix))

    counter = max(existing_numbers, default=0) + 1
    while True:
        run_dir = base_dir / f"output_{counter}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            counter += 1


def write_submission_csv(df: pd.DataFrame, csv_path: Path) -> Path:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    return csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate p1_alerts.csv and p2_signals.csv for the equity bonus problems."
    )
    parser.add_argument(
        "--student-pack",
        type=Path,
        default=DEFAULT_STUDENT_PACK,
        help="Path to the student-pack root directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="Directory where submission CSVs will be written.",
    )
    parser.add_argument(
        "--problems",
        choices=["p1", "p2", "all"],
        default="all",
        help="Which problem outputs to generate.",
    )
    parser.add_argument(
        "--max-p1-alerts",
        type=int,
        default=4,
        help="Maximum number of Problem 1 alerts to emit.",
    )
    parser.add_argument(
        "--max-p2-signals",
        type=int,
        default=4,
        help="Maximum number of Problem 2 signal rows to emit.",
    )
    parser.add_argument(
        "--include-extended-hours",
        action="store_true",
        help="Use all market_data timestamps instead of focusing on 09:00-16:59.",
    )
    parser.add_argument(
        "--filings-csv",
        type=Path,
        default=None,
        help="Optional cached or hand-curated filings CSV for Problem 2.",
    )
    parser.add_argument(
        "--save-raw-filings",
        type=Path,
        default=None,
        help="Optional path to save raw filings fetched from EDGAR.",
    )
    parser.add_argument(
        "--user-agent",
        default="HackathonEquitySolver/1.0 (student project)",
        help='User-Agent string for EDGAR requests. Use a real identity, e.g. "Your Name your_email@example.com".',
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.02,
        help="Delay in seconds between EDGAR requests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=6.0,
        help="HTTP timeout in seconds for EDGAR requests.",
    )
    parser.add_argument(
        "--include-unflagged-events",
        action="store_true",
        help="Include clean Problem 2 events with pre_drift_flag=0.",
    )
    parser.add_argument(
        "--edgar-workers",
        type=int,
        default=6,
        help="Number of worker threads to use when fetching EDGAR filings.",
    )
    parser.add_argument(
        "--refresh-edgar-cache",
        action="store_true",
        help="Ignore the reusable EDGAR cache and fetch filings live again.",
    )
    return parser.parse_args()


def ensure_student_pack(root: Path) -> tuple[Path, Path]:
    equity_dir = root / "equity"
    docs_dir = root / "docs"
    expected = [
        equity_dir / "market_data.csv",
        equity_dir / "ohlcv.csv",
        equity_dir / "trade_data.csv",
        docs_dir / "problem_statement_p1.md",
        docs_dir / "problem_statement_p2.md",
    ]
    missing = [str(path) for path in expected if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Could not find the expected student-pack files. Missing:\n- "
            + "\n- ".join(missing)
        )
    return equity_dir, docs_dir


def safe_div(num: float, den: float) -> float:
    if den is None or den == 0 or pd.isna(den):
        return np.nan
    return num / den


def pct(value: float) -> str:
    if pd.isna(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def fmt_num(value: float) -> str:
    if pd.isna(value):
        return "n/a"
    return f"{value:,.0f}"


def longest_non_empty(values: Iterable[object]) -> str:
    text_values = [str(v).strip() for v in values if isinstance(v, str) and str(v).strip()]
    if not text_values:
        return ""
    return max(text_values, key=len)


def classify_event(text: str) -> str:
    text = (text or "").lower()
    for event_type, keywords in EVENT_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return event_type
    return "other"


def choose_headline(src: dict, fallback_name: str) -> str:
    candidate_fields: list[str] = []
    for key in (
        "title",
        "display_names",
        "document_description",
        "description",
        "items",
        "entity_name",
    ):
        value = src.get(key)
        if isinstance(value, list):
            candidate_fields.append(" ".join(str(item) for item in value))
        elif value:
            candidate_fields.append(str(value))
    headline = longest_non_empty(candidate_fields)
    if headline:
        return headline
    if fallback_name:
        return f"{fallback_name} 8-K filing"
    return "8-K filing"


def clean_company_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    text = (
        name.replace(",", " ")
        .replace(".", " ")
        .replace("&", " and ")
        .replace("/", " ")
        .strip()
    )
    tokens = [token for token in text.split() if token]
    common_suffixes = {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "llc",
        "ltd",
        "limited",
        "holdings",
        "group",
        "etf",
        "trust",
    }
    while tokens and tokens[-1].lower() in common_suffixes:
        tokens.pop()
    return " ".join(tokens) if tokens else name


def build_query_terms(ticker: str, name: str) -> list[str]:
    terms: list[str] = []
    for candidate in [ticker, clean_company_name(name), name]:
        candidate = (candidate or "").strip()
        if candidate and candidate not in terms:
            terms.append(candidate)
        if len(terms) >= 2:
            break
    return terms


def extract_runs(df: pd.DataFrame, mask_col: str, min_length: int) -> list[pd.DataFrame]:
    active = df.loc[df[mask_col]].copy()
    if active.empty:
        return []
    active = active.sort_values("timestamp")
    gap_break = active["timestamp"].diff().gt(pd.Timedelta(minutes=2))
    active["_run_id"] = gap_break.cumsum()
    runs = [run.drop(columns="_run_id") for _, run in active.groupby("_run_id") if len(run) >= min_length]
    return runs


def normalise_filings_df(filings: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    filings = filings.copy()
    if "ticker" not in filings.columns and "sec_id" not in filings.columns:
        raise ValueError("filings CSV must contain at least 'ticker' or 'sec_id'.")

    meta = meta.copy()
    meta["ticker"] = meta["ticker"].astype(str)

    if "sec_id" not in filings.columns:
        filings = filings.merge(meta[["sec_id", "ticker", "name"]], on="ticker", how="left")
    elif "ticker" not in filings.columns:
        filings = filings.merge(meta[["sec_id", "ticker", "name"]], on="sec_id", how="left")
    else:
        filings = filings.merge(
            meta[["sec_id", "ticker", "name"]],
            on=["sec_id", "ticker"],
            how="left",
            suffixes=("", "_meta"),
        )

    if "file_date" not in filings.columns:
        raise ValueError("filings CSV must contain 'file_date'.")

    filings["file_date"] = pd.to_datetime(filings["file_date"], errors="coerce").dt.normalize()
    filings = filings.dropna(subset=["sec_id", "ticker", "file_date"]).copy()
    filings["sec_id"] = filings["sec_id"].astype(int)

    if "headline" not in filings.columns:
        fallback_name = filings.get("name", pd.Series("", index=filings.index)).fillna("")
        filings["headline"] = fallback_name.map(lambda value: f"{value} 8-K filing" if value else "8-K filing")

    if "source_url" not in filings.columns:
        filings["source_url"] = ""
    filings["source_url"] = filings["source_url"].fillna("").astype(str)

    filing_dates = filings["file_date"].dt.strftime("%Y-%m-%d")
    fallback_terms = filings.get("ticker", filings.get("name", pd.Series("", index=filings.index))).fillna("")
    missing_source_mask = filings["source_url"].str.strip().eq("")
    filings.loc[missing_source_mask, "source_url"] = [
        build_edgar_search_url(term, date_str, date_str)
        for term, date_str in zip(fallback_terms[missing_source_mask], filing_dates[missing_source_mask])
    ]

    if "event_type" not in filings.columns:
        text_blob = (
            filings["headline"].fillna("")
            + " "
            + filings.get("entity_name", pd.Series("", index=filings.index)).fillna("")
        )
        filings["event_type"] = text_blob.map(classify_event)

    return filings


def fetch_edgar_filings(
    meta: pd.DataFrame,
    start_date: str,
    end_date: str,
    user_agent: str,
    request_delay: float,
    timeout: float,
    edgar_workers: int,
) -> pd.DataFrame:
    def fetch_record(record: tuple[int, str, str]) -> list[dict]:
        sec_id, ticker, name = record
        local_rows: list[dict] = []
        found_hits_for_security = False
        for term in build_query_terms(ticker, name):
            url = build_edgar_search_url(query_term=term, start_date=start_date, end_date=end_date)
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type.lower() or "<html" in body.lower():
                raise RuntimeError(
                    "SEC EDGAR rejected the request as automated traffic. Re-run with a real "
                    '--user-agent like: --user-agent "Your Name your_email@example.com"'
                )
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "SEC EDGAR returned a non-JSON response. Re-run with a real "
                    '--user-agent like: --user-agent "Your Name your_email@example.com"'
                ) from exc

            hits = payload.get("hits", {}).get("hits", [])
            if hits:
                found_hits_for_security = True
            for hit in hits:
                src = hit.get("_source", {})
                form_type = str(src.get("form_type") or "8-K")
                if form_type.upper() != "8-K":
                    continue
                headline = choose_headline(src, name)
                raw_entity_name = str(src.get("entity_name", "") or "")
                source_url = resolve_sec_source_url(
                    src=src,
                    query_term=term,
                    start_date=start_date,
                    end_date=end_date,
                )
                event_text = " ".join(
                    [
                        raw_entity_name,
                        headline,
                        str(src.get("document_description", "")),
                        " ".join(src.get("items", [])) if isinstance(src.get("items"), list) else str(src.get("items", "")),
                    ]
                )
                local_rows.append(
                    {
                    "sec_id": int(sec_id),
                    "ticker": ticker,
                    "name": name,
                    "query_term": term,
                    "entity_name": raw_entity_name or name,
                    "file_date": src.get("file_date", ""),
                    "headline": headline,
                    "source_url": source_url,
                    "form_type": form_type,
                    "event_text": event_text,
                    "event_type": classify_event(event_text),
                    }
                )
            time.sleep(request_delay)
            if found_hits_for_security:
                break
        return local_rows

    rows: list[dict] = []
    errors: list[str] = []
    records = [
        (int(row.sec_id), str(row.ticker), str(row.name))
        for row in meta[["sec_id", "ticker", "name"]].drop_duplicates().itertuples(index=False)
    ]
    if not records:
        return pd.DataFrame(rows)

    max_workers = max(1, min(int(edgar_workers), len(records)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_record, record) for record in records]
        for future in concurrent.futures.as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception as exc:
                errors.append(str(exc))

    filings = pd.DataFrame(rows)
    if filings.empty:
        if errors:
            raise RuntimeError(errors[0])
        return filings

    filings["file_date"] = pd.to_datetime(filings["file_date"], errors="coerce").dt.normalize()
    filings = filings.dropna(subset=["file_date"]).copy()
    filings = filings.drop_duplicates(subset=["sec_id", "file_date", "source_url", "headline"]).reset_index(drop=True)
    filings = filings.sort_values(["file_date", "sec_id", "headline"]).reset_index(drop=True)
    return filings


def aggregate_filings_by_pair(filings: pd.DataFrame) -> pd.DataFrame:
    if filings.empty:
        return filings.copy()

    filings = filings.copy()
    filings["event_priority"] = filings["event_type"].map(EVENT_PRIORITY).fillna(EVENT_PRIORITY["other"])

    grouped_rows: list[dict] = []
    for (sec_id, ticker, file_date), group in filings.groupby(["sec_id", "ticker", "file_date"], sort=True):
        if "match_score" in group.columns:
            group = group.sort_values(["event_priority", "match_score", "headline", "source_url"], ascending=[True, False, True, True])
        else:
            group = group.sort_values(["event_priority", "headline", "source_url"])
        best = group.iloc[0]
        headline = longest_non_empty(group["headline"].tolist()) or best["headline"]
        source_url = best["source_url"]
        name = best.get("name", "")
        grouped_rows.append(
            {
                "sec_id": int(sec_id),
                "ticker": ticker,
                "name": name,
                "file_date": pd.Timestamp(file_date).normalize(),
                "event_type": best["event_type"],
                "headline": headline,
                "source_url": source_url,
                "event_text": " ".join(group.get("event_text", pd.Series("", index=group.index)).fillna("").tolist()),
                "match_score": float(group["match_score"].max()) if "match_score" in group.columns else np.nan,
            }
        )

    return pd.DataFrame(grouped_rows)


def load_equity_files(equity_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    market = pd.read_csv(equity_dir / "market_data.csv", parse_dates=["timestamp"])
    ohlcv = pd.read_csv(equity_dir / "ohlcv.csv", parse_dates=["trade_date"])
    trades = pd.read_csv(equity_dir / "trade_data.csv", parse_dates=["timestamp"])
    return market, ohlcv, trades


def prepare_market_data(market: pd.DataFrame, include_extended_hours: bool) -> pd.DataFrame:
    market = market.copy()
    if not include_extended_hours:
        market = market[
            (market["timestamp"].dt.hour >= MARKET_START_HOUR)
            & (market["timestamp"].dt.hour <= MARKET_END_HOUR)
        ].copy()

    bid_size_cols = [f"bid_size_level{i:02d}" for i in range(1, 11)]
    ask_size_cols = [f"ask_size_level{i:02d}" for i in range(1, 11)]

    market[bid_size_cols] = market[bid_size_cols].fillna(0)
    market[ask_size_cols] = market[ask_size_cols].fillna(0)

    market["trade_date"] = market["timestamp"].dt.normalize()
    market["trade_date_str"] = market["timestamp"].dt.strftime("%Y-%m-%d")
    market["time_window_start"] = market["timestamp"].dt.strftime("%H:%M:%S")
    market["minutes_from_open"] = market["timestamp"].dt.hour * 60 + market["timestamp"].dt.minute

    market["total_bid"] = market[bid_size_cols].sum(axis=1)
    market["total_ask"] = market[ask_size_cols].sum(axis=1)
    market["total_depth"] = market["total_bid"] + market["total_ask"]
    market["obi"] = (market["total_bid"] - market["total_ask"]) / market["total_depth"].replace(0, np.nan)
    market["spread_bps"] = (
        (market["ask_price_level01"] - market["bid_price_level01"])
        / market["bid_price_level01"].replace(0, np.nan)
        * 10000
    )
    market["bid_concentration"] = market["bid_size_level01"] / market["total_bid"].replace(0, np.nan)
    market["ask_concentration"] = market["ask_size_level01"] / market["total_ask"].replace(0, np.nan)
    market["top2_bid_share"] = (
        market["bid_size_level01"] + market["bid_size_level02"].fillna(0)
    ) / market["total_bid"].replace(0, np.nan)
    market["top2_ask_share"] = (
        market["ask_size_level01"] + market["ask_size_level02"].fillna(0)
    ) / market["total_ask"].replace(0, np.nan)

    market = market.sort_values(["sec_id", "timestamp"]).reset_index(drop=True)

    for col in ["total_bid", "total_ask", "spread_bps", "bid_concentration", "ask_concentration"]:
        baseline = market.groupby("sec_id")[col].transform(
            lambda series: series.rolling(30, min_periods=10).median().shift(1)
        )
        market[f"{col}_base30"] = baseline
        market[f"{col}_ratio30"] = market[col] / baseline.replace(0, np.nan)

    positive_spread = market["spread_bps"].where(market["spread_bps"] > 0)
    market["spread_sec_median"] = positive_spread.groupby(market["sec_id"]).transform("median")
    market["opening_window"] = market["timestamp"].dt.hour.eq(MARKET_START_HOUR) & market["timestamp"].dt.minute.le(45)

    market["bid_local_event"] = (
        market["obi"].gt(0.80)
        & market["total_bid_ratio30"].gt(2.0)
        & market["total_ask_ratio30"].lt(1.0)
        & market["bid_concentration"].gt(0.55)
    )
    market["ask_local_event"] = (
        market["obi"].lt(-0.80)
        & market["total_ask_ratio30"].gt(2.0)
        & market["total_bid_ratio30"].lt(1.0)
        & market["ask_concentration"].gt(0.55)
    )
    market["spread_event"] = (
        market["opening_window"]
        & market["spread_bps"].gt(np.maximum(250.0, market["spread_sec_median"].fillna(0) * 5.0))
        & market["obi"].abs().gt(0.70)
        & np.maximum(market["bid_concentration"], market["ask_concentration"]).gt(0.55)
        & market["spread_bps"].gt(0)
    )
    return market


def prepare_trade_data(trades: pd.DataFrame, include_extended_hours: bool) -> pd.DataFrame:
    trades = trades.copy()
    if not include_extended_hours:
        trades = trades[
            (trades["timestamp"].dt.hour >= MARKET_START_HOUR)
            & (trades["timestamp"].dt.hour <= MARKET_END_HOUR)
        ].copy()

    trades["trade_date"] = trades["timestamp"].dt.normalize()
    trades["minute"] = trades["timestamp"].dt.floor("min")
    trades["status_upper"] = trades["order_status"].fillna("").str.upper()
    trades["is_filled"] = trades["status_upper"].eq("FILLED")
    trades["is_cancelled"] = trades["status_upper"].eq("CANCELLED")
    trades["fill_buy_qty"] = np.where(trades["is_filled"] & trades["side"].eq("BUY"), trades["quantity"], 0)
    trades["fill_sell_qty"] = np.where(trades["is_filled"] & trades["side"].eq("SELL"), trades["quantity"], 0)
    trades["cancel_buy_qty"] = np.where(trades["is_cancelled"] & trades["side"].eq("BUY"), trades["quantity"], 0)
    trades["cancel_sell_qty"] = np.where(trades["is_cancelled"] & trades["side"].eq("SELL"), trades["quantity"], 0)
    return trades


def build_trade_minute_features(trades: pd.DataFrame) -> pd.DataFrame:
    minute = (
        trades.groupby(["sec_id", "minute"])
        .agg(
            fill_buy_qty=("fill_buy_qty", "sum"),
            fill_sell_qty=("fill_sell_qty", "sum"),
            cancel_buy_qty=("cancel_buy_qty", "sum"),
            cancel_sell_qty=("cancel_sell_qty", "sum"),
            fill_count=("is_filled", "sum"),
            cancel_count=("is_cancelled", "sum"),
        )
        .reset_index()
    )
    return minute


def summarise_book_run(
    run: pd.DataFrame,
    anomaly_type: str,
    meta_lookup: pd.DataFrame,
) -> dict:
    sec_id = int(run["sec_id"].iloc[0])
    avg_obi = run["obi"].mean()
    dominant_side = "BUY" if avg_obi >= 0 else "SELL"
    same_depth_ratio = run["total_bid_ratio30"] if dominant_side == "BUY" else run["total_ask_ratio30"]
    other_depth_ratio = run["total_ask_ratio30"] if dominant_side == "BUY" else run["total_bid_ratio30"]
    same_concentration = run["bid_concentration"] if dominant_side == "BUY" else run["ask_concentration"]
    same_fill_qty = run["fill_buy_qty"].sum() if dominant_side == "BUY" else run["fill_sell_qty"].sum()
    other_fill_qty = run["fill_sell_qty"].sum() if dominant_side == "BUY" else run["fill_buy_qty"].sum()
    ticker = meta_lookup.loc[sec_id, "ticker"]

    max_same_ratio = float(np.nanmax(same_depth_ratio.to_numpy(dtype=float)))
    avg_same_ratio = float(np.nanmean(same_depth_ratio.to_numpy(dtype=float)))
    avg_other_ratio = float(np.nanmean(other_depth_ratio.to_numpy(dtype=float)))
    avg_concentration = float(np.nanmean(same_concentration.to_numpy(dtype=float)))

    if anomaly_type == "spread_dislocation":
        remarks = (
            f"Opening spread widened to {run['spread_bps'].max():.1f} bps on {ticker} and stayed dislocated for "
            f"{len(run)} consecutive minutes; {dominant_side} side dominated the book with avg OBI {avg_obi:.2f} "
            f"and level-1 concentration {avg_concentration:.0%}."
        )
        score = (
            len(run) * 1.5
            + math.log1p(max(run["spread_bps"].max(), 0)) * 6
            + abs(avg_obi) * 8
            + avg_concentration * 5
        )
    else:
        remarks = (
            f"{dominant_side} side depth expanded to {max_same_ratio:.1f}x the prior 30-minute median from "
            f"{run['timestamp'].iloc[0].strftime('%H:%M')} to {run['timestamp'].iloc[-1].strftime('%H:%M')}; "
            f"avg OBI was {avg_obi:.2f}, level-1 concentration averaged {avg_concentration:.0%}, and the "
            f"opposite side stayed near {avg_other_ratio:.0%} of local baseline. Filled volume was {fmt_num(same_fill_qty)} "
            f"shares on the displayed side versus {fmt_num(other_fill_qty)} on the opposite side."
        )
        score = (
            len(run) * 2.0
            + avg_same_ratio * 5.0
            + max(0.0, 1.0 - avg_other_ratio) * 6.0
            + abs(avg_obi) * 10.0
            + avg_concentration * 6.0
        )

    severity = "LOW"
    if score >= 45 or len(run) >= 12 or run["spread_bps"].max() >= 1000:
        severity = "HIGH"
    elif score >= 25 or len(run) >= 6:
        severity = "MEDIUM"

    return {
        "sec_id": sec_id,
        "trade_date": run["timestamp"].iloc[0].strftime("%Y-%m-%d"),
        "time_window_start": run["timestamp"].iloc[0].strftime("%H:%M:%S"),
        "anomaly_type": anomaly_type,
        "severity": severity,
        "remarks": remarks,
        "score": score,
        "start_ts": run["timestamp"].iloc[0],
        "end_ts": run["timestamp"].iloc[-1],
    }


def build_p1_candidates(
    market: pd.DataFrame,
    trades: pd.DataFrame,
    meta: pd.DataFrame,
) -> list[dict]:
    meta_lookup = meta.set_index("sec_id")

    trade_minute = build_trade_minute_features(trades)
    market = market.merge(
        trade_minute,
        left_on=["sec_id", "timestamp"],
        right_on=["sec_id", "minute"],
        how="left",
    )
    for col in [
        "fill_buy_qty",
        "fill_sell_qty",
        "cancel_buy_qty",
        "cancel_sell_qty",
        "fill_count",
        "cancel_count",
    ]:
        market[col] = market[col].fillna(0)

    candidates: list[dict] = []

    for mask_col, anomaly_type in [
        ("bid_local_event", "order_book_imbalance"),
        ("ask_local_event", "order_book_imbalance"),
        ("spread_event", "spread_dislocation"),
    ]:
        for sec_id, group in market.groupby("sec_id", sort=True):
            group = group.sort_values("timestamp")
            runs = extract_runs(group, mask_col, min_length=4)
            for run in runs:
                candidates.append(summarise_book_run(run, anomaly_type, meta_lookup))

    cancelled = trades.loc[trades["is_cancelled"]].sort_values(["sec_id", "trader_id", "side", "timestamp"]).copy()
    filled = trades.loc[trades["is_filled"]].copy()

    for (sec_id, trader_id, side), group in cancelled.groupby(["sec_id", "trader_id", "side"], sort=False):
        timestamps = group["timestamp"].to_list()
        quantities = group["quantity"].to_numpy(dtype=float)
        prefix_qty = np.concatenate([[0.0], np.cumsum(quantities)])
        left = 0
        raw_candidates: list[dict] = []

        for right in range(len(group)):
            while timestamps[right] - timestamps[left] > pd.Timedelta(minutes=12):
                left += 1
            count = right - left + 1
            total_qty = float(prefix_qty[right + 1] - prefix_qty[left])
            if count >= 4 and total_qty >= 1500:
                window = group.iloc[left : right + 1]
                raw_candidates.append(
                    {
                        "sec_id": int(sec_id),
                        "trader_id": trader_id,
                        "side": side,
                        "start_ts": window["timestamp"].iloc[0].floor("min"),
                        "end_ts": window["timestamp"].iloc[-1].floor("min"),
                        "cancel_count": count,
                        "cancel_qty": total_qty,
                    }
                )

        deduped: list[dict] = []
        for candidate in sorted(raw_candidates, key=lambda item: (item["cancel_count"], item["cancel_qty"]), reverse=True):
            if any(
                existing["start_ts"] <= candidate["end_ts"] + pd.Timedelta(minutes=2)
                and candidate["start_ts"] <= existing["end_ts"] + pd.Timedelta(minutes=2)
                for existing in deduped
            ):
                continue
            deduped.append(candidate)

        for candidate in deduped:
            start_ts = candidate["start_ts"]
            end_ts = candidate["end_ts"]
            context = market[
                (market["sec_id"].eq(sec_id))
                & (market["timestamp"].ge(start_ts - pd.Timedelta(minutes=2)))
                & (market["timestamp"].le(end_ts + pd.Timedelta(minutes=3)))
            ].copy()
            context_obi = context["obi"].mean() if not context.empty else np.nan
            aligned = (
                side == "BUY" and pd.notna(context_obi) and context_obi > 0.60
            ) or (
                side == "SELL" and pd.notna(context_obi) and context_obi < -0.60
            )
            opp_side = "SELL" if side == "BUY" else "BUY"
            opposite_fills = filled[
                filled["sec_id"].eq(sec_id)
                & filled["side"].eq(opp_side)
                & filled["timestamp"].ge(start_ts)
                & filled["timestamp"].le(end_ts + pd.Timedelta(minutes=3))
            ]
            nearby_book = [
                book_candidate
                for book_candidate in candidates
                if book_candidate["sec_id"] == sec_id
                and book_candidate["start_ts"] <= end_ts + pd.Timedelta(minutes=10)
                and start_ts <= book_candidate["end_ts"] + pd.Timedelta(minutes=10)
            ]
            book_context = ""
            score_bonus = 0.0
            if nearby_book:
                strongest_book = max(nearby_book, key=lambda item: item["score"])
                book_context = (
                    f" Nearby order-book signal started at {strongest_book['time_window_start']} with "
                    f"{strongest_book['anomaly_type']} behaviour."
                )
                score_bonus += strongest_book["score"] * 0.15

            remarks = (
                f"{candidate['trader_id']} submitted {candidate['cancel_count']} {side} cancellations in a 12-minute window "
                f"totaling {fmt_num(candidate['cancel_qty'])} shares."
            )
            if aligned:
                remarks += (
                    f" The displayed book leaned the same way during the burst (avg OBI {context_obi:.2f}), "
                    f"which is consistent with spoofing-like liquidity."
                )
            else:
                remarks += " The cancellation burst stood out against the surrounding flow even without a clean one-sided book signal."
            if not opposite_fills.empty:
                remarks += f" Opposite-side FILLED volume reached {fmt_num(opposite_fills['quantity'].sum())} shares shortly after the cancellations."
            else:
                remarks += " There was little matching opposite-side FILLED volume immediately after the cancellations."
            remarks += book_context

            score = candidate["cancel_count"] * 3.5 + candidate["cancel_qty"] / 450.0 + score_bonus
            if aligned:
                score += 6.0
            if not opposite_fills.empty:
                score += min(6.0, opposite_fills["quantity"].sum() / 600.0)

            severity = "LOW"
            if score >= 35 or candidate["cancel_qty"] >= 4000:
                severity = "HIGH"
            elif score >= 20:
                severity = "MEDIUM"

            candidates.append(
                {
                    "sec_id": int(sec_id),
                    "trade_date": start_ts.strftime("%Y-%m-%d"),
                    "time_window_start": start_ts.strftime("%H:%M:%S"),
                    "anomaly_type": "unusual_cancel_pattern",
                    "severity": severity,
                    "remarks": remarks,
                    "score": score,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                }
            )

    return candidates


def overlaps_existing(selected: list[dict], candidate: dict) -> bool:
    for existing in selected:
        if existing["sec_id"] != candidate["sec_id"]:
            continue
        if existing["trade_date"] != candidate["trade_date"]:
            continue
        if existing["start_ts"] <= candidate["end_ts"] + pd.Timedelta(minutes=20) and candidate["start_ts"] <= existing["end_ts"] + pd.Timedelta(minutes=20):
            return True
    return False


def select_p1_alerts(candidates: list[dict], max_alerts: int) -> list[dict]:
    type_caps = {
        "unusual_cancel_pattern": 2,
        "order_book_imbalance": 2,
        "spread_dislocation": 1,
    }
    selected: list[dict] = []
    type_counts: Counter[str] = Counter()

    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        anomaly_type = candidate["anomaly_type"]
        if type_counts[anomaly_type] >= type_caps.get(anomaly_type, max_alerts):
            continue
        if overlaps_existing(selected, candidate):
            continue
        selected.append(candidate)
        type_counts[anomaly_type] += 1
        if len(selected) >= max_alerts:
            break

    return sorted(selected, key=lambda item: (item["trade_date"], item["time_window_start"], item["sec_id"]))


def solve_problem_1(
    market: pd.DataFrame,
    trades: pd.DataFrame,
    ohlcv: pd.DataFrame,
    include_extended_hours: bool,
    max_alerts: int,
) -> tuple[pd.DataFrame, float]:
    started = time.perf_counter()
    market_prepared = prepare_market_data(market, include_extended_hours=include_extended_hours)
    trades_prepared = prepare_trade_data(trades, include_extended_hours=include_extended_hours)
    meta = ohlcv[["sec_id", "ticker", "name"]].drop_duplicates()

    candidates = build_p1_candidates(market_prepared, trades_prepared, meta)
    selected = select_p1_alerts(candidates, max_alerts=max_alerts)
    runtime = round(time.perf_counter() - started, 2)

    rows = []
    for alert_id, candidate in enumerate(selected, start=1):
        rows.append(
            {
                "alert_id": alert_id,
                "sec_id": candidate["sec_id"],
                "trade_date": candidate["trade_date"],
                "time_window_start": candidate["time_window_start"],
                "anomaly_type": candidate["anomaly_type"],
                "severity": candidate["severity"],
                "remarks": candidate["remarks"],
                "time_to_run": runtime,
            }
        )

    result = pd.DataFrame(
        rows,
        columns=[
            "alert_id",
            "sec_id",
            "trade_date",
            "time_window_start",
            "anomaly_type",
            "severity",
            "remarks",
            "time_to_run",
        ],
    )
    return result, runtime


def prepare_ohlcv_for_p2(ohlcv: pd.DataFrame) -> pd.DataFrame:
    ohlcv = ohlcv.copy()
    ohlcv = ohlcv.sort_values(["sec_id", "trade_date"]).reset_index(drop=True)
    ohlcv["daily_return"] = ohlcv.groupby("sec_id")["close"].pct_change()
    ohlcv["vol_15d_mean"] = ohlcv.groupby("sec_id")["volume"].transform(
        lambda series: series.shift(1).rolling(15, min_periods=10).mean()
    )
    ohlcv["vol_15d_std"] = ohlcv.groupby("sec_id")["volume"].transform(
        lambda series: series.shift(1).rolling(15, min_periods=10).std()
    )
    ohlcv["ret_15d_std"] = ohlcv.groupby("sec_id")["daily_return"].transform(
        lambda series: series.shift(1).rolling(15, min_periods=10).std()
    )
    ohlcv["volume_z"] = (ohlcv["volume"] - ohlcv["vol_15d_mean"]) / ohlcv["vol_15d_std"].replace(0, np.nan)
    return ohlcv


def build_trade_side_evidence(
    sec_fills: pd.DataFrame,
    suspicious_start: pd.Timestamp,
    file_date: pd.Timestamp,
) -> dict:
    prior_sec_fills = sec_fills.loc[sec_fills["trade_date"].lt(suspicious_start)]
    sec_baseline_qty = prior_sec_fills["quantity"].median()
    if pd.isna(sec_baseline_qty) or sec_baseline_qty <= 0:
        sec_baseline_qty = sec_fills["quantity"].median()

    window_fills = sec_fills[
        sec_fills["trade_date"].ge(suspicious_start) & sec_fills["trade_date"].lt(file_date)
    ].copy()
    if window_fills.empty:
        return {
            "score": 0.0,
            "dominant_side": "BUY",
            "remark": "trade_data shows no FILLED trades in the pre-announcement window",
            "window_qty": 0.0,
        }

    grouped = (
        window_fills.groupby(["trader_id", "side"])
        .agg(total_qty=("quantity", "sum"), max_single=("quantity", "max"), fills=("quantity", "size"))
        .reset_index()
    )
    candidates: list[dict] = []

    for row in grouped.itertuples(index=False):
        prior_same = prior_sec_fills[
            prior_sec_fills["trader_id"].eq(row.trader_id) & prior_sec_fills["side"].eq(row.side)
        ]
        baseline = prior_same["quantity"].median()
        no_prior = prior_same.empty
        if pd.isna(baseline) or baseline <= 0:
            baseline = sec_baseline_qty
        ratio = safe_div(row.total_qty, baseline)
        score = 0.0
        if pd.notna(ratio):
            score += min(12.0, ratio)
        if no_prior and pd.notna(sec_baseline_qty) and sec_baseline_qty > 0:
            score += min(6.0, row.total_qty / sec_baseline_qty)
        score += min(4.0, row.max_single / max(sec_baseline_qty, 1.0))
        candidates.append(
            {
                "trader_id": row.trader_id,
                "side": row.side,
                "total_qty": float(row.total_qty),
                "fills": int(row.fills),
                "ratio": ratio,
                "no_prior": no_prior,
                "score": score,
            }
        )

    best = max(candidates, key=lambda item: item["score"])
    ratio_text = (
        f"{best['ratio']:.1f}x the prior median"
        if pd.notna(best["ratio"]) and np.isfinite(best["ratio"])
        else "well above the normal size for this ticker"
    )
    prior_text = "with no prior same-side history" if best["no_prior"] else f"at {ratio_text}"
    remark = (
        f"trade_data shows concentrated {best['side']} activity from {best['trader_id']} totaling "
        f"{fmt_num(best['total_qty'])} shares across {best['fills']} fills, {prior_text}"
    )
    return {
        "score": best["score"],
        "dominant_side": best["side"],
        "remark": remark,
        "window_qty": best["total_qty"],
    }


def evaluate_p2_signals(
    filings: pd.DataFrame,
    ohlcv: pd.DataFrame,
    trades: pd.DataFrame,
    include_unflagged_events: bool,
    max_signals: int,
    signal_workers: int,
) -> pd.DataFrame:
    started = time.perf_counter()

    prepared_ohlcv = prepare_ohlcv_for_p2(ohlcv)
    fills = trades.copy()
    fills["trade_date"] = fills["timestamp"].dt.normalize()
    fills = fills.loc[fills["order_status"].fillna("").str.upper().eq("FILLED")].copy()
    ohlcv_by_sec = {
        int(sec_id): group.sort_values("trade_date").copy()
        for sec_id, group in prepared_ohlcv.groupby("sec_id", sort=False)
    }
    fills_by_sec = {
        int(sec_id): group.copy()
        for sec_id, group in fills.groupby("sec_id", sort=False)
    }

    def evaluate_filing(filing: object) -> dict | None:
        sec_history = ohlcv_by_sec.get(int(filing.sec_id))
        if sec_history is None or sec_history.empty:
            return None
        pre_window = sec_history.loc[sec_history["trade_date"].lt(filing.file_date)].tail(5).copy()
        if len(pre_window) < 3:
            return None

        suspicious_start = pre_window["trade_date"].iloc[0].normalize()
        suspicious_end = pre_window["trade_date"].iloc[-1].normalize()
        pre_drift = (1.0 + pre_window["daily_return"].fillna(0.0)).prod() - 1.0
        sigma = pre_window["ret_15d_std"].dropna().iloc[-1] if not pre_window["ret_15d_std"].dropna().empty else pre_window["daily_return"].std()
        drift_z = abs(pre_drift) / (sigma * math.sqrt(len(pre_window))) if sigma and not pd.isna(sigma) and sigma > 0 else np.nan

        volume_peak = pre_window.loc[pre_window["volume_z"].fillna(-np.inf).idxmax()]
        volume_z = float(volume_peak["volume_z"]) if pd.notna(volume_peak["volume_z"]) else np.nan

        trade_evidence = build_trade_side_evidence(
            sec_fills=fills_by_sec.get(int(filing.sec_id), fills.iloc[0:0].copy()),
            suspicious_start=suspicious_start,
            file_date=filing.file_date,
        )
        event_bonus = {
            "merger": 3.0,
            "restatement": 2.0,
            "earnings": 1.5,
            "leadership": 1.0,
            "bankruptcy": 2.0,
            "other": 0.0,
        }.get(filing.event_type, 0.0)

        score = event_bonus
        if pd.notna(volume_z):
            score += max(0.0, volume_z - 2.0) * 2.5
        if pd.notna(drift_z):
            score += max(0.0, drift_z - 1.5) * 3.0
        score += trade_evidence["score"]

        drift_side = "BUY" if pre_drift >= 0 else "SELL"
        if trade_evidence["dominant_side"] == drift_side:
            score += 1.5

        strong_volume = pd.notna(volume_z) and volume_z >= 2.8
        strong_drift = pd.notna(drift_z) and drift_z >= 2.0
        strong_trade = trade_evidence["score"] >= 7.0
        pre_drift_flag = int(score >= 10.0 and (strong_volume or strong_drift or strong_trade))

        remarks = (
            f"Pre-announcement drift from {suspicious_start.strftime('%Y-%m-%d')} to "
            f"{suspicious_end.strftime('%Y-%m-%d')} was {pct(pre_drift)}; "
        )
        if pd.notna(volume_z):
            remarks += (
                f"max volume z-score was {volume_z:.2f} on {volume_peak['trade_date'].strftime('%Y-%m-%d')}; "
            )
        else:
            remarks += "volume stayed close to baseline; "
        remarks += trade_evidence["remark"]

        return {
            "sec_id": int(filing.sec_id),
            "ticker": filing.ticker,
            "event_date": filing.file_date.strftime("%Y-%m-%d"),
            "event_type": filing.event_type,
            "headline": filing.headline,
            "source_url": filing.source_url,
            "pre_drift_flag": pre_drift_flag,
            "suspicious_window_start": suspicious_start.strftime("%Y-%m-%d"),
            "remarks": remarks,
            "score": score,
        }

    filings_to_score = list(filings.sort_values(["file_date", "sec_id"]).itertuples(index=False))
    signals: list[dict] = []
    max_workers = max(1, min(int(signal_workers), len(filings_to_score))) if filings_to_score else 1
    if max_workers == 1:
        for filing in filings_to_score:
            result = evaluate_filing(filing)
            if result is not None:
                signals.append(result)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(evaluate_filing, filing) for filing in filings_to_score]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    signals.append(result)

    runtime = round(time.perf_counter() - started, 2)

    if not signals:
        return pd.DataFrame(
            columns=[
                "sec_id",
                "event_date",
                "event_type",
                "headline",
                "source_url",
                "pre_drift_flag",
                "suspicious_window_start",
                "remarks",
                "time_to_run",
            ]
        )

    signals_df = pd.DataFrame(signals)
    missing_source_mask = signals_df["source_url"].fillna("").astype(str).str.strip().eq("")
    if missing_source_mask.any():
        signals_df.loc[missing_source_mask, "source_url"] = [
            build_edgar_search_url(query_term=ticker, start_date=event_date, end_date=event_date)
            for ticker, event_date in zip(
                signals_df.loc[missing_source_mask, "ticker"],
                signals_df.loc[missing_source_mask, "event_date"],
            )
        ]
    signals_df = signals_df.sort_values(["score", "event_date", "sec_id"], ascending=[False, True, True])
    if not include_unflagged_events:
        signals_df = signals_df.loc[signals_df["pre_drift_flag"].eq(1)].copy()
    signals_df = signals_df.head(max_signals).copy()
    signals_df["time_to_run"] = runtime

    return signals_df[
        [
            "sec_id",
            "event_date",
            "event_type",
            "headline",
            "source_url",
            "pre_drift_flag",
            "suspicious_window_start",
            "remarks",
            "time_to_run",
        ]
    ]


def solve_problem_2(
    ohlcv: pd.DataFrame,
    trades: pd.DataFrame,
    filings_csv: Path | None,
    user_agent: str,
    request_delay: float,
    timeout: float,
    edgar_workers: int,
    include_unflagged_events: bool,
    max_signals: int,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    started = time.perf_counter()
    meta = ohlcv[["sec_id", "ticker", "name"]].drop_duplicates().copy()
    trade_start = trades["timestamp"].min().normalize()
    trade_end = trades["timestamp"].max().normalize()

    if filings_csv is not None:
        raw_filings = normalise_filings_df(pd.read_csv(filings_csv), meta)
    else:
        raw_filings = fetch_edgar_filings(
            meta=meta,
            start_date=trade_start.strftime("%Y-%m-%d"),
            end_date=trade_end.strftime("%Y-%m-%d"),
            user_agent=user_agent,
            request_delay=request_delay,
            timeout=timeout,
            edgar_workers=edgar_workers,
        )
        if raw_filings.empty:
            raise RuntimeError(
                "EDGAR returned no filings. Re-run with internet access, a better --user-agent, "
                "or provide --filings-csv with cached filings."
            )

    pair_filings = aggregate_filings_by_pair(raw_filings)
    pair_filings = pair_filings.loc[
        pair_filings["file_date"].between(trade_start, trade_end)
        & pair_filings["event_type"].ne("other")
    ].copy()

    signals = evaluate_p2_signals(
        filings=pair_filings,
        ohlcv=ohlcv,
        trades=trades,
        include_unflagged_events=include_unflagged_events,
        max_signals=max_signals,
        signal_workers=edgar_workers,
    )
    runtime = round(time.perf_counter() - started, 2)
    if not signals.empty:
        signals = signals.copy()
        signals["time_to_run"] = runtime
    return signals, raw_filings, runtime


def main() -> int:
    overall_started = time.perf_counter()
    args = parse_args()
    equity_dir, _ = ensure_student_pack(args.student_pack)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_output_dir = create_run_output_dir(args.output_dir)
    script_cache_path = Path(__file__).resolve().with_name("_edgar_filings_cache.csv")
    output_cache_path = args.output_dir / "_edgar_filings_cache.csv"
    print(f"[RUN] Output folder: {run_output_dir}")

    market, ohlcv, trades = load_equity_files(equity_dir)

    if args.problems in {"p1", "all"}:
        p1_started = time.perf_counter()
        p1_alerts, p1_solver_runtime = solve_problem_1(
            market=market,
            trades=trades,
            ohlcv=ohlcv,
            include_extended_hours=args.include_extended_hours,
            max_alerts=args.max_p1_alerts,
        )
        p1_path = run_output_dir / "p1_alerts.csv"
        p1_csv_path = write_submission_csv(p1_alerts, p1_path)
        print(f"Wrote {p1_csv_path} ({len(p1_alerts)} rows)")
        p1_elapsed = time.perf_counter() - p1_started
        print(
            f"[P1] Solver runtime: {format_elapsed(p1_solver_runtime)} | "
            f"end-to-end: {format_elapsed(p1_elapsed)}"
        )

    if args.problems in {"p2", "all"}:
        raw_filings_path = args.save_raw_filings
        if raw_filings_path is None and args.filings_csv is None:
            raw_filings_path = run_output_dir / "edgar_filings_raw.csv"

        effective_filings_csv = args.filings_csv
        reusable_cache_path = script_cache_path if script_cache_path.exists() else output_cache_path
        if effective_filings_csv is None and not args.refresh_edgar_cache and reusable_cache_path.exists():
            effective_filings_csv = reusable_cache_path
            print(f"[P2] Reusing EDGAR cache: {reusable_cache_path}")

        p2_started = time.perf_counter()
        p2_signals, raw_filings, p2_solver_runtime = solve_problem_2(
            ohlcv=ohlcv,
            trades=trades,
            filings_csv=effective_filings_csv,
            user_agent=args.user_agent,
            request_delay=args.request_delay,
            timeout=args.timeout,
            edgar_workers=args.edgar_workers,
            include_unflagged_events=args.include_unflagged_events,
            max_signals=args.max_p2_signals,
        )
        p2_path = run_output_dir / "p2_signals.csv"
        p2_csv_path = write_submission_csv(p2_signals, p2_path)
        print(f"Wrote {p2_csv_path} ({len(p2_signals)} rows)")
        if raw_filings_path is not None and raw_filings is not None and not raw_filings.empty:
            raw_filings.to_csv(raw_filings_path, index=False)
            print(f"Saved raw EDGAR filings to {raw_filings_path}")
            if effective_filings_csv is None:
                raw_filings.to_csv(script_cache_path, index=False)
                print(f"[P2] Updated EDGAR cache: {script_cache_path}")
        p2_elapsed = time.perf_counter() - p2_started
        print(
            f"[P2] Solver runtime: {format_elapsed(p2_solver_runtime)} | "
            f"end-to-end: {format_elapsed(p2_elapsed)}"
        )

    overall_elapsed = time.perf_counter() - overall_started
    print(f"[TOTAL] Runtime: {format_elapsed(overall_elapsed)}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - surface a friendly error to the user
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
