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

# Maximum simultaneous open positions
MAX_POSITIONS = 6

# Minimum dollars per position (safety floor)
MIN_POSITION_USD = 150

# Maximum dollars per single position cap (optional protection)
MAX_POSITION_USD = 1000


# ============================
# POSITION SIZING BEHAVIOR
# ============================

# Risk weighting model:
# Higher score & lower risk → larger allocation
# Lower score / higher risk → smaller allocation

BASE_RISK_UNIT = 1.0

# These adjust weight influence
SCORE_WEIGHT = 0.6
RISK_WEIGHT = 0.4


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

# % expressed as decimals: 0.15 = 15%, 0.35 = 35%
STOP_LOSS_PCT = 0.15
TAKE_PROFIT_PCT = 0.35


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