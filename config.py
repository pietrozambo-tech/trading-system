import os
from dotenv import load_dotenv

load_dotenv()

# === ALPACA ===
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# === ANTHROPIC ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LLM_MODEL         = "claude-sonnet-4-6"

# === TELEGRAM ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# === UNIVERSE FILTERS (L1) ===
MIN_MARKET_CAP        = 2_000_000_000   # $2B
MIN_PRICE             = 5.0
MIN_ADV               = 1_000_000       # 1M shares/day
MAX_BID_ASK_SPREAD    = 0.006           # 0.6%
MIN_PREMARKET_GAP     = 0.015           # 1.5%
MIN_PREMARKET_VOL_ADV = 1.0             # 100% of ADV
SPY_BLOCK_THRESHOLD   = -0.018          # -1.8%

# === SIGNAL THRESHOLDS (L2) ===
OR_POSITION_THRESHOLD   = 0.66
GAP_RETENTION_THRESHOLD = 0.70
VOL_RATIO_HIGH          = 3.0    # boost +0.10
VOL_RATIO_MID           = 2.0    # boost +0.05
CONFIDENCE_THRESHOLD    = 0.65

# === CATALYST MULTIPLIERS ===
CATALYST_TIER1 = 1.00
CATALYST_TIER2 = 0.80
CATALYST_TIER3 = 0.55
CATALYST_NONE  = 0.30

# === RISK MANAGEMENT ===
POSITION_SIZE_USD    = 500       # $500 per position (paper uses USD)
MAX_POSITIONS        = 2
HARD_BLOCKER_PCT     = 0.045     # -4.5%
ATR_MULTIPLIER       = 1.5
ATR_LOOKBACK         = 14        # days
MAX_DAILY_LOSS_USD   = 50        # system stops for the day

# === TIMING (ET) ===
WATCHLIST_TIME       = "09:25"
ENTRY_TIME           = "09:45"
ORDER_TIME           = "09:47"
MONITORING_INTERVAL  = 300       # 5 minutes in seconds
EOD_CLOSE_TIME       = "15:45"
TELEGRAM_NOTIFY_TIME = "16:05"

# === GENERAL ===
MAX_CANDIDATES_TO_LLM = 15
TIMEZONE = "America/New_York"
