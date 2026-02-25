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
```

`notify_scan.py` is designed to be run from the **project root** (`penny-system\`) — it calls `Trader_bot\main_daily_run.py` and `Trader_bot\fill_suggested_qty.py` as subprocesses. The `.bat` files in `Trader_bot\` are for Windows Task Scheduler.

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
| `src_storage.py` | `connect_db()`, `ensure_schema()`, `create_run()`, `insert_recommendations()` | All SQLite I/O. Schema adds `suggested_qty` column via ALTER if missing. |
| `src_ibkr_client.py` | `connect_ib()`, `get_effective_purse_usd(ib)` | IBKR connection (TWS paper: port 7497, clientId 12). Effective purse = `min(configured, IBKR available funds)`. |
| `src_news.py` | `fetch_and_analyze_news(symbol)` | Marketaux API news fetch + keyword scoring. `news_summary_for_contract(ib, contract)` uses IBKR historical headlines as fallback. |
| `src_regime.py` | `compute_regime(ib)` | SPY + VXX-based regime: `risk_on | neutral | risk_off`. |
| `src_settings.py` | `apply_overrides_to_config(config)` | Reads `runtime_overrides.json`, patches `config` module at runtime. Called at start of sizing. |
| `src_briefing.py` | `write_brief()` | Legacy brief writer (richer format with portfolio). `main_daily_run.py` owns brief writing now. |
| `approve.py` | — | Interactive CLI: shows BUY recs, accepts `APPROVE <SYM>` / `REJECT <SYM>` / `EXIT`. Places bracket orders (parent limit buy + take-profit limit + stop-loss stop). |
| `notify_scan.py` | — | Orchestrates the full scan, then sends Telegram notifications. Morning mode always sends; hourly mode deduplicates on `(run_id, signature)`. |
| `telegram_bot.py` | — | Telegram bot for runtime control: `/purse`, `/maxpos`, `/fx`, `/minpos` (all require `/confirm`). First `/start` registers the chat ID. |

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
