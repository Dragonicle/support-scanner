"""
support_scanner.py
===================

A FREE stock scanner that finds US stocks currently trading within a
configurable percentage (default 5%) ABOVE a meaningful, multi-touch
horizontal support zone.

WHAT THIS IS
------------
This scans a broad universe of US stocks, reconstructs the kind of
horizontal support lines you'd draw by hand on a TradingView chart
(clusters of swing lows that price has bounced off more than once),
and shortlists stocks whose current price sits just above that
support. It does NOT recommend trades, predict bounces, or tell you
which options to sell. It only flags a technical setup for you to
review manually.

DATA SOURCES (all free, no API key, no TradingView plan required)
-------------------------------------------------------------------
1. Stock universe (tickers):
   - S&P 500 constituent list scraped from Wikipedia:
     https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
   - Nasdaq-100 constituent list scraped from Wikipedia:
     https://en.wikipedia.org/wiki/Nasdaq-100
   These pages are maintained by editors and are usually accurate and
   current, but they are NOT an official/authoritative index feed.
   Occasionally a ticker will be stale (recently added/removed name).

2. Price data: Yahoo Finance, via the `yfinance` Python package.
   - Free, no key required.
   - Unofficial: yfinance scrapes/queries endpoints Yahoo does not
     formally publish as a supported API. It can occasionally rate
     limit you, return gaps, or break if Yahoo changes something.
   - Good enough for a once-a-day scan of a few hundred tickers, but
     do NOT treat it as institutional-grade, real-time, or
     guaranteed-accurate data. Always sanity check anything you act on
     in TradingView before making a decision.

LIMITATIONS TO KEEP IN MIND
----------------------------
- "Support" here is a statistical/geometric approximation, not a fact.
  Two people (or two algorithms) will draw the line slightly
  differently. Treat the output as a SHORTLIST to inspect manually,
  never as a signal to act on directly.
- Daily bars only. No intraday precision on the swing points.
- yfinance bulk downloads can silently skip a ticker that has no data
  for the period (e.g. very recent IPO). Those are logged and skipped,
  not silently dropped without a trace.
- This script does not use or require options data. It is a purely
  technical / price-based scanner, deliberately kept that way per the
  request that drove this project (see the docstring on options use).

HOW TO RUN
----------
    pip install -r requirements.txt
    python support_scanner.py

All the interesting parameters are in the CONFIG section directly
below `import` statements — change them there, no need to touch the
rest of the code.

OUTPUT
------
- A console table of every candidate that passed the filters, sorted
  by distance from support (closest first).
- results.csv with the same data plus a few extra diagnostic columns.
- One PNG chart per candidate in ./charts/ showing candles, the
  swing lows used, the resulting support zone, current price, and the
  MAX_DISTANCE_FROM_SUPPORT line above it — so you can eyeball it
  against what you see on your own TradingView chart.
"""

from __future__ import annotations

import io
import re
import sys
import time
import traceback
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ======================================================================
# CONFIG — everything you're likely to want to tweak lives here.
# ======================================================================

# --- Universe ---------------------------------------------------------
INCLUDE_SP500 = True          # Pull S&P 500 tickers from Wikipedia
INCLUDE_NASDAQ100 = True      # Pull Nasdaq-100 tickers from Wikipedia
EXTRA_TICKERS: List[str] = [] # Add any tickers you want manually, e.g. ["CRWV"]
INCLUDE_ETFS = False          # ETFs are excluded by default (see below)
WIKI_SCRAPE_DEBUG = False
# Set True to print every table pandas found on the Wikipedia page (its
# flattened column names and row count) when a scrape fails. Useful for
# diagnosing why the live Nasdaq-100 scrape isn't finding the constituents
# table on your machine — paste that output back for a proper fix.

WATCHLIST: List[str] = []
# Tickers here are always analyzed and always shown in the results —
# even if they currently fall outside MAX_DISTANCE_FROM_SUPPORT or
# MIN_VOLATILITY_PCT — so you don't lose track of a stock you're
# actively watching just because it moved slightly out of range. They
# still need a valid, qualifying support zone to appear at all (this is
# a support scanner, not a general stock tracker), and liquidity filters
# (MIN_PRICE, MIN_AVG_VOLUME) are still bypassed for them since you
# added them on purpose.

# --- Lookback / candle settings ---------------------------------------
LOOKBACK_DAYS = 180
# How many calendar days of daily history to pull per ticker.
# 180 calendar days ~ roughly 120 trading days (~6 months), giving the
# "3-6 months" of data requested. Increase if you want to catch older
# support that hasn't been retested recently.

# --- Swing low detection ------------------------------------------------
SWING_LEFT = 3
SWING_RIGHT = 3
# A candle's LOW counts as a "swing low" (a local bottom) only if it is
# lower than the lows of SWING_LEFT candles before it AND SWING_RIGHT
# candles after it. Bigger numbers = fewer, more significant swing lows.
# Smaller numbers = more, noisier swing lows.

# --- Clustering swing lows into support zones ---------------------------
SUPPORT_CLUSTER_TOLERANCE = 0.02
# Two swing lows are considered "the same" support zone if they are
# within this fraction of each other (0.02 = 2%). Swing lows that
# cluster together become one zone, whose price is their average.

# --- Support strength scoring -------------------------------------------
MIN_TOUCHES = 2
# A support zone needs at least this many swing-low touches to be
# considered "meaningful" at all. A single bounce is not a support
# level, it's a data point.

RECENCY_HALF_LIFE_DAYS = 60
# Older touches count for less. A touch RECENCY_HALF_LIFE_DAYS ago
# contributes half the weight of a touch today. This makes the scanner
# prefer support that is still structurally relevant, not ancient
# history from 6 months ago that price has since ignored.

# --- Distance filter (the core scan) -------------------------------------
MAX_DISTANCE_FROM_SUPPORT = 0.05
# 0.05 = 5%. Change to 0.03 for 3%, 0.10 for 10%, etc. This is a single
# number — nothing else in the script needs to change.
MIN_DISTANCE_FROM_SUPPORT = 0.0
# Keep at 0 unless you want to exclude stocks that are AT or slightly
# below support (negative distance isn't possible with this scanner
# since support is always selected as the nearest level *below* price,
# but you could raise this to e.g. 0.005 to require at least a small
# cushion above support).

# --- Liquidity / quality filters ------------------------------------------
MIN_PRICE = 10.0
MIN_AVG_VOLUME = 500_000       # 20-day average daily volume
AVG_VOLUME_WINDOW = 20

# --- Volatility -------------------------------------------------------
VOLATILITY_WINDOW = 20
# Trading days used to compute annualized realized (historical) volatility
# from daily log returns. This is NOT implied volatility (IV) — IV comes
# from live options prices, which isn't available through a free data
# source. Realized volatility is a free proxy for "how much does this
# stock actually move," computed from the same price history already
# being downloaded — useful context for options premium, but not a
# substitute for checking the actual options chain before trading.
MIN_VOLATILITY_PCT = 0.0
# 0 = no filter (show everything, sorted/labeled by volatility as normal).
# Raise this (e.g. 40) to only surface candidates with at least that much
# annualized realized volatility — i.e. "meaningful support AND volatile
# enough to be interesting for premium selling."

# --- Output ---------------------------------------------------------------
OUTPUT_CSV = "results.csv"
CHARTS_DIR = "charts"
GENERATE_CHARTS = True
MAX_CHARTS = 40                # cap chart generation for very large hit lists

# --- Networking -------------------------------------------------------
BATCH_SIZE = 50                # tickers per yfinance.download() batch call
REQUEST_PAUSE_SECONDS = 1.0    # polite pause between batches
MAX_RETRIES = 3
YFINANCE_THREADS = False
# yfinance keeps a small local SQLite cache (ticker timezone lookups).
# Downloading many tickers in parallel (threads=True) can make separate
# threads hit that cache file at once and throw "database is locked".
# False is slightly slower but avoids that entirely. Flip to True if you
# want faster downloads and don't mind an occasional dropped ticker.

# ======================================================================
# END CONFIG
# ======================================================================


# ----------------------------------------------------------------------
# Universe construction
# ----------------------------------------------------------------------

WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

# Static fallback for the Nasdaq-100 constituents, used only if the live
# Wikipedia scrape fails (e.g. table markup changes, network hiccup).
# Captured from https://en.wikipedia.org/wiki/Nasdaq-100 on 2026-09-22
# (101 constituents per the page's infobox as of June 5, 2026). This WILL
# drift out of date as the index is reconstituted (quarterly/annual
# rebalances) — treat it strictly as a backstop, not a source of truth.
# To refresh it: open the Wikipedia page's constituents table yourself and
# paste the current "Ticker" column values in below.
NASDAQ_100_FALLBACK_DATE = "2026-09-22"
NASDAQ_100_FALLBACK = [
    "ADBE", "AMD", "ABNB", "ALNY", "GOOGL", "GOOG", "AMZN", "AEP", "AMGN", "ADI",
    "AAPL", "AMAT", "APP", "ARM", "ASML", "ALAB", "ADSK", "ADP", "AXON", "BKR",
    "BKNG", "AVGO", "CDNS", "CTAS", "CSCO", "CCEP", "CMCSA", "CEG", "CPRT", "CRWV",
    "COST", "CRWD", "CSX", "DDOG", "DXCM", "FANG", "DASH", "EA", "EXC", "FAST",
    "FER", "FTNT", "GEHC", "GILD", "HON", "IDXX", "INTC", "INTU", "ISRG", "KDP",
    "KLAC", "KHC", "LRCX", "LIN", "LITE", "MAR", "MRVL", "MELI", "META", "MCHP",
    "MU", "MSFT", "MSTR", "MDLZ", "MPWR", "MNST", "NBIS", "NFLX", "NVDA", "NXPI",
    "ORLY", "ODFL", "PCAR", "PLTR", "PANW", "PAYX", "PYPL", "PDD", "PEP", "QCOM",
    "REGN", "RKLB", "ROP", "ROST", "SNDK", "STX", "SHOP", "SBUX", "SNPS", "TMUS",
    "TTWO", "TER", "TSLA", "TXN", "TRI", "VRTX", "WMT", "WBD", "WDC", "WDAY", "XEL",
]

# A short list of common leveraged/inverse ETF suffixes/names to keep out
# even if someone flips INCLUDE_ETFS on and a leveraged product slips in
# via a manual EXTRA_TICKERS entry.
LEVERAGED_ETF_HINTS = ("2X", "3X", "BULL", "BEAR", "ULTRA", "INVERSE")


_TICKER_RE = re.compile(r"^[A-Z]{1,6}([.\-][A-Z]{1,2})?$")


def _flatten_columns(cols) -> List[str]:
    """Turn possibly-multiindex pandas columns into flat, clean strings."""
    flat = []
    for c in cols:
        if isinstance(c, tuple):
            # Join levels, skipping duplicated/empty pieces and any
            # "Unnamed: N_level_M" placeholders pandas inserts for
            # merged header cells.
            parts = [str(p).strip() for p in c if str(p).strip()
                     and not str(p).startswith("Unnamed")]
            flat.append(" ".join(dict.fromkeys(parts)))  # de-dup, keep order
        else:
            flat.append(str(c).strip())
    return flat


def _clean_ticker_series(s: pd.Series) -> List[str]:
    cleaned = (
        s.astype(str)
        .str.strip()
        .str.replace(r"\[.*?\]", "", regex=True)   # strip footnote markers e.g. AAPL[1]
        .str.replace(".", "-", regex=False)          # BRK.B -> BRK-B for yfinance
        .str.upper()
    )
    return [t for t in cleaned if t and t.lower() != "nan"]


def _get_wikipedia_table(url: str, ticker_column_candidates: List[str],
                          min_rows: int = 20) -> List[str]:
    """
    Scrape a Wikipedia page and return the ticker list from whichever table
    looks like the actual constituents table. Robust to:
    - multiple tables with similarly-named columns (e.g. historical
      "added/removed" tables also mention "Ticker")
    - multi-row / merged headers (pandas MultiIndex columns)
    - footnote markers embedded in cell text (e.g. "AAPL[1]")
    """
    headers = {"User-Agent": "Mozilla/5.0 (support-scanner/1.0)"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))

    candidates_lower = [c.lower() for c in ticker_column_candidates]
    best: Optional[List[str]] = None
    debug_rows = []

    for i, table in enumerate(tables):
        flat_cols = _flatten_columns(table.columns)
        table.columns = flat_cols
        debug_rows.append((i, flat_cols, table.shape))
        for col in flat_cols:
            if col.lower() not in candidates_lower:
                continue
            tickers = _clean_ticker_series(table[col])
            # Validate: must look like real ticker symbols and there must
            # be a plausible number of them (filters out small unrelated
            # tables and huge historical change-log tables that happen to
            # reuse the same column name).
            valid = [t for t in tickers if _TICKER_RE.match(t)]
            if len(valid) < min_rows:
                continue
            # Prefer the table with the most valid-looking tickers, since
            # that's almost always the actual constituents table rather
            # than a partial/side table.
            if best is None or len(valid) > len(best):
                best = valid

    if best is None:
        if WIKI_SCRAPE_DEBUG:
            print(f"[wiki-debug] {url} — {len(tables)} tables found, "
                  f"none matched {ticker_column_candidates} with >= {min_rows} "
                  f"valid-looking tickers:")
            for i, cols, shape in debug_rows:
                print(f"    table[{i}] shape={shape} columns={cols}")
        raise ValueError(f"Could not find a ticker column in tables at {url}")
    return best


def get_universe() -> List[str]:
    """Build the ticker universe from the sources enabled in CONFIG."""
    tickers: set[str] = set()

    if INCLUDE_SP500:
        try:
            sp500 = _get_wikipedia_table(WIKI_SP500_URL, ["Symbol"], min_rows=400)
            print(f"[universe] S&P 500: {len(sp500)} tickers from Wikipedia")
            tickers.update(sp500)
        except Exception as e:
            print(f"[universe] WARNING: failed to fetch S&P 500 list: {e}")

    if INCLUDE_NASDAQ100:
        try:
            ndx = _get_wikipedia_table(WIKI_NDX_URL, ["Ticker", "Symbol"], min_rows=90)
            print(f"[universe] Nasdaq-100: {len(ndx)} tickers from Wikipedia")
            tickers.update(ndx)
        except Exception as e:
            print(f"[universe] WARNING: failed to fetch Nasdaq-100 list live ({e})")
            print(f"[universe] Falling back to a static Nasdaq-100 snapshot "
                  f"captured {NASDAQ_100_FALLBACK_DATE} "
                  f"({len(NASDAQ_100_FALLBACK)} tickers). This will drift out of "
                  f"date as the index is reconstituted — see NASDAQ_100_FALLBACK "
                  f"in the source if you want to refresh it, or set "
                  f"WIKI_SCRAPE_DEBUG = True in CONFIG to see why the live "
                  f"scrape is failing on your machine.")
            tickers.update(NASDAQ_100_FALLBACK)

    tickers.update(EXTRA_TICKERS)

    if not INCLUDE_ETFS:
        # Best-effort: known common ETF tickers that otherwise sneak in via
        # EXTRA_TICKERS or edge cases. The Wikipedia index pages above are
        # equity-only, so this mainly guards manual additions.
        tickers = {
            t for t in tickers
            if not any(hint in t.upper() for hint in LEVERAGED_ETF_HINTS)
        }

    universe = sorted(tickers)
    print(f"[universe] Combined universe: {len(universe)} unique tickers")
    return universe


# ----------------------------------------------------------------------
# Data download
# ----------------------------------------------------------------------

def download_price_data(tickers: List[str], lookback_days: int,
                         progress_callback=None) -> dict:
    """
    Download daily OHLCV data for every ticker via yfinance, in batches.
    Returns {ticker: DataFrame} for tickers that returned usable data.

    progress_callback, if given, is called as progress_callback(done, total)
    after each batch — used by the web interface to drive a progress bar.
    """
    import yfinance as yf

    # Route yfinance's tz/cookie cache to a fresh temp location and use a
    # single shared requests session. Combined with threads=False (see
    # CONFIG), this avoids the "sqlite3.OperationalError: database is
    # locked" errors that can occur when many tickers are fetched at once.
    try:
        import tempfile
        yf.set_tz_cache_location(tempfile.mkdtemp(prefix="yfinance_cache_"))
    except Exception:
        pass  # older/newer yfinance versions may not expose this; safe to skip

    end = datetime.today()
    start = end - timedelta(days=lookback_days + 30)  # small buffer

    data: dict = {}
    failed: List[str] = []

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f"[download] batch {i // BATCH_SIZE + 1}: {len(batch)} tickers "
              f"({i + 1}-{i + len(batch)} of {len(tickers)})")

        attempt = 0
        raw = None
        while attempt < MAX_RETRIES:
            try:
                raw = yf.download(
                    batch,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    threads=YFINANCE_THREADS,
                    progress=False,
                )
                break
            except Exception as e:
                attempt += 1
                print(f"[download] batch failed (attempt {attempt}): {e}")
                time.sleep(2.0 * attempt)

        if raw is None or raw.empty:
            failed.extend(batch)
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        for t in batch:
            try:
                if len(batch) == 1:
                    df = raw.copy()
                else:
                    if t not in raw.columns.get_level_values(0):
                        failed.append(t)
                        continue
                    df = raw[t].copy()
                df = df.dropna(how="all")
                if df.empty or "Close" not in df.columns:
                    failed.append(t)
                    continue
                df = df.dropna(subset=["Close"])
                if len(df) < max(30, SWING_LEFT + SWING_RIGHT + 5):
                    failed.append(t)
                    continue
                data[t] = df
            except Exception:
                failed.append(t)

        time.sleep(REQUEST_PAUSE_SECONDS)

        if progress_callback is not None:
            try:
                progress_callback(min(i + len(batch), len(tickers)), len(tickers))
            except Exception:
                pass  # never let a UI callback break the actual download

    if failed:
        print(f"[download] {len(failed)} tickers skipped (no usable data): "
              f"{', '.join(failed[:20])}{' ...' if len(failed) > 20 else ''}")

    print(f"[download] usable data for {len(data)} of {len(tickers)} tickers")
    return data


# ----------------------------------------------------------------------
# Support detection
# ----------------------------------------------------------------------

@dataclass
class SupportZone:
    price: float                 # zone price = mean of clustered swing lows
    low: float                   # min of clustered swing lows
    high: float                  # max of clustered swing lows
    touches: int
    touch_dates: List[pd.Timestamp] = field(default_factory=list)
    touch_prices: List[float] = field(default_factory=list)
    strength_score: float = 0.0
    strength_label: str = ""


def find_swing_lows(df: pd.DataFrame, left: int, right: int) -> pd.DataFrame:
    """
    Return the subset of rows in df whose Low is a local minimum:
    lower than the Low of `left` candles before and `right` after.
    """
    lows = df["Low"].values
    n = len(lows)
    idx = []
    for i in range(left, n - right):
        window_left = lows[i - left:i]
        window_right = lows[i + 1:i + 1 + right]
        if lows[i] <= window_left.min() and lows[i] <= window_right.min():
            idx.append(i)
    return df.iloc[idx]


def find_swing_highs(df: pd.DataFrame, left: int, right: int) -> pd.DataFrame:
    """
    The mirror image of find_swing_lows: rows whose High is a local
    maximum — used to detect resistance zones the same way swing lows
    are used to detect support.
    """
    highs = df["High"].values
    n = len(highs)
    idx = []
    for i in range(left, n - right):
        window_left = highs[i - left:i]
        window_right = highs[i + 1:i + 1 + right]
        if highs[i] >= window_left.max() and highs[i] >= window_right.max():
            idx.append(i)
    return df.iloc[idx]


def cluster_support_zones(swing_points: pd.DataFrame, tolerance: float,
                           value_col: str = "Low") -> List[SupportZone]:
    """
    Cluster swing prices that are within `tolerance` (fractional) of
    each other into SupportZone objects. Works for support (value_col=
    "Low", swing lows) and resistance (value_col="High", swing highs)
    alike — the clustering/scoring logic is identical either way, only
    the input points differ.
    """
    if swing_points.empty:
        return []

    pts = swing_points[[value_col]].copy()
    pts["date"] = swing_points.index
    pts = pts.sort_values(value_col)

    zones: List[SupportZone] = []
    current_prices: List[float] = []
    current_dates: List[pd.Timestamp] = []

    def flush():
        if not current_prices:
            return
        arr = np.array(current_prices)
        zones.append(SupportZone(
            price=float(arr.mean()),
            low=float(arr.min()),
            high=float(arr.max()),
            touches=len(arr),
            touch_dates=list(current_dates),
            touch_prices=list(current_prices),
        ))

    ref_price = None
    for _, row in pts.iterrows():
        p = float(row[value_col])
        d = row["date"]
        if ref_price is None:
            ref_price = p
            current_prices = [p]
            current_dates = [d]
            continue
        if abs(p - ref_price) / ref_price <= tolerance:
            current_prices.append(p)
            current_dates.append(d)
            # keep ref_price as running mean so cluster can drift slightly
            ref_price = float(np.mean(current_prices))
        else:
            flush()
            ref_price = p
            current_prices = [p]
            current_dates = [d]
    flush()

    return zones


def score_support_zones(zones: List[SupportZone], as_of: pd.Timestamp,
                         half_life_days: float) -> None:
    """
    Score each zone in place based on: number of touches, recency of
    touches (exponential decay), and touch tightness. Also assigns a
    human-readable strength_label. Used for both support and resistance
    zones — the scoring logic doesn't care which direction the zone is.
    """
    for zone in zones:
        if zone.touches < 1:
            zone.strength_score = 0.0
            zone.strength_label = "Weak"
            continue

        recency_weight = 0.0
        for d in zone.touch_dates:
            age_days = max((as_of - pd.Timestamp(d)).days, 0)
            recency_weight += 0.5 ** (age_days / half_life_days)

        tightness = 1.0
        if zone.high > zone.low > 0:
            spread = (zone.high - zone.low) / zone.price
            tightness = max(0.3, 1.0 - spread * 5)  # tighter clusters score higher

        score = recency_weight * tightness * (1 + 0.15 * (zone.touches - 1))
        zone.strength_score = round(score, 3)

        if zone.touches >= 4 and score >= 2.0:
            zone.strength_label = "Strong"
        elif zone.touches >= 3 and score >= 1.0:
            zone.strength_label = "Strong"
        elif zone.touches >= MIN_TOUCHES and score >= 0.4:
            zone.strength_label = "Moderate"
        else:
            zone.strength_label = "Weak"


def select_nearest_zone(zones: List[SupportZone], current_price: float,
                         direction: str) -> Optional[SupportZone]:
    """
    direction="below": nearest qualifying zone below current_price (support).
    direction="above": nearest qualifying zone above current_price (resistance).
    A zone must have at least MIN_TOUCHES to qualify either way.
    """
    if direction == "below":
        candidates = [z for z in zones if z.price < current_price and z.touches >= MIN_TOUCHES]
        if not candidates:
            return None
        return min(candidates, key=lambda z: current_price - z.price)
    else:
        candidates = [z for z in zones if z.price > current_price and z.touches >= MIN_TOUCHES]
        if not candidates:
            return None
        return min(candidates, key=lambda z: z.price - current_price)


def select_nearest_support(zones: List[SupportZone], current_price: float) -> Optional[SupportZone]:
    """Backward-compatible name — nearest qualifying support zone below current price."""
    return select_nearest_zone(zones, current_price, direction="below")


# ----------------------------------------------------------------------
# Per-ticker analysis
# ----------------------------------------------------------------------

@dataclass
class Candidate:
    ticker: str
    price: float
    support: float
    support_low: float
    support_high: float
    distance_pct: float
    touches: int
    strength: str
    strength_score: float
    avg_volume: float
    lookback_days: int
    volatility_pct: float
    volatility_tier: str
    resistance: Optional[float]
    resistance_distance_pct: Optional[float]
    resistance_touches: Optional[int]
    resistance_strength: Optional[str]
    df: pd.DataFrame
    zone: SupportZone
    all_zones: List[SupportZone]
    resistance_zone: Optional[SupportZone] = None
    from_watchlist: bool = False


def summarize_candidate(c: "Candidate") -> str:
    """
    A one-line, plain-English summary of a candidate, built entirely from
    fields already on the Candidate — no AI, just templating. Meant to be
    easier to skim at a glance than a row of table columns.
    """
    vol_part = (
        f"{c.volatility_tier} volatility ({c.volatility_pct:.0f}%)"
        if not np.isnan(c.volatility_pct) else "volatility unavailable"
    )
    months = round(c.lookback_days / 30)
    period = f"~{months} month{'s' if months != 1 else ''}"

    sentence = (
        f"{c.ticker} is {c.distance_pct:.1f}% above a {c.strength.lower()} support level "
        f"tested {c.touches} time{'s' if c.touches != 1 else ''} in {period}, {vol_part}."
    )

    if c.resistance is not None and c.resistance_distance_pct is not None:
        sentence += (
            f" Nearest resistance is ≈${c.resistance:.2f} "
            f"({c.resistance_distance_pct:.1f}% above current price)."
        )
    else:
        sentence += " No clear resistance found within the lookback window."

    return sentence


def compute_realized_volatility(df: pd.DataFrame, window: int) -> float:
    """
    Annualized realized (historical) volatility, in percent, from daily
    log returns over the last `window` trading days:
        vol% = std(log(close_t / close_t-1)) * sqrt(252) * 100
    This is NOT implied volatility — it's a free proxy computed purely
    from price history, useful as a rough "how much does this move"
    signal but not a substitute for checking the live options chain.
    """
    closes = df["Close"].tail(window + 1)
    if len(closes) < 3:
        return float("nan")
    log_returns = np.log(closes / closes.shift(1)).dropna()
    if log_returns.empty or log_returns.std() == 0:
        return 0.0
    return float(log_returns.std() * np.sqrt(252) * 100)


def volatility_tier(vol_pct: float) -> str:
    """Rough, configurable-in-spirit bucketing for readability in the table."""
    if np.isnan(vol_pct):
        return "Unknown"
    if vol_pct >= 60:
        return "Very High"
    if vol_pct >= 40:
        return "High"
    if vol_pct >= 25:
        return "Moderate"
    return "Low"


def analyze_ticker(ticker: str, df: pd.DataFrame, stats: Optional[dict] = None,
                    bypass_filters: bool = False) -> Optional[Candidate]:
    """
    bypass_filters=True skips the liquidity (MIN_PRICE, MIN_AVG_VOLUME),
    distance-window, and min-volatility filters — used for WATCHLIST
    tickers so they always show up regardless of current filter settings.
    A valid, qualifying support zone is still required either way; this
    is a support scanner, so a ticker with no detectable support simply
    can't produce a Candidate.
    """
    def bump(key):
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    df = df.tail(LOOKBACK_DAYS + 40)  # a little extra so swing detection has context
    if len(df) < max(30, SWING_LEFT + SWING_RIGHT + 5):
        bump("insufficient_history")
        return None

    current_price = float(df["Close"].iloc[-1])
    avg_vol = float(df["Volume"].tail(AVG_VOLUME_WINDOW).mean())

    # --- liquidity / quality filters ---
    if current_price <= 0 or np.isnan(current_price):
        bump("bad_price_data")
        return None
    if not bypass_filters:
        if current_price < MIN_PRICE:
            bump("below_min_price")
            return None
        if avg_vol < MIN_AVG_VOLUME:
            bump("below_min_volume")
            return None

    swing_lows = find_swing_lows(df, SWING_LEFT, SWING_RIGHT)
    if swing_lows.empty:
        bump("no_swing_lows")
        return None

    zones = cluster_support_zones(swing_lows, SUPPORT_CLUSTER_TOLERANCE, value_col="Low")
    if not zones:
        bump("no_support_zones")
        return None

    as_of = pd.Timestamp(df.index[-1])
    score_support_zones(zones, as_of, RECENCY_HALF_LIFE_DAYS)

    best = select_nearest_zone(zones, current_price, direction="below")
    if best is None:
        bump("no_qualifying_support_below_price")
        return None

    distance_pct = (current_price - best.price) / best.price * 100.0

    if not bypass_filters:
        if not (MIN_DISTANCE_FROM_SUPPORT * 100 <= distance_pct <= MAX_DISTANCE_FROM_SUPPORT * 100):
            bump("outside_distance_window")
            return None

    vol_pct = compute_realized_volatility(df, VOLATILITY_WINDOW)

    if not bypass_filters:
        if MIN_VOLATILITY_PCT > 0 and (np.isnan(vol_pct) or vol_pct < MIN_VOLATILITY_PCT):
            bump("below_min_volatility")
            return None

    # --- nearby resistance (informational only — never filters candidates out) ---
    swing_highs = find_swing_highs(df, SWING_LEFT, SWING_RIGHT)
    resistance_zone = None
    if not swing_highs.empty:
        res_zones = cluster_support_zones(swing_highs, SUPPORT_CLUSTER_TOLERANCE, value_col="High")
        if res_zones:
            score_support_zones(res_zones, as_of, RECENCY_HALF_LIFE_DAYS)
            resistance_zone = select_nearest_zone(res_zones, current_price, direction="above")

    if resistance_zone is not None:
        resistance = round(resistance_zone.price, 2)
        resistance_distance_pct = round(
            (resistance_zone.price - current_price) / current_price * 100.0, 2
        )
        resistance_touches = resistance_zone.touches
        resistance_strength = resistance_zone.strength_label
    else:
        resistance = None
        resistance_distance_pct = None
        resistance_touches = None
        resistance_strength = None

    bump("passed")

    return Candidate(
        ticker=ticker,
        price=round(current_price, 2),
        support=round(best.price, 2),
        support_low=round(best.low, 2),
        support_high=round(best.high, 2),
        distance_pct=round(distance_pct, 2),
        touches=best.touches,
        strength=best.strength_label,
        strength_score=best.strength_score,
        avg_volume=round(avg_vol, 0),
        lookback_days=LOOKBACK_DAYS,
        volatility_pct=round(vol_pct, 1) if not np.isnan(vol_pct) else float("nan"),
        volatility_tier=volatility_tier(vol_pct),
        resistance=resistance,
        resistance_distance_pct=resistance_distance_pct,
        resistance_touches=resistance_touches,
        resistance_strength=resistance_strength,
        df=df,
        zone=best,
        all_zones=zones,
        resistance_zone=resistance_zone,
        from_watchlist=False,  # set by run_scan() for watchlist-sourced candidates
    )


# ----------------------------------------------------------------------
# Charting
# ----------------------------------------------------------------------

def build_chart_figure(candidate: Candidate):
    """
    Build (but don't save) the matplotlib Figure for a candidate: candles,
    the swing lows used, the support zone, current price, and the
    max-distance line. Returns None if matplotlib isn't installed.
    Used both by generate_chart() (CLI, saves to disk) and the Streamlit
    app (embeds the figure directly with st.pyplot).
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.patches import Rectangle
    except ImportError:
        return None

    df = candidate.df.tail(150)  # keep charts readable
    fig, ax = plt.subplots(figsize=(11, 6))

    # --- simple candlesticks (no external candlestick dependency needed) ---
    dates = mdates.date2num(df.index.to_pydatetime())
    width = 0.6
    for i, (d, row) in enumerate(zip(dates, df.itertuples())):
        color = "#26a69a" if row.Close >= row.Open else "#ef5350"
        ax.add_line(plt.Line2D([d, d], [row.Low, row.High], color=color, linewidth=0.8))
        ax.add_patch(Rectangle(
            (d - width / 2, min(row.Open, row.Close)),
            width,
            max(abs(row.Close - row.Open), 0.01),
            facecolor=color, edgecolor=color,
        ))

    # --- support zone shading ---
    zone = candidate.zone
    ax.axhspan(zone.low, zone.high, color="#1976d2", alpha=0.15,
               label=f"Support zone ({zone.touches} touches)")
    ax.axhline(zone.price, color="#1976d2", linestyle="--", linewidth=1.2,
               label=f"Support ≈ {zone.price:.2f}")

    # --- swing low touch markers ---
    touch_dates = mdates.date2num([pd.Timestamp(d).to_pydatetime() for d in zone.touch_dates])
    ax.scatter(touch_dates, zone.touch_prices, marker="^", color="#1976d2",
               s=60, zorder=5, label="Swing low (touch)")

    # --- current price + max-distance line ---
    ax.axhline(candidate.price, color="#333333", linestyle="-", linewidth=1,
               label=f"Current price = {candidate.price:.2f}")
    max_line = zone.price * (1 + MAX_DISTANCE_FROM_SUPPORT)
    ax.axhline(max_line, color="#f57c00", linestyle=":", linewidth=1.2,
               label=f"{MAX_DISTANCE_FROM_SUPPORT*100:.0f}% above support = {max_line:.2f}")

    # --- nearby resistance, if any was found ---
    if candidate.resistance_zone is not None:
        rzone = candidate.resistance_zone
        ax.axhspan(rzone.low, rzone.high, color="#c62828", alpha=0.12,
                   label=f"Resistance zone ({rzone.touches} touches)")
        ax.axhline(rzone.price, color="#c62828", linestyle="--", linewidth=1.2,
                   label=f"Resistance ≈ {rzone.price:.2f}")
        res_touch_dates = mdates.date2num(
            [pd.Timestamp(d).to_pydatetime() for d in rzone.touch_dates]
        )
        ax.scatter(res_touch_dates, rzone.touch_prices, marker="v", color="#c62828",
                   s=60, zorder=5, label="Swing high (touch)")

    ax.xaxis_date()
    fig.autofmt_xdate()
    vol_label = f"{candidate.volatility_pct:.0f}% vol" if not np.isnan(candidate.volatility_pct) else "vol n/a"
    ax.set_title(f"{candidate.ticker} — {candidate.distance_pct:.2f}% above support "
                 f"({candidate.strength}, {candidate.touches} touches, {vol_label})")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    return fig


def generate_chart(candidate: Candidate, out_dir: Path) -> Optional[Path]:
    fig = build_chart_figure(candidate)
    if fig is None:
        print("[chart] matplotlib not installed, skipping charts")
        return None

    import matplotlib.pyplot as plt
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{candidate.ticker}.png"
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path


# ----------------------------------------------------------------------
# Output formatting
# ----------------------------------------------------------------------

def print_table(candidates: List[Candidate]) -> None:
    if not candidates:
        print("\nNo stocks currently within "
              f"{MAX_DISTANCE_FROM_SUPPORT*100:.1f}% of a meaningful support level.\n")
        return

    header = (f"{'Rank':>4} {'W':<1} {'Ticker':<7} {'Price':>9} {'Support':>9} {'Distance':>9} "
              f"{'Touches':>8} {'Strength':<9} {'Volatility':>11} {'VolTier':<10} "
              f"{'Resistance':>11} {'ResDist':>9} {'AvgVol':>12}")
    print("\nStocks within "
          f"{MAX_DISTANCE_FROM_SUPPORT*100:.1f}% of meaningful support "
          "(plus any watchlist tickers, marked W)\n")
    print(header)
    print("-" * len(header))
    for i, c in enumerate(candidates, 1):
        vol_str = f"{c.volatility_pct:.1f}%" if not np.isnan(c.volatility_pct) else "n/a"
        res_str = f"{c.resistance:.2f}" if c.resistance is not None else "n/a"
        res_dist_str = f"{c.resistance_distance_pct:.1f}%" if c.resistance_distance_pct is not None else "n/a"
        watch_flag = "*" if c.from_watchlist else ""
        print(f"{i:>4} {watch_flag:<1} {c.ticker:<7} {c.price:>9.2f} {c.support:>9.2f} "
              f"{c.distance_pct:>8.2f}% {c.touches:>8} {c.strength:<9} "
              f"{vol_str:>11} {c.volatility_tier:<10} {res_str:>11} {res_dist_str:>9} "
              f"{c.avg_volume:>12,.0f}")
    print()


def write_csv(candidates: List[Candidate], path: str) -> None:
    rows = []
    for i, c in enumerate(candidates, 1):
        rows.append({
            "rank": i,
            "ticker": c.ticker,
            "price": c.price,
            "support": c.support,
            "support_zone_low": c.support_low,
            "support_zone_high": c.support_high,
            "distance_pct": c.distance_pct,
            "touches": c.touches,
            "strength": c.strength,
            "strength_score": c.strength_score,
            "volatility_pct_annualized": c.volatility_pct,
            "volatility_tier": c.volatility_tier,
            "resistance": c.resistance,
            "resistance_distance_pct": c.resistance_distance_pct,
            "resistance_touches": c.resistance_touches,
            "resistance_strength": c.resistance_strength,
            "avg_volume_20d": c.avg_volume,
            "lookback_days": c.lookback_days,
            "from_watchlist": c.from_watchlist,
            "summary": summarize_candidate(c),
        })
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"[output] wrote {path}")


# ----------------------------------------------------------------------
# News (free, via yfinance — no API key)
# ----------------------------------------------------------------------

def fetch_ticker_news(ticker: str, max_items: int = 6) -> List[dict]:
    """
    Recent news headlines for a single ticker, via yfinance's free news
    feed (no API key, no extra dependency). Returns a list of dicts with
    keys: title, publisher, link, published — most recent first. Returns
    an empty list (never raises) if nothing is available or the request
    fails, so a flaky/empty news response never breaks the app.

    yfinance's raw news payload shape has changed across versions — this
    handles both the older flat schema and the newer nested "content"
    schema so it keeps working across yfinance upgrades.
    """
    import yfinance as yf

    try:
        raw_items = yf.Ticker(ticker).news or []
    except Exception:
        return []

    articles = []
    for item in raw_items[:max_items]:
        try:
            if "content" in item:  # newer nested schema
                content = item["content"]
                title = content.get("title", "")
                publisher = (content.get("provider") or {}).get("displayName", "")
                url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
                link = url_obj.get("url", "")
                published = content.get("pubDate", "")
            else:  # older flat schema
                title = item.get("title", "")
                publisher = item.get("publisher", "")
                link = item.get("link", "")
                ts = item.get("providerPublishTime")
                published = (
                    datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
                )

            if title:
                articles.append({
                    "title": title,
                    "publisher": publisher,
                    "link": link,
                    "published": published,
                })
        except Exception:
            continue  # one malformed article shouldn't drop the rest

    return articles


# ----------------------------------------------------------------------
# Reusable scan engine (shared by the CLI below and streamlit_app.py)
# ----------------------------------------------------------------------

@dataclass
class ScanResult:
    candidates: List[Candidate]
    stats: dict
    universe_size: int
    usable_data_count: int
    errors: int


def run_scan(progress_callback=None, log=print) -> ScanResult:
    """
    Run one full scan: build the universe, download price data, analyze
    every ticker, and return the results. This is the one place the CLI
    and the Streamlit web app both call, so they never drift out of sync.

    progress_callback(done, total), if given, is forwarded to the
    downloader for a UI progress bar. log(msg), if given, receives the
    same status lines the CLI prints (defaults to print; pass a no-op to
    silence them).
    """
    log("=" * 70)
    log("FREE SUPPORT-LEVEL STOCK SCANNER")
    log("=" * 70)
    log(f"Config: lookback={LOOKBACK_DAYS}d, swing={SWING_LEFT}/{SWING_RIGHT}, "
        f"cluster_tol={SUPPORT_CLUSTER_TOLERANCE*100:.1f}%, "
        f"max_distance={MAX_DISTANCE_FROM_SUPPORT*100:.1f}%, "
        f"min_price={MIN_PRICE}, min_avg_vol={MIN_AVG_VOLUME:,}")

    universe = get_universe()
    if not universe:
        log("No tickers to scan — check your internet connection / Wikipedia access.")
        return ScanResult([], {}, 0, 0, 0)

    # Watchlist tickers must be downloaded even if they aren't in the
    # chosen index universe, so they can still be analyzed and shown.
    watchlist_clean = [t.strip().upper() for t in WATCHLIST if t.strip()]
    if watchlist_clean:
        universe = sorted(set(universe) | set(watchlist_clean))

    price_data = download_price_data(universe, LOOKBACK_DAYS, progress_callback)

    candidates: List[Candidate] = []
    errors = 0
    stats: dict = {}
    for ticker, df in price_data.items():
        try:
            c = analyze_ticker(ticker, df, stats)
            if c is not None:
                candidates.append(c)
        except Exception:
            errors += 1

    # Second pass: make sure every watchlist ticker is represented,
    # bypassing the distance/volatility/liquidity filters for them. A
    # ticker already present from the normal pass just gets flagged;
    # one that was filtered out gets re-analyzed without those filters.
    if watchlist_clean:
        present = {c.ticker for c in candidates}
        for ticker in watchlist_clean:
            if ticker in present:
                for c in candidates:
                    if c.ticker == ticker:
                        c.from_watchlist = True
                continue
            df = price_data.get(ticker)
            if df is None:
                continue
            try:
                c = analyze_ticker(ticker, df, stats, bypass_filters=True)
                if c is not None:
                    c.from_watchlist = True
                    candidates.append(c)
            except Exception:
                errors += 1

    if errors:
        log(f"[analyze] {errors} tickers raised errors during analysis and were skipped")

    log("[analyze] breakdown of why tickers were excluded:")
    for key in ("insufficient_history", "bad_price_data", "below_min_price",
                "below_min_volume", "no_swing_lows", "no_support_zones",
                "no_qualifying_support_below_price", "outside_distance_window",
                "below_min_volatility", "passed"):
        if key in stats:
            log(f"    {key:<35} {stats[key]}")

    # Watchlist items always float to the top, regardless of sort order;
    # within each group, keep the usual closest-to-support-first order.
    candidates.sort(key=lambda c: (not c.from_watchlist, c.distance_pct))

    return ScanResult(
        candidates=candidates,
        stats=stats,
        universe_size=len(universe),
        usable_data_count=len(price_data),
        errors=errors,
    )


# ----------------------------------------------------------------------
# Main (CLI entry point)
# ----------------------------------------------------------------------

def main():
    result = run_scan()
    candidates = result.candidates

    print_table(candidates)
    write_csv(candidates, OUTPUT_CSV)

    if GENERATE_CHARTS and candidates:
        out_dir = Path(CHARTS_DIR)
        n_charts = min(len(candidates), MAX_CHARTS)
        print(f"[chart] generating {n_charts} chart(s) in ./{CHARTS_DIR}/ ...")
        for c in candidates[:n_charts]:
            try:
                generate_chart(c, out_dir)
            except Exception as e:
                print(f"[chart] failed for {c.ticker}: {e}")
        print("[chart] done")

    print("\nReminder: these are candidate technical setups only, not trade "
          "recommendations. Verify support quality, trend, volume, and "
          "options liquidity manually before acting on anything here.\n")


if __name__ == "__main__":
    main()
