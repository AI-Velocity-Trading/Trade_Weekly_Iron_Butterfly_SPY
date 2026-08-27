# SPY Weekly Iron Butterfly

A dynamic, timing-optimized weekly Iron Butterfly options strategy on SPY —
backtesting engine plus a live-trading counterpart.

## Strategy

A 4-leg Iron Butterfly, opened Monday and closed by Thursday:

```
longPut   @ ATM − WING_WIDTH   (buy)
shortPut  @ ATM                (sell)
shortCall @ ATM                (sell)
longCall  @ ATM + WING_WIDTH   (buy)
```

Instead of trading on a fixed clock schedule, entry and exit timing are
**dynamic**, driven by SPY's own realized 1-minute price movement:

- **Entry** (Monday only, ~9:35–10:00 ET): picks the quietest minute — the one
  with the smallest trailing high/low range — and sets the ATM strike from
  that minute's open price.
- **Exit** (Monday–Thursday): closes immediately if price ever moves
  `BREACH_FRACTION × WING_WIDTH` away from the ATM strike ("underlying
  breach"), or at a profit target (+90% of max profit) / stop loss (−80%),
  otherwise falls back to a scheduled close by Thursday 15:00 ET.

Trade filters can skip a week entirely (e.g. Monday gap too large, prior-week
range too wide, credit too rich) — current defaults were tuned via a 5-year
parameter sweep (see [SESSION_NOTES.md](SESSION_NOTES.md)).

## Files

| File | Purpose |
|---|---|
| [weekly_iron_butterfly_spy_backtest_dynamic.py](weekly_iron_butterfly_spy_backtest_dynamic.py) | Backtest engine. Replays historical SPY price bars + Alpaca OPRA options data to simulate the strategy and report P&L, win rate, drawdown, Sharpe, etc. Includes an `--optimize` mode that grid-searches entry window, exit cutoff, breach fraction, and filter thresholds. |
| [weekly_trading_spy.py](weekly_trading_spy.py) | Live-trading counterpart. Runs the same dynamic entry/exit logic against a live 1-minute price feed and places real orders via Alpaca on a single hardcoded account. |
| [get_5yrs_spy_bars.py](get_5yrs_spy_bars.py) | One-off/refresh utility that fetches 5 years of SPY 1-minute SIP bars (4:00 AM–8:00 PM ET) from Alpaca and writes them to `underlying-tickers/SPY.csv`, the format the backtest expects. |
| [underlying-tickers/SPY.csv](underlying-tickers/SPY.csv) | Historical 1-min SPY bars (`date_et,time_et,open,high,low,close`) used by the backtest. |
| [SESSION_NOTES.md](SESSION_NOTES.md) | Running log of setup steps, bug fixes, optimizer runs, and tuned parameter history. |

## Setup

1. Install dependencies:
   ```
   pip install requests python-dotenv
   ```
   (`weekly_trading_spy.py` additionally needs `supabase`.)
2. Create a `.env` file in the project root with Alpaca API credentials:
   ```
   apiDataKey=...
   apiDataSecret=...
   apiKeyAIV011P=...      # optional — used only for the market calendar endpoint
   apiSecretAIV011P=...
   ```
3. Populate `underlying-tickers/SPY.csv` by running:
   ```
   python3 get_5yrs_spy_bars.py
   ```

## Running the backtest

```bash
python3 weekly_iron_butterfly_spy_backtest_dynamic.py
python3 weekly_iron_butterfly_spy_backtest_dynamic.py --csv results.csv
python3 weekly_iron_butterfly_spy_backtest_dynamic.py --optimize
```

Override the date range with environment variables:

```bash
BT_START_OVERRIDE=2021-08-26 BT_END_OVERRIDE=2026-08-25 python3 weekly_iron_butterfly_spy_backtest_dynamic.py
```

Options pricing (Alpaca OPRA daily/1-min bars) is fetched live from Alpaca —
only the underlying SPY price history needs to be local.

## Current tuned parameters

(See [SESSION_NOTES.md](SESSION_NOTES.md) for the full 5-year optimizer sweep results.)

- Entry search window: 9:35–10:00 ET, 5-minute settle window
- Exit cutoff: Thursday 15:00 ET
- Wing width: $10, breach fraction: 1.0
- Gap filter: skip if Monday open gaps > 0.8% from prior close
- Credit and prior-range filters: disabled

Backtest result over 2021-08-26 → 2026-08-25: 97 trades, 59.8% win rate,
+$108,840 total P&L, Sharpe 1.06, max drawdown $36,280.

## Live trading

`weekly_trading_spy.py` mirrors the backtest's dynamic timing but reads live
prices from a local CSV feed (written by a separate websocket subscriber) and
trades a single hardcoded Alpaca account. It loads Alpaca OAuth/data
credentials from Supabase at startup (`SUPABASE_URL` / `SUPABASE_KEY` in
`.env`). See the module docstring in
[weekly_trading_spy.py](weekly_trading_spy.py) for full configuration details.
