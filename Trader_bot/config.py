"""
CONFIGURATION FILE
Trader Bot System
Keep this clean and centralized.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ============================
# IBKR CONNECTION
# ============================

IB_HOST = "127.0.0.1"
IB_PORT = 7497          # 7497 = TWS paper, 4002 = IB Gateway paper
IB_CLIENT_ID = 12       # Change if client ID conflict occurs

# Market data type:
# 1 = Live
# 2 = Frozen
# 3 = Delayed
# 4 = Delayed frozen
MARKET_DATA_TYPE = 1


# ============================
# TRADING CAPITAL CONTROL
# ============================

# This is your defined trading purse.
# This is NOT your IBKR total account value.
# This is the capital the bot is allowed to deploy.

TRADE_PURSE_USD = 1500.0
# Alias used by other scripts
PURSE = TRADE_PURSE_USD

# Minimum dollars per position (safety floor)
MIN_POSITION_USD = 150

# Maximum dollars per single position cap (optional protection)
MAX_POSITION_USD = 1000


# ============================
# ALLOCATION TIERS (sizing)
# ============================

# Thresholds to classify a BUY candidate into HIGH / MEDIUM / LOW tier.
# All three criteria must be met (AND logic) to qualify for a tier.
# score_total: 0-100 | confidence: 0-1 | risk_score: 0-1 (lower = safer)

ALLOC_TIER_HIGH_MIN_SCORE = 87.0
ALLOC_TIER_HIGH_MIN_CONF  = 0.75
ALLOC_TIER_HIGH_MAX_RISK  = 0.35

ALLOC_TIER_MED_MIN_SCORE  = 83.0
ALLOC_TIER_MED_MIN_CONF   = 0.60
ALLOC_TIER_MED_MAX_RISK   = 0.50

# % of remaining purse to allocate per candidate at each tier.
# Candidates are processed highest-score-first; each gets its full tier %
# of the remaining purse at the time of allocation (first-come, first-served).
ALLOC_TIER_HIGH_PCT = 0.25   # 25% of remaining purse
ALLOC_TIER_MED_PCT  = 0.18   # 18% of remaining purse
ALLOC_TIER_LOW_PCT  = 0.12   # 12% of remaining purse


# ============================
# UNIVERSE CONTROL
# ============================

# Max symbols loaded from static universe file
STATIC_POOL_MAX = 200

# Cap for weekly dynamic universe refresh (src_universe_refresh.py)
MAX_UNIVERSE_SIZE = 150

# Allow dynamic scanner symbols to be merged
ENABLE_DYNAMIC_UNIVERSE = True


# ============================
# NEWS / LOOKBACK
# ============================

NEWS_LOOKBACK_HOURS = 72


# ============================
# TELEGRAM SETTINGS
# ============================

# ⚠️ YOU MUST SET THIS
TELEGRAM_BOT_TOKEN ="8328222966:AAHk-WxPXoNThfNRV8MWKURbf2IWq-ev_Dw"

# Leave as None. Bot will auto-detect after /start
TELEGRAM_CHAT_ID = None


# ============================
# SAFETY CONTROLS
# ============================

# Require manual approval before placing orders
REQUIRE_APPROVAL = True

# Allow auto-sizing (recommended True)
AUTO_POSITION_SIZING = True


# ============================
# BRACKET PARAMETERS
# ============================

# % expressed as decimals: 0.15 = 15%
STOP_LOSS_PCT = 0.15


# ============================
# CONDITIONAL SELL SYSTEM
# ============================

# First trigger: position gains this % from entry price
CONDITIONAL_SELL_INITIAL_PCT = 0.50      # +50% from entry

# After HOLD: re-trigger when price moves this % from the new anchor
CONDITIONAL_SELL_SUBSEQUENT_PCT = 0.30   # ±30% from last anchor

# Price check interval (seconds) during market hours
CONDITIONAL_SELL_CHECK_INTERVAL = 60     # every 60 seconds


# ============================
# LOGGING
# ============================

ENABLE_LOGGING = True
LOG_LEVEL = "INFO"


# ============================
# EXTERNAL API KEYS
# ============================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MARKETAUX_API_KEY = os.getenv("MARKETAUX_API_KEY")


# ============================
# TRADE TRACKING
# ============================

# clientId used by the fill monitor / reconciliation thread in src_trade_tracker.py
FILL_MONITOR_CLIENT_ID = 21

# How often to reconcile open positions against IBKR (minutes)
RECONCILIATION_INTERVAL_MINUTES = 5

# Drawdown thresholds for stop-loss alert (% expressed as negative decimals)
# WARNING fires first; ALERT fires when the stop is breached
STOP_WARNING_THRESHOLD_PCT = -0.10   # -10%
STOP_ALERT_THRESHOLD_PCT   = -0.15   # -15%  (mirrors STOP_LOSS_PCT)

# Commission estimation fallback when IBKR does not report it
# $0.0035/share, min $0.35, max 1% of trade value
COMMISSION_PER_SHARE = 0.0035
COMMISSION_MIN        = 0.35
COMMISSION_MAX_PCT    = 0.01   # 1% of trade value


# ============================
# LEARNING ENGINE
# ============================

# Minimum sample size before a signal bucket appears in reports
LEARNING_MIN_SAMPLE = 5

# Sample sizes for confidence tiers (see learning-model-brief.md)
LEARNING_LOW_SAMPLE      = 14    # N <= this → preliminary
LEARNING_MODERATE_SAMPLE = 29    # N <= this → emerging
# N > LEARNING_MODERATE_SAMPLE → reliable (no constant needed)

# HTML reports are saved here (email delivery stubbed for future)
REPORT_OUTPUT_DIR = "output/reports"

# DB path (all scripts should use this via config rather than hardcoding)
DB_PATH = "output/trader.sqlite"