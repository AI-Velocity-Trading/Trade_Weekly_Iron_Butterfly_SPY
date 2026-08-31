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
parameter sweep.

## Files

| File | Purpose |
|---|---|
| [weekly_iron_butterfly_spy_backtest_dynamic.py](weekly_iron_butterfly_spy_backtest_dynamic.py) | Backtest engine. Replays historical SPY price bars + Alpaca OPRA options data to simulate the strategy and report P&L, win rate, drawdown, Sharpe, etc. Includes an `--optimize` mode that grid-searches entry window, exit cutoff, breach fraction, and filter thresholds. |
| [weekly_trading_spy.py](weekly_trading_spy.py) | Live-trading counterpart. Runs the same dynamic entry/exit logic against Alpaca's live price feed and places real orders via Alpaca on a single account. |
| [get_5yrs_spy_bars.py](get_5yrs_spy_bars.py) | One-off/refresh utility that fetches 5 years of SPY 1-minute SIP bars (4:00 AM–8:00 PM ET) from Alpaca and writes them to `underlying-tickers/SPY.csv`, the format the backtest expects. Requires a SIP market-data subscription (see [Setup](#setup)). |
| [underlying-tickers/SPY.csv](underlying-tickers/SPY.csv) | Historical 1-min SPY bars (`date_et,time_et,open,high,low,close`) used by the backtest. Already included in the repo, so the backtest can be run without a SIP data plan. |
| [launch_weekly_trading.sh](launch_weekly_trading.sh) | Launcher used by an unattended/scheduled `launchd` job to start `weekly_trading_spy.py` detached (`nohup` + `disown`), logging to `logs/`. See [Unattended/scheduled launch](#unattendedscheduled-launch). |

## Setup

1. Install the [Alpaca CLI](https://github.com/alpacahq/cli) — both scripts shell
   out to it (via subprocess) for all trading and market-data calls instead of
   making direct HTTPS requests:
   ```
   brew install alpacahq/tap/cli
   ```
2. No `.env` file or manual key entry is required up front. On first run,
   if no `paper` alpaca CLI profile exists yet, each script prompts
   interactively for your Alpaca API key/secret and registers them with
   `alpaca profile login --api-key` (stored under `~/.config/alpaca/profiles/`
   with restricted permissions) — future runs won't ask again. The same
   profile is used for both trading and market-data requests.

   To trade a **different** Alpaca account without overwriting the default
   `paper`/`live` profile, set `ALPACA_CLI_PROFILE=<name>` before running
   `weekly_trading_spy.py` — it overrides which alpaca CLI profile is used
   (registering it on first run, same as above, under that name instead).
3. `underlying-tickers/SPY.csv` is already included in the repo with 5 years
   of SPY 1-min bars, so the backtest can be run as-is — **no SIP data plan
   is required** just to run the backtest.

   Only regenerate/refresh this file by running `get_5yrs_spy_bars.py` if you
   need newer data. That script calls Alpaca's stock bars endpoint with
   `feed=sip`, which **requires a SIP market-data subscription** (Alpaca's
   paid Algo Trader Plus plan) — the free/basic plan only has access to the
   IEX feed and will fail or return incomplete data.
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
only the underlying SPY price history needs to be local. Fetching OPRA options
data requires Alpaca's paid **Algo Trader Plus** plan; the free/basic plan
does not include OPRA access and will fail or return incomplete data.

## Current tuned parameters

- Entry search window: 9:35–10:00 ET, 5-minute settle window
- Exit cutoff: Thursday 15:00 ET
- Wing width: $10, breach fraction: 1.0
- Gap filter: skip if Monday open gaps > 0.8% from prior close
- Credit and prior-range filters: disabled

Backtest result over 2021-08-26 → 2026-08-25: 97 trades, 59.8% win rate,
+$108,840 total P&L, Sharpe 1.06, max drawdown $36,280.

## Live trading

`weekly_trading_spy.py` mirrors the backtest's dynamic timing but polls
prices directly from Alpaca via the CLI's `alpaca data latest-trade`
passthrough (no websocket subscriber or local CSV feed) and trades a single
account. It uses its own `paper`/`live` alpaca CLI profile (selected by the
`ALLOW_LIVE_TRADING` constant), prompting for and registering the API
key/secret on first run if that profile doesn't exist yet, and asks
interactively at startup for the number of option contracts to trade per leg.
Its own entry window (9:35–10:30 ET) and exit cutoff (15:40 ET) constants are
independent of the backtest's tuned defaults above. See the module docstring in
[weekly_trading_spy.py](weekly_trading_spy.py) for full configuration
details.

### Unattended/scheduled launch

Since `weekly_trading_spy.py` runs as one long-lived process from Monday's
open through Thursday's close, it can be started automatically via a macOS
`launchd` LaunchAgent instead of running it by hand in a terminal:

- [launch_weekly_trading.sh](launch_weekly_trading.sh) sets `ALPACA_CLI_PROFILE`,
  starts the trader detached with `nohup ... & disown` (so it survives the
  terminal/session closing) logging to `logs/`, then removes its own
  LaunchAgent so a one-off scheduled run doesn't recur.
- A `StartCalendarInterval` LaunchAgent plist (Month/Day/Hour/Minute, no
  `Weekday`/`Year`) fires the launcher once on a specific date — e.g. Monday
  market open — then self-unloads via the script above.
- Because the trader keeps polling prices for days, the machine running it
  must stay powered on and awake (not sleeping) for the whole Monday–Thursday
  window; consider `caffeinate -s` or adjusting Energy Saver settings.
- The unattended "number of contracts per leg" prompt receives no input in
  this mode and silently falls back to `QTY = 1`.
