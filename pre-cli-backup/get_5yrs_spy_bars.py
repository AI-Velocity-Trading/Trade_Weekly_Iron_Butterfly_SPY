#!/usr/bin/env python3
"""
get_5yrs_spy_bars.py
=====================
Fetches SPY 1-minute SIP bars from Alpaca, covering 4:00 AM - 8:00 PM
America/New_York (pre-market through after-hours) for the past 5 years,
and writes them to underlying-tickers/SPY.csv in the format expected by
weekly_iron_butterfly_spy_backtest_dynamic.py (date_et, time_et, open,
high, low, close).

Usage:
  python3 get_5yrs_spy_bars.py
"""

from __future__ import annotations

import os
import csv
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("get_5yrs_spy_bars")

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_FILE)

DATA_KEY    = os.getenv("apiDataKey",    "")
DATA_SECRET = os.getenv("apiDataSecret", "")
DATA_BASE   = "https://data.alpaca.markets"

TICKER     = "SPY"
FEED       = "sip"
TIMEFRAME  = "1Min"
YEARS_BACK = 5

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# Session window (ET) — pre-market through after-hours.
SESSION_START_HOUR = 4
SESSION_END_HOUR   = 20  # 8:00 PM — bars with time_et < 20:00:00 are kept

OUT_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "underlying-tickers", f"{TICKER}.csv"
)


def fetch_bars(start: date, end: date) -> list[dict]:
    """Fetches all 1-min SIP bars for TICKER between start and end (inclusive), paginated."""
    hdrs = {
        "apca-api-key-id":     DATA_KEY,
        "apca-api-secret-key": DATA_SECRET,
    }
    start_iso = datetime.combine(start, datetime.min.time(), tzinfo=UTC).isoformat()
    end_iso   = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC).isoformat()

    bars: list[dict] = []
    cursor: str | None = None
    while True:
        params = {
            "symbols":    TICKER,
            "timeframe":  TIMEFRAME,
            "start":      start_iso,
            "end":        end_iso,
            "limit":      10000,
            "feed":       FEED,
            "adjustment": "raw",
        }
        if cursor:
            params["page_token"] = cursor

        r = requests.get(f"{DATA_BASE}/v2/stocks/bars", headers=hdrs, params=params, timeout=30)
        if not r.ok:
            log.error("Bars fetch HTTP %d: %s", r.status_code, r.text[:500])
            r.raise_for_status()

        data = r.json()
        bars.extend(data.get("bars", {}).get(TICKER, []))
        cursor = data.get("next_page_token")
        log.info("Fetched %d bars so far (cursor=%s)…", len(bars), bool(cursor))
        if not cursor:
            break

    return bars


def main() -> None:
    if not DATA_KEY or not DATA_SECRET:
        log.error("Missing apiDataKey / apiDataSecret — check %s", _ENV_FILE)
        return

    today = date.today()
    start = today.replace(year=today.year - YEARS_BACK)
    end   = today - timedelta(days=1)

    log.info("Fetching %s 1-min SIP bars from %s to %s …", TICKER, start, end)
    raw_bars = fetch_bars(start, end)
    log.info("Fetched %d raw bars total.", len(raw_bars))

    rows = []
    for bar in raw_bars:
        ts_utc = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
        ts_et  = ts_utc.astimezone(ET)
        if not (SESSION_START_HOUR <= ts_et.hour < SESSION_END_HOUR):
            continue
        rows.append({
            "date_et": ts_et.date().isoformat(),
            "time_et": ts_et.strftime("%H:%M:%S"),
            "open":    bar["o"],
            "high":    bar["h"],
            "low":     bar["l"],
            "close":   bar["c"],
        })

    rows.sort(key=lambda row: (row["date_et"], row["time_et"]))

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date_et", "time_et", "open", "high", "low", "close"])
        writer.writeheader()
        writer.writerows(rows)

    log.info("Wrote %d bars (4AM-8PM ET session) to %s", len(rows), OUT_CSV)


if __name__ == "__main__":
    main()
