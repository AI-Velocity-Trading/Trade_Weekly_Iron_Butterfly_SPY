#!/usr/bin/env python3
"""
weekly_trading_spy.py  —  SPY Iron Butterfly, single-account LIVE dynamic trader
=================================================================================
Live-trading counterpart of weekly_iron_butterfly_spy_backtest_dynamic.py.

Same 4-leg iron butterfly structure as the weekly_iron_butterfly_* family
(longPut=ATM-wing, shortPut=ATM, shortCall=ATM, longCall=ATM+wing), but:

  1. Trades ONLY a single account, configured via .env plus an interactive
     prompt at startup (no multi-account threading / no account lookup
     service of any kind).
  2. Opens ONLY on Monday and closes ONLY on Thursday (weekly cadence),
     using DYNAMIC entry/exit timing ported from the backtest's
     find_optimal_entry() / find_dynamic_exit_week() instead of a fixed
     09:40/15:45 clock schedule.
  3. Reads live underlying prices by polling Alpaca's latest-trade endpoint
     directly (no websocket subscriber, no local CSV feed) and aggregates
     the polled ticks into in-memory per-minute OHLC bars.

Structure:
  longPut   = ATM - wingWidth   (buy)
  shortPut  = ATM               (sell)   ← both at ATM strike
  shortCall = ATM               (sell)   ←
  longCall  = ATM + wingWidth   (buy)

Parameters:  wingWidth=10 (matches weekly_iron_butterfly_spy_backtest_dynamic.py);
             qty is prompted for interactively at startup.

Entry (dynamic, Monday only):
  Polls Alpaca's latest-trade price for SPY 09:35–10:30 ET on Monday,
  tracking the trailing 20-minute high/low range minute-by-minute. Since a
  live process cannot act on a minute already in the past (unlike the
  backtest, which sees the whole window in hindsight), it commits to
  entering once the trailing range has gone ENTRY_PATIENCE_MIN consecutive
  minutes without setting a new low (the quiet period appears to have
  passed), or the window times out at 10:30 ET — whichever comes first.

Exit (dynamic, Monday–Thursday):
  Continuously polls Alpaca's latest-trade price after entry (all week);
  exits immediately ("underlying_breach") once price moves BREACH_FRACTION *
  wingWidth away from the ATM strike. Falls back to a 15:40 ET Thursday
  scheduled close if no breach occurs by then. Also auto-closes on:
  - P&L ≥ +90% of max profit  (MAX_PROFIT_90%)
  - P&L ≤ -80% of max profit  (STOP_LOSS_80%)

Environment
-----------
All Alpaca access (trading + market data) goes through the Alpaca CLI
(https://github.com/alpacahq/cli — `brew install alpacahq/tap/cli`), invoked
as a subprocess rather than via direct HTTPS requests. On first run, if no
"paper"/"live" CLI profile exists yet, the API key/secret are requested
interactively and registered with `alpaca profile login --api-key` so future
runs don't ask again. No separate DATA API key is required — the same
profile is used for market-data requests via `alpaca api --use-data-api`.

The number of option contracts to trade per leg is also requested
interactively at startup (not persisted — set fresh each run).

Live price feed
----------------
Polled directly via `alpaca data latest-trade --symbol SPY` (through the raw
`alpaca api` passthrough) — no websocket subscriber or local CSV feed
required.
"""

from __future__ import annotations

import os
import sys
import csv
import json
import time
import math
import re
import shutil
import subprocess
import threading
import signal
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from pathlib import Path

# ── Timezone sanity check ─────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    _ET_CHECK = ZoneInfo("America/New_York")
    _utc_offset = _ET_CHECK.utcoffset(__import__("datetime").datetime(2026, 1, 1))
    if _utc_offset is not None and _utc_offset.total_seconds() == 0:
        raise RuntimeError(
            "ZoneInfo('America/New_York') resolved to UTC offset 0 — "
            "tzdata package is missing.  Install it:  pip install tzdata"
        )
except ZoneInfoNotFoundError:
    raise RuntimeError(
        "ZoneInfo('America/New_York') not found — "
        "tzdata package is missing.  Install it:  pip install tzdata"
    )

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weekly_trading_spy")

ET = ZoneInfo("America/New_York")

# ── Config ────────────────────────────────────────────────────────────────────
STRATEGY_LABEL = "SPY Weekly Iron Butterfly (Dynamic Live)"
# Per-account credentials/state live in thread-local storage (_ctx).
_ctx = threading.local()
_all_shutdowns: list[threading.Event] = []

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE  = "https://api.alpaca.markets"
DATA_BASE  = "https://data.alpaca.markets"

# Strategy parameters (match weekly_iron_butterfly_spy_backtest_dynamic.py)
TICKER     = "SPY"
QTY        = 1
WING_WIDTH = 10

# ── Weekly schedule — OPEN Monday only, CLOSE Thursday only ──────────────────
OPEN_WEEKDAY  = 0      # Monday
CLOSE_WEEKDAY = 3      # Thursday

# ── Dynamic entry/exit timing (ported from the backtest_dynamic script) ──────
ENTRY_SEARCH_START = (9, 35)
ENTRY_SEARCH_END   = (10, 30)
ENTRY_SETTLE_WINDOW_MIN = 20
ENTRY_PATIENCE_MIN = 3     # consecutive non-improving minutes before entering early
EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN = 15, 40
BREACH_FRACTION = 1.0
DYNAMIC_EXIT_POLL_SEC = 20  # how often to poll Alpaca for wing-breach checks

# Auto-exit thresholds (fraction of max profit)
PROFIT_TARGET_PCT = 0.90   # close at +90%
STOP_LOSS_PCT     = -0.80  # close at -80%

# Fill-poll settings
POLL_INITIAL_WAIT = 3
POLL_RETRY_WAIT   = 5
POLL_MAX_RETRIES  = 8

# Monitor interval (seconds)
MONITOR_INTERVAL = 300  # 5 min

# How often to log each leg's current market value + IMV-combined value
MARKET_VALUE_LOG_INTERVAL = 60  # 1 min

# Retry settings when leg prices are unavailable
MAX_EV_RETRIES = 8
EV_RETRY_WAIT  = 60     # seconds between retries

# ── Test mode (set by --test arg; bypasses all trade filters) ────────────────
TEST_MODE = False
ALLOW_LIVE_TRADING = False   # set True only to allow live-account orders
FILTER_CHECK_ONLY = False    # set by --dry-run: return right after the gap/prior-range
                             # filter check, before any snapshot fetch or order placement
FORCE_TRADE_NOW = False      # set by --force: bypass Mon-only + open-window guards (one-off test)

# ── Trade filters (set to None to disable) ────────────────────────────────────
# Values match weekly_iron_butterfly_spy_backtest_dynamic.py's optimizer-tuned baseline.
MAX_CREDIT_PCT: float | None = None          # disabled — optimizer found no credit filter helps
MIN_CREDIT_ABS: float | None = 1.00
MAX_PROB_MAX_LOSS: float | None = 0.32       # live-only safety cap on max-loss probability
MAX_PRIOR_RANGE_PCT: float | None = 0.030    # skip if prior-week (high-low)/close > 3%
MAX_GAP_PCT: float | None = 0.008            # skip if Monday open gaps > 0.8% from prior Friday close

# ── Alpaca CLI (subprocess) — replaces direct HTTPS requests entirely ───────
ALPACA_CLI_BIN = shutil.which("alpaca")

def _require_alpaca_cli() -> str:
    if not ALPACA_CLI_BIN:
        log.critical(
            "alpaca CLI not found on PATH — install it first:  "
            "brew install alpacahq/tap/cli   (see https://github.com/alpacahq/cli)"
        )
        sys.exit(1)
    return ALPACA_CLI_BIN

def _cli_profile_name() -> str:
    return "live" if ALLOW_LIVE_TRADING else "paper"

def _prompt(label: str) -> str:
    try:
        return input(label).strip()
    except EOFError:
        return ""

def _cli_profile_exists(name: str) -> bool:
    bin_path = _require_alpaca_cli()
    proc = subprocess.run([bin_path, "profile", "list", "--quiet"],
                           capture_output=True, text=True, timeout=15)
    return proc.returncode == 0 and re.search(rf"\b{re.escape(name)}\b", proc.stdout) is not None

def ensure_alpaca_profile() -> str:
    """
    Ensure an `alpaca` CLI profile exists for this environment (paper/live),
    prompting for the API key/secret and registering them via
    `alpaca profile login --api-key` on first run. Returns the profile name.
    """
    bin_path = _require_alpaca_cli()
    name  = _cli_profile_name()
    scope = "LIVE" if ALLOW_LIVE_TRADING else "PAPER"
    if _cli_profile_exists(name):
        log.info("Using existing alpaca CLI profile %r (%s)", name, scope)
        return name

    log.info("No %r alpaca CLI profile found — one-time setup.", name)
    key    = ""
    secret = ""
    while not key:
        key = _prompt(f"Alpaca {scope} TRADING API key: ")
    while not secret:
        secret = _prompt(f"Alpaca {scope} TRADING API secret: ")

    args = [bin_path, "profile", "login", "--api-key", "--key", key, "--secret", secret, "--name", name]
    if ALLOW_LIVE_TRADING:
        args.append("--live")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        log.critical("alpaca profile login failed: %s", (proc.stderr or proc.stdout).strip())
        sys.exit(1)
    log.info("Alpaca CLI profile %r registered.", name)
    return name

def load_credentials() -> dict:
    """
    Ensure an alpaca CLI profile exists (prompting for and registering API
    keys via `alpaca profile login` if missing) and interactively prompt for
    the number of option contracts to trade per leg. Returns a dict with
    keys: acctname, slot, qty, cli_profile, trade_base.
    """
    cli_profile = ensure_alpaca_profile()

    qty_raw = _prompt(f"Number of {TICKER} option contracts to trade per leg [{QTY}]: ")
    if not qty_raw:
        qty = QTY
    else:
        try:
            qty = int(qty_raw)
            if qty <= 0:
                raise ValueError
        except ValueError:
            log.critical("Invalid quantity %r — must be a positive integer. Aborting.", qty_raw)
            sys.exit(1)

    account = {
        "acctname":    "weekly_spy",
        "slot":        "P1",
        "qty":         qty,
        "cli_profile": cli_profile,
        "trade_base":  LIVE_BASE if ALLOW_LIVE_TRADING else PAPER_BASE,
    }
    log.info("Credentials loaded — qty=%d  env=%s", qty, "LIVE" if ALLOW_LIVE_TRADING else "PAPER")
    return account

# ── CSV trade log ─────────────────────────────────────────────────────────────
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csv-files")
os.makedirs(CSV_DIR, exist_ok=True)
CSV_LOG_PATH = os.path.join(CSV_DIR, "weekly_trading_spy_trades_log.csv")

_CSV_COLUMNS = [
    "timestamp", "strategy", "expiry", "atm", "spy_price",
    "longPut_strike", "shortPut_strike", "shortCall_strike", "longCall_strike",
    "longPut_mid", "shortPut_mid", "shortCall_mid", "longCall_mid",
    "net_credit", "max_profit", "max_loss", "prob_max_loss", "ev",
    "order_status", "close_pnl", "close_reason",
]

def _csv_ensure_header() -> None:
    try:
        write_header = not os.path.exists(CSV_LOG_PATH) or os.path.getsize(CSV_LOG_PATH) == 0
        if write_header:
            with open(CSV_LOG_PATH, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_CSV_COLUMNS).writeheader()
            log.info("CSV log created: %s", CSV_LOG_PATH)
    except Exception as exc:
        log.error("CSV header write failed: %s", exc)

def _csv_log_open(
    timestamp: str, expiry: date, atm: int, spy_price: float,
    strikes: dict, leg_mids: dict, ev_data: dict, order_status: str,
) -> None:
    _csv_ensure_header()
    row = {
        "timestamp":        timestamp,
        "strategy":         STRATEGY_LABEL,
        "expiry":           expiry.isoformat(),
        "atm":              atm,
        "spy_price":        round(spy_price, 4),
        "longPut_strike":   strikes["longPut"],
        "shortPut_strike":  strikes["shortPut"],
        "shortCall_strike": strikes["shortCall"],
        "longCall_strike":  strikes["longCall"],
        "longPut_mid":      round(leg_mids["longPut"],   4) if leg_mids.get("longPut")   is not None else "",
        "shortPut_mid":     round(leg_mids["shortPut"],  4) if leg_mids.get("shortPut")  is not None else "",
        "shortCall_mid":    round(leg_mids["shortCall"], 4) if leg_mids.get("shortCall") is not None else "",
        "longCall_mid":     round(leg_mids["longCall"],  4) if leg_mids.get("longCall")  is not None else "",
        "net_credit":       round(ev_data["net_credit"],    4),
        "max_profit":       round(ev_data["max_profit"],    2),
        "max_loss":         round(ev_data["max_loss"],      2),
        "prob_max_loss":    round(ev_data["prob_max_loss"], 6),
        "ev":               round(ev_data["ev"],            2),
        "order_status":     order_status,
        "close_pnl":        "",
        "close_reason":     "",
    }
    try:
        with open(CSV_LOG_PATH, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_CSV_COLUMNS).writerow(row)
        log.info("CSV open row written → %s", CSV_LOG_PATH)
    except Exception as exc:
        log.error("CSV open row write failed: %s", exc)

def _csv_log_close(close_pnl: float, close_reason: str) -> None:
    _csv_ensure_header()
    row = {col: "" for col in _CSV_COLUMNS}
    row["timestamp"]    = datetime.now(ET).isoformat()
    row["strategy"]     = STRATEGY_LABEL
    row["order_status"] = "CLOSE"
    row["close_pnl"]    = round(close_pnl, 2)
    row["close_reason"] = close_reason
    try:
        with open(CSV_LOG_PATH, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_CSV_COLUMNS).writerow(row)
        log.info("CSV close row written → %s  pnl=$%.2f  reason=%s",
                 CSV_LOG_PATH, close_pnl, close_reason)
    except Exception as exc:
        log.error("CSV close row write failed: %s", exc)

def _recover_last_max_profit_from_csv() -> float | None:
    """Best-effort recovery of max_profit from the strategy CSV log."""
    try:
        if not os.path.exists(CSV_LOG_PATH):
            return None
        with open(CSV_LOG_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
        for row in reversed(rows):
            raw = (row.get("max_profit") or "").strip()
            if not raw:
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            if val > 0:
                return val
    except Exception as exc:
        log.warning("max_profit CSV recovery failed: %s", exc)
    return None

# ── IMV (initial market value) from Activities/FILL ──────────────────────────
def get_fills_for_order(order: dict) -> list[dict]:
    """
    Fetch account FILL activities and return only the ones belonging to this
    mleg order — matched by the parent order id or any child leg order id.
    """
    order_ids = {order.get("id")} if order.get("id") else set()
    for leg in order.get("legs") or []:
        if leg.get("id"):
            order_ids.add(leg["id"])
    if not order_ids:
        return []

    activities = _get(
        f"{_ctx.trade_base}/v2/account/activities/FILL",
        _trade_headers(),
        params={"direction": "desc", "page_size": "100"},
    )
    if not isinstance(activities, list):
        return []
    return [a for a in activities if isinstance(a, dict) and a.get("order_id") in order_ids]

def calculate_imv_from_fills(fills: list[dict]) -> float:
    """
    IMV = sum of each fill's cash-flow contribution: a SELL fill produces
    income (+), a BUY fill is a cost (-). Options multiplier = 100/contract.
    """
    imv = 0.0
    for f in fills:
        try:
            price = float(f.get("price") or 0)
            qty   = float(f.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        sign = 1.0 if (f.get("side") or "").lower().startswith("sell") else -1.0
        imv += sign * price * qty * 100
    return round(imv, 2)

# ── Market-value CSV log (per-leg + combined value, logged every minute) ─────
MARKET_VALUE_CSV_PATH = os.path.join(CSV_DIR, "weekly_trading_spy_market_value_log.csv")
_MV_CSV_COLUMNS = [
    "timestamp", "symbol", "qty", "current_price", "market_value",
    "unrealized_pl", "imv", "combined_market_value", "pnl_pct",
]

def _mv_csv_ensure_header() -> None:
    try:
        write_header = not os.path.exists(MARKET_VALUE_CSV_PATH) or os.path.getsize(MARKET_VALUE_CSV_PATH) == 0
        if write_header:
            with open(MARKET_VALUE_CSV_PATH, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_MV_CSV_COLUMNS).writeheader()
            log.info("Market-value CSV log created: %s", MARKET_VALUE_CSV_PATH)
    except Exception as exc:
        log.error("Market-value CSV header write failed: %s", exc)

def log_position_market_values() -> None:
    """
    Every MARKET_VALUE_LOG_INTERVAL seconds: log each open leg's current
    market value, plus one TOTAL row combining leg_total + IMV.
    """
    positions = get_spy_options_positions()
    ts = datetime.now(ET).isoformat()
    _mv_csv_ensure_header()

    if not positions:
        try:
            with open(MARKET_VALUE_CSV_PATH, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=_MV_CSV_COLUMNS).writerow({
                    "timestamp": ts, "symbol": "NONE", "qty": "", "current_price": "",
                    "market_value": "", "unrealized_pl": "", "imv": "",
                    "combined_market_value": "", "pnl_pct": "",
                })
        except Exception as exc:
            log.error("Market-value CSV row write failed: %s", exc)
        return

    with _ctx.state_lock:
        imv = _ctx.imv_state.get("imv")

    rows = []
    leg_total = 0.0
    for pos in positions:
        market_value = float(pos.get("market_value") or 0)
        leg_total += market_value
        row = {
            "timestamp":      ts,
            "symbol":         pos.get("symbol", ""),
            "qty":            pos.get("qty", ""),
            "current_price":  round(float(pos.get("current_price") or 0), 4),
            "market_value":   round(market_value, 2),
            "unrealized_pl":  round(float(pos.get("unrealized_pl") or 0), 2),
            "imv":            "",
            "combined_market_value": "",
            "pnl_pct":        "",
        }
        rows.append(row)
        log.info("Leg market value: %-22s qty=%-4s price=$%.4f  market_value=$%.2f  unrealized_pl=$%.2f",
                 row["symbol"], row["qty"], row["current_price"], row["market_value"], row["unrealized_pl"])

    combined = round(leg_total + imv, 2) if imv is not None else ""
    pnl_pct  = round(combined / abs(imv) * 100, 2) if imv else ""
    rows.append({
        "timestamp": ts, "symbol": "TOTAL", "qty": "", "current_price": "",
        "market_value": round(leg_total, 2), "unrealized_pl": "",
        "imv": imv if imv is not None else "", "combined_market_value": combined,
        "pnl_pct": pnl_pct,
    })
    log.info("Combined market value: leg_total=$%.2f  imv=%s  combined=%s  pnl_pct=%s",
             leg_total, f"${imv:.2f}" if imv is not None else "n/a",
             f"${combined:.2f}" if combined != "" else "n/a",
             f"{pnl_pct:.2f}%" if pnl_pct != "" else "n/a")

    try:
        with open(MARKET_VALUE_CSV_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_MV_CSV_COLUMNS)
            writer.writerows(rows)
    except Exception as exc:
        log.error("Market-value CSV row write failed: %s", exc)

# ── Per-week trade sentinel (prevents duplicate opens across restarts) ───────
_SENTINEL_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "_sentinels"

def _sentinel_path(acctname: str, slot: str) -> Path:
    """Return path of this Monday's sentinel file for this account/slot."""
    today = datetime.now(ET).strftime("%Y%m%d")
    _SENTINEL_DIR.mkdir(exist_ok=True)
    return _SENTINEL_DIR / f"weekly_trading_spy_{acctname}_{slot}_{today}.opened"

def _sentinel_exists(acctname: str, slot: str) -> bool:
    return _sentinel_path(acctname, slot).exists()

def _sentinel_write(acctname: str, slot: str) -> None:
    try:
        p = _sentinel_path(acctname, slot)
        p.write_text(datetime.now(ET).isoformat())
        log.info("Trade sentinel written: %s", p)
    except Exception as exc:
        log.error("Failed to write trade sentinel: %s", exc)

# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _sighandler(sig, frame):
    log.info("Shutdown signal received — exiting cleanly.")
    for _ev in _all_shutdowns:
        _ev.set()

signal.signal(signal.SIGINT,  _sighandler)
signal.signal(signal.SIGTERM, _sighandler)

# ── HTTP-style helpers (backed by the alpaca CLI, not raw HTTPS requests) ───
def _trade_headers() -> dict:
    """No-op — auth is handled by the alpaca CLI profile, not manual headers."""
    return {}

def _data_headers() -> dict:
    """No-op — auth is handled by the alpaca CLI profile, not manual headers."""
    return {}

def _url_to_cli_args(url: str) -> tuple[str, bool]:
    """Split a full Alpaca base+path URL into (path, use_data_api)."""
    for base in (DATA_BASE, PAPER_BASE, LIVE_BASE):
        if url.startswith(base):
            return url[len(base):], base == DATA_BASE
    raise ValueError(f"Unrecognized Alpaca base URL: {url}")

def _run_alpaca_cli(args: list[str]) -> tuple[dict | list | None, int]:
    """Run `alpaca <args>` under the active profile and return (json_body, http_status)."""
    bin_path = _require_alpaca_cli()
    cmd = [bin_path, "--profile", _cli_profile_name(), "--quiet"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"error": "alpaca CLI timed out"}, 599
    out = proc.stdout.strip()
    if proc.returncode == 0:
        try:
            return (json.loads(out) if out else {}), 200
        except json.JSONDecodeError:
            return {"raw": out}, 200
    err_raw = (proc.stderr or out).strip()
    try:
        err = json.loads(err_raw)
    except json.JSONDecodeError:
        err = {"error": err_raw or "unknown alpaca CLI error"}
    status = int(err.get("status") or (401 if proc.returncode == 2 else 500))
    return err, status

def _get(url: str, headers: dict, params: dict | None = None) -> dict | list | None:
    path, use_data = _url_to_cli_args(url)
    args = ["api", "GET", path]
    if params:
        args += ["--query", urlencode(params)]
    if use_data:
        args.append("--use-data-api")
    payload, status = _run_alpaca_cli(args)
    if status != 200:
        log.error("GET %s failed: %s", url, payload)
        return None
    return payload

def _post(url: str, headers: dict, body: dict) -> tuple[dict | None, int]:
    """Returns (parsed_json_or_None, http_status_code)."""
    path, use_data = _url_to_cli_args(url)
    args = ["api", "POST", path, "--body", json.dumps(body)]
    if use_data:
        args.append("--use-data-api")
    payload, status = _run_alpaca_cli(args)
    if status < 200 or status >= 300:
        log.error("POST %s HTTP %s: %s", url, status, payload)
        return None, status
    return payload, status

def _delete(url: str, headers: dict) -> tuple[dict | None, int]:
    """Returns (parsed_json_or_None, http_status_code)."""
    path, use_data = _url_to_cli_args(url)
    args = ["api", "DELETE", path]
    if use_data:
        args.append("--use-data-api")
    payload, status = _run_alpaca_cli(args)
    if status < 200 or status >= 300:
        log.error("DELETE %s HTTP %s: %s", url, status, payload)
        return None, status
    return payload, status

# ── OCC symbol builder ────────────────────────────────────────────────────────
def occ_symbol(underlying: str, expiry: date, cp: str, strike: float) -> str:
    """Build OCC option symbol, e.g. SPY   260403P00631000 (underlying padded to 6 chars)."""
    cp_c       = "C" if cp.upper() == "C" else "P"
    strike_str = str(round(strike * 1000)).zfill(8)
    return f"{underlying.ljust(6)}{expiry.strftime('%y%m%d')}{cp_c}{strike_str}"

# ── Date helpers ──────────────────────────────────────────────────────────────
_market_holidays: set[date] = set()

def _load_market_holidays(lookahead_days: int = 60) -> None:
    global _market_holidays
    if _market_holidays:
        return
    today = datetime.now(ET).date()
    end   = today + timedelta(days=lookahead_days)
    try:
        calendar = _get(
            f"{_ctx.trade_base}/v2/calendar",
            _trade_headers(),
            params={"start": today.isoformat(), "end": end.isoformat()},
        )
        if not isinstance(calendar, list):
            log.warning("Market calendar fetch failed — holiday check skipped")
            return
        open_days = {date.fromisoformat(d["date"]) for d in calendar}
        _market_holidays = set()
        cur = today
        while cur <= end:
            if cur not in open_days:
                _market_holidays.add(cur)
            cur += timedelta(days=1)
        log.info("Market calendar loaded — %d holiday/weekend days in next %d days",
                 len(_market_holidays), lookahead_days)
    except Exception as exc:
        log.warning("Could not load market calendar: %s — holiday check skipped", exc)

def next_friday(ref: date | None = None) -> date:
    """Return the next Friday that is also a trading day (skips holidays like Good Friday)."""
    _load_market_holidays()
    d = ref or datetime.now(ET).date()
    days_ahead = (4 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    candidate = d + timedelta(days=days_ahead)
    while candidate in _market_holidays:
        log.info("next_friday: %s is a market holiday — trying %s instead",
                 candidate, candidate + timedelta(days=7))
        candidate += timedelta(days=7)
    return candidate

def now_et() -> datetime:
    return datetime.now(ET)

def today_et() -> date:
    return now_et().date()

def _open_window_is_active(now: datetime | None = None) -> bool:
    """True while the dynamic ENTRY_SEARCH window is still open, Monday only."""
    if FORCE_TRADE_NOW:
        return True
    now = now or now_et()
    if now.weekday() != OPEN_WEEKDAY:
        return False
    t = (now.hour, now.minute)
    return ENTRY_SEARCH_START <= t <= ENTRY_SEARCH_END

def _seconds_until_open_cutoff(now: datetime | None = None) -> int:
    now = now or now_et()
    cutoff = now.replace(
        hour=ENTRY_SEARCH_END[0],
        minute=ENTRY_SEARCH_END[1],
        second=0,
        microsecond=0,
    )
    return max(0, int((cutoff - now).total_seconds()))

def _log_retry_heartbeat(retry: int) -> None:
    if retry <= 0 or retry % 2 != 0:
        return
    elapsed_seconds = retry * EV_RETRY_WAIT
    remaining_seconds = _seconds_until_open_cutoff()
    elapsed_m, elapsed_s = divmod(elapsed_seconds, 60)
    remain_m, remain_s = divmod(remaining_seconds, 60)
    log.info(
        "Open retry heartbeat: attempt=%d elapsed=%02dm%02ds remaining_to_cutoff=%02dm%02ds",
        retry, elapsed_m, elapsed_s, remain_m, remain_s,
    )

# ── Live price feed (direct Alpaca polling, no websocket/CSV) ────────────────
_tick_lock = threading.Lock()
_session_ticks: dict[date, list[tuple[datetime, float]]] = {}

def _fetch_latest_trade_price() -> float | None:
    """Fetch SPY's latest trade price via the alpaca CLI's data API passthrough."""
    url = f"{DATA_BASE}/v2/stocks/{TICKER}/trades/latest"
    data = _get(url, _data_headers())
    if data is None:
        return None
    price = (data.get("trade") or {}).get("p")
    return float(price) if price is not None else None

def read_session_ticks(day: date) -> list[tuple[datetime, float]]:
    """Return all (timestamp, price) ticks polled so far for the session date."""
    with _tick_lock:
        return list(_session_ticks.get(day, []))

def build_minute_bars(ticks: list[tuple[datetime, float]]) -> dict[tuple[int, int], dict]:
    """Aggregate raw (timestamp, price) ticks into per-minute OHLC bars."""
    bars: dict[tuple[int, int], dict] = {}
    for ts, price in ticks:
        key = (ts.hour, ts.minute)
        b = bars.get(key)
        if b is None:
            bars[key] = {"open": price, "high": price, "low": price, "close": price}
        else:
            b["high"]  = max(b["high"], price)
            b["low"]   = min(b["low"], price)
            b["close"] = price
    return bars

def get_spy_price() -> float | None:
    """Poll SPY's latest trade price directly from Alpaca and record the tick in-memory."""
    price = _fetch_latest_trade_price()
    if price is None:
        log.error("No live trade price available for %s from Alpaca.", TICKER)
        return None
    ts = now_et()
    with _tick_lock:
        _session_ticks.setdefault(ts.date(), []).append((ts, price))
    log.info("%s live price: $%.2f  (polled @ %s ET)", TICKER, price, ts.strftime("%H:%M:%S"))
    return price

def get_spy_prev_week_stats() -> dict | None:
    """
    Return prior-week stats used only for the Monday gap / prior-week-range
    pre-filters (not the intraday dynamic timing). Sourced from Alpaca daily
    bars since it is historical, low-frequency data:
      - prev_close:       most recent completed session's close (prior Friday)
      - prior_week_high:  max daily high over the last 5 completed sessions
      - prior_week_low:   min daily low over the last 5 completed sessions
    """
    start = (today_et() - timedelta(days=14)).isoformat()
    end   = today_et().isoformat()
    url   = f"{DATA_BASE}/v2/stocks/{TICKER}/bars"
    params = {
        "timeframe": "1Day",
        "start":     start,
        "end":       end,
        "limit":     10,
        "feed":      "iex",
        "sort":      "asc",
    }
    data = _get(url, _data_headers(), params=params)
    if not data:
        log.warning("get_spy_prev_week_stats: no response")
        return None
    bars = data.get("bars") or []
    if len(bars) < 2:
        log.warning("get_spy_prev_week_stats: only %d bar(s) returned — cannot determine prior week", len(bars))
        return None
    prev_bars = bars[:-1]         # exclude today (if present) — only completed sessions
    last_5 = prev_bars[-5:] if len(prev_bars) >= 5 else prev_bars
    prev_close = float(prev_bars[-1]["c"])
    prior_week_high = max(float(b["h"]) for b in last_5)
    prior_week_low  = min(float(b["l"]) for b in last_5)
    return {
        "prev_close":      prev_close,
        "prior_week_high": prior_week_high,
        "prior_week_low":  prior_week_low,
    }

# ── Dynamic entry timing (causal live approximation of find_optimal_entry) ───
def wait_for_dynamic_entry() -> None:
    """
    Polls Alpaca's live price from ENTRY_SEARCH_START through
    ENTRY_SEARCH_END on Monday, tracking the trailing ENTRY_SETTLE_WINDOW_MIN-
    minute high/low range minute-by-minute (mirrors find_optimal_entry() in
    weekly_iron_butterfly_spy_backtest_dynamic.py). Unlike the backtest, which
    can look at the WHOLE window in hindsight, this live version can only act
    causally: it commits to entering once the trailing range has gone
    ENTRY_PATIENCE_MIN consecutive minutes without setting a new low (the
    quiet period appears to have passed), or the window times out at
    ENTRY_SEARCH_END — whichever comes first.
    """
    if FORCE_TRADE_NOW:
        return
    log.info("Waiting for dynamic entry window %02d:%02d-%02d:%02d ET …",
             *ENTRY_SEARCH_START, *ENTRY_SEARCH_END)
    best_range: float | None = None
    stale_minutes = 0
    last_seen_minute: tuple[int, int] | None = None
    while not _ctx.shutdown.is_set():
        now = now_et()
        t = (now.hour, now.minute)
        if t >= ENTRY_SEARCH_END:
            log.info("Dynamic entry window elapsed — entering at %02d:%02d ET.", *t)
            return
        get_spy_price()  # poll Alpaca now to record a tick for this minute's bar
        ticks = read_session_ticks(now.date())
        bars  = build_minute_bars(ticks)
        window = sorted((k, v) for k, v in bars.items() if ENTRY_SEARCH_START <= k < ENTRY_SEARCH_END)
        if len(window) >= ENTRY_SETTLE_WINDOW_MIN and window[-1][0] != last_seen_minute:
            last_seen_minute = window[-1][0]
            trailing = window[-ENTRY_SETTLE_WINDOW_MIN:]
            rng = max(b["high"] for _, b in trailing) - min(b["low"] for _, b in trailing)
            if best_range is None or rng < best_range:
                best_range = rng
                stale_minutes = 0
                log.info("Dynamic entry: new quietest trailing range %.4f at %02d:%02d ET.",
                         rng, *last_seen_minute)
            else:
                stale_minutes += 1
                log.info("Dynamic entry: range %.4f not an improvement (%d/%d stale minutes).",
                         rng, stale_minutes, ENTRY_PATIENCE_MIN)
                if stale_minutes >= ENTRY_PATIENCE_MIN:
                    log.info("Dynamic entry: quiet period appears to have passed — entering now.")
                    return
        if _ctx.shutdown.wait(15):
            return

# ── Dynamic exit (wing-breach detection) ──────────────────────────────────────
def check_underlying_breach(atm: float) -> bool:
    """True if Alpaca's live price has moved BREACH_FRACTION * WING_WIDTH away from atm."""
    price = get_spy_price()
    if price is None:
        return False
    breach_dist = BREACH_FRACTION * WING_WIDTH
    return abs(price - atm) > breach_dist

def monitor_underlying_breach() -> None:
    """Runs periodically Mon-Thu while a position is open; closes early on wing-breach."""
    with _ctx.state_lock:
        if not _ctx.trade_executed or not _ctx.trade_history:
            return
        atm = _ctx.trade_history[-1].get("atm")
    if atm is None:
        return
    if check_underlying_breach(float(atm)):
        log.info("Underlying breach detected (ATM=%.2f, BREACH_FRACTION=%.2f) — auto-closing.",
                 atm, BREACH_FRACTION)
        close_butterfly(reason="underlying_breach")

# ── Options snapshot ──────────────────────────────────────────────────────────
def _snap_key(occ: str) -> str:
    """
    Convert a padded OCC symbol to the unpadded form used by Alpaca's
    data API and returned as snapshot dict keys.
    'SPY   260403P00631000' → 'SPY260403P00631000'
    The 6-char padded form is only used in order payloads.
    """
    return occ.replace(" ", "")

def get_options_snapshots(symbols: list[str]) -> dict:
    """
    Fetch option snapshots for the given OCC symbols.
    Sends unpadded symbols to the API (spaces stripped).
    Returns dict keyed by unpadded symbol → snapshot data.
    """
    unpadded = [_snap_key(s) for s in symbols]
    joined   = ",".join(unpadded)
    url      = f"{DATA_BASE}/v1beta1/options/snapshots"
    params   = {"symbols": joined, "feed": "opra"}
    data     = _get(url, _data_headers(), params=params)
    if not data:
        return {}
    if "snapshots" in data:
        snaps = data["snapshots"]
    else:
        snaps = data
    if not isinstance(snaps, dict):
        return {}
    log.debug("Options snapshot raw keys (%d): %s", len(snaps), list(snaps.keys())[:8])
    return snaps

def _occ_cp_and_strike(occ: str) -> tuple[str | None, float | None]:
    """Parse OCC symbol into (C/P, strike) from either padded or unpadded forms."""
    raw = occ.replace(" ", "")
    m = re.match(rf"^{re.escape(TICKER)}\d{{6}}([CP])(\d{{8}})$", raw)
    if not m:
        return None, None
    cp, strike_raw = m.groups()
    return cp, int(strike_raw) / 1000.0

def _pick_nearest_strike(
    strikes: list[float],
    target: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float | None:
    """Return nearest strike to target, optionally bounded."""
    candidates = []
    for s in strikes:
        if min_value is not None and s < min_value:
            continue
        if max_value is not None and s > max_value:
            continue
        candidates.append(s)
    if not candidates:
        return None
    return sorted(candidates, key=lambda s: (abs(s - target), s))[0]

def _resolve_snapshot_available_butterfly(
    atm: int,
    expiry: date,
) -> tuple[dict, dict, dict] | tuple[None, None, None]:
    """Pick nearest snapshot-available strikes for all four butterfly legs."""
    step = globals().get("STRIKE_STEP", 1)
    center = round(atm / step) * step if step else round(atm)

    target_lp = center - WING_WIDTH
    target_sp = center
    target_sc = center
    target_lc = center + WING_WIDTH

    scan_pad = max(8, WING_WIDTH)
    min_strike = max(1, int(math.floor(center - WING_WIDTH - scan_pad)))
    max_strike = int(math.ceil(center + WING_WIDTH + scan_pad))

    universe: list[str] = []
    for strike in range(min_strike, max_strike + 1):
        universe.append(occ_symbol(TICKER, expiry, "P", strike))
        universe.append(occ_symbol(TICKER, expiry, "C", strike))

    snaps = get_options_snapshots(universe)
    if not snaps:
        log.warning("No option snapshots returned for strike-universe scan.")
        return None, None, None

    available_puts: set[float] = set()
    available_calls: set[float] = set()
    for key, snap in snaps.items():
        cp, strike = _occ_cp_and_strike(str(key))
        if cp is None or strike is None:
            continue
        if _extract_mid(snap, f"universe-{key}", log_missing=False) is None:
            continue
        if cp == "P":
            available_puts.add(strike)
        elif cp == "C":
            available_calls.add(strike)

    if not available_puts or not available_calls:
        log.warning(
            "No priced snapshots found for puts/calls (puts=%d calls=%d).",
            len(available_puts), len(available_calls),
        )
        return None, None, None

    short_common = sorted(set(available_puts) & set(available_calls))
    short_str = _pick_nearest_strike(short_common, float(center))
    if short_str is None:
        log.warning("Could not find a common put/call strike for the butterfly shorts.")
        return None, None, None

    lp_str = _pick_nearest_strike(sorted(available_puts), target_lp, max_value=short_str - 0.001)
    lc_str = _pick_nearest_strike(sorted(available_calls), target_lc, min_value=short_str + 0.001)
    if lp_str is None or lc_str is None:
        log.warning(
            "Could not find valid wing strikes beyond short strike %.1f.",
            short_str,
        )
        return None, None, None

    strikes = {
        "longPut": lp_str,
        "shortPut": short_str,
        "shortCall": short_str,
        "longCall": lc_str,
    }
    symbols = {
        "longPut": occ_symbol(TICKER, expiry, "P", lp_str),
        "shortPut": occ_symbol(TICKER, expiry, "P", short_str),
        "shortCall": occ_symbol(TICKER, expiry, "C", short_str),
        "longCall": occ_symbol(TICKER, expiry, "C", lc_str),
    }

    selected_snaps = get_options_snapshots(list(symbols.values()))
    if selected_snaps:
        snaps.update(selected_snaps)

    if any(_extract_mid(snaps.get(_snap_key(sym)), leg, log_missing=False) is None for leg, sym in symbols.items()):
        log.warning("Selected butterfly legs still missing prices after resolution.")
        return None, None, None

    if any(abs(strikes[k] - v) > 1e-9 for k, v in {
        "longPut": float(target_lp),
        "shortPut": float(target_sp),
        "shortCall": float(target_sc),
        "longCall": float(target_lc),
    }.items()):
        log.info(
            "Adjusted strikes to snapshot-available contracts: "
            "target LP/SP/SC/LC=%s/%s/%s/%s -> selected %s/%s/%s/%s",
            target_lp, target_sp, target_sc, target_lc,
            strikes["longPut"], strikes["shortPut"], strikes["shortCall"], strikes["longCall"],
        )

    return symbols, strikes, snaps

def _extract_mid(snap: dict | None, label: str = "", log_missing: bool = True) -> float | None:
    if not snap:
        if log_missing:
            log.warning("_extract_mid(%s): snapshot is None/empty — no price available", label)
        return None
    q = snap.get("latestQuote") or snap.get("latest_quote") or {}
    ap = float(q.get("ap") or q.get("ask_price") or 0)
    bp = float(q.get("bp") or q.get("bid_price") or 0)
    if ap > 0 and bp > 0:
        return (ap + bp) / 2
    t = snap.get("latestTrade") or snap.get("latest_trade") or {}
    p = float(t.get("p") or t.get("price") or 0)
    if p > 0:
        return p
    if log_missing:
        log.warning("_extract_mid(%s): no bid/ask or trade price in snapshot: %s",
                    label, str(snap)[:200])
    return None

# ── Fill poll ─────────────────────────────────────────────────────────────────
def poll_fill(order_id: str, label: str,
              max_retries: int = POLL_MAX_RETRIES) -> dict | None:
    time.sleep(POLL_INITIAL_WAIT)
    url   = f"{_ctx.trade_base}/v2/orders/{order_id}"
    order = None
    for attempt in range(max_retries + 1):
        order = _get(url, _trade_headers())
        if order:
            status = order.get("status", "")
            if status == "filled":
                log.info("%s: order filled ✓", label)
                return order
            if status in ("rejected", "canceled", "expired"):
                log.warning("%s: terminal status=%s", label, status)
                return order
            log.info("%s: status=%s, attempt %d/%d", label, status, attempt, max_retries)
        else:
            log.warning("%s: GET order failed (attempt %d)", label, attempt)
        if attempt < max_retries:
            time.sleep(POLL_RETRY_WAIT)
    return order

# ── SPY options positions ─────────────────────────────────────────────────────
def get_spy_options_positions() -> list[dict]:
    positions = _get(f"{_ctx.trade_base}/v2/positions", _trade_headers())
    if not isinstance(positions, list):
        return []
    return [
        p for p in positions
        if isinstance(p, dict)
        and str(p.get("symbol", "")).startswith(TICKER)
        and p.get("asset_class") in ("option", "us_option")
    ]

# ── EV calculation ────────────────────────────────────────────────────────────
def calculate_ev(snaps: dict, symbols: dict, qty: int, wing_width: int) -> dict | None:
    """
    Calculate iron butterfly risk metrics from live option snapshots.
    Returns dict with: net_credit, max_profit, max_loss, ev, prob_max_loss.
    Returns None if any leg price is missing.
    """
    lp_price = _extract_mid(snaps.get(_snap_key(symbols["longPut"])),   "longPut")
    sp_price = _extract_mid(snaps.get(_snap_key(symbols["shortPut"])),  "shortPut")
    sc_price = _extract_mid(snaps.get(_snap_key(symbols["shortCall"])), "shortCall")
    lc_price = _extract_mid(snaps.get(_snap_key(symbols["longCall"])),  "longCall")

    log.info(
        "Leg prices: longPut=%s  shortPut=%s  shortCall=%s  longCall=%s",
        f"${lp_price:.2f}" if lp_price is not None else "MISSING",
        f"${sp_price:.2f}" if sp_price is not None else "MISSING",
        f"${sc_price:.2f}" if sc_price is not None else "MISSING",
        f"${lc_price:.2f}" if lc_price is not None else "MISSING",
    )

    if any(p is None for p in (lp_price, sp_price, sc_price, lc_price)):
        log.error(
            "Cannot calculate EV — one or more leg prices are missing. "
            "Snapshot keys returned: %s", list(snaps.keys())
        )
        return None

    net_credit = (sp_price + sc_price) - (lp_price + lc_price)
    max_profit = net_credit * 100 * qty
    max_loss   = max(0.0, (wing_width - net_credit)) * 100 * qty

    if max_loss == 0:
        log.warning("max_loss=0 (net_credit=%.2f ≥ wing_width=%d) — skipping trade.",
                    net_credit, wing_width)
        return None

    def _delta_abs(sym: str) -> float:
        snap = snaps.get(_snap_key(sym)) or {}
        g    = snap.get("greeks") or {}
        raw  = abs(float(g.get("delta") or 0))
        return min(raw, 0.25)

    prob_below_lp = _delta_abs(symbols["longPut"])
    prob_above_lc = _delta_abs(symbols["longCall"])
    prob_max_loss = prob_below_lp + prob_above_lc
    prob_partial  = min(0.25, max(0.0, 1 - prob_max_loss - 0.35))
    prob_profit   = max(0.10, 1 - prob_max_loss - prob_partial)

    liquidation_factor  = 0.88
    adj_max_profit      = max_profit * liquidation_factor
    expected_max_loss   = max_loss   * prob_max_loss
    expected_max_profit = adj_max_profit * prob_profit * 0.65
    avg_partial_profit  = adj_max_profit * 0.5
    expected_partial    = avg_partial_profit * prob_partial
    ev                  = expected_max_profit + expected_partial - expected_max_loss

    log.info(
        "EV calc: net_credit=%.2f  max_profit=$%.2f  max_loss=$%.2f  "
        "prob_max_loss=%.1f%%  EV=$%.2f",
        net_credit, max_profit, max_loss,
        prob_max_loss * 100, ev,
    )
    return {
        "net_credit":   net_credit,
        "max_profit":   max_profit,
        "max_loss":     max_loss,
        "ev":           ev,
        "prob_max_loss":prob_max_loss,
    }

# ── Open butterfly ────────────────────────────────────────────────────────────
def open_butterfly():
    """
    Fetch SPY live CSV price → calculate strikes → check net_credit > 0
    (retry up to 8× if prices unavailable) → submit mleg market order.
    Runs at most ONCE per account per week (Monday only), enforced by an
    in-memory flag AND a persistent sentinel file so restarts cannot cause
    duplicate orders. Callers should invoke wait_for_dynamic_entry() first
    so this fires at/after the dynamically-chosen entry minute.
    """
    if _sentinel_exists(_ctx.acct_name, _ctx.slot):
        log.info(
            "Trade sentinel exists for %s/%s today — skipping open (already traded).",
            _ctx.acct_name, _ctx.slot,
        )
        with _ctx.state_lock:
            _ctx.trade_executed = True
        return "skip_sentinel_exists"

    with _ctx.state_lock:
        if _ctx.trade_executed:
            log.info("Trade already executed this week (in-memory) — skipping open.")
            return "skip_already_executed"

    log.info("═══ Iron Butterfly SPY: OPEN ═══")

    expiry = next_friday()
    retry  = 0

    while True:
        if _ctx.shutdown.is_set():
            return "skip_shutdown"

        if not _open_window_is_active():
            log.warning(
                "Dynamic entry window closed (%02d:%02d ET) — stopping open attempts.",
                *ENTRY_SEARCH_END,
            )
            return "skip_open_window_closed"

        # 1. Fetch SPY live price from local CSV feed
        spy_price = get_spy_price()
        if spy_price is None:
            log.error("Cannot get SPY price — aborting open.")
            return "abort_no_price_data"

        atm = round(spy_price)

        # 1b. Gap / prior-week-range pre-filters (once per attempt, uses prior week bars)
        if not TEST_MODE and retry == 0 and (MAX_GAP_PCT is not None or MAX_PRIOR_RANGE_PCT is not None):
            prev_stats = get_spy_prev_week_stats()
            if prev_stats is not None:
                monday_gap_pct     = abs(spy_price - prev_stats["prev_close"]) / prev_stats["prev_close"]
                prior_week_range_pct = (
                    (prev_stats["prior_week_high"] - prev_stats["prior_week_low"]) / prev_stats["prev_close"]
                )
                log.info(
                    "Weekly gap filter: gap=%.2f%%  prior_week_range=%.2f%%",
                    monday_gap_pct * 100, prior_week_range_pct * 100,
                )
                if MAX_GAP_PCT is not None and monday_gap_pct > MAX_GAP_PCT:
                    log.warning(
                        "Monday gap filter FAIL: gap %.2f%% > %.1f%% — abandoning open for this week.",
                        monday_gap_pct * 100, MAX_GAP_PCT * 100,
                    )
                    _ctx.day_done.set()
                    return "skip_gap_filter"
                if MAX_PRIOR_RANGE_PCT is not None and prior_week_range_pct > MAX_PRIOR_RANGE_PCT:
                    log.warning(
                        "Prior-week range filter FAIL: %.2f%% > %.2f%% — abandoning open for this week.",
                        prior_week_range_pct * 100, MAX_PRIOR_RANGE_PCT * 100,
                    )
                    _ctx.day_done.set()
                    return "skip_prior_range_filter"
            else:
                log.warning("get_spy_prev_week_stats returned None — skipping gap/prior-range filters.")

        if FILTER_CHECK_ONLY:
            return "filter_ok"

        symbols = {
            "longPut":   occ_symbol(TICKER, expiry, "P", atm - WING_WIDTH),
            "shortPut":  occ_symbol(TICKER, expiry, "P", atm),
            "shortCall": occ_symbol(TICKER, expiry, "C", atm),
            "longCall":  occ_symbol(TICKER, expiry, "C", atm + WING_WIDTH),
        }

        log.info(
            "SPY=%.2f  ATM=%d  expiry=%s\n"
            "  longPut=%s  shortPut=%s  shortCall=%s  longCall=%s",
            spy_price, atm, expiry,
            symbols["longPut"],  symbols["shortPut"],
            symbols["shortCall"], symbols["longCall"],
        )
        try:
            symbols, strikes, snaps = _resolve_snapshot_available_butterfly(atm, expiry)
        except NameError as exc:
            log.error("Resolver helper missing (%s) — using direct ATM butterfly legs fallback.", exc)
            strikes = {
                "longPut": atm - WING_WIDTH,
                "shortPut": atm,
                "shortCall": atm,
                "longCall": atm + WING_WIDTH,
            }
            symbols = {
                "longPut": occ_symbol(TICKER, expiry, "P", strikes["longPut"]),
                "shortPut": occ_symbol(TICKER, expiry, "P", strikes["shortPut"]),
                "shortCall": occ_symbol(TICKER, expiry, "C", strikes["shortCall"]),
                "longCall": occ_symbol(TICKER, expiry, "C", strikes["longCall"]),
            }
            snaps = get_options_snapshots(list(symbols.values()))

        if not symbols or not strikes or not snaps:
            retry += 1
            _log_retry_heartbeat(retry)
            if retry > MAX_EV_RETRIES and not _open_window_is_active():
                log.error(
                    "Legs still unavailable after %d retries — abandoning.",
                    MAX_EV_RETRIES,
                )
                return "abort_no_leg_prices"
            log.warning(
                "Snapshot leg resolution failed — retry %d/%d in %ds",
                retry, MAX_EV_RETRIES, EV_RETRY_WAIT,
            )
            if _ctx.shutdown.wait(EV_RETRY_WAIT):
                return "skip_shutdown"
            continue

        atm = strikes["shortCall"]

        log.info(
            "%s target/open strikes: LP=%s SP=%s SC=%s LC=%s",
            TICKER,
            symbols["longPut"],
            symbols["shortPut"],
            symbols["shortCall"],
            symbols["longCall"],
        )

        ev_data = calculate_ev(snaps, symbols, _ctx.qty, WING_WIDTH)

        if ev_data is None:
            retry += 1
            _log_retry_heartbeat(retry)
            if retry > MAX_EV_RETRIES and not _open_window_is_active():
                log.error(
                    "Leg prices still unavailable after %d retries — abandoning.",
                    MAX_EV_RETRIES,
                )
                return "abort_no_leg_prices"
            log.warning(
                "Leg price data missing — retry %d/%d in %ds",
                retry, MAX_EV_RETRIES, EV_RETRY_WAIT,
            )
            if _ctx.shutdown.wait(EV_RETRY_WAIT):
                return "skip_shutdown"
            continue

        net_credit   = ev_data["net_credit"]
        credit_pct   = net_credit / WING_WIDTH

        if TEST_MODE:
            log.info("TEST MODE — skipping all filters (credit=%.4f  credit_pct=%.3f).",
                     net_credit, credit_pct)
            break

        filter_fail: str | None = None
        if net_credit <= 0:
            filter_fail = f"net_credit={net_credit:.4f} ≤ 0"
        elif MIN_CREDIT_ABS is not None and net_credit < MIN_CREDIT_ABS:
            filter_fail = f"net_credit=${net_credit:.4f} < min ${MIN_CREDIT_ABS:.2f}"
        elif MAX_CREDIT_PCT is not None and credit_pct > MAX_CREDIT_PCT:
            filter_fail = (f"credit_pct={credit_pct:.3f} > {MAX_CREDIT_PCT} "
                           f"(${net_credit:.4f} / ${WING_WIDTH})")
        elif MAX_PROB_MAX_LOSS is not None and ev_data.get("prob_max_loss", 0.0) > MAX_PROB_MAX_LOSS:
            filter_fail = (f"prob_max_loss={ev_data.get('prob_max_loss', 0.0)*100:.1f}% > "
                           f"{MAX_PROB_MAX_LOSS*100:.1f}%")

        if filter_fail is None:
            log.info(
                "Filters passed — credit=%.4f  credit_pct=%.3f — proceeding.",
                net_credit, credit_pct,
            )
            break

        retry += 1
        _log_retry_heartbeat(retry)
        if retry > MAX_EV_RETRIES and not _open_window_is_active():
            log.warning(
                "Filter still failing after %d retries (%s) — abandoning.",
                MAX_EV_RETRIES, filter_fail,
            )
            return "skip_filter_failed"
        log.warning(
            "Filter failed: %s — retry %d/%d in %ds",
            filter_fail, retry, MAX_EV_RETRIES, EV_RETRY_WAIT,
        )
        if _ctx.shutdown.wait(EV_RETRY_WAIT):
            return "skip_shutdown"

    live_positions = get_spy_options_positions()
    if live_positions:
        log.warning(
            "open_butterfly: %d %s option position(s) already open — "
            "skipping order to avoid double-entry.",
            len(live_positions), TICKER,
        )
        with _ctx.state_lock:
            _ctx.trade_executed = True
        return "skip_duplicate_position"

    _sentinel_write(_ctx.acct_name, _ctx.slot)
    with _ctx.state_lock:
        _ctx.trade_executed = True

    body = {
        "order_class":   "mleg",
        "type":          "market",
        "time_in_force": "day",
        "qty":           str(_ctx.qty),
        "legs": [
            {"symbol": _snap_key(symbols["longPut"]),   "side": "buy",  "ratio_qty": "1"},
            {"symbol": _snap_key(symbols["shortPut"]),  "side": "sell", "ratio_qty": "1"},
            {"symbol": _snap_key(symbols["shortCall"]), "side": "sell", "ratio_qty": "1"},
            {"symbol": _snap_key(symbols["longCall"]),  "side": "buy",  "ratio_qty": "1"},
        ],
    }
    log.info("Submitting Iron Butterfly mleg order (qty=%d) …", _ctx.qty)
    order, status_code = _post(f"{_ctx.trade_base}/v2/orders", _trade_headers(), body)

    if order is None:
        log.error("Iron Butterfly order POST failed (HTTP %d)", status_code)
        return f"failed_order_post_http_{status_code}"

    order_id = order.get("id", "")
    log.info("Order submitted: id=%s  status=%s", order_id, order.get("status"))

    order = poll_fill(order_id, "butterfly-open") or order

    fills = get_fills_for_order(order)
    imv = calculate_imv_from_fills(fills)
    with _ctx.state_lock:
        _ctx.imv_state["imv"] = imv
    log.info("IMV (initial market value from %d fill(s)) = $%.2f", len(fills), imv)

    ts = datetime.now(ET).isoformat()
    record = {
        "timestamp":  ts,
        "strategy":   STRATEGY_LABEL,
        "status":     order.get("status", "unknown"),
        "order_id":   order_id,
        "expiry":     expiry.isoformat(),
        "atm":        atm,
        "strikes":    strikes,
        "symbols":    symbols,
        "qty":        _ctx.qty,
        "ev":         ev_data["ev"],
        "net_credit": ev_data["net_credit"],
        "max_profit": ev_data["max_profit"],
        "max_loss":   ev_data["max_loss"],
        "imv":        imv,
    }
    with _ctx.state_lock:
        _ctx.trade_history.append(record)
        _ctx.last_max_profit = ev_data["max_profit"]
        _ctx.trade_executed = True

    _csv_log_open(
        timestamp  = ts,
        expiry     = expiry,
        atm        = atm,
        spy_price  = spy_price,
        strikes    = strikes,
        leg_mids   = {
            "longPut":   _extract_mid(snaps.get(_snap_key(symbols["longPut"]))),
            "shortPut":  _extract_mid(snaps.get(_snap_key(symbols["shortPut"]))),
            "shortCall": _extract_mid(snaps.get(_snap_key(symbols["shortCall"]))),
            "longCall":  _extract_mid(snaps.get(_snap_key(symbols["longCall"]))),
        },
        ev_data      = ev_data,
        order_status = order.get("status", "unknown"),
    )

    log.info(
        "Iron Butterfly SPY OPEN recorded. net_credit=%.2f  "
        "max_profit=$%.2f  max_loss=$%.2f",
        ev_data["net_credit"], ev_data["max_profit"], ev_data["max_loss"],
    )

    return f"opened:{order_id}"

# ── Close butterfly ───────────────────────────────────────────────────────────
def close_butterfly(reason: str = "scheduled"):
    """
    Fetch current SPY options positions → build mleg close orders → submit all.
    """
    log.info("═══ Iron Butterfly SPY: CLOSE (%s) ═══", reason)

    positions = get_spy_options_positions()
    if not positions:
        log.info("No SPY options positions to close.")
        _ctx.day_done.set()
        return

    log.info("Found %d SPY option position(s) to close.", len(positions))

    by_expiry: dict[str, list[dict]] = {}
    qty_by_expiry: dict[str, int]    = {}
    for pos in positions:
        sym    = str(pos.get("symbol", "")).strip()
        expiry = sym[-15:-9]  # last 15 chars = YYMMDDCXXXXXXXX
        qty    = abs(float(pos.get("qty") or 0))
        if expiry not in by_expiry:
            by_expiry[expiry]     = []
            qty_by_expiry[expiry] = 0
        if qty > qty_by_expiry[expiry]:
            qty_by_expiry[expiry] = int(qty)
        close_side = "sell" if float(pos.get("qty") or 0) > 0 else "buy"
        by_expiry[expiry].append({
            "symbol":    sym,
            "side":      close_side,
            "ratio_qty": "1",
        })

    close_orders = []
    for expiry, legs in by_expiry.items():
        order_qty = qty_by_expiry[expiry] or 1
        for chunk_start in range(0, len(legs), 4):
            chunk = legs[chunk_start: chunk_start + 4]
            if len(chunk) >= 2:
                close_orders.append({
                    "expiry": expiry,
                    "qty":    order_qty,
                    "order": {
                        "order_class":   "mleg",
                        "type":          "market",
                        "time_in_force": "day",
                        "qty":           str(order_qty),
                        "legs":          chunk,
                    },
                })
            else:
                log.warning("Skipping expiry %s: only %d leg(s)", expiry, len(chunk))

    if not close_orders:
        log.error("Could not build any close orders.")
        return

    log.info("Sending %d close order(s) …", len(close_orders))
    results = []
    for i, co in enumerate(close_orders, 1):
        log.info("Close order %d/%d: expiry=%s qty=%d legs=%d",
                 i, len(close_orders), co["expiry"], co["qty"], len(co["order"]["legs"]))
        order, sc = _post(f"{_ctx.trade_base}/v2/orders", _trade_headers(), co["order"])
        success = sc in (200, 201)
        oid = order.get("id", "") if order else ""
        if success:
            log.info("Close order %d submitted: id=%s  status=%s", i, oid, order.get("status"))
            poll_fill(oid, f"butterfly-close-{i}")
        else:
            log.error("Close order %d FAILED (HTTP %d)", i, sc)
        results.append({"success": success, "order_id": oid, "expiry": co["expiry"]})

    total_pnl = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    log.info("Iron Butterfly SPY CLOSE complete. unrealized_pnl=$%.2f  reason=%s",
             total_pnl, reason)

    _csv_log_close(close_pnl=total_pnl, close_reason=reason)

    record = {
        "timestamp": datetime.now(ET).isoformat(),
        "type":      "LIQUIDATION",
        "strategy":  STRATEGY_LABEL,
        "reason":    reason,
        "pnl":       total_pnl,
        "results":   results,
    }
    with _ctx.state_lock:
        _ctx.liq_history.append(record)

    _print_cumulative()
    _cleanup_short_legs()
    _ctx.day_done.set()
    log.info("Trading week complete — process will exit once the scheduler loop notices.")

# ── Short-leg cleanup ─────────────────────────────────────────────────────────
def _cleanup_short_legs(
    max_attempts: int = 3,
    attempt_wait: int = 10,
    limit_credit: float = 0.01,
) -> None:
    """
    After the primary mleg close, verify all short SPY option legs are closed.
    For any that remain: try BTC limit at $0.01, escalate to market BTC.
    """
    time.sleep(attempt_wait)
    remaining = get_spy_options_positions()
    short_legs = [p for p in remaining if float(p.get("qty") or 0) < 0]

    if not short_legs:
        long_legs = [p for p in remaining if float(p.get("qty") or 0) > 0]
        if long_legs:
            log.info("Short-leg cleanup: all shorts cleared. %d long leg(s) remain (no liability).", len(long_legs))
        else:
            log.info("Short-leg cleanup: no positions remain — fully flat.")
        return

    log.warning("Short-leg cleanup: %d short leg(s) still open — attempting individual buy-to-close.", len(short_legs))

    for pos in short_legs:
        sym = str(pos.get("symbol", "")).strip()
        qty = abs(int(float(pos.get("qty") or 0)))
        if qty == 0:
            continue
        closed = False
        limit_body = {"symbol": sym, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{limit_credit:.2f}", "time_in_force": "day"}
        log.info("Short-leg cleanup: BTC limit $%.2f  sym=%s  qty=%d", limit_credit, sym, qty)
        order, sc = _post(f"{_ctx.trade_base}/v2/orders", _trade_headers(), limit_body)
        if order and sc in (200, 201):
            oid = order.get("id", "")
            filled = poll_fill(oid, f"cleanup-limit-{sym}")
            if filled and filled.get("status") == "filled":
                log.info("Short-leg cleanup: limit BTC filled  sym=%s", sym)
                closed = True
            else:
                _delete(f"{_ctx.trade_base}/v2/orders/{oid}", _trade_headers())
                time.sleep(2)
        if not closed:
            market_body = {"symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day"}
            log.warning("Short-leg cleanup: escalating to market BTC  sym=%s  qty=%d", sym, qty)
            order2, sc2 = _post(f"{_ctx.trade_base}/v2/orders", _trade_headers(), market_body)
            if order2 and sc2 in (200, 201):
                oid2 = order2.get("id", "")
                filled2 = poll_fill(oid2, f"cleanup-market-{sym}")
                if filled2 and filled2.get("status") == "filled":
                    log.info("Short-leg cleanup: market BTC filled  sym=%s", sym)
                else:
                    log.error("Short-leg cleanup: market BTC did NOT fill  sym=%s  status=%s — manual intervention required.", sym, filled2.get("status") if filled2 else "unknown")
            else:
                log.error("Short-leg cleanup: market BTC POST failed (HTTP %d)  sym=%s — manual intervention required.", sc2, sym)

# ── Position monitor (P&L-based auto-exit) ────────────────────────────────────
def monitor_positions():
    positions = get_spy_options_positions()
    if not positions:
        return

    total_pnl = sum(float(p.get("unrealized_pl") or 0) for p in positions)

    with _ctx.state_lock:
        max_profit = _ctx.trade_history[-1]["max_profit"] if _ctx.trade_history else _ctx.last_max_profit

    if not max_profit:
        recovered = _recover_last_max_profit_from_csv()
        if recovered:
            max_profit = recovered
            with _ctx.state_lock:
                _ctx.last_max_profit = recovered
            log.info("Position monitor: recovered max_profit=%.2f from CSV log.", recovered)
        else:
            log.warning("Position monitor: %d %s opts  pnl=%.2f  (no max_profit on record - skipping pnl_pct/auto-exit check)",
                        len(positions), TICKER, total_pnl)
            return

    pnl_pct = total_pnl / max_profit

    log.info(
        "Position monitor: %d SPY opts  pnl=$%.2f  pnl_pct=%.1f%%",
        len(positions), total_pnl, pnl_pct * 100,
    )

    if pnl_pct >= PROFIT_TARGET_PCT:
        log.info("MAX_PROFIT_90%% hit (%.1f%%) — auto-closing.", pnl_pct * 100)
        close_butterfly(reason="MAX_PROFIT_90%")
    elif pnl_pct <= STOP_LOSS_PCT:
        log.info("STOP_LOSS_80%% hit (%.1f%%) — auto-closing.", pnl_pct * 100)
        close_butterfly(reason="STOP_LOSS_80%")

def _print_cumulative():
    with _ctx.state_lock:
        liq = list(_ctx.liq_history)
    if not liq:
        return
    total   = sum(r.get("pnl", 0) for r in liq)
    wins    = sum(1 for r in liq if r.get("pnl", 0) > 0)
    n       = len(liq)
    win_pct = wins / n * 100 if n else 0
    log.info(
        "Cumulative: %d liquidations  total_pnl=$%.2f  win_rate=%.1f%%",
        n, total, win_pct,
    )

def _run_open_attempt() -> None:
    with _ctx.state_lock:
        _ctx.run_state["open_attempted"] = True
        _ctx.run_state["last_open_outcome"] = "in_progress"

    wait_for_dynamic_entry()
    outcome = open_butterfly()

    with _ctx.state_lock:
        _ctx.run_state["last_open_outcome"] = outcome

    log.info("Open attempt finished: %s", outcome)

# ── Weekly state reset (each Monday) ─────────────────────────────────────────
def _reset_weekly():
    with _ctx.state_lock:
        _ctx.trade_executed = False
        _ctx.run_state["open_attempted"] = False
        _ctx.run_state["last_open_outcome"] = None
    log.info("Weekly state reset (trade_executed=False)")

# ── Startup diagnostics ───────────────────────────────────────────────────────
def startup_diagnostics():
    """
    Run immediately on launch. Verifies:
      1. SPY live price poll (direct Alpaca latest-trade)
      2. OCC symbol generation for this week's Friday expiry
      3. Options snapshot API reachability
      4. Leg prices + greeks
      5. EV / net_credit calculation with live data
      6. Current open SPY options positions
    """
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("STARTUP DIAGNOSTICS  (Iron Butterfly SPY — Weekly Dynamic Live)")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    failures: list[str] = []

    log.info("[0/6] Checking Alpaca paper trading auth …")
    acct = _get(f"{_ctx.trade_base}/v2/account", _trade_headers())
    if acct is None:
        log.error("  ✗ FAIL — auth check failed — check the alpaca CLI profile (`alpaca profile list`)")
        failures.append("Auth check failed — invalid or missing alpaca CLI profile")
    else:
        log.info("  ✓ PASS — account_number=%s  status=%s  buying_power=$%.2f",
                 acct.get("account_number", "?"),
                 acct.get("status", "?"),
                 float(acct.get("buying_power") or 0))

    log.info("[1/6] Polling SPY live price from Alpaca …")
    spy_price = get_spy_price()
    if spy_price is None:
        log.error("  ✗ FAIL — could not fetch SPY latest-trade price from Alpaca")
        failures.append("SPY live price fetch")
        spy_price = 600.0
        log.warning("  Using dummy SPY=%.2f to continue diagnostics", spy_price)
    else:
        log.info("  ✓ PASS — SPY = $%.2f", spy_price)

    log.info("[2/6] Building OCC symbols …")
    expiry = next_friday()
    atm    = round(spy_price)

    symbols = {
        "longPut":   occ_symbol(TICKER, expiry, "P", atm - WING_WIDTH),
        "shortPut":  occ_symbol(TICKER, expiry, "P", atm),
        "shortCall": occ_symbol(TICKER, expiry, "C", atm),
        "longCall":  occ_symbol(TICKER, expiry, "C", atm + WING_WIDTH),
    }

    log.info("  ATM=%d  expiry=%s  (this week's Friday)", atm, expiry)
    log.info("  Strikes:  longPut=%-6d  shortPut=%-6d  shortCall=%-6d  longCall=%-6d",
             atm - WING_WIDTH, atm, atm, atm + WING_WIDTH)
    for leg, sym in symbols.items():
        ok   = len(sym) == 21
        mark = "✓" if ok else "✗"
        log.info("  %s %-12s = '%s'  (len=%d)", mark, leg, sym, len(sym))
        if not ok:
            failures.append(f"OCC symbol length ({leg}={len(sym)}, expected 21)")
    if not any("OCC" in f for f in failures):
        log.info("  ✓ PASS — all OCC symbols 21 chars")

    log.info("[3/6] Fetching options snapshots …")
    sym_list = list(symbols.values())
    log.info("  Requesting (unpadded): %s", ", ".join(_snap_key(s) for s in sym_list))
    snaps = get_options_snapshots(sym_list)

    log.info("  Snapshot keys returned (%d):", len(snaps))
    if snaps:
        for k in snaps:
            log.info("    • '%s'", k)
    else:
        log.warning("  (none — snapshot dict is empty; market may be closed or symbols not yet active)")

    found = {leg: _snap_key(symbols[leg]) in snaps for leg in symbols}
    for leg, hit in found.items():
        mark = "✓" if hit else "✗ MISSING"
        log.info("  %s %-12s → %s", mark, leg, symbols[leg])

    missing = [leg for leg, hit in found.items() if not hit]
    if missing:
        log.warning(
            "  ✗ WARN — snapshot missing %d leg(s): %s  "
            "(indicative feed is sparse; open_butterfly() retries up to %d× at launch)",
            len(missing), missing, MAX_EV_RETRIES,
        )
    else:
        log.info("  ✓ PASS — all 4 legs present in snapshot")

    log.info("[4/6] Extracting leg prices and greeks …")
    for leg, sym in symbols.items():
        snap = snaps.get(_snap_key(sym))
        if snap is None:
            log.warning("  ✗ %-12s: no snapshot data", leg)
            continue
        price = _extract_mid(snap, leg)
        q   = snap.get("latestQuote") or snap.get("latest_quote") or {}
        g   = snap.get("greeks") or {}
        ap  = float(q.get("ap") or q.get("ask_price") or 0)
        bp  = float(q.get("bp") or q.get("bid_price") or 0)
        iv  = float(snap.get("impliedVolatility") or snap.get("implied_volatility") or 0)
        dlt = float(g.get("delta") or 0)
        gma = float(g.get("gamma") or 0)
        tha = float(g.get("theta") or 0)
        vga = float(g.get("vega")  or 0)
        price_str = f"${price:.4f}" if price is not None else "MISSING"
        log.info(
            "  %-12s  mid=%-10s  bid=%.4f  ask=%.4f  iv=%.1f%%"
            "  Δ=%.4f  Γ=%.5f  Θ=%.4f  V=%.4f",
            leg, price_str, bp, ap, iv * 100, dlt, gma, tha, vga,
        )

    log.info("[5/6] Running EV calculation …")
    ev_data = calculate_ev(snaps, symbols, _ctx.qty, WING_WIDTH)
    if ev_data is None:
        log.warning(
            "  ✗ WARN — EV calc skipped (snapshot data unavailable). "
            "open_butterfly() retries up to %d× with %ds waits.",
            MAX_EV_RETRIES, EV_RETRY_WAIT,
        )
    else:
        ev_sign  = "+" if ev_data["ev"] >= 0 else ""
        verdict  = "✓ PASS — net_credit > 0" if ev_data["net_credit"] > 0 else "✗ net_credit ≤ 0 — will retry"
        log.info("  net_credit    = $%.4f per contract", ev_data["net_credit"])
        log.info("  max_profit    = $%.2f  (%d contracts × $%.4f × 100)",
                 ev_data["max_profit"], _ctx.qty, ev_data["net_credit"])
        log.info("  max_loss      = $%.2f", ev_data["max_loss"])
        log.info("  prob_max_loss = %.2f%%", ev_data["prob_max_loss"] * 100)
        log.info("  EV            = %s$%.2f", ev_sign, ev_data["ev"])
        log.info("  %s", verdict)

    log.info("[6/6] Checking existing SPY options positions …")
    positions = get_spy_options_positions()
    if not positions:
        log.info("  (none — no open SPY options positions)")
    else:
        log.info("  Found %d open position(s):", len(positions))
        for pos in positions:
            sym  = pos.get("symbol", "?")
            qty  = pos.get("qty", "?")
            pnl  = float(pos.get("unrealized_pl") or 0)
            cost = float(pos.get("cost_basis") or 0)
            log.info("    %-24s  qty=%-6s  cost=$%.2f  unrealized_pnl=$%.2f",
                     sym, qty, cost, pnl)

    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if failures:
        log.error("DIAGNOSTICS COMPLETE — %d hard failure(s):", len(failures))
        for i, f in enumerate(failures, 1):
            log.error("  [%d] %s", i, f)
    else:
        log.info(
            "DIAGNOSTICS COMPLETE — configuration OK ✓  "
            "(snapshot/EV warnings above are non-fatal — "
            "open_butterfly() retries up to %d× during the entry window)",
            MAX_EV_RETRIES,
        )
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

# ── Thread-context propagation ───────────────────────────────────────────────
def _make_thread_target(fn, **kwargs):
    """
    Returns a callable that, when run in a new thread, first initialises _ctx
    with credentials and shared per-account objects copied from the *spawning*
    thread's _ctx, then calls fn(**kwargs).
    """
    snap = dict(
        cli_profile    = _ctx.cli_profile,
        acct_name      = _ctx.acct_name,
        slot           = _ctx.slot,
        qty            = _ctx.qty,
        trade_base     = _ctx.trade_base,
        state_lock     = _ctx.state_lock,
        trade_history  = _ctx.trade_history,
        liq_history    = _ctx.liq_history,
        run_state      = _ctx.run_state,
        last_max_profit = _ctx.last_max_profit,
        imv_state      = _ctx.imv_state,
        shutdown       = _ctx.shutdown,
        day_done       = _ctx.day_done,
    )
    def _wrapper():
        for k, v in snap.items():
            setattr(_ctx, k, v)
        _ctx.trade_executed = _sentinel_exists(_ctx.acct_name, _ctx.slot)
        fn(**kwargs)
    return _wrapper

# ── Scheduler ─────────────────────────────────────────────────────────────────
def _seconds_until(hour: int, minute: int, tz=ET) -> float:
    now    = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def run_scheduler():
    log.info("Iron Butterfly SPY (Weekly Dynamic Live) scheduler started.")
    log.info("Strategy: SPY butterfly  qty=%d  wingWidth=%d", _ctx.qty, WING_WIDTH)
    log.info("ENTRY search window: Monday only %02d:%02d–%02d:%02d ET (dynamic, quietest-minute)",
             *ENTRY_SEARCH_START, *ENTRY_SEARCH_END)
    log.info("EXIT: dynamic wing-breach (BREACH_FRACTION=%.2f) Mon–Thu, Thursday fallback %02d:%02d ET",
             BREACH_FRACTION, EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN)

    last_open_date:    date | None = None
    last_close_date:   date | None = None
    last_monitor_ts:   float       = 0.0
    last_breach_ts:    float       = 0.0
    last_mv_log_ts:    float       = 0.0
    last_reset_week:   int | None = None
    last_heartbeat_ts: float       = 0.0

    while not _ctx.shutdown.is_set() and not _ctx.day_done.is_set():
        now       = now_et()
        today     = now.date()
        weekday   = now.weekday()
        hhmm_mins = now.hour * 60 + now.minute
        is_active_day = FORCE_TRADE_NOW or (0 <= weekday <= CLOSE_WEEKDAY)   # Mon–Thu only

        open_start_mins  = ENTRY_SEARCH_START[1] + ENTRY_SEARCH_START[0] * 60
        open_cutoff_mins = ENTRY_SEARCH_END[0] * 60 + ENTRY_SEARCH_END[1]
        close_mins       = EXIT_DEFAULT_HOUR * 60 + EXIT_DEFAULT_MIN

        iso_week = today.isocalendar()[1]
        if weekday == OPEN_WEEKDAY and iso_week != last_reset_week:
            _reset_weekly()
            last_reset_week = iso_week

        if is_active_day:
            is_open_day  = FORCE_TRADE_NOW or weekday == OPEN_WEEKDAY
            is_close_day = FORCE_TRADE_NOW or weekday == CLOSE_WEEKDAY

            in_open_window = is_open_day and (
                FORCE_TRADE_NOW or (open_start_mins <= hhmm_mins < open_cutoff_mins)
            )
            with _ctx.state_lock:
                already_traded = _ctx.trade_executed
            if in_open_window and not already_traded and last_open_date != today:
                last_open_date = today
                threading.Thread(target=_make_thread_target(_run_open_attempt), daemon=True).start()

            if is_close_day and hhmm_mins >= close_mins and last_close_date != today:
                last_close_date = today
                threading.Thread(target=_make_thread_target(close_butterfly, reason="scheduled"), daemon=True).start()

            if time.time() - last_breach_ts >= DYNAMIC_EXIT_POLL_SEC:
                last_breach_ts = time.time()
                threading.Thread(target=_make_thread_target(monitor_underlying_breach), daemon=True).start()

            if time.time() - last_monitor_ts >= MONITOR_INTERVAL:
                last_monitor_ts = time.time()
                threading.Thread(target=_make_thread_target(monitor_positions), daemon=True).start()

            if time.time() - last_mv_log_ts >= MARKET_VALUE_LOG_INTERVAL:
                last_mv_log_ts = time.time()
                threading.Thread(target=_make_thread_target(log_position_market_values), daemon=True).start()

        if time.time() - last_heartbeat_ts >= 300:
            last_heartbeat_ts = time.time()
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            day_name  = day_names[weekday]
            if not is_active_day:
                log.info("Heartbeat [%s %s ET] — Fri/weekend, no trading (Mon-open/Thu-close week).",
                         day_name, now.strftime("%H:%M"))
                _ctx.day_done.set()
                log.info("Not a Mon–Thu trading day — exiting.")
            else:
                close_secs  = _seconds_until(EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN)
                close_fired = last_close_date == today

                def _fmt(secs: float, fired: bool, label: str) -> str:
                    if fired:
                        return f"{label} DONE ✓"
                    h, rem = divmod(int(secs), 3600)
                    m, s   = divmod(rem, 60)
                    return f"{label} in {h:02d}h{m:02d}m{s:02d}s"

                with _ctx.state_lock:
                    trade_done = _ctx.trade_executed
                    open_attempted = bool(_ctx.run_state.get("open_attempted"))
                    open_outcome = _ctx.run_state.get("last_open_outcome")

                is_open_day = FORCE_TRADE_NOW or weekday == OPEN_WEEKDAY
                in_open_window = is_open_day and (
                    FORCE_TRADE_NOW or (open_start_mins <= hhmm_mins < open_cutoff_mins)
                )
                if isinstance(open_outcome, str) and open_outcome.startswith("opened:"):
                    open_status = f"OPEN SUBMITTED ({open_outcome.split(':', 1)[1]})"
                elif open_outcome and open_outcome != "in_progress":
                    open_status = f"OPEN ATTEMPTED -> {open_outcome}"
                elif open_outcome == "in_progress":
                    open_status = "OPEN ATTEMPT IN PROGRESS"
                elif in_open_window and not trade_done:
                    open_status = f"ENTRY WINDOW ACTIVE (closes {ENTRY_SEARCH_END[0]:02d}:{ENTRY_SEARCH_END[1]:02d} ET)"
                elif is_open_day and hhmm_mins < open_start_mins:
                    secs_to_open = (open_start_mins - hhmm_mins) * 60
                    open_status  = _fmt(secs_to_open, False, "ENTRY")
                elif trade_done:
                    open_status = "OPEN ALREADY SATISFIED"
                elif open_attempted:
                    open_status = "OPEN ATTEMPTED"
                elif not is_open_day:
                    open_status = "NOT MONDAY — WAITING FOR NEXT WEEK"
                else:
                    open_status = "ENTRY WINDOW CLOSED"

                log.info("Heartbeat [%s %s ET] — trade_executed=%s  open_attempted=%s  |  %s  |  %s",
                         day_name, now.strftime("%H:%M"), trade_done, open_attempted,
                         open_status, _fmt(close_secs, close_fired, "CLOSE"))

        _ctx.shutdown.wait(15)

    log.info("Iron Butterfly SPY (Weekly Dynamic Live) scheduler stopped.")

# ── Per-account thread entry ─────────────────────────────────────────────────
def run_account(acct: dict) -> None:
    """Initialise per-thread state in _ctx and run the scheduler for one account."""
    _ctx.cli_profile   = acct["cli_profile"]
    _ctx.acct_name     = acct["acctname"]
    _ctx.slot          = acct["slot"]
    _ctx.qty           = acct["qty"]
    _ctx.trade_base    = acct["trade_base"]
    _ctx.state_lock    = threading.Lock()
    _ctx.trade_executed = False
    _ctx.trade_history  = []
    _ctx.liq_history    = []
    _ctx.run_state      = {"open_attempted": False, "last_open_outcome": None}
    _ctx.last_max_profit = None
    _ctx.imv_state       = {"imv": None}
    _ctx.shutdown       = threading.Event()
    _ctx.day_done       = threading.Event()
    _all_shutdowns.append(_ctx.shutdown)
    log.info("Account thread started — acctname=%s  slot=%s  profile=%s",
             acct["acctname"], acct["slot"], acct["cli_profile"])

    if _sentinel_exists(acct["acctname"], acct["slot"]):
        _ctx.trade_executed = True
        log.warning(
            "Startup: sentinel file found for %s/%s — "
            "trade_executed set to True (will not re-enter this week).",
            acct["acctname"], acct["slot"],
        )
    else:
        existing = get_spy_options_positions()
        if existing:
            _ctx.trade_executed = True
            _sentinel_write(acct["acctname"], acct["slot"])
            log.warning(
                "Startup: found %d existing %s option position(s) — "
                "trade_executed set to True and sentinel written.",
                len(existing), TICKER,
            )

    try:
        run_scheduler()
    finally:
        _print_cumulative()

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Weekly Iron Butterfly SPY trader — dynamic live, single-account"
    )
    parser.add_argument("--dry-run",    action="store_true",
                        help="Show accounts and config that would run — no orders placed")
    parser.add_argument("--test-csv",   action="store_true",
                        help="Write a dummy CSV row and exit")
    parser.add_argument("--test-trade", action="store_true",
                        help="Run startup diagnostics + one open attempt, then exit")
    parser.add_argument("--test",       action="store_true",
                        help="Execute a trade immediately, bypassing all filters")
    parser.add_argument("--close",      action="store_true",
                        help="Liquidate all open SPY options positions and exit")
    parser.add_argument("--force",      action="store_true",
                        help="Bypass the Monday-only + dynamic entry-window "
                             "guards for this run (one-off live test) and run the full "
                             "scheduler (open now, per-minute logging, close at "
                             f"{EXIT_DEFAULT_HOUR:02d}:{EXIT_DEFAULT_MIN:02d} ET fallback)")
    args = parser.parse_args()

    global TEST_MODE, FILTER_CHECK_ONLY, FORCE_TRADE_NOW
    if args.test:
        TEST_MODE = True
    if args.force:
        TEST_MODE = True
        FORCE_TRADE_NOW = True
        log.warning("--force: bypassing Monday-only + entry-window guards for this run.")

    account = load_credentials()

    _ctx.cli_profile   = account["cli_profile"]
    _ctx.acct_name     = account["acctname"]
    _ctx.slot          = account["slot"]
    _ctx.qty           = account["qty"]
    _ctx.trade_base    = account["trade_base"]
    _ctx.state_lock    = threading.Lock()
    _ctx.trade_executed = False
    _ctx.trade_history  = []
    _ctx.liq_history    = []
    _ctx.run_state      = {"open_attempted": False, "last_open_outcome": None}
    _ctx.last_max_profit = None
    _ctx.imv_state       = {"imv": None}
    _ctx.shutdown       = threading.Event()
    _ctx.day_done       = threading.Event()
    _all_shutdowns.append(_ctx.shutdown)

    log.info("Starting weekly_trading_spy.py …")
    log.info("Live price feed: direct Alpaca latest-trade polling")

    if args.dry_run:
        log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        log.info("DRY RUN — no orders will be placed")
        log.info("Strategy : %s", STRATEGY_LABEL)
        log.info("Ticker   : %s  wing_width=%d", TICKER, WING_WIDTH)
        log.info("Entry    : dynamic %02d:%02d-%02d:%02d ET (Monday only)",
                 *ENTRY_SEARCH_START, *ENTRY_SEARCH_END)
        log.info("Exit     : dynamic wing-breach Mon-Thu, Thursday fallback %02d:%02d ET",
                 EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN)
        sentinel_today = _sentinel_exists(account["acctname"], account["slot"])
        log.info(
            "Account  : acctname=%-20s  slot=%-6s  qty=%d option(s)  env=%s  sentinel=%s",
            account["acctname"],
            account["slot"],
            account["qty"],
            "LIVE" if account["trade_base"] == LIVE_BASE else "PAPER",
            "EXISTS (will skip open this week)" if sentinel_today else "none",
        )
        FILTER_CHECK_ONLY = True
        filter_result = open_butterfly()
        FILTER_CHECK_ONLY = False
        log.info("FILTER_CHECK: %s", filter_result)
        startup_diagnostics()
        log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return

    if args.test_csv:
        log.info("--test-csv: writing dummy CSV row …")
        _csv_log_open(
            timestamp  = datetime.now(ET).isoformat(),
            expiry     = next_friday(),
            atm        = 0,
            spy_price  = 0.0,
            strikes    = {"longPut": 0, "shortPut": 0, "shortCall": 0, "longCall": 0},
            leg_mids   = {"longPut": None, "shortPut": None, "shortCall": None, "longCall": None},
            ev_data    = {"net_credit": 0, "max_profit": 0, "max_loss": 0,
                          "prob_max_loss": 0, "ev": 0},
            order_status = "TEST",
        )
        _csv_log_close(close_pnl=0.0, close_reason="TEST")
        log.info("--test-csv: done. Check %s", CSV_LOG_PATH)
        return

    startup_diagnostics()

    if args.test_trade:
        log.info("--test-trade: running one open attempt …")
        open_butterfly()
        log.info("--test-trade: done.")
        return

    if args.test:
        log.info("--test: running one open attempt (all filters bypassed) …")
        open_butterfly()
        log.info("--test: done.")
        return

    if args.close:
        log.info("--close: liquidating all open SPY options positions …")
        close_butterfly(reason="manual-close")
        log.info("--close: done.")
        return

    t = threading.Thread(target=run_account, args=(account,), daemon=True)
    t.start()
    try:
        t.join()
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        log.info("Exiting.")

if __name__ == "__main__":
    main()
