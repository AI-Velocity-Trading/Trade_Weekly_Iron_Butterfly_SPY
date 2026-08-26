#!/usr/bin/env python3
"""
weekly_iron_butterfly_spy_backtest_dynamic.py
==============================================
Backtest of the SPY Weekly Iron Butterfly strategy with DYNAMIC entry/exit
timing, driven by the underlying's own realized 1-min price movement
(backtests/underlying-tickers/SPY.csv — SIP 1-min bars) instead of the fixed
Monday-open / Thursday-close clock times used by weekly_iron_butterfly_spy_backtest.py.

Same 4-leg Iron Butterfly structure (WING_WIDTH=10, QTY=40):
    longPut   @ ATM − WING_WIDTH  (buy)
    shortPut  @ ATM               (sell)
    shortCall @ ATM               (sell)
    longCall  @ ATM + WING_WIDTH  (buy)

Timing logic (driven purely by SPY.csv price bars, no option data needed):

  ENTRY: scanned on MONDAY only, within ENTRY_SEARCH_START..ENTRY_SEARCH_END
    (9:35-10:30 ET). Picks the minute whose trailing ENTRY_SETTLE_WINDOW_MIN
    -minute high-low range is smallest — the quietest point after the open.
    ATM strike is set from that minute's open price.

  EXIT: scanned forward from Monday's entry minute, across Tuesday and
    Wednesday, through Thursday at EXIT_DEFAULT_HOUR:MIN (3:40 PM ET) at the
    LATEST — the week is always closed out by then, matching the user's
    "starts Monday, ends Thursday at 3:40 pm at the latest" requirement.
    If SPY's high/low ever moves further than BREACH_FRACTION * WING_WIDTH
    away from the ATM strike, exits immediately at that minute
    ("underlying_breach"). Otherwise falls back to the last available bar
    at/before Thursday 3:40 PM ET ("scheduled").

  Auto-exit simulation (checked at the resolved exit minute):
    +90% of max profit  → exit early (profit target)
    -80% of max profit  → exit early (stop loss)

Filters (kept from weekly_iron_butterfly_spy_backtest.py, now tunable via
--optimize): MAX_CREDIT_PCT, MAX_PRIOR_RANGE_PCT, MAX_GAP_PCT.

Pricing data:
  Underlying:  backtests/underlying-tickers/SPY.csv (SIP 1-min bars) — NOT
               re-fetched from Alpaca; run backtests/get_underlying_ticker_prices.py
               first to (re)generate it.
  Options:     Alpaca OPRA options bars — daily and 1-min. 1-min bars used
               when available; daily is the fallback.

Usage:
  python3 weekly_iron_butterfly_spy_backtest_dynamic.py
  python3 weekly_iron_butterfly_spy_backtest_dynamic.py --csv results.csv
  python3 weekly_iron_butterfly_spy_backtest_dynamic.py --optimize
"""

from __future__ import annotations

import os
import sys
import csv
import math
import argparse
import itertools
import logging
from datetime import datetime, date, timedelta

import requests
from dotenv import load_dotenv

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bt_spy_weekly_ibf_dyn")

# ── Config ────────────────────────────────────────────────────────────────────
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_FILE)

DATA_KEY    = os.getenv("apiDataKey",       "")
DATA_SECRET = os.getenv("apiDataSecret",    "")
TRADE_KEY   = os.getenv("apiKeyAIV011P",    "")
TRADE_SEC   = os.getenv("apiSecretAIV011P", "")

DATA_BASE  = "https://data.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"

# Strategy parameters (mirrors weekly_iron_butterfly_spy.py)
TICKER     = "SPY"
QTY        = 40
WING_WIDTH = 10   # $10 wings (ATM ± 10)

# Auto-exit thresholds (fraction of max profit — applied at the resolved exit bar)
PROFIT_TARGET_PCT = 0.90
STOP_LOSS_PCT     = -0.80

# Backtest window — start fixed, end = today
BT_START = (date.fromisoformat(os.getenv("BT_START_OVERRIDE")) if os.getenv("BT_START_OVERRIDE") else date(2024, 1, 1))
BT_END   = (date.fromisoformat(os.getenv("BT_END_OVERRIDE")) if os.getenv("BT_END_OVERRIDE") else date.today() - timedelta(days=1))

# ── Dynamic entry/exit timing (driven by underlying-tickers/SPY.csv) ─────────
UNDERLYING_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "underlying-tickers", f"{TICKER}.csv"
)
# Entry: quietest minute (smallest trailing range) on Monday, within this window.
ENTRY_SEARCH_START = (9, 35)
ENTRY_SEARCH_END   = (10, 0)    # optimizer found narrower/earlier beats 10:30 (5yr sweep, 2026-08-26)
ENTRY_SETTLE_WINDOW_MIN = 5     # optimizer found 5min beats 20min (5yr sweep, 2026-08-26)
# Exit: hard cutoff — the week is always closed by Thursday at this ET time,
# at the latest (per the "starts Monday, ends Thursday 3:40 pm" requirement).
EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN = 15, 0   # optimizer found 15:00 beats 15:40 (5yr sweep, 2026-08-26)
# Exit early ("underlying_breach") once price moves this fraction of WING_WIDTH away from ATM.
BREACH_FRACTION = 1.0

# ── Trade filters (set to None to disable) ───────────────────────────────────
# 1. Credit / wing filter — high credit = market expects a large move → skip.
MAX_CREDIT_PCT: float | None = None         # disabled — optimizer found no credit filter helps
#
# 2. Prior-week realized range filter — skip high-volatility regime weeks.
MAX_PRIOR_RANGE_PCT: float | None = None    # disabled — optimizer found no prior-range filter helps (5yr sweep, 2026-08-26)
#
# 3. Monday gap filter — skip when SPY already gapped hard at the open.
MAX_GAP_PCT: float | None = 0.008           # skip if Monday open gaps > 0.8% from prior close


# ── OCC symbol helpers ────────────────────────────────────────────────────────

def occ_symbol(underlying: str, exp: date, opt_type: str, strike: float) -> str:
    """Build OCC symbol with 6-char padded underlying. e.g. 'SPY   260117P00580000'"""
    strike_int = round(strike * 1000)
    root = underlying.ljust(6)
    return f"{root}{exp.strftime('%y%m%d')}{opt_type.upper()}{strike_int:08d}"


def snap_key(occ: str) -> str:
    """Strip spaces → unpadded OCC used as API / dict key."""
    return occ.replace(" ", "")


# ── Market calendar ───────────────────────────────────────────────────────────

# NYSE market holidays for 2024-2027 (used when Alpaca calendar API is unavailable)
_NYSE_HOLIDAYS: frozenset[date] = frozenset([
    # 2024
    date(2024,  1,  1), date(2024,  1, 15), date(2024,  2, 19), date(2024,  3, 29),
    date(2024,  5, 27), date(2024,  6, 19), date(2024,  7,  4), date(2024,  9,  2),
    date(2024, 11, 28), date(2024, 12, 25),
    # 2025
    date(2025,  1,  1), date(2025,  1,  9), date(2025,  1, 20), date(2025,  2, 17),
    date(2025,  4, 18), date(2025,  5, 26), date(2025,  6, 19), date(2025,  7,  4),
    date(2025,  9,  1), date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026,  1,  1), date(2026,  1, 19), date(2026,  2, 16), date(2026,  4,  3),
    date(2026,  5, 25), date(2026,  6, 19), date(2026,  7,  3), date(2026,  9,  7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027,  1,  1), date(2027,  1, 18), date(2027,  2, 15), date(2027,  3, 26),
    date(2027,  5, 31), date(2027,  6, 18), date(2027,  7,  5), date(2027,  9,  6),
    date(2027, 11, 25), date(2027, 12, 24),
])


def get_trading_days(start: date, end: date) -> list[date]:
    hdrs = {
        "apca-api-key-id":     TRADE_KEY,
        "apca-api-secret-key": TRADE_SEC,
    }
    try:
        r = requests.get(
            f"{PAPER_BASE}/v2/calendar",
            headers=hdrs,
            params={"start": start.isoformat(), "end": end.isoformat()},
            timeout=15,
        )
        if r.ok:
            return sorted(date.fromisoformat(d["date"]) for d in r.json())
        log.warning("Calendar HTTP %d — falling back to Mon–Fri", r.status_code)
    except Exception as exc:
        log.warning("Calendar fetch failed: %s — falling back to Mon–Fri", exc)

    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5 and cur not in _NYSE_HOLIDAYS:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def next_friday_from(d: date, trade_day_set: set) -> date:
    """Next Friday that is an actual trading day.
    If that Friday is a holiday, advances by one week to the following Friday.
    """
    days_ahead = (4 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    candidate = d + timedelta(days=days_ahead)
    for _ in range(8):
        if candidate in trade_day_set:
            return candidate
        candidate += timedelta(days=7)
    return d + timedelta(days=days_ahead)  # fallback: original Friday


# ── Underlying price loader (from underlying-tickers/SPY.csv, NOT Alpaca) ────

def load_underlying_minute_bars(csv_path: str) -> dict[date, list[tuple[tuple[int, int], dict]]]:
    """
    Loads backtests/underlying-tickers/<TICKER>.csv into
    {date: [((hour, min), bar), ...]}, each day's bars sorted chronologically.
    """
    by_day: dict[date, list[tuple[tuple[int, int], dict]]] = {}
    if not os.path.exists(csv_path):
        log.error("Underlying CSV not found: %s — run get_underlying_ticker_prices.py first.", csv_path)
        return by_day
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            d = date.fromisoformat(row["date_et"])
            h, m, _ = row["time_et"].split(":")
            bar = {
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            }
            by_day.setdefault(d, []).append(((int(h), int(m)), bar))
    for d in by_day:
        by_day[d].sort(key=lambda x: x[0])
    log.info("Underlying bars loaded: %d days from %s", len(by_day), csv_path)
    return by_day


def find_optimal_entry(
    day_bars: list[tuple[tuple[int, int], dict]],
    settle_window: int = ENTRY_SETTLE_WINDOW_MIN,
    search_start: tuple[int, int] = ENTRY_SEARCH_START,
    search_end: tuple[int, int] = ENTRY_SEARCH_END,
) -> tuple[tuple[int, int], dict] | None:
    """
    Picks the minute within search_start..search_end whose trailing
    settle_window-minute high-low range is smallest — the quietest point after
    the open, once opening-range volatility has settled.
    """
    window = [(t, b) for t, b in day_bars if search_start <= t <= search_end]
    if len(window) < settle_window:
        return window[0] if window else None

    best, best_range = None, None
    for i in range(settle_window - 1, len(window)):
        trailing = window[i - settle_window + 1: i + 1]
        rng = max(b["high"] for _, b in trailing) - min(b["low"] for _, b in trailing)
        if best_range is None or rng < best_range:
            best, best_range = window[i], rng
    return best


def find_dynamic_exit_week(
    week_bars: list[tuple[date, tuple[int, int], dict]],
    entry_date: date,
    entry_time: tuple[int, int],
    thursday: date,
    atm: float,
    breach_fraction: float = BREACH_FRACTION,
    exit_cutoff: tuple[int, int] = (EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN),
) -> tuple[date, tuple[int, int], dict, str]:
    """
    Scans forward from (entry_date, entry_time) across the rest of the week,
    hard-stopping at (thursday, exit_cutoff) — the week is never held past
    that Thursday ET time. Exits immediately the first minute SPY's
    high/low moves further than breach_fraction * WING_WIDTH away from ATM
    ("underlying_breach"); otherwise falls back to the last bar at/before the
    Thursday cutoff ("scheduled").
    """
    breach_dist = breach_fraction * WING_WIDTH
    cutoff = (thursday, exit_cutoff)
    fallback: tuple[date, tuple[int, int], dict] | None = None

    for d, t, b in week_bars:
        if (d, t) <= (entry_date, entry_time):
            continue
        if (d, t) > cutoff:
            break
        fallback = (d, t, b)
        if (b["high"] - atm) > breach_dist or (atm - b["low"]) > breach_dist:
            return d, t, b, "underlying_breach"

    if fallback:
        return fallback[0], fallback[1], fallback[2], "scheduled"
    if week_bars:
        d, t, b = week_bars[-1]
        return d, t, b, "scheduled"
    return entry_date, entry_time, {"open": atm, "high": atm, "low": atm, "close": atm}, "scheduled"


# ── Options bar fetchers ──────────────────────────────────────────────────────

def fetch_options_bars_batch_daily(
    symbols: list[str], start: date, end: date
) -> dict[str, dict[date, dict]]:
    """Daily OPRA option bars. Returns {unpadded_sym: {date: {open, close}}}."""
    hdrs = {
        "apca-api-key-id":     DATA_KEY,
        "apca-api-secret-key": DATA_SECRET,
    }
    result: dict[str, dict[date, dict]] = {s: {} for s in symbols}
    BATCH = 50
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i: i + BATCH]
        cursor = None
        while True:
            params: dict = {
                "timeframe": "1Day",
                "start":     start.isoformat(),
                "end":       end.isoformat(),
                "limit":     1000,
            }
            if cursor:
                params["page_token"] = cursor
            try:
                sym_qs  = "symbols=" + ",".join(batch)
                qs      = "&".join(f"{k}={v}" for k, v in params.items())
                req_url = f"{DATA_BASE}/v1beta1/options/bars?{sym_qs}&{qs}"
                r = requests.get(req_url, headers=hdrs, timeout=30)
                if not r.ok:
                    log.warning("Options daily bars HTTP %d: %s", r.status_code, r.text[:200])
                    break
                data = r.json()
            except Exception as exc:
                log.warning("Options daily bars batch failed: %s", exc)
                break
            for sym, bar_list in data.get("bars", {}).items():
                for b in bar_list:
                    d = date.fromisoformat(b["t"][:10])
                    result.setdefault(sym, {})[d] = {
                        "open":  float(b["o"]),
                        "close": float(b["c"]),
                    }
            cursor = data.get("next_page_token")
            if not cursor:
                break
    found = sum(1 for v in result.values() if v)
    log.info("Options daily bars: %d/%d symbols have data", found, len(symbols))
    return result


def fetch_options_bars_batch_1min(
    symbols: list[str], start: date, end: date
) -> dict[str, dict[str, dict]]:
    """
    1-minute OPRA option bars. Returns {unpadded_sym: {iso_ts_str: {open, close}}}.
    Uses concurrent batch requests (ThreadPoolExecutor) to speed up fetching.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    hdrs = {
        "apca-api-key-id":     DATA_KEY,
        "apca-api-secret-key": DATA_SECRET,
    }
    result: dict[str, dict[str, dict]] = {s: {} for s in symbols}
    BATCH = 10

    def _fetch_batch(batch: list[str]) -> dict[str, dict[str, dict]]:
        batch_result: dict[str, dict[str, dict]] = {}
        cursor = None
        while True:
            params: dict = {
                "timeframe": "1Min",
                "start":     f"{start.isoformat()}T09:00:00Z",
                "end":       f"{end.isoformat()}T23:59:59Z",
                "limit":     10000,
            }
            if cursor:
                params["page_token"] = cursor
            try:
                sym_qs  = "symbols=" + ",".join(batch)
                qs      = "&".join(f"{k}={v}" for k, v in params.items())
                req_url = f"{DATA_BASE}/v1beta1/options/bars?{sym_qs}&{qs}"
                r = requests.get(req_url, headers=hdrs, timeout=90)
                if not r.ok:
                    log.warning("Options 1-min bars HTTP %d: %s", r.status_code, r.text[:200])
                    break
                data = r.json()
            except Exception as exc:
                log.warning("Options 1-min bars batch failed: %s", exc)
                break
            for sym, bar_list in data.get("bars", {}).items():
                for b in bar_list:
                    batch_result.setdefault(sym, {})[b["t"]] = {
                        "open":  float(b["o"]),
                        "close": float(b["c"]),
                    }
            cursor = data.get("next_page_token")
            if not cursor:
                break
        return batch_result

    batches = [symbols[i: i + BATCH] for i in range(0, len(symbols), BATCH)]
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_fetch_batch, batch) for batch in batches]
        for future in as_completed(futures):
            for sym, bars in future.result().items():
                result[sym].update(bars)
    found = sum(1 for v in result.values() if v)
    log.info("Options 1-min bars: %d/%d symbols have data", found, len(symbols))
    return result


# ── Price lookup helpers ──────────────────────────────────────────────────────

def _et_hour_min(ts_str: str) -> tuple[int, int]:
    """Convert a UTC ISO timestamp string to an (hour, minute) tuple in ET."""
    ts = ts_str.rstrip("Z").replace("+00:00", "")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return (-1, -1)
    d = dt.date()
    offset_hours = -4 if d >= date(2026, 3, 8) else -5
    total_minutes = dt.hour * 60 + dt.minute + offset_hours * 60
    total_minutes %= 1440
    return (total_minutes // 60, total_minutes % 60)


def find_1min_bar(
    min_bars: dict[str, dict], target_date: date, target_et_hour: int, target_et_min: int,
) -> dict | None:
    """Find the 1-min bar on target_date whose ET hour:min matches exactly."""
    for ts, bar in min_bars.items():
        if ts[:10] != target_date.isoformat():
            continue
        h, m = _et_hour_min(ts)
        if h == target_et_hour and m == target_et_min:
            return bar
    return None


def get_entry_price(sym: str, d: date, entry_hour: int, entry_min: int,
                    opt_1min: dict[str, dict[str, dict]],
                    opt_daily: dict[str, dict[date, dict]]) -> float | None:
    """Entry: open of the dynamically-resolved entry-minute 1-min bar; fallback to daily open."""
    bar = find_1min_bar(opt_1min.get(sym, {}), d, entry_hour, entry_min)
    if bar:
        return bar["open"]
    daily = opt_daily.get(sym, {}).get(d)
    if daily:
        return daily["open"]
    return None


def get_exit_price(sym: str, d: date, exit_hour: int, exit_min: int,
                   opt_1min: dict[str, dict[str, dict]],
                   opt_daily: dict[str, dict[date, dict]]) -> float | None:
    """Exit: close of the dynamically-resolved exit-minute 1-min bar; fallback to daily close."""
    bar = find_1min_bar(opt_1min.get(sym, {}), d, exit_hour, exit_min)
    if bar:
        return bar["close"]
    daily = opt_daily.get(sym, {}).get(d)
    if daily:
        return daily["close"]
    return None


# ── Core backtest ─────────────────────────────────────────────────────────────

def collect_candidates(
    trade_day_set: set, all_trade_days: list[date], entry_settle_window: int = ENTRY_SETTLE_WINDOW_MIN,
    entry_search_start: tuple[int, int] = ENTRY_SEARCH_START,
    entry_search_end: tuple[int, int] = ENTRY_SEARCH_END,
) -> tuple[list[dict], dict, dict]:
    """
    Resolves each week's Monday ENTRY (independent of any post-entry sweepable
    filter, but driven by entry_settle_window — the optimizer re-runs this once
    per settle-window value, since a different entry minute means a different
    ATM strike/option symbols). Exit time is NOT resolved here since it depends
    on breach_fraction (a sweepable param); apply_filters() resolves it per call
    using the already-built week_bars — no re-fetch.
    """
    log.info("Loading SPY underlying bars from %s …", UNDERLYING_CSV)
    underlying_bars = load_underlying_minute_bars(UNDERLYING_CSV)

    if len(underlying_bars) < 5:
        log.error("Not enough underlying bar data — aborting.")
        return [], {}, {}

    mondays = [d for d in all_trade_days if BT_START <= d <= BT_END and d.weekday() == 0]
    log.info("%d Mondays in window", len(mondays))

    candidates: list[dict] = []
    sym_set: set[str] = set()
    sym_list: list[str] = []

    for monday in mondays:
        # Thursday = 3rd trading day after Monday, advanced past holidays.
        thursday = None
        for offset in range(7):
            cand_date = monday + timedelta(days=3 + offset)
            if cand_date in trade_day_set:
                thursday = cand_date
                break
        if thursday is None:
            continue

        week_days = [monday] + [d for d in all_trade_days if monday < d <= thursday]
        if not week_days or week_days[-1] != thursday:
            continue

        monday_bars = underlying_bars.get(monday)
        if not monday_bars:
            log.debug("%s: no underlying bars — skipping week", monday)
            continue

        entry_candidate = find_optimal_entry(monday_bars, entry_settle_window, entry_search_start, entry_search_end)
        if entry_candidate is None:
            log.debug("%s: no entry candidate in search window — skipping week", monday)
            continue
        entry_time, entry_bar = entry_candidate
        spy_price = entry_bar["open"]
        if not spy_price or spy_price <= 0:
            continue

        atm    = round(spy_price)
        expiry = next_friday_from(monday, trade_day_set)

        lp_sym = snap_key(occ_symbol(TICKER, expiry, "P", atm - WING_WIDTH))
        sp_sym = snap_key(occ_symbol(TICKER, expiry, "P", atm))
        sc_sym = snap_key(occ_symbol(TICKER, expiry, "C", atm))
        lc_sym = snap_key(occ_symbol(TICKER, expiry, "C", atm + WING_WIDTH))
        for sym in (lp_sym, sp_sym, sc_sym, lc_sym):
            if sym not in sym_set:
                sym_list.append(sym)
                sym_set.add(sym)

        # Flatten Monday→Thursday 1-min bars into one chronological list for the exit scan.
        # Restricted to regular trading hours (9:30-16:00 ET) so thin pre/post-market
        # bars on Tue/Wed/Thu don't trigger spurious "underlying_breach" wicks.
        week_bars: list[tuple[date, tuple[int, int], dict]] = []
        for d in week_days:
            for t, b in underlying_bars.get(d, []):
                if (9, 30) <= t <= (16, 0):
                    week_bars.append((d, t, b))

        # Prior-week realized range (from the same 1-min CSV — no extra API calls).
        prior_days  = [d for d in all_trade_days if monday - timedelta(days=8) <= d < monday]
        prior_ohlc  = []
        for d in prior_days:
            db = underlying_bars.get(d)
            if db:
                prior_ohlc.append({
                    "high":  max(b["high"] for _, b in db),
                    "low":   min(b["low"]  for _, b in db),
                    "close": db[-1][1]["close"],
                })
        prior_week_range_pct = None
        if len(prior_ohlc) >= 3:
            pw_range = max(o["high"] for o in prior_ohlc) - min(o["low"] for o in prior_ohlc)
            pw_close = prior_ohlc[-1]["close"]
            prior_week_range_pct = pw_range / pw_close if pw_close else None

        # Monday gap vs prior trading day's close.
        prior_trading_days = [d for d in all_trade_days if d < monday]
        monday_gap_pct = None
        if prior_trading_days:
            prev_db = underlying_bars.get(prior_trading_days[-1])
            if prev_db:
                prev_close = prev_db[-1][1]["close"]
                if prev_close:
                    monday_gap_pct = abs(spy_price - prev_close) / prev_close

        candidates.append({
            "monday":               monday,
            "thursday":             thursday,
            "week_bars":            week_bars,
            "expiry":               expiry,
            "spy_open":             spy_price,
            "atm":                  atm,
            "lp_sym":               lp_sym,
            "sp_sym":               sp_sym,
            "sc_sym":               sc_sym,
            "lc_sym":               lc_sym,
            "entry_time":           entry_time,
            "entry_hour":           entry_time[0],
            "entry_min":            entry_time[1],
            "prior_week_range_pct": prior_week_range_pct,
            "monday_gap_pct":       monday_gap_pct,
        })

    log.info("Pass 1 complete: %d candidates, %d unique option symbols",
             len(candidates), len(sym_list))

    fetch_end = BT_END + timedelta(days=7)  # safety margin: Thursday exits can land after BT_END
    log.info("Pass 2: fetching daily options bars for %d unique symbols …", len(sym_list))
    opt_daily = fetch_options_bars_batch_daily(sym_list, BT_START - timedelta(days=3), fetch_end)

    log.info("Pass 3: fetching 1-min options bars for %d unique symbols …", len(sym_list))
    opt_1min = fetch_options_bars_batch_1min(sym_list, BT_START - timedelta(days=2), fetch_end)

    return candidates, opt_daily, opt_1min


def default_filter_params() -> dict:
    return {
        "max_credit_pct":      MAX_CREDIT_PCT,
        "max_prior_range_pct": MAX_PRIOR_RANGE_PCT,
        "max_gap_pct":         MAX_GAP_PCT,
        "breach_fraction":     BREACH_FRACTION,
        "exit_cutoff":         (EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN),
    }


def apply_filters(candidates: list[dict], opt_daily: dict, opt_1min: dict, params: dict) -> list[dict]:
    """
    Resolves each week's exit time (using params['breach_fraction']) and
    applies every tunable filter threshold from `params`. Pure in-memory —
    no network calls — so this is cheap to re-run once per optimizer combo.
    """
    trades: list[dict] = []
    equity = 0.0

    for cand in candidates:
        monday, thursday = cand["monday"], cand["thursday"]

        # ── Pre-filters (SPY price only, no option data needed) ─────────────
        if (params["max_prior_range_pct"] is not None
                and cand["prior_week_range_pct"] is not None
                and cand["prior_week_range_pct"] > params["max_prior_range_pct"]):
            continue
        if (params["max_gap_pct"] is not None
                and cand["monday_gap_pct"] is not None
                and cand["monday_gap_pct"] > params["max_gap_pct"]):
            continue
        # ─────────────────────────────────────────────────────────────────────

        exit_date, exit_time, exit_bar, underlying_exit_reason = find_dynamic_exit_week(
            cand["week_bars"], monday, cand["entry_time"], thursday, cand["atm"],
            params["breach_fraction"], params.get("exit_cutoff", (EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN)),
        )
        exit_hour, exit_min = exit_time

        lp_entry = get_entry_price(cand["lp_sym"], monday, cand["entry_hour"], cand["entry_min"], opt_1min, opt_daily)
        sp_entry = get_entry_price(cand["sp_sym"], monday, cand["entry_hour"], cand["entry_min"], opt_1min, opt_daily)
        sc_entry = get_entry_price(cand["sc_sym"], monday, cand["entry_hour"], cand["entry_min"], opt_1min, opt_daily)
        lc_entry = get_entry_price(cand["lc_sym"], monday, cand["entry_hour"], cand["entry_min"], opt_1min, opt_daily)

        lp_exit = get_exit_price(cand["lp_sym"], exit_date, exit_hour, exit_min, opt_1min, opt_daily)
        sp_exit = get_exit_price(cand["sp_sym"], exit_date, exit_hour, exit_min, opt_1min, opt_daily)
        sc_exit = get_exit_price(cand["sc_sym"], exit_date, exit_hour, exit_min, opt_1min, opt_daily)
        lc_exit = get_exit_price(cand["lc_sym"], exit_date, exit_hour, exit_min, opt_1min, opt_daily)

        if None in (lp_entry, sp_entry, sc_entry, lc_entry,
                    lp_exit,  sp_exit,  sc_exit,  lc_exit):
            continue

        net_credit_open = (sp_entry + sc_entry) - (lp_entry + lc_entry)
        if net_credit_open <= 0:
            continue

        # ── Credit / wing filter ─────────────────────────────────────────────
        credit_pct = net_credit_open / WING_WIDTH
        if params["max_credit_pct"] is not None and credit_pct > params["max_credit_pct"]:
            continue
        # ─────────────────────────────────────────────────────────────────────

        net_credit_close = (sp_exit + sc_exit) - (lp_exit + lc_exit)

        max_profit = net_credit_open * 100 * QTY
        max_loss   = max(0.0, WING_WIDTH - net_credit_open) * 100 * QTY

        pnl_at_close = (net_credit_open - net_credit_close) * 100 * QTY
        exit_reason  = "scheduled"
        if max_profit > 0:
            pnl_pct = pnl_at_close / max_profit
            if pnl_pct >= PROFIT_TARGET_PCT:
                exit_reason = "profit_target_90pct"
            elif pnl_pct <= STOP_LOSS_PCT:
                exit_reason = "stop_loss_80pct"

        pnl = round(pnl_at_close, 2)
        equity += pnl

        trades.append({
            "monday":           monday.isoformat(),
            "exit_date":        exit_date.isoformat(),
            "expiry":           cand["expiry"].isoformat(),
            "entry_time":       "%02d:%02d" % (cand["entry_hour"], cand["entry_min"]),
            "exit_time":        "%02d:%02d" % (exit_hour, exit_min),
            "underlying_exit_reason": underlying_exit_reason,
            "spy_open":         round(cand["spy_open"], 2),
            "spy_exit":         round(exit_bar["close"], 2),
            "atm":              cand["atm"],
            "lp_K":             cand["atm"] - WING_WIDTH,
            "sp_K":             cand["atm"],
            "sc_K":             cand["atm"],
            "lc_K":             cand["atm"] + WING_WIDTH,
            "lp_entry":         round(lp_entry, 4),
            "sp_entry":         round(sp_entry, 4),
            "sc_entry":         round(sc_entry, 4),
            "lc_entry":         round(lc_entry, 4),
            "lp_exit":          round(lp_exit, 4),
            "sp_exit":          round(sp_exit, 4),
            "sc_exit":          round(sc_exit, 4),
            "lc_exit":          round(lc_exit, 4),
            "net_credit_open":  round(net_credit_open, 4),
            "net_credit_close": round(net_credit_close, 4),
            "max_profit":       round(max_profit, 2),
            "max_loss":         round(max_loss, 2),
            "exit_reason":      exit_reason,
            "pnl":              pnl,
            "cum_pnl":          round(equity, 2),
        })

    return trades


def run_backtest(candidates: list[dict], opt_daily: dict, opt_1min: dict, params: dict | None = None) -> list[dict]:
    """Thin wrapper: params=None uses module-constant defaults (default_filter_params())."""
    if params is None:
        params = default_filter_params()
    trades = apply_filters(candidates, opt_daily, opt_1min, params)
    log.info("Backtest complete: %d trades executed (of %d candidates)", len(trades), len(candidates))
    return trades


def compute_summary(trades: list[dict], trading_days_in_report: int) -> dict:
    """Shared summary-stat calc reused by print_results() and the optimizer."""
    if not trades:
        return {"n": 0, "total": 0.0, "sharpe": 0.0, "win_rate": 0.0, "max_dd": 0.0}
    n      = len(trades)
    wins   = [t for t in trades if t["pnl"] > 0]
    total  = sum(t["pnl"] for t in trades)
    avg    = total / n
    win_rate = len(wins) / n * 100
    pnl_vals = [t["pnl"] for t in trades]
    pnl_std  = math.sqrt(sum((v - avg) ** 2 for v in pnl_vals) / max(1, n - 1))
    sharpe   = (avg / pnl_std * math.sqrt(52)) if pnl_std > 0 else 0.0
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": n, "total": total, "avg": avg, "win_rate": win_rate,
        "sharpe": sharpe, "max_dd": max_dd,
        "avg_daily_profit": total / trading_days_in_report if trading_days_in_report else 0.0,
    }


def _fmt(v, spec: str = "%.3f") -> str:
    return "None" if v is None else (spec % v)


def run_optimization(
    trade_day_set: set, all_trade_days: list[date], trading_days_in_report: int,
    target_rate: float = 50.0, tolerance: float = 10.0, top: int = 15,
) -> None:
    """
    Full entry+exit optimizer. Outer loop sweeps entry-side knobs — settle
    window AND entry search window (start/end) — since any of these picks a
    different Monday minute, and therefore a different ATM strike/option
    symbols, so collect_candidates() is re-run — with its option-data fetch —
    once per combo. Inner loop sweeps MAX_CREDIT_PCT / MAX_PRIOR_RANGE_PCT /
    MAX_GAP_PCT / BREACH_FRACTION / exit cutoff time (the exit-side knobs)
    purely in-memory, no extra network calls.
    """
    # (settle_window, search_start, search_end) — curated combos, not a full
    # cross-product, to keep the number of option-data re-fetches manageable.
    grid_entry_window: list[tuple[int, tuple[int, int], tuple[int, int]]] = [
        (20, (9, 35), (10, 30)),   # baseline / current default
        (12, (9, 35), (10, 30)),
        (8,  (9, 35), (10, 30)),
        (5,  (9, 35), (10, 30)),
        (5,  (9, 35), (10, 0)),    # tighter, right after the open
        (10, (9, 45), (11, 0)),    # later, wider window
        (15, (9, 35), (11, 0)),
    ]
    grid_max_credit_pct      = [0.40, 0.50, 0.62, 0.75, None]
    grid_max_prior_range_pct = [0.020, 0.030, 0.040, None]
    grid_max_gap_pct         = [0.006, 0.008, 0.010, 0.015, None]
    grid_breach_fraction     = [0.3, 0.5, 0.7, 1.0]
    grid_exit_cutoff         = [(14, 30), (15, 0), (15, 40), (15, 55)]

    exit_combos = list(itertools.product(
        grid_max_credit_pct, grid_max_prior_range_pct, grid_max_gap_pct,
        grid_breach_fraction, grid_exit_cutoff,
    ))
    total_combos = len(grid_entry_window) * len(exit_combos)
    log.info("Optimizer: sweeping %d entry windows × %d exit/filter combos (%d total) …",
             len(grid_entry_window), len(exit_combos), total_combos)

    all_viable: list[tuple[tuple[int, tuple[int, int], tuple[int, int]], dict, dict, float]] = []
    for entry_combo in grid_entry_window:
        esw, search_start, search_end = entry_combo
        log.info("Entry: settle=%d min, search=%02d:%02d-%02d:%02d — collecting candidates …",
                  esw, *search_start, *search_end)
        candidates, opt_daily, opt_1min = collect_candidates(
            trade_day_set, all_trade_days, esw, search_start, search_end,
        )
        if not candidates:
            log.warning("Entry combo %s: no candidates — skipping", entry_combo)
            continue
        for mcp, mprp, mgp, bf, ec in exit_combos:
            params = {
                "max_credit_pct":      mcp,
                "max_prior_range_pct": mprp,
                "max_gap_pct":         mgp,
                "breach_fraction":     bf,
                "exit_cutoff":         ec,
            }
            trades = apply_filters(candidates, opt_daily, opt_1min, params)
            if len(trades) < 15:
                continue
            summary = compute_summary(trades, trading_days_in_report)
            trade_rate = len(trades) / len(candidates) * 100 if candidates else 0.0
            all_viable.append((entry_combo, params, summary, trade_rate))

    results = [r for r in all_viable if abs(r[3] - target_rate) <= tolerance]
    if not results:
        log.warning("No combos matched trade-rate target %.0f%%±%.0f — showing all viable combos instead",
                     target_rate, tolerance)
        results = all_viable

    results.sort(key=lambda r: (r[2]["total"], r[2]["sharpe"]), reverse=True)

    print(f"\n  Optimizer results — top {min(top, len(results))} of {len(results)} viable combos "
          f"({total_combos} tested, min 15 trades)\n")
    hdr = (f"  {'Rank':<5}{'Settle':>7}{'Search':>13}{'ExitCut':>8}{'CreditPct':>10}{'PriorRng':>9}{'GapPct':>8}{'Breach':>8}  "
           f"{'Trades':>7}{'Rate%':>7}{'WinR%':>7}{'Total$':>11}{'Sharpe':>8}{'MaxDD$':>9}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for i, (entry_combo, params, summary, trade_rate) in enumerate(results[:top], 1):
        esw, search_start, search_end = entry_combo
        ec = params["exit_cutoff"]
        search_str = f"{search_start[0]:02d}:{search_start[1]:02d}-{search_end[0]:02d}:{search_end[1]:02d}"
        print(
            f"  {i:<5}{esw:>7}{search_str:>13}{f'{ec[0]:02d}:{ec[1]:02d}':>8}"
            f"{_fmt(params['max_credit_pct'], '%.2f'):>10}"
            f"{_fmt(params['max_prior_range_pct']):>9}"
            f"{_fmt(params['max_gap_pct']):>8}"
            f"{params['breach_fraction']:>8.1f}  "
            f"{summary['n']:>7}{trade_rate:>7.1f}{summary['win_rate']:>7.1f}"
            f"{summary['total']:>+11,.0f}{summary['sharpe']:>8.2f}{summary['max_dd']:>9,.0f}"
        )
    print()


# ── Results display ───────────────────────────────────────────────────────────

def print_results(trades: list[dict], all_trade_days: list[date]) -> None:
    if not trades:
        print("\n  No trades generated — check data availability.\n")
        return

    n        = len(trades)
    wins     = [t for t in trades if t["pnl"] > 0]
    losses   = [t for t in trades if t["pnl"] < 0]
    flat     = n - len(wins) - len(losses)
    total    = sum(t["pnl"] for t in trades)
    avg      = total / n
    trading_days_in_report = sum(1 for d in all_trade_days if BT_START <= d <= BT_END)
    avg_daily_profit = total / trading_days_in_report if trading_days_in_report else 0.0
    avg_annual_profit = avg_daily_profit * 252
    win_rate = len(wins) / n * 100
    avg_win  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
    payoff   = abs(avg_win / avg_loss)                      if avg_loss else float("inf")

    pnl_vals = [t["pnl"] for t in trades]
    pnl_std  = math.sqrt(sum((v - avg) ** 2 for v in pnl_vals) / max(1, n - 1))
    sharpe   = (avg / pnl_std * math.sqrt(52)) if pnl_std > 0 else 0.0

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        max_dd = max(max_dd, peak - equity)

    n_sched = sum(1 for t in trades if t["exit_reason"] == "scheduled")
    n_pt    = sum(1 for t in trades if t["exit_reason"] == "profit_target_90pct")
    n_sl    = sum(1 for t in trades if t["exit_reason"] == "stop_loss_80pct")
    n_breach = sum(1 for t in trades if t["underlying_exit_reason"] == "underlying_breach")

    hdr = (
        f"  {'Monday':<12} {'ExitDate':<12} {'Entry':<7} {'Exit':<7} {'Expiry':<12} "
        f"{'SPY':>7} {'ATM':>5} "
        f"{'Credit':>8} {'Close':>8} "
        f"{'P&L':>10} {'Cum P&L':>11} {'Exit':<18}"
    )
    sep = "  " + "─" * (len(hdr) - 2)

    print()
    print(f"  {'━'*108}")
    print(f"  SPY Weekly Iron Butterfly Backtest (Dynamic)  ·  qty={QTY}  ·  wing={WING_WIDTH}")
    print(f"  {BT_START} → {BT_END}")
    print(f"  Entry = quietest Monday minute in {ENTRY_SEARCH_START[0]}:{ENTRY_SEARCH_START[1]:02d}-{ENTRY_SEARCH_END[0]}:{ENTRY_SEARCH_END[1]:02d} ET  ·  "
          f"Exit = wing breach or Thursday {EXIT_DEFAULT_HOUR}:{EXIT_DEFAULT_MIN:02d} ET fallback (latest)")
    print(f"  {'━'*108}")
    print(hdr)
    print(sep)

    for t in trades:
        pnl_s = f"${t['pnl']:>+9,.0f}"
        cum_s = f"${t['cum_pnl']:>+10,.0f}"
        print(
            f"  {t['monday']:<12} {t['exit_date']:<12} {t['entry_time']:<7} {t['exit_time']:<7} {t['expiry']:<12} "
            f"${t['spy_open']:>6.2f} {t['atm']:>5} "
            f"${t['net_credit_open']:>7.4f} ${t['net_credit_close']:>7.4f} "
            f"{pnl_s} {cum_s} {t['exit_reason']}"
        )

    print(sep)
    print()
    print(f"  {'─'*62}")
    print(f"  SPY Weekly Iron Butterfly (Dynamic)  ·  {BT_START} – {BT_END}")
    print(f"  {'─'*62}")
    filters: list[str] = []
    if MAX_CREDIT_PCT is not None:
        filters.append(f"credit≤{MAX_CREDIT_PCT:.0%}wing")
    if MAX_PRIOR_RANGE_PCT is not None:
        filters.append(f"prior_wk_range≤{MAX_PRIOR_RANGE_PCT:.1%}")
    if MAX_GAP_PCT is not None:
        filters.append(f"mon_gap≤{MAX_GAP_PCT:.1%}")
    filter_str = "  |  ".join(filters) if filters else "none"

    print(f"  Weeks traded           : {n}")
    print(f"  Potential trading days : {trading_days_in_report}")
    print(f"  Active filters         : {filter_str}")
    print(f"  Winners / Losers / Flat: {len(wins)} / {len(losses)} / {flat}")
    print(f"  Win rate               : {win_rate:.1f}%")
    print(f"  Avg P&L / week         : ${avg:>+,.2f}")
    print(f"  Avg P&L / day          : ${avg_daily_profit:>+,.2f}  across {trading_days_in_report} trading days")
    print(f"  Avg winner             : ${avg_win:>+,.2f}")
    print(f"  Avg loser              : ${avg_loss:>+,.2f}")
    print(f"  Avg annual profit      : ${avg_annual_profit:>+,.0f}  (252 × avg daily P&L)")
    print(f"  Payoff ratio           : {payoff:.2f}x")
    print(f"  Total P&L              : ${total:>+,.0f}")
    print(f"  Max drawdown           : ${max_dd:>,.0f}")
    print(f"  Annualised Sharpe      : {sharpe:.2f}  (weekly ×√52)")
    print(f"  {'─'*62}")
    print(f"  Underlying breaches    : {n_breach}  ·  Exit breakdown: scheduled={n_sched}  "
          f"profit_target_90={n_pt}  stop_loss_80={n_sl}")
    print(f"  {'─'*62}")
    print()
    print(f"  Entry = quietest Monday minute (smallest trailing {ENTRY_SETTLE_WINDOW_MIN}-min range) in "
          f"{ENTRY_SEARCH_START[0]:02d}:{ENTRY_SEARCH_START[1]:02d}-{ENTRY_SEARCH_END[0]:02d}:{ENTRY_SEARCH_END[1]:02d} ET")
    print(f"  Exit  = first minute SPY breaches ATM±WING_WIDTH, else the Thursday {EXIT_DEFAULT_HOUR}:{EXIT_DEFAULT_MIN:02d} ET bar (latest)")
    print("  Auto-exit thresholds applied at exit time only (no intraweek monitoring).")
    print("  No commissions or slippage modelled.")
    print()


# ── CSV export ────────────────────────────────────────────────────────────────

def export_csv(trades: list[dict], path: str) -> None:
    if not trades:
        log.warning("No trades to export.")
        return
    fieldnames = list(trades[0].keys())
    try:
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(trades)
        log.info("Results saved → %s  (%d rows)", path, len(trades))
    except Exception as exc:
        log.error("CSV write failed: %s", exc)


# ── HTML report ───────────────────────────────────────────────────────────────

def export_html(trades: list[dict], path: str, all_trade_days: list[date]) -> None:
    """Generate a self-contained HTML backtest report."""
    if not trades:
        log.warning("No trades — skipping HTML export.")
        return

    n        = len(trades)
    wins     = [t for t in trades if t["pnl"] > 0]
    losses   = [t for t in trades if t["pnl"] < 0]
    total    = sum(t["pnl"] for t in trades)
    avg      = total / n
    trading_days_in_report = sum(1 for d in all_trade_days if BT_START <= d <= BT_END)
    avg_daily_profit = total / trading_days_in_report if trading_days_in_report else 0.0
    avg_annual_profit = avg_daily_profit * 252
    avg_capital_req = sum(t["max_loss"] for t in trades) / n
    avg_annual_return_pct = (avg_annual_profit / avg_capital_req * 100) if avg_capital_req > 0 else 0.0
    win_rate = len(wins) / n * 100
    avg_win  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
    best     = max(trades, key=lambda t: t["pnl"])
    worst    = min(trades, key=lambda t: t["pnl"])
    payoff   = abs(avg_win / avg_loss) if avg_loss else 0.0

    pnl_vals = [t["pnl"] for t in trades]
    pnl_std  = math.sqrt(sum((v - avg) ** 2 for v in pnl_vals) / max(1, n - 1))
    sharpe   = (avg / pnl_std * math.sqrt(52)) if pnl_std > 0 else 0.0

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        max_dd = max(max_dd, peak - equity)

    profit_factor = (
        abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses))
        if losses else float("inf")
    )

    avg_credit    = sum(t["net_credit_open"] for t in trades) / n
    avg_width_pct = avg_credit / WING_WIDTH * 100

    def fmt_pnl(v):
        sign = "+" if v >= 0 else ""
        return f"{sign}${v:,.0f}"

    def cls(v):
        return "positive" if v >= 0 else "negative"

    rows_html = []
    for t in trades:
        p = t["pnl"]
        row_cls = "win-row" if p >= 0 else "loss-row"
        rows_html.append(f"""
        <tr class="{row_cls}">
          <td>{t['monday']}</td>
          <td>{t['exit_date']}</td>
          <td>{t['entry_time']}</td>
          <td>{t['exit_time']}</td>
          <td>{t['expiry']}</td>
          <td>${t['spy_open']:.2f}</td>
          <td>${t['spy_exit']:.2f}</td>
          <td>{t['atm']}</td>
          <td>{t['lp_K']}/{t['sp_K']}/{t['sc_K']}/{t['lc_K']}</td>
          <td>${t['net_credit_open']:.4f}</td>
          <td>${t['net_credit_close']:.4f}</td>
          <td class="{cls(p)}">{fmt_pnl(p)}</td>
          <td class="{cls(t['cum_pnl'])}">{fmt_pnl(t['cum_pnl'])}</td>
          <td>{t['exit_reason']}</td>
        </tr>""")

    rows = "\n".join(rows_html)
    generated  = datetime.now().strftime("%B %d, %Y")
    equity_pts = ",".join(str(t["cum_pnl"]) for t in trades)
    final_color = "#10b981" if total >= 0 else "#ef4444"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <div id="ga-script-head-placeholder"></div>
  <meta charset="utf-8">
  <title>SPY Weekly Iron Butterfly Backtest (Dynamic) | AI Velocity</title>
  <meta content="SPY Weekly Iron Butterfly Backtest with dynamic entry/exit timing — {BT_START} to {BT_END}" name="description">
  <meta content="width=device-width, initial-scale=1" name="viewport">
  <link href="/css/normalize.css" rel="stylesheet">
  <link href="/css/navbar.css" rel="stylesheet">
  <link href="/fonts/fonts.css" rel="stylesheet">
  <style>
    :root {{
      --success:        #10b981;
      --success-dark:   #059669;
      --warning:        #f59e0b;
      --danger:         #ef4444;
      --bg:             #040911;
      --bg-card:        #0D1525;
      --bg-code:        #0a1628;
      --text-primary:   #ffffff;
      --text-secondary: #A0ABBE;
      --border:         rgba(139, 92, 246, 0.2);
      --accent:         #7c3aed;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text-primary); font-family: 'Inter', sans-serif; line-height: 1.6; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .backtest-container {{ max-width: 1280px; margin: 0 auto; padding: 2rem 1.5rem; }}
    .backtest-hero {{ text-align: center; padding: 2rem 2.5rem; margin-bottom: 2rem; }}
    .backtest-hero h1 {{ font-family: 'IBM Plex Serif', serif; font-size: 2.4rem; font-weight: 600; margin-bottom: 0.5rem; }}
    .subtitle {{ display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap; margin-top: 1.25rem; }}
    .badge {{ background: rgba(255,255,255,0.15); color: white; padding: 0.4rem 1rem; border-radius: 4px; font-size: 0.82rem; font-weight: 600; }}
    .badge-report   {{ background: rgba(245,158,11,0.25); color: #fcd34d; border: 1px solid rgba(245,158,11,0.4); cursor: pointer; }}
    .badge-add      {{ background: rgba(16,185,129,0.25); color: #6ee7b7; border: 1px solid rgba(16,185,129,0.4); cursor: pointer; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 1.25rem; margin-bottom: 2rem; }}
    .stat-card {{ background: var(--bg-card); border: 2px solid var(--border); border-radius: 14px; padding: 1.4rem 1.25rem; text-align: center; transition: transform 0.2s, box-shadow 0.2s; }}
    .stat-card:hover {{ transform: translateY(-3px); box-shadow: 0 8px 24px rgba(0,0,0,0.25); }}
    .stat-card.highlight {{ background: linear-gradient(135deg, var(--success-dark), var(--success)); border: none; }}
    .stat-card.highlight-loss {{ background: linear-gradient(135deg, #dc2626, var(--danger)); border: none; }}
    .stat-card .lbl {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-secondary); margin-bottom: 0.4rem; }}
    .stat-card.highlight .lbl, .stat-card.highlight-loss .lbl {{ color: rgba(255,255,255,0.8); }}
    .stat-card .val {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.85rem; font-weight: 700; color: var(--text-primary); }}
    .stat-card.highlight .val, .stat-card.highlight-loss .val {{ color: white; }}
    .stat-card .sub {{ font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.2rem; }}
    .stat-card.highlight .sub, .stat-card.highlight-loss .sub {{ color: rgba(255,255,255,0.7); }}
    .val.positive {{ color: var(--success); }}
    .val.negative {{ color: var(--danger); }}
    .section {{ background: var(--bg-card); border: 2px solid var(--border); border-radius: 16px; padding: 2rem; margin-bottom: 2rem; animation: fadeIn 0.4s ease-out; }}
    .section h2 {{ font-family: 'IBM Plex Serif', serif; font-size: 1.4rem; font-weight: 600; margin-bottom: 1.5rem; display: flex; align-items: center; gap: 0.5rem; }}
    .section h3 {{ font-family: 'IBM Plex Serif', serif; font-size: 1.05rem; font-weight: 500; margin: 1.5rem 0 0.75rem; color: var(--text-secondary); }}
    .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }}
    @media (max-width: 860px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
    .config-box {{ background: var(--bg-code); border-radius: 12px; padding: 1.5rem; border: 1px solid var(--border); }}
    .config-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem; }}
    .config-item {{ text-align: center; }}
    .config-item .lbl {{ font-size: 0.78rem; color: var(--text-secondary); margin-bottom: 0.25rem; }}
    .config-item .val {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.15rem; font-weight: 600; color: #a78bfa; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 0.7rem 0.9rem; text-align: left; border-bottom: 1px solid var(--border); font-size: 0.88rem; }}
    th {{ background: var(--bg-code); color: var(--text-secondary); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; }}
    td {{ color: var(--text-primary); font-family: 'IBM Plex Mono', monospace; }}
    tr:hover td {{ background: rgba(124,58,237,0.05); }}
    .win-row  td {{ border-left: 3px solid var(--success); }}
    .loss-row td {{ border-left: 3px solid var(--danger); }}
    .positive {{ color: var(--success) !important; font-weight: 600; }}
    .negative {{ color: var(--danger)  !important; font-weight: 600; }}
    .progress-container {{ margin: 0.9rem 0; }}
    .progress-label {{ display: flex; justify-content: space-between; font-size: 0.87rem; margin-bottom: 0.4rem; color: var(--text-primary); }}
    .progress-bar {{ height: 22px; background: var(--bg-code); border-radius: 11px; overflow: hidden; border: 1px solid var(--border); }}
    .progress-fill {{ height: 100%; border-radius: 11px; display: flex; align-items: center; justify-content: center; font-size: 0.72rem; font-weight: 700; color: white; }}
    .progress-fill.win  {{ background: linear-gradient(90deg, var(--success-dark), var(--success)); }}
    .progress-fill.loss {{ background: linear-gradient(90deg, #dc2626, var(--danger)); }}
    .risk-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; margin: 1rem 0; }}
    .risk-card {{ background: var(--bg-code); border-radius: 12px; padding: 1.25rem; text-align: center; border: 1px solid var(--border); }}
    .risk-card h4 {{ font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 0.5rem; font-weight: 500; }}
    .risk-card .val {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.45rem; font-weight: 700; }}
    .alert {{ padding: 1rem 1.25rem; border-radius: 12px; margin: 0.85rem 0; display: flex; align-items: flex-start; gap: 1rem; }}
    .alert-success {{ background: rgba(16,185,129,0.1); border: 1px solid var(--success); }}
    .alert-warning  {{ background: rgba(245,158,11,0.1); border: 1px solid var(--warning); }}
    .alert-danger   {{ background: rgba(239,68,68,0.1);  border: 1px solid var(--danger); }}
    .alert-icon {{ font-size: 1.4rem; line-height: 1; }}
    .alert-content h4 {{ font-family: 'IBM Plex Serif', serif; margin-bottom: 0.25rem; font-size: 0.97rem; }}
    .alert-content p  {{ font-size: 0.86rem; color: var(--text-secondary); margin: 0; }}
    .chart-wrap {{ background: var(--bg-code); border-radius: 12px; padding: 1.5rem; border: 1px solid var(--border); overflow-x: auto; }}
    svg.equity-chart {{ width: 100%; height: 260px; display: block; }}
    .table-scroll {{ overflow-x: auto; }}
    .footer {{ text-align: center; padding: 2rem 1rem; color: var(--text-secondary); font-size: 0.85rem; border-top: 1px solid var(--border); margin-top: 1rem; }}
    .footer p {{ margin: 0.4rem 0; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(16px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    @media (max-width: 600px) {{ .backtest-hero h1 {{ font-size: 1.6rem; }} .stat-card .val {{ font-size: 1.4rem; }} }}
    #trade-log-mobile-notice {{ display: none; padding: 1.5rem; text-align: center; color: var(--text-secondary); font-size: 0.95rem; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-card); }}
    .is-mobile #trade-log-section > :not(#trade-log-mobile-notice) {{ display: none; }}
    .is-mobile #trade-log-mobile-notice {{ display: block; }}
  </style>
  <script>if(/Mobi|Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent)){{document.documentElement.classList.add('is-mobile');}}</script>
  <!-- Google Tag Manager -->
  <script>(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':
  new Date().getTime(),event:'gtm.js'}});var f=d.getElementsByTagName(s)[0],
  j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
  'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
  }})(window,document,'script','dataLayer','GTM-WCG69XDQ');</script>
  <!-- End Google Tag Manager -->
</head>

<body>
  <noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-WCG69XDQ" height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
  <div id="nav-placeholder"></div>

  <div class="backtest-container">

    <header class="backtest-hero">
      <h1>Weekly Iron Butterfly SPY (Dynamic)</h1>
      <div class="subtitle">
        <a href="/strategies.html" style="text-decoration:none;"><span class="badge badge-report">&#x1F4CA; Strategies</span></a>
        <a href="/strategy-descriptions/weekly-iron-butterfly-SPY.html" style="text-decoration:none;"><span class="badge badge-report">&#x1F4CA; Performance Report</span></a>
        <a href="/add_strategy.html?strategy=1018" style="text-decoration:none;"><span class="badge badge-add">&#x2795; Add to My Account</span></a>
      </div>
    </header>

    <div class="stats-grid">
      <div class="stat-card {'highlight' if total >= 0 else 'highlight-loss'}">
        <div class="lbl">Total P&amp;L</div>
        <div class="val">{fmt_pnl(total)}</div>
        <div class="sub">{BT_START} → {BT_END}</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Win Rate</div>
        <div class="val">{win_rate:.1f}%</div>
        <div class="sub">{len(wins)} wins / {len(losses)} losses</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Total Trades</div>
        <div class="val">{n}</div>
        <div class="sub">Weekly entries</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Avg P&amp;L / Trade</div>
        <div class="val {cls(avg)}">{fmt_pnl(avg)}</div>
        <div class="sub">Per trade average</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Avg P&amp;L / Day</div>
        <div class="val {cls(avg_daily_profit)}">{fmt_pnl(avg_daily_profit)}</div>
        <div class="sub">Across {trading_days_in_report} trading days</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Avg Annual Profit</div>
        <div class="val {cls(avg_annual_profit)}">{fmt_pnl(avg_annual_profit)}</div>
        <div class="sub">Avg daily P&amp;L &times; 252</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Capital Required</div>
        <div class="val">${avg_capital_req:,.0f}</div>
        <div class="sub">Avg max loss / trade</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Avg Annual Return</div>
        <div class="val {cls(avg_annual_return_pct)}">{avg_annual_return_pct:,.1f}%</div>
        <div class="sub">Annual profit / avg capital</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Profit Factor</div>
        <div class="val">{profit_factor:.2f}</div>
        <div class="sub">Gross wins / gross losses</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Sharpe Ratio</div>
        <div class="val">{sharpe:.2f}</div>
        <div class="sub">Weekly × √52 annualised</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Max Drawdown</div>
        <div class="val negative">${max_dd:,.0f}</div>
        <div class="sub">Peak-to-trough equity</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Payoff Ratio</div>
        <div class="val">{payoff:.2f}x</div>
        <div class="sub">Avg win / avg loss</div>
      </div>
      <div class="stat-card">
        <div class="lbl">Contracts / Leg</div>
        <div class="val">{QTY}</div>
        <div class="sub">{QTY * 4} total contracts · {QTY * 4 * n} across all trades</div>
      </div>
    </div>

    <section class="section">
      <h2>📈 Equity Curve</h2>
      <div class="chart-wrap">
        <svg class="equity-chart" viewBox="0 0 1000 240" preserveAspectRatio="none" id="equity-svg"></svg>
      </div>
    </section>

    <section class="section">
      <h2>⚙️ Strategy Configuration</h2>
      <div class="config-box">
        <div class="config-grid">
          <div class="config-item"><div class="lbl">Ticker</div><div class="val">SPY</div></div>
          <div class="config-item"><div class="lbl">Contracts</div><div class="val">{QTY}</div></div>
          <div class="config-item"><div class="lbl">Structure</div><div class="val">4-Leg IBF</div></div>
          <div class="config-item"><div class="lbl">Wing Width</div><div class="val">${WING_WIDTH}</div></div>
          <div class="config-item"><div class="lbl">Max Credit %</div><div class="val">{(str(round(MAX_CREDIT_PCT*100)) + '%') if MAX_CREDIT_PCT is not None else 'off'}</div></div>
          <div class="config-item"><div class="lbl">Max Prior Wk Range</div><div class="val">{('%.1f%%' % (MAX_PRIOR_RANGE_PCT*100)) if MAX_PRIOR_RANGE_PCT is not None else 'off'}</div></div>
          <div class="config-item"><div class="lbl">Max Mon Gap</div><div class="val">{('%.1f%%' % (MAX_GAP_PCT*100)) if MAX_GAP_PCT is not None else 'off'}</div></div>
          <div class="config-item"><div class="lbl">Breach Fraction</div><div class="val">{BREACH_FRACTION}</div></div>
          <div class="config-item"><div class="lbl">Profit Target</div><div class="val">+{PROFIT_TARGET_PCT:.0%}</div></div>
          <div class="config-item"><div class="lbl">Stop Loss</div><div class="val">{STOP_LOSS_PCT:.0%}</div></div>
          <div class="config-item"><div class="lbl">Entry Day</div><div class="val">Monday</div></div>
          <div class="config-item"><div class="lbl">Entry Window</div><div class="val">{ENTRY_SEARCH_START[0]}:{ENTRY_SEARCH_START[1]:02d}-{ENTRY_SEARCH_END[0]}:{ENTRY_SEARCH_END[1]:02d} ET</div></div>
          <div class="config-item"><div class="lbl">Exit Cutoff</div><div class="val">Thu {EXIT_DEFAULT_HOUR}:{EXIT_DEFAULT_MIN:02d} ET</div></div>
          <div class="config-item"><div class="lbl">Expiry</div><div class="val">Next Friday</div></div>
          <div class="config-item"><div class="lbl">ATM Rounding</div><div class="val">$1</div></div>
          <div class="config-item"><div class="lbl">Pricing Model</div><div class="val">OPRA Bars</div></div>
        </div>
      </div>
      <h3>Strike Construction</h3>
      <table>
        <thead><tr><th>Leg</th><th>Strike</th><th>Action</th><th>Role</th></tr></thead>
        <tbody>
          <tr><td>Long Put</td><td>ATM − ${WING_WIDTH}</td><td class="negative">Buy</td><td>Lower wing — defines max loss on downside</td></tr>
          <tr><td>Short Put</td><td>ATM</td><td class="positive">Sell</td><td>ATM body — highest premium collection</td></tr>
          <tr><td>Short Call</td><td>ATM</td><td class="positive">Sell</td><td>ATM body — highest premium collection</td></tr>
          <tr><td>Long Call</td><td>ATM + ${WING_WIDTH}</td><td class="negative">Buy</td><td>Upper wing — defines max loss on upside</td></tr>
        </tbody>
      </table>
      <h3>P&amp;L Mechanics</h3>
      <table>
        <tr><td>Net credit at entry</td><td class="positive">Max profit = credit × 100 × {QTY}  (SPY pins exactly at ATM)</td></tr>
        <tr><td>SPY moves more than ${WING_WIDTH} from ATM</td><td class="negative">Max loss = (${WING_WIDTH} − credit) × 100 × {QTY}</td></tr>
        <tr><td>Avg net credit collected</td><td>${avg_credit:.4f} / share ({avg_width_pct:.1f}% of wing width)</td></tr>
      </table>
    </section>

    <section class="section">
      <h2>📊 Win / Loss Analysis</h2>
      <div class="two-col">
        <div>
          <h3>Win Rate Breakdown</h3>
          <div class="progress-container">
            <div class="progress-label"><span>Winners ({len(wins)})</span><span>{win_rate:.1f}%</span></div>
            <div class="progress-bar"><div class="progress-fill win" style="width:{win_rate:.1f}%">{win_rate:.1f}%</div></div>
          </div>
          <div class="progress-container">
            <div class="progress-label"><span>Losers ({len(losses)})</span><span>{100-win_rate:.1f}%</span></div>
            <div class="progress-bar"><div class="progress-fill loss" style="width:{100-win_rate:.1f}%">{100-win_rate:.1f}%</div></div>
          </div>
        </div>
        <div>
          <h3>P&amp;L Breakdown</h3>
          <table>
            <tr><td>Average winner</td><td class="positive">{fmt_pnl(avg_win)}</td></tr>
            <tr><td>Average loser</td><td class="negative">{fmt_pnl(avg_loss)}</td></tr>
            <tr><td>Best trade</td><td class="positive">{fmt_pnl(best['pnl'])} ({best['monday']})</td></tr>
            <tr><td>Worst trade</td><td class="negative">{fmt_pnl(worst['pnl'])} ({worst['monday']})</td></tr>
            <tr><td>Total gross wins</td><td class="positive">{fmt_pnl(sum(t['pnl'] for t in wins))}</td></tr>
            <tr><td>Total gross losses</td><td class="negative">{fmt_pnl(sum(t['pnl'] for t in losses))}</td></tr>
          </table>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>⚠️ Risk Assessment</h2>
      <div class="risk-grid">
        <div class="risk-card"><h4>Max Drawdown</h4><div class="val negative">${max_dd:,.0f}</div></div>
        <div class="risk-card"><h4>Worst Single Loss</h4><div class="val negative">{fmt_pnl(worst['pnl'])}</div></div>
        <div class="risk-card"><h4>Avg Max Loss / Trade</h4><div class="val">${sum(t['max_loss'] for t in trades)/n:,.0f}</div></div>
        <div class="risk-card"><h4>Profit Factor</h4><div class="val">{profit_factor:.2f}</div></div>
        <div class="risk-card"><h4>Sharpe (Annualised)</h4><div class="val">{sharpe:.2f}</div></div>
        <div class="risk-card"><h4>Payoff Ratio</h4><div class="val">{payoff:.2f}x</div></div>
      </div>
    </section>

    <section class="section">
      <h2>💡 Key Insights</h2>
      {'<div class="alert alert-success"><span class="alert-icon">✅</span><div class="alert-content"><h4>Profitable Strategy</h4><p>Total P&L of ' + fmt_pnl(total) + ' over ' + str(n) + ' trades. Dynamic entry/exit timing (quietest Monday minute, wing-breach exit, Thursday ' + f"{EXIT_DEFAULT_HOUR}:{EXIT_DEFAULT_MIN:02d}" + ' ET cutoff) replaces the fixed clock times of the original weekly script.</p></div></div>' if total > 0 else '<div class="alert alert-danger"><span class="alert-icon">❌</span><div class="alert-content"><h4>Net Loss Period</h4><p>Total P&L of ' + fmt_pnl(total) + ' over ' + str(n) + ' trades. The iron butterfly requires SPY to stay near ATM through the week.</p></div></div>'}
      <div class="alert alert-warning">
        <span class="alert-icon">⚠️</span>
        <div class="alert-content">
          <h4>Model Limitations</h4>
          <p>No commissions, slippage, or pin risk modelled. Entry/exit timing is derived solely from SPY's realized 1-min price bars (backtests/underlying-tickers/SPY.csv), not option Greeks.</p>
        </div>
      </div>
    </section>

    <section class="section" id="trade-log-section">
      <div id="trade-log-mobile-notice"><p>📋 Full Trade Log is not displayed on mobile devices to prevent page freezing. View this report on a desktop browser to see all {n} trades.</p></div>
      <h2>📋 Full Trade Log ({n} trades)</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Monday</th><th>Exit Date</th><th>Entry</th><th>Exit</th><th>Expiry</th>
              <th>SPY Open</th><th>SPY Exit</th><th>ATM</th><th>LP / SP / SC / LC</th>
              <th>Credit Open</th><th>Credit Close</th><th>P&amp;L</th><th>Cum P&amp;L</th><th>Exit Reason</th>
            </tr>
          </thead>
          <tbody>
{rows}
          </tbody>
        </table>
      </div>
    </section>

    <footer class="footer">
      <p>SPY Weekly Iron Butterfly Backtest (Dynamic) &bull; {BT_START} – {BT_END} &bull; Generated {generated}</p>
      <p>P&amp;L calculated using real OPRA options bar prices from Alpaca data API. Entry/exit timing derived from SPY 1-min underlying bars.</p>
      <p style="font-size:0.78rem; margin-top:0.5rem;">
        Past performance does not guarantee future results. Options trading involves significant risk of loss.
      </p>
    </footer>

  </div>

  <script>
  (function() {{
    var data = [{equity_pts}];
    var svg  = document.getElementById('equity-svg');
    if (!svg || data.length < 2) return;
    var W = 1000, H = 240, PAD = 20;
    var minV = Math.min(0, Math.min.apply(null, data));
    var maxV = Math.max(0, Math.max.apply(null, data));
    var range = maxV - minV || 1;
    function toX(i) {{ return PAD + (i / (data.length - 1)) * (W - PAD * 2); }}
    function toY(v) {{ return H - PAD - ((v - minV) / range) * (H - PAD * 2); }}
    var zeroY = toY(0);
    var zline = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    zline.setAttribute('x1', PAD); zline.setAttribute('x2', W - PAD);
    zline.setAttribute('y1', zeroY); zline.setAttribute('y2', zeroY);
    zline.setAttribute('stroke', 'rgba(255,255,255,0.15)');
    zline.setAttribute('stroke-dasharray', '4,4');
    svg.appendChild(zline);
    var pts = data.map(function(v, i) {{ return toX(i) + ',' + toY(v); }}).join(' ');
    var fill = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    var fillPts = (PAD + ',' + (H - PAD)) + ' ' + pts + ' ' + ((W - PAD) + ',' + (H - PAD));
    fill.setAttribute('points', fillPts);
    fill.setAttribute('fill', data[data.length-1] >= 0 ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)');
    svg.appendChild(fill);
    var poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    poly.setAttribute('points', pts);
    poly.setAttribute('fill', 'none');
    poly.setAttribute('stroke', '{final_color}');
    poly.setAttribute('stroke-width', '2.5');
    poly.setAttribute('stroke-linejoin', 'round');
    poly.setAttribute('stroke-linecap', 'round');
    svg.appendChild(poly);
    var lastX = toX(data.length - 1), lastY = toY(data[data.length - 1]);
    var dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', lastX); dot.setAttribute('cy', lastY);
    dot.setAttribute('r', '5');
    dot.setAttribute('fill', '{final_color}');
    svg.appendChild(dot);
    var lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    lbl.setAttribute('x', Math.min(lastX + 8, W - 90));
    lbl.setAttribute('y', lastY + 4);
    lbl.setAttribute('fill', '{final_color}');
    lbl.setAttribute('font-size', '13');
    lbl.setAttribute('font-family', 'IBM Plex Mono, monospace');
    lbl.setAttribute('font-weight', '600');
    lbl.textContent = '{fmt_pnl(total)}';
    svg.appendChild(lbl);
  }})();
  </script>

  <script src="/js/site.js"></script>
</body>
</html>"""

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log.info("HTML report saved → %s", path)
    except Exception as exc:
        log.error("HTML write failed: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"SPY Weekly Iron Butterfly backtest (dynamic entry/exit) — {BT_START} to {BT_END}"
    )
    parser.add_argument(
        "--csv", default=None, metavar="FILE",
        help="CSV output path (default: auto-named in script dir)",
    )
    parser.add_argument("--optimize", action="store_true",
                         help="Sweep filter thresholds + breach_fraction instead of a single run")
    parser.add_argument("--top", type=int, default=15, help="Optimizer: number of top combos to print")
    parser.add_argument("--target-rate", type=float, default=50.0,
                         help="Optimizer: preferred trade rate %% (default 50)")
    parser.add_argument("--tolerance", type=float, default=10.0,
                         help="Optimizer: ± tolerance around --target-rate")
    args = parser.parse_args()

    if not DATA_KEY or not DATA_SECRET:
        log.error("apiDataKey / apiDataSecret missing in root .env — aborting.")
        sys.exit(1)

    log.info("Backtest: SPY Weekly Iron Butterfly (dynamic entry/exit)  %s → %s", BT_START, BT_END)
    log.info("Wing=%d  Qty=%d  Entry=quietest Monday %02d:%02d-%02d:%02d ET minute  Exit=wing breach or Thu %d:%02d ET (latest)  PT=+90%%  SL=-80%%",
             WING_WIDTH, QTY, *ENTRY_SEARCH_START, *ENTRY_SEARCH_END, EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN)

    cal_start = BT_START - timedelta(days=10)
    cal_end   = BT_END   + timedelta(days=14)
    log.info("Fetching market calendar %s → %s …", cal_start, cal_end)
    all_trade_days = get_trading_days(cal_start, cal_end)
    trade_day_set  = set(all_trade_days)
    log.info("Calendar loaded: %d trading days", len(all_trade_days))

    trading_days_in_report = sum(1 for d in all_trade_days if BT_START <= d <= BT_END)

    if args.optimize:
        run_optimization(trade_day_set, all_trade_days, trading_days_in_report,
                          target_rate=args.target_rate, tolerance=args.tolerance, top=args.top)
        return

    candidates, opt_daily, opt_1min = collect_candidates(trade_day_set, all_trade_days)
    if not candidates:
        log.error("No candidates collected — aborting.")
        sys.exit(1)

    trades = run_backtest(candidates, opt_daily, opt_1min)
    print_results(trades, all_trade_days)

    _csv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backtest-csv-files")
    os.makedirs(_csv_dir, exist_ok=True)
    csv_path = args.csv or os.path.join(_csv_dir, os.path.splitext(os.path.basename(__file__))[0] + ".csv")
    export_csv(trades, csv_path)

    _html_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backtest-html-reports")
    os.makedirs(_html_dir, exist_ok=True)
    html_path = os.path.join(_html_dir, "weekly-iron-butterfly-spy-backtest-dynamic.html")
    export_html(trades, html_path, all_trade_days)


if __name__ == "__main__":
    main()
