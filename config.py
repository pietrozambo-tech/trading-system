import os
from dotenv import load_dotenv

load_dotenv()

# === ALPACA ===
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
# Data feed: "iex" = free tier (IEX exchange only), "sip" = paid tier (consolidated tape)
# Switch to "sip" in Railway env vars when upgrading to Alpaca Algo Trader Plus.
ALPACA_DATA_FEED  = os.getenv("ALPACA_DATA_FEED", "iex")

# === ANTHROPIC ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LLM_MODEL         = "claude-sonnet-4-6"

# === TELEGRAM ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# === UNIVERSE FILTERS (L1) ===
MIN_ADV               = 5_000_000         # 5M shares/day consolidated (SIP)
MIN_PREMARKET_GAP     = 0.005            # +0.5% — gap meaningfully above yesterday's close
SPY_BLOCK_THRESHOLD   = -0.020          # -2.0%
PM_OPEN_RETENTION     = 0.50            # min fraction of pre-market gap still intact at 9:30 open

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

# === SHORT SQUEEZE BONUS ===
# Applied only when short_float > threshold AND a catalyst is present.
# Short interest alone doesn't cause a squeeze — the catalyst is what forces covering.
SHORT_SQUEEZE_THRESHOLD     = 0.15   # 15% of float sold short
SHORT_SQUEEZE_BONUS         = 0.10   # additive bonus (capped — never stacks)
SHORT_SQUEEZE_GAP_THRESHOLD = 0.10   # 10% pre-market gap as standalone squeeze trigger

# === RISK MANAGEMENT ===
# Ogni trade = (equity - CASH_CUSHION_USD) / MAX_POSITIONS, arrotondato per difetto.
CASH_CUSHION_USD         = 1_000   # $1k sempre disponibile, mai investito
MAX_POSITIONS            = 2
HARD_BLOCKER_PCT         = 0.020   # -2.0% dal prezzo di entrata
ATR_LOOKBACK             = 14      # days
MAX_DAILY_LOSS_USD       = None    # disabilitato — ogni trade ha il proprio hard stop

# === EXIT RULES ===
VWAP_EXIT_MIN_PROFIT_PCT = 0.015   # VWAP exit solo se profit >= 1.5%
# Step-ratchet stop (sostituisce il break-even semplice). Lista di gradini
# (peak_trigger_pct, stop_floor_pct) relativi al prezzo di entrata: quando il
# guadagno di PICCO raggiunge peak_trigger_pct, lo stop viene alzato a
# entry*(1+stop_floor_pct), ma solo se più alto dello stop corrente (ratchet
# monotòno — lo stop non scende mai). Un floor ≤ 0 è una protezione tipo break-even
# (incluso un piccolo cuscinetto sotto l'entry); un floor > 0 blocca profitto.
# Backtest (631 trade, gen 2025–giu 2026, --exit): "Step C + buffer −0.2%" è l'ottimo
# per P&L — +$61.3k vs +$56.3k di Step C puro (+9%), win rate 43% vs 38%, profit
# factor 1.45, max drawdown $10.0k. Il cuscinetto −0.2% sul primo gradino fa
# sopravvivere i pullback che tornano esattamente all'entry (caso MRVL 18 giu) e
# ripartire, al costo di ~$1k di drawdown in più vs Step C puro. Convive con il VWAP
# take-profit, che resta attivo. Imposta a None per disabilitare (solo hard/ATR stop).
STEP_STOPS = [
    (0.005, -0.002),  # picco +0.5% → stop a entry −0.2% (break-even con cuscinetto anti-rumore)
    (0.015,  0.010),  # picco +1.5% → blocca +1.0%
    (0.030,  0.020),  # picco +3.0% → blocca +2.0%
]

# === TIMING (ET) ===
WATCHLIST_TIME       = "09:25"
ENTRY_TIME           = "09:35"
MONITORING_INTERVAL  = 30        # seconds between position checks (30s: più reattivo su stop/gradini vs i 60s iniziali)
EOD_CLOSE_TIME       = "15:45"

# === DATA QUALITY / ROBUSTEZZA ===
PRICE_MAX_AGE_S      = 120   # età massima dell'ultimo trade IEX per i check degli stop
EOD_CLOSE_ATTEMPTS   = 3     # tentativi di chiusura per posizione alle 15:45
RECONCILE_MISS_LIMIT = 2     # cicli consecutivi di assenza prima di rimuovere una posizione

# === ORDER FILL CONFIRMATION ===
# L'ask IEX stantio può tenere il limit order pendente per minuti (12 giugno: 2m18s).
# La posizione viene creata SOLO dopo fill confermato; allo scadere del timeout
# l'ordine viene cancellato (fill parziale → si tiene la qty eseguita).
FILL_CONFIRM_TIMEOUT_S = 240     # attesa massima fill del limit order
FILL_POLL_INTERVAL_S   = 5       # intervallo polling stato ordine

# === CHIUSURA POSIZIONE ===
# Il prezzo di uscita REALE è filled_avg_price dell'ordine di chiusura, letto SOLO quando
# l'ordine è COMPLETAMENTE eseguito (filled_qty >= qty), esattamente come per le entry.
# Leggerlo al primo prezzo non-nullo registra la media di un fill PARZIALE; ripiegare
# troppo presto su snapshot/quote registra un prezzo di mercato che NON è il fill reale —
# entrambi falsano il PnL (15 giu: AMD 548.12 da snapshot vs 547.81 reale; 18 giu: AMAT
# 614.60 snapshot vs 614.71 reale, MRVL 307.26 vs 307.24, INTC 133.5715 parziale vs
# 133.5766 finale). 10×1s = 10s: ampi per il lag di pubblicazione del fill su paper trading.
CLOSE_FILL_POLL_ATTEMPTS   = 10  # tentativi di lettura del fill (attende il fill COMPLETO)
CLOSE_FILL_POLL_INTERVAL_S = 1   # secondi tra i tentativi

# === GENERAL ===
MAX_CANDIDATES_TO_LLM  = 15
TIMEZONE               = "America/New_York"
PAPER_INITIAL_EQUITY   = 100_000   # saldo iniziale paper — per calcolo P&L cumulativo
