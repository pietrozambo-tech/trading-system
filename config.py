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
MIN_PREMARKET_GAP          = 0.005   # +0.5% — gap meaningfully above yesterday's close
SPY_BLOCK_THRESHOLD   = -0.020          # -2.0%

# === SIGNAL THRESHOLDS (L2) ===
OR_POSITION_THRESHOLD   = 0.66
GAP_RETENTION_THRESHOLD = 0.70
VOL_RATIO_HIGH          = 3.0    # boost +0.10
VOL_RATIO_MID           = 2.0    # boost +0.05
CONFIDENCE_THRESHOLD    = 0.65

# === CATALYST BONUSES (additive, not multiplicative) ===
CATALYST_TIER1 = 0.30   # +0.30 — major catalyst
CATALYST_TIER2 = 0.20   # +0.20 — real but moderate news
CATALYST_TIER3 = 0.10   # +0.10 — rumour / speculative
CATALYST_NONE  = 0.00   # +0.00 — pure technical setup

# === RISK MANAGEMENT ===
# Tieni sempre $2k nel conto come cuscinetto (fee/emergenze).
# Ogni trade = (equity - CASH_CUSHION_USD) / MAX_POSITIONS, arrotondato per difetto.
CASH_CUSHION_USD         = 2_000   # sempre disponibile, mai investito
MAX_POSITIONS            = 2
HARD_BLOCKER_PCT         = 0.020   # -2.0% dal prezzo di entrata
ATR_MULTIPLIER           = 1.2
ATR_LOOKBACK             = 14      # days
MAX_DAILY_LOSS_USD       = None    # disabilitato — ogni trade ha il proprio hard stop

# === EXIT RULES ===
VWAP_EXIT_MIN_PROFIT_PCT = 0.025   # VWAP exit solo se profit >= 2.5% (da sensitivity analysis)

# === TIMING (ET) ===
WATCHLIST_TIME       = "09:25"
ENTRY_TIME           = "09:40"
ORDER_TIME           = "09:42"
MONITORING_INTERVAL  = 300       # 5 minutes in seconds
EOD_CLOSE_TIME       = "15:45"
TELEGRAM_NOTIFY_TIME = "16:05"

# === GENERAL ===
MAX_CANDIDATES_TO_LLM  = 15
TIMEZONE               = "America/New_York"
PAPER_INITIAL_EQUITY   = 100_000   # saldo iniziale paper — per calcolo P&L cumulativo
