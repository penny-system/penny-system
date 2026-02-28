# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Penny System is a semi-automated penny-stock trading bot that:
1. Scans a universe of symbols via IBKR TWS
2. Scores candidates using a deterministic rule engine
3. Sends actionable alerts via Telegram
4. Requires human approval before placing bracket orders in IBKR

**Design principle:** The deterministic engine decides trades. OpenAI is QC/risk overlay only — it never decides trades directly.

---

## Key Paths

- **Root:** `C:\Users\kieyf\penny-system`
- **Bot code:** `Trader_bot\` (all `src_*.py` scripts live here)
- **Active DB:** `Trader_bot\output\trader.sqlite` (NOT `Trader_bot\db\trader.sqlite`)
- **Venv:** `venv\Scripts\activate`
- **Config:** `Trader_bot\config.py` (loads `.env` via `python-dotenv`)
- **Runtime overrides:** `Trader_bot\runtime_overrides.json`

---

## Common Commands

All bot scripts must be run from the `Trader_bot\` directory (they resolve paths relatively):

```bash
# Activate venv (from project root)
venv\Scripts\activate

# Full daily scan pipeline (from Trader_bot\)
python main_daily_run.py

# Backfill suggested quantities for BUY recs
python fill_suggested_qty.py --only-buy --overwrite

# Approve BUY recs and place bracket orders interactively
python approve.py
python approve.py --dry-run   # preview without placing orders

# Send Telegram notifications
python notify_scan.py --mode morning
python notify_scan.py --mode hourly

# Start Telegram settings bot
python telegram_bot.py

# Weekly dynamic universe refresh (from Trader_bot\)
python src_universe_refresh.py
```

`notify_scan.py` is designed to be run from the **project root** (`penny-system\`) — it calls `Trader_bot\main_daily_run.py` and `Trader_bot\fill_suggested_qty.py` as subprocesses. The `.bat` files in `Trader_bot\` are for Windows Task Scheduler.

`run_weekly_refresh.bat` activates the venv and runs `src_universe_refresh.py`. Add to Task Scheduler to run every Sunday at 8:00 PM.

---

## Weekly Universe Refresh

`src_universe_refresh.py` rebuilds `Trader_bot\universe_dynamic.txt` weekly using live data.

### Sources (in order)
1. **IBKR scanner** — two scan types merged: `TOP_PERC_GAIN` and `HIGH_VS_13W_HL`. Filters: price $1–$8, avg volume ≥ 500k, US exchanges (`STK.US.MAJOR`).
2. **yfinance validation** — each IBKR symbol is validated (price still in range, 3-month avg vol ≥ 500k). Dropped symbols are logged.
3. **yf.screen() extras** — `day_gainers` screen pulled for additional candidates (best-effort; silently skipped if not supported).
4. **Static CSV fallback** — if combined results < 20 symbols, supplements from `scan_active.csv` + `scan_gainers.csv` (validated via yfinance).

### Output
- `Trader_bot\universe_dynamic.txt` — one symbol per line, sorted, capped at `MAX_UNIVERSE_SIZE = 150`
- `Trader_bot\output\universe_refresh_log.txt` — append-only log with counts per source

### Universe Load Priority (`load_static_universe()`)
```
1st: universe_dynamic.txt  (if exists and non-empty)
2nd: scan_*.csv files      (manually exported scans)
3rd: universe_static.txt   (final hardcoded fallback)
```
Logs: `[Universe] Loaded from: <source> -- N symbols`

### IBKR clientId
`src_universe_refresh.py` standalone uses **clientId 20** (dedicated, avoids pipeline conflicts).

---

## Architecture

### Pipeline (main_daily_run.py)

```
src_universe.py  →  src_features.py  →  src_scoring.py  →  src_sizing.py
   (symbols)          (IBKR bars)          (score/BUY?)       (qty alloc)
                                                   ↓
                                          src_storage.py (SQLite)
                                          write_brief_file() → output/brief.txt
```

### Module Responsibilities

| Module | Key function(s) | Role |
|---|---|---|
| `config.py` | — | Central config; reads `.env`. All scripts import this. |
| `src_universe.py` | `load_static_universe()` | Loads symbols from `scan_*.csv` (first column); falls back to `universe_static.txt` |
| `src_features.py` | `compute_features_from_bars(bars)` | Computes `ref_price`, `ret_1`, `ret_5`, `vol_surge`, `dollar_vol`, `breakout` from 1-min IBKR bars |
| `src_scoring.py` | `score_candidate(feat, horizon)` | Returns `(score, confidence, setup_type, rationale)`. Score ≥80 = BUY. Score is hard-capped at 74.9 if the setup is "unconfirmed". |
| `src_sizing.py` | `size_recommendations(recs, ib=None)` | Risk-weighted position allocation: lower `risk_score` → larger allocation. Applies `runtime_overrides.json` before sizing. |
| `src_storage.py` | `connect_db()`, `ensure_schema()`, `create_run()`, `insert_recommendations()`, `snapshot_recommendations()`, `insert_open_position()`, `close_position()` | All SQLite I/O. Schema adds columns via ALTER IF MISSING. |
| `src_ibkr_client.py` | `connect_ib()`, `get_effective_purse_usd(ib)` | IBKR connection (TWS paper: port 7497, clientId 12). Effective purse = `min(configured, IBKR available funds)`. |
| `src_news.py` | `fetch_and_analyze_news(symbol)` | Marketaux API news fetch + keyword scoring. `news_summary_for_contract(ib, contract)` uses IBKR historical headlines as fallback. |
| `src_regime.py` | `compute_regime(ib)` | SPY + VXX-based regime: `risk_on | neutral | risk_off`. |
| `src_settings.py` | `apply_overrides_to_config(config)` | Reads `runtime_overrides.json`, patches `config` module at runtime. Called at start of sizing. |
| `src_briefing.py` | `write_brief()` | Legacy brief writer (richer format with portfolio). `main_daily_run.py` owns brief writing now. |
| `src_trade_tracker.py` | `start_fill_monitor()`, `monitor_stop_loss(ib)`, `is_market_hours()`, `handle_hold(conn, symbol, price)`, `execute_conditional_sell(ib, symbol, qty)` | Fill monitor (clientId 21): tracks IBKR `execDetailsEvent` + `commissionReportEvent` to record entries/exits in DB. Also hosts the stop-loss monitor (-10% warning, -15% alert) and the conditional sell monitor (price polling every 60s). Started as background thread by `telegram_bot.py`. |
| `src_conditional_sell.py` | `evaluate_stock_health(ib, conn, symbol, entry, current, direction)` | Stock health evaluation engine: 5-factor analysis (momentum, volume, risk, news, technical) → hold_score → SELL/HOLD recommendation + rationale. Called by the conditional sell monitor. |
| `src_learning.py` | `run_learning_analysis(conn)`, `save_learning_snapshot(conn, analysis)` | 4-layer analytics engine: signal buckets, combination analysis, temporal trend, slippage analysis. Returns structured dict with profit_factor, expectancy, observations. |
| `src_report.py` | `generate_report(conn)` | Dark-theme HTML performance report. Sections: overview stats, open positions, temporal, signal analysis, combinations, slippage, trade history (with key signals column). Saved to `output/reports/`. |
| `approve.py` | — | Interactive CLI: shows BUY recs, accepts `APPROVE <SYM>` / `REJECT <SYM>` / `EXIT`. Places bracket orders (parent limit buy + take-profit limit + stop-loss stop). |
| `notify_scan.py` | — | Orchestrates the full scan, then sends Telegram notifications. Morning mode always sends; hourly mode deduplicates on signature. Calls `backfill_snapshot_gate()` after OpenAI gate decisions. |
| `telegram_bot.py` | — | Telegram bot: runtime control (`/purse`, `/maxpos`, `/fx`, `/minpos`), trade analytics (`/history`, `/performance`, `/report`), IBKR position views (`/portfolio`, `/positions`). Starts fill monitor on launch. |

### Scoring Logic

- **Confirmation required for BUY**: A stock is only scoreable above 74.9 if `breakout==1 AND vol_surge>=1.20` OR `dollar_vol >= $25M`.
- **Horizons**: `swing` (uses `ret_5`) and `momentum` (uses `ret_1`). Both are scored per symbol.
- **Decision threshold**: `score_total >= 80` → `"BUY"`, else `"WATCH"`.

### News / AI Triggers

- **Trigger A**: A headline contains ≥2 risk keywords simultaneously (e.g., "offering + warrant"). Deduped per `symbol|headline` key in `notify_state.json`.
- **Trigger C (Soft Gate)**: BUY candidate with Trigger A headlines → OpenAI (`gpt-4o-mini` by default) reviews the conflict and returns `APPROVE | HOLD | REDUCE | BLOCK`.
- OpenAI model can be overridden via `OPENAI_MODEL` env var.

### Runtime Overrides

`runtime_overrides.json` overrides these config keys at runtime (without restarting):
- `TRADE_PURSE_CAD`, `USD_PER_CAD`, `MAX_POSITIONS`, `MIN_POSITION_USD`

The Telegram bot (`/purse`, `/maxpos`, `/fx`, `/minpos`) writes to this file via a pending → confirm flow.

### Database Schema

SQLite at `Trader_bot\output\trader.sqlite`:
- `runs(run_id, created_at)` — one row per scan run
- `recommendations(id, run_id, symbol, decision, horizon, score_total, confidence, risk_score, ref_price, stop_price, take_price, rationale, suggested_qty)`
- `recommendation_snapshots` — signal features at time of scan (breakout, vol_surge, confidence, risk_score, ret_5, setup_type, gate_decision, news_risk_note, purse_usd, etc.). One row per symbol per run, backfilled with gate result by `notify_scan.py`.
- `open_positions(position_id, symbol, entry_price, entry_qty, stop_price, take_price, entry_time, status, ibkr_exec_id_entry, commission_entry)` — written by `FillMonitor` on entry fill. `status` = `'open'` | `'closed'`.
- `closed_trades(trade_id, symbol, entry_price, exit_price, entry_qty, exit_qty, net_pnl, gross_pnl, pnl_pct, commission_total, hold_seconds, hold_duration_hours, outcome, exit_reason, entry_time, closed_at, ibkr_exec_id_entry, ibkr_exec_id_exit, ...)` — written by `FillMonitor` on exit fill. `outcome` = `WIN | LOSS | BREAKEVEN`.
- `learning_snapshots` — periodic learning engine output: stats, profit_factor, expectancy, signal_analysis JSON, observations JSON. Written by `save_learning_snapshot()`.

All schema migrations are idempotent (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` with `PRAGMA table_info` guard). Safe to run `ensure_schema()` repeatedly.

---

## Trade Tracking & Learning Engine

### Fill Monitor (`src_trade_tracker.py`)

`FillMonitor` is a background thread started by `telegram_bot.py` on launch. It connects to IBKR with **clientId 21** (read-only) and subscribes to `execDetailsEvent` and `commissionReportEvent`.

**Entry fill flow:**
1. `execDetailsEvent` fires for a BUY fill → `insert_open_position()` in DB with `status='open'`
2. `commissionReportEvent` fires → updates `commission_entry` in `open_positions`

**Exit fill flow:**
1. `execDetailsEvent` fires for a SELL fill → `close_position()` in DB
2. Computes: `gross_pnl`, `net_pnl` (after commissions), `pnl_pct`, `hold_seconds`, `hold_duration_hours`, `outcome` (WIN/LOSS/BREAKEVEN), `exit_reason` (take_profit / stop_loss / manual)
3. Sends exit notification via Telegram with full P&L breakdown and session summary

**Reconciliation:** runs every `RECONCILIATION_INTERVAL_MINUTES` (default 5). Checks IBKR open positions against `open_positions` DB — logs any phantom positions. Paper account phantom symbols (AAPL) are filtered via `_PHANTOM_SYMBOLS` set.

**Stop-loss monitor** (`monitor_stop_loss(ib)`): called from `main_daily_run.py` during market hours. Fires a -10% drawdown warning once per position (tracked in `_warned_symbols` set), then a -15% alert. Cross-references `open_positions` DB for expected stop price.

### Learning Engine (`src_learning.py`)

`run_learning_analysis(conn)` → returns `analysis` dict with 4 layers:

| Layer | Function | Output |
|---|---|---|
| Signal analysis | `_signal_analysis()` | Bucket win rate per feature (vol_surge, score_total, confidence, risk_score, ret_5, breakout, setup_type) |
| Combination analysis | `_combination_analysis()` | 7 preset combos (breakout+gate, breakout+vol_surge, high_conf+low_risk, etc.), sorted by win rate |
| Temporal analysis | `_temporal_analysis()` | First/last half comparison, last 10 trades, trend (IMPROVING/STABLE/DEGRADING), max drawdown |
| Slippage analysis | `_slippage_analysis()` | Avg entry/exit slippage, paper-to-live P&L haircut estimate |

Overview stats include: `profit_factor` (None = no losses = ∞), `expectancy`, `avg_win_pnl`, `avg_loss_pnl`.

**Confidence tiers** (N = number of closed trades):
- N < 5: `insufficient` — no signal-level breakdown shown
- 5 ≤ N ≤ 14: `preliminary`
- 15 ≤ N ≤ 29: `emerging`
- N ≥ 30: `reliable`

### Performance Report (`src_report.py`)

`generate_report(conn)` → dark-theme HTML saved to `output/reports/report_YYYYMMDD_HHMMSS.html`. Sections:
1. Overview stat boxes (total trades, win rate, profit factor, expectancy, avg hold, P&L)
2. Key observations (plain English, confidence-gated)
3. Open positions (live status from `open_positions` table)
4. Temporal analysis table
5. Signal analysis (bucket tables per feature)
6. Combination analysis
7. Slippage (paper-to-live readiness)
8. Trade history (last 100 trades with key signals column: breakout, vol_surge, confidence, gate)

### New Telegram Commands

- `/history [days|month|SYM]` — trade history with flexible filtering; defaults to last 30 days. Summary stats: W/L, gross, fees, net, profit factor, avg hold, best/worst.
- `/performance` — learning engine analysis: profit factor, expectancy, signal quality bullets, trend comparison (last 10 vs first half WR).
- `/report` — generate and save full HTML report; returns file path.

---

## Environment Variables (`.env`)

Required in the project root or `Trader_bot\`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...          # numeric; required for scheduled/non-interactive scripts
MARKETAUX_API_KEY=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini      # optional override
```

`config.py` assigns these from `os.getenv()` after loading `.env`. The hardcoded fallback values earlier in `config.py` are overwritten by the `os.getenv()` calls at the bottom — always treat the `.env` values as authoritative.

---

## IBKR / TWS

- TWS Paper trading, port `7497`, clientId `12`
- Market data type: `1` (Live) — change to `3` (Delayed) if not subscribed
- TWS must be open and API enabled before running any script that calls `connect_ib()`
- If IBKR is down, `notify_scan.py` sends a concise Telegram warning and exits cleanly (no traceback)
- `connect_ib_with_retry()` retries the connection up to 3 times with 10-second delays before failing; used by `main_daily_run.py`

### Preventing TWS disconnects

**Windows power settings:** Set the PC to never sleep while TWS is running. In Settings → System → Power & Sleep, set both "Screen" and "Sleep" to **Never** (or use a power plan with sleep disabled). A sleeping PC will drop the TWS connection and cause hourly scans to fail silently.

**TWS auto-logoff:** TWS has a built-in daily auto-logoff that will disconnect the API session. To disable it: in TWS go to **Edit (or Configure) → Global Configuration → API → Settings** and uncheck **"Auto-logoff"** (or set the logoff time to a window outside your scan hours). Without this, TWS will log itself off mid-day regardless of activity.

### Bracket order persistence (GTC)

Brackets are now **2-leg** (BUY + stop-loss only). Take-profit is handled by the conditional sell monitor, not IBKR.

- **Parent BUY order**: `tif="DAY"` (intentional — stale unfilled BUY orders should not linger)
- **Stop-loss SELL stop**: `tif="GTC"` — persists across sessions until filled or manually cancelled

**If TWS disconnects after bracket placement**: GTC orders persist on IBKR's servers and survive restarts. Use `/portfolio` in the Telegram bot to verify open positions after any TWS restart.

---

### Conditional Sell System

Replaces the fixed 35% take-profit bracket. Bracket orders are now 2-leg (BUY + stop-loss only). Take-profit is handled by the conditional sell monitor running inside the FillMonitor thread.

**Flow:**
1. Position reaches +50% from entry → engine evaluates stock health
2. Telegram notification: SELL CONDITIONAL with SELL/HOLD recommendation + factor breakdown
3. User sends `/sell SYMBOL` or `/hold SYMBOL`
4. If HOLD: new anchor set at current price, next triggers at ±30%
5. Cycle repeats at each new anchor until `/sell` or stop-loss triggers

**Key files:**
- `src_conditional_sell.py` — stock health evaluation (momentum, volume, risk, news, technical)
- `src_trade_tracker.py` — conditional sell monitor loop (shares clientId=21 FillMonitor connection)
- `approve.py` — 2-leg bracket placement (no take-profit order)

**DB table:** `conditional_sell_state` — tracks anchor prices, trigger thresholds, and status per symbol.

**Config:**
- `CONDITIONAL_SELL_INITIAL_PCT = 0.50` — first trigger at +50% from entry
- `CONDITIONAL_SELL_SUBSEQUENT_PCT = 0.30` — subsequent triggers at ±30% from anchor
- `CONDITIONAL_SELL_CHECK_INTERVAL = 60` — price poll interval in seconds
- Stop-loss remains at `STOP_LOSS_PCT = 0.15` as IBKR bracket order (GTC)

**Decision logic:** 5 factors scored to a `hold_score` (-12 to +8). `hold_score >= 2` → HOLD, otherwise SELL. Neutral zone (-1 to +1) defaults to SELL — when in doubt, take profits.

**Edge cases:**
- Stop-loss fills first → FillMonitor marks `conditional_sell_state.status = 'SOLD'`; monitor skips
- PENDING_DECISION > 2 hours → single reminder sent; stop-loss still protects downside
- IBKR disconnect → state in SQLite, survives restarts; monitor resumes on reconnect
