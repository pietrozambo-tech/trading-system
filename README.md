# Trading System — Automated Intraday Gap-and-Go

Automated day trading system for US equities. Runs daily on Railway via cron job, uses Alpaca for execution, Claude AI for trade selection, and Telegram for end-of-day reporting.

---

## Overview

**Strategy:** Gap-and-Go intraday momentum (long only)  
**Universe:** 57 large-cap US equities across 10 sectors  
**Max trades/day:** 2  
**Position size:** $45,000/trade (on $100k paper account)  
**Session:** US market hours, all times Eastern Time (ET)

---

## Daily Timeline

| Time (ET) | Action |
|-----------|--------|
| 09:25 | Pre-market scan — build watchlist from universe |
| 09:45 | Apply L1 binary filters → compute L2 signals → LLM decision |
| 09:47 | Place orders |
| Every 5 min | Monitor open positions (stop loss / take profit checks) |
| 15:45 | Force-close all remaining positions |
| 16:05 | Send EOD recap via Telegram |

---

## Pipeline: 4-Stage Filter

```
Universe (57 tickers)
       │
       ▼ Stage 1 — Pre-market scan (09:25 ET)
       │  Gap > +0.2% AND pre-market volume > 150% of 10-day avg
       │
       ▼ Stage 2 — L1 Binary filters (09:45 ET)
       │  Price ≥ $5, ADV > 1M shares, bid-ask spread < 0.6%
       │  Asset tradable on Alpaca, no earnings tonight
       │  SPY not down more than -1.8% (market circuit breaker)
       │
       ▼ Stage 3 — L2 Signal scoring
       │  S1 VWAP position, S2 Opening Range, S3 Gap Retention, S4 Volume Boost
       │  Confidence score ≥ 0.65
       │
       ▼ Stage 4 — LLM (Claude AI)
          Final selection of up to 2 trades with reasoning
```

---

## Stage 1 — Pre-Market Scan

Runs at **09:25 ET** across the full 57-ticker universe.

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Min gap | +0.2% | Long-only; filters out flat/negative pre-market |
| Min pre-market volume | 150% of 10-day pre-market avg | Confirms above-average catalyst-driven activity |
| Volume comparison window | 04:00–09:25 ET | Apples-to-apples vs historical pre-market window |
| Lookback for avg | 10 trading days | Rolling baseline, weekends excluded |

---

## Stage 2 — L1 Binary Filters

Applied at **09:45 ET** after the opening range forms.

| Filter | Threshold | Why |
|--------|-----------|-----|
| Min price | ≥ $5.00 | Avoid penny stock volatility |
| Min ADV | > 1,000,000 shares | Liquidity — ensures fills at market |
| Bid-ask spread | < 0.6% | Entry friction cap |
| Alpaca tradability | Must be active | Avoid halted/delisted stocks |
| Earnings tonight | Excluded | Overnight gap risk too high |
| SPY daily change | > -1.8% | Market-wide risk-off circuit breaker |

---

## Stage 3 — L2 Signal Scoring

Four signals computed from 09:30–09:45 ET data.

### Signals

| Signal | Condition | Contribution |
|--------|-----------|-------------|
| **S1** — VWAP position | Price > VWAP at 09:45 | Binary: yes/no |
| **S2** — Opening Range position | Price in top 66%+ of 09:30–09:45 range | Binary: yes/no |
| **S3** — Gap retention | Current price ≥ 70% of opening gap vs prev close | Binary: yes/no |
| **S4** — Volume boost | 1-min volume vs historical 09:30–09:45 avg | +0.10 (>3×), +0.05 (2–3×), +0.00 (<2×) |

### Catalyst Multiplier (from news)

Classified automatically from Alpaca/Benzinga news headlines:

| Tier | Examples | Multiplier |
|------|----------|-----------|
| Tier 1 | Earnings beat >5%, FDA approval, confirmed M&A, broker upgrade | ×1.00 |
| Tier 2 | Earnings beat modest, Fed speak, Trump tweet on sector, price target raise, insider buying | ×0.80 |
| Tier 3 | Unconfirmed rumor, speculative article, sector sentiment | ×0.55 |
| None | No identifiable news | ×0.30 |

### Confidence Formula

```
direction_score = sum([above_vwap, or_position > 0.66, gap_retention > 0.70])  # 0–3

confidence = min((direction_score / 3 × catalyst_multiplier) + volume_boost, 1.0)
```

**Threshold:** confidence ≥ 0.65 required to proceed to LLM stage.

---

## Stage 4 — LLM Decision (Claude AI)

Up to `MAX_CANDIDATES_TO_LLM` candidates sent to Claude with:
- All computed signals and confidence scores
- Recent news headlines
- SPY context for the day

Claude returns a JSON response selecting 0, 1, or 2 trades with reasoning. It is instructed to:
- Only go long
- Not force trades if confidence is weak
- Penalise if both trades are in the same GICS sector

---

## Position Sizing

| Parameter | Value |
|-----------|-------|
| Position size | $45,000/trade |
| Max simultaneous positions | 2 |
| Max daily capital deployed | $90,000 (90% of $100k) |

With $5k real money (20:1 scale from paper), each trade = $2,250.

---

## Exit Strategy

Exits are checked every **5 minutes** during the session, in priority order:

| Priority | Rule | Trigger |
|----------|------|---------|
| 1 | **Dollar stop** | Loss ≥ $2,025 on the position |
| 2 | **Hard blocker** | Price drops ≥ 4.5% from entry |
| 3 | **ATR stop** | Price drops ≥ 1.5 × ATR14 from entry |
| 4 | **VWAP exit** | Price crosses below VWAP AND profit ≥ 2.5% (takes profit, not a stop) |
| 5 | **EOD close** | Hard close at 15:45 ET regardless |

The effective stop price is `entry × (1 - max(4.5%, ATR14×1.5/entry))` capped by the $2,025 dollar stop.

### VWAP Exit Threshold

The 2.5% minimum profit requirement was calibrated via sensitivity analysis on 6 months of backtest data:

| VWAP threshold | Avg Win/Loss ratio |
|----------------|--------------------|
| 1.0% | 0.48 |
| 1.5% | 0.71 |
| 2.0% | 0.89 |
| **2.5%** | **1.12** ← selected |
| 3.0% | 1.09 |

---

## Ticker Universe (57 tickers)

| Sector | Tickers |
|--------|---------|
| Tech / Growth | AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AMD, NFLX, CRM, ORCL, ADBE, INTC, QCOM, MU, AVGO, TXN, AMAT |
| Finance | JPM, BAC, GS, MS, C, WFC, BLK, SCHW |
| Healthcare | UNH, JNJ, PFE, ABBV, MRK, BMY |
| Energy | XOM, CVX, SLB, HAL, OXY |
| Airlines / Cruises | DAL, AAL, NCLH, CCL |
| Space | RKLB, ASTS, BKSY, RDW, LUNR |
| Nuclear / Uranium | UUUU, CCJ, NNE, SMR |
| Quantum Computing | IONQ, QBTS, QUBT, RGTI |
| ETF | SPY, QQQ, IWM |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11 |
| Market data & execution | [Alpaca Trade API](https://alpaca.markets) (paper account, IEX feed) |
| AI analysis | [Anthropic Claude API](https://anthropic.com) (claude-sonnet-4-6) |
| News | Alpaca News API (Benzinga) |
| Hosting / cron | [Railway](https://railway.app) — cron `0 13 * * 1-5` UTC |
| Notifications | Telegram Bot API |
| CI | GitHub Actions |

---

## Project Structure

```
trading-system/
├── main.py                  # Orchestrator — daily pipeline entry point
├── config.py                # All parameters and thresholds
├── data/
│   └── fetcher.py           # All Alpaca API calls (with retry logic)
├── signals/
│   ├── eligibility.py       # L1 filters (pre-market scan + binary)
│   └── triggers.py          # L2 signal scoring
├── llm/
│   └── analyst.py           # Claude API calls (trade selection + EOD recap)
├── execution/
│   └── trader.py            # Order placement and position monitoring
├── notify/
│   └── telegram.py          # EOD Telegram message
├── backtest/
│   └── engine.py            # Backtesting engine (bulk fetch, sensitivity analysis)
└── logs/
    └── YYYY-MM-DD.json      # Daily pipeline log (auto-generated, gitignored)
```

---

## Configuration Reference (`config.py`)

```python
# Pre-market scan
MIN_PREMARKET_GAP           = 0.002   # +0.2%
MIN_PREMARKET_VOL_RATIO     = 1.5     # 150% of 10-day avg
PREMARKET_VOL_LOOKBACK      = 10      # days

# L1 filters
SPY_BLOCK_THRESHOLD         = -0.018  # -1.8%
MIN_PRICE                   = 5.0
MIN_ADV                     = 1_000_000
MAX_SPREAD_PCT              = 0.006   # 0.6%

# L2 signals
OR_POSITION_THRESHOLD       = 0.66
GAP_RETENTION_THRESHOLD     = 0.70
CONFIDENCE_THRESHOLD        = 0.65

# Catalyst multipliers
CATALYST_TIER1              = 1.00
CATALYST_TIER2              = 0.80
CATALYST_TIER3              = 0.55
CATALYST_NONE               = 0.30

# Position sizing
POSITION_SIZE_USD           = 45_000
MAX_POSITIONS               = 2

# Exit rules
HARD_BLOCKER_PCT            = 0.045   # -4.5%
MAX_LOSS_PER_TRADE_USD      = 2_025   # dollar stop
ATR_MULTIPLIER              = 1.5
ATR_LOOKBACK                = 14
VWAP_EXIT_MIN_PROFIT_PCT    = 0.025   # 2.5%
MAX_DAILY_LOSS_USD          = None    # disabled

# Timing (ET)
WATCHLIST_TIME              = "09:25"
ENTRY_TIME                  = "09:45"
ORDER_TIME                  = "09:47"
EOD_CLOSE_TIME              = "15:45"
TELEGRAM_NOTIFY_TIME        = "16:05"

# Account
PAPER_INITIAL_EQUITY        = 100_000
```

---

## Environment Variables (Railway Secrets)

| Variable | Description |
|----------|-------------|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets/v2` |
| `ANTHROPIC_API_KEY` | Anthropic (Claude) API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |

---

## Daily Log Format

Each session saves a structured log to `logs/YYYY-MM-DD.json`:

```json
{
  "date": "2026-05-29",
  "spy_pct": 0.0042,
  "blocked": null,
  "pipeline": [
    { "stage": "universe",          "count": 57, "tickers": ["AAPL", "..."] },
    { "stage": "premarket_scan",    "count": 4,  "tickers": ["NVDA", "TSLA", "AMD", "IONQ"] },
    { "stage": "binary_filters_L1", "count": 3,  "tickers": ["NVDA", "TSLA", "AMD"] },
    { "stage": "L2_signals_passed", "count": 2,  "tickers": ["NVDA", "TSLA"] }
  ],
  "signals": [ "..." ],
  "llm_input":  ["NVDA", "TSLA"],
  "llm_output": { "trade_1": { "ticker": "NVDA", "..." }, "trade_2": null },
  "trades": [ "..." ]
}
```
