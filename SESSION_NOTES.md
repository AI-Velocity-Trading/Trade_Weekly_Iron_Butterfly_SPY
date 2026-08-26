# Session Notes — SPY Weekly Iron Butterfly Backtest Setup

Date: 2026-08-26

## 1. Prompt: "what files do I need to run this script?"

Investigated `weekly_iron_butterfly_spy_backtest_dynamic.py` to determine its dependencies.

Findings:
- Requires a `.env` file with Alpaca API keys: `apiDataKey`, `apiDataSecret` (market data), and
  `apiKeyAIV011P` / `apiSecretAIV011P` (trading/paper account, used only for the market calendar
  endpoint — optional, script falls back to a Mon–Fri holiday calendar if missing).
- Requires `underlying-tickers/SPY.csv` (1-min SIP bars for SPY), loaded by
  `load_underlying_minute_bars()`. This file did not exist in the workspace yet.
- Requires Python packages `requests` and `python-dotenv`.
- Options pricing data (Alpaca OPRA daily + 1-min bars) is fetched live via the API — no local
  file needed.
- Noted a bug: the script originally pointed `_ENV_FILE` at `../.env` (one directory above the
  script), but the actual `.env` with the needed keys lives alongside the script itself.

## 2. Prompt: "create a get_5yrs_spy_bars.py that get one min bars for spy from 4 AM to 8 pm NY
time for the past 5 years using sip data."

Created [get_5yrs_spy_bars.py](get_5yrs_spy_bars.py):
- Loads Alpaca data-API keys from the local `.env`.
- Calls Alpaca's `/v2/stocks/bars` endpoint for `SPY`, `timeframe=1Min`, `feed=sip`, paginating via
  `next_page_token`, covering the last 5 years (`today - 5 years` → `yesterday`).
- Converts each bar's UTC timestamp to `America/New_York`, keeps only bars within the 4:00 AM–8:00
  PM ET session window.
- Writes results to `underlying-tickers/SPY.csv` with columns `date_et,time_et,open,high,low,close`
  — the exact format expected by `weekly_iron_butterfly_spy_backtest_dynamic.py`.

## 3. Prompt: "run this"

Ran `get_5yrs_spy_bars.py`.
- First attempt failed: `Missing apiDataKey / apiDataSecret` — the script's `_ENV_FILE` pointed to
  `../.env` (parent directory), which doesn't contain the Alpaca keys.
- Confirmed the local `.env` (in `trade-iron-butterfly-weekly/`) has `apiDataKey` / `apiDataSecret`,
  while the parent directory's `.env` does not.
- Fixed `_ENV_FILE` in `get_5yrs_spy_bars.py` to point at the local `.env`.
- Re-ran successfully: fetched 1,044,332 raw 1-min bars, wrote 1,044,306 rows (after the 4 AM–8 PM
  ET filter) to `underlying-tickers/SPY.csv`.

## 4. Prompt: "run this backtest for the bast 5 years using the SPY bars contained in
underying-tickers."

- Ran `weekly_iron_butterfly_spy_backtest_dynamic.py` with `BT_START_OVERRIDE=2021-08-26`.
- Hit the same `.env` path bug as the fetcher script (`_ENV_FILE` pointed at `../.env`). Fixed it
  to point at the local `.env`, matching `get_5yrs_spy_bars.py`.
- Re-ran successfully. Results for 2021-08-26 → 2026-08-25:
  - 70 trades executed (of 234 Monday candidates found)
  - Win rate: 61.4% (43 winners / 27 losers)
  - Total P&L: +$84,760
  - Avg P&L/week: +$1,210.86
  - Max drawdown: $36,720
  - Annualised Sharpe: 1.23
  - Exit breakdown: 69 scheduled, 1 stop-loss (80%)
  - Note: many of the ~249 possible Mondays were skipped because Alpaca only had options data for
    502/934 unique option contracts requested, plus the prior-week-range/gap filters skipping some
    weeks.
- Output files:
  - `../backtest-csv-files/weekly_iron_butterfly_spy_backtest_dynamic.csv`
  - `../backtest-html-reports/weekly-iron-butterfly-spy-backtest-dynamic.html`

## Files changed/created this session
- Created: `get_5yrs_spy_bars.py`
- Created: `underlying-tickers/SPY.csv` (data file, 1,044,306 rows)
- Modified: `weekly_iron_butterfly_spy_backtest_dynamic.py` — fixed `_ENV_FILE` path from `../.env`
  to local `.env`.

## 5. Prompt: "are the entrance and exit times optimized for p&l?"

Reviewed `run_optimization()`. Found the `--optimize` sweep covered `ENTRY_SETTLE_WINDOW_MIN`,
`BREACH_FRACTION`, and the trade filters (`MAX_CREDIT_PCT`, `MAX_PRIOR_RANGE_PCT`, `MAX_GAP_PCT`),
but **not** the entry search window (`ENTRY_SEARCH_START`/`ENTRY_SEARCH_END`, hardcoded
9:35–10:30 ET) or the exit cutoff time (`EXIT_DEFAULT_HOUR`/`EXIT_DEFAULT_MIN`, hardcoded
15:40 ET) — those were fixed constants never swept for P&L.

## 6. Prompt: "run `--optimize` to find better parameter values, and extend the optimizer to also
sweep the entry window/exit cutoff time"

- Parametrized `find_optimal_entry()` to accept `search_start`/`search_end`, `find_dynamic_exit_week()`
  to accept an `exit_cutoff` tuple, and `collect_candidates()` to accept `entry_search_start`/
  `entry_search_end` — replacing the hardcoded globals used inside those functions.
- Added `exit_cutoff` to `default_filter_params()` / `apply_filters()` so it flows through like
  `breach_fraction`.
- Rewrote `run_optimization()`: outer loop now sweeps 7 curated `(settle_window, search_start,
  search_end)` combos (each triggering a full re-fetch of option data), inner loop sweeps
  `MAX_CREDIT_PCT` × `MAX_PRIOR_RANGE_PCT` × `MAX_GAP_PCT` × `BREACH_FRACTION` × a new
  `exit_cutoff` grid (`14:30`, `15:00`, `15:40`, `15:55`) — 1,600 in-memory combos per entry
  window, 11,200 total.
- Ran `--optimize` over 2021-08-26 → 2026-08-25 (BT_START_OVERRIDE). Took ~40 minutes total
  (~6 min per entry-window combo for the in-memory inner sweep, plus option-data fetch time).
- **Winning combo (rank #1 of 920 viable):** settle window = 5 min, entry search = 09:35–10:00 ET,
  exit cutoff = 15:00 ET, filters = gap ≤ 0.8% only (no credit/prior-range filter), breach
  fraction = 1.0 → 97 trades, 59.8% win rate, **+$108,840 total P&L**, Sharpe 1.06, max drawdown
  $36,280. This beat the prior baseline (+$84,760 total, Sharpe 1.23) by ~28% more total P&L.

## 7. Prompt: "yes" (apply the winning optimizer parameters as new defaults)

Updated the module-level constants in `weekly_iron_butterfly_spy_backtest_dynamic.py` to the
optimizer's winning combo:
- `ENTRY_SEARCH_END`: `(10, 30)` → `(10, 0)`
- `ENTRY_SETTLE_WINDOW_MIN`: `20` → `5`
- `EXIT_DEFAULT_HOUR, EXIT_DEFAULT_MIN`: `15, 40` → `15, 0`
- `MAX_PRIOR_RANGE_PCT`: `0.030` → `None` (disabled)
- `MAX_GAP_PCT` and `MAX_CREDIT_PCT` left unchanged (0.008 / `None`)

Re-ran the plain (non-`--optimize`) backtest with `BT_START_OVERRIDE=2021-08-26` to confirm the
new defaults reproduce the optimizer's #1 result exactly: 97 trades, 59.8% win rate, +$108,840
total P&L, Sharpe 1.06, max drawdown $36,280.

Also fixed two hardcoded `"9:35-10:30 ET"` strings (in the startup log line and the results
footer) that no longer matched the actual `ENTRY_SEARCH_START`/`ENTRY_SEARCH_END` values — now
built dynamically from those constants.

## Files changed/created this session (updated)
- Created: `get_5yrs_spy_bars.py`
- Created: `underlying-tickers/SPY.csv` (data file, 1,044,306 rows)
- Modified: `weekly_iron_butterfly_spy_backtest_dynamic.py`:
  - Fixed `_ENV_FILE` path from `../.env` to local `.env`.
  - Parametrized entry search window and exit cutoff time so `run_optimization()` can sweep them.
  - Extended `run_optimization()` to sweep entry window (7 combos) × exit cutoff time (4 values)
    in addition to the existing filter/breach-fraction grid.
  - Applied the optimizer's winning parameters as the new module defaults (see #7 above).
  - Fixed two hardcoded "9:35-10:30 ET" log/print strings to reflect the actual entry window.

