# Automated Trading System — Gap-and-Go

A fully automated day trading bot for US stocks. Every weekday morning it wakes up, scans the market, picks the best 1-2 trades, executes them, manages risk throughout the day, and sends a summary to Telegram at the end of the session.

No manual intervention needed.

---

## What it does, step by step

### 1. Pre-market scan — 9:25 AM New York time

The bot scans all 57 stocks looking for ones **gapping up at least +0.5%** above yesterday's close. That's the only filter here — a meaningful overnight move signals that something happened (earnings, news, an upgrade) worth investigating further. Stocks that drifted up 0.2% on no news don't qualify.

Pre-market volume is intentionally not filtered here — it's noisy and unreliable in thin pre-market hours. Volume gets measured properly in Stage 3 using the first 10 minutes of real market trading.

### 2. Quality check — 9:40 AM

Once the market opens and the first 10 minutes settle, the bot applies a set of hard filters to remove anything that doesn't meet the bar:

| Check | Requirement | Why |
|-------|-------------|-----|
| Price | At least $5 | Avoid erratic penny stocks |
| Daily volume | >1 million shares on average | Makes sure we can buy and sell without moving the price |
| Bid-ask spread | <0.6% | Entry cost too high otherwise |
| Earnings tonight | Excluded | Overnight risk is unpredictable — but stocks that *already* reported earnings yesterday are kept, as that's the catalyst we want |
| Market mood | SPY not down >1.8% | Don't trade against a falling market |

### 3. Signal scoring

For each stock that passes the quality check, the bot scores 4 signals based on what happened in the first 10 minutes of trading:

| Signal | What it means | How it's calculated |
|--------|--------------|---------------------|
| **VWAP position** | Are buyers in control right now? | VWAP (Volume Weighted Average Price) is the average price of every trade so far, weighted by how many shares were traded at each price. If the current price is above it, most people who traded today are sitting on a profit — a sign of strength. Computed from all 1-minute bars since 9:30. |
| **Opening range position** | Is the stock pushing toward the top of its early range, not the bottom? | Take the highest and lowest price between 9:30 and 9:40. Calculate where the current price sits within that range as a percentage (0% = at the low, 100% = at the high). We require ≥66% — meaning the stock is in the upper third. |
| **Gap retention** | Is the pre-market gap holding, or is it already being sold off? | Compare the size of the gap at open (today's open minus yesterday's close) with how much of it has been "eaten" by sellers during the first 10 minutes (measured by how far the price dipped from the open). We require ≥70% of the gap still intact. |
| **Volume boost** | Is today unusually active in the first 10 minutes? | Total shares traded 9:30–9:40 today, divided by the average of the same 9:30–9:40 window over the past 20 trading days. >3× average = +0.10 bonus, 2–3× = +0.05, below 2× = no bonus. |

These combine into a **confidence score** between 0 and 1. Only stocks scoring 0.65 or above go to the next step.

The confidence score also factors in a **catalyst bonus** — an additive bump based on how strong the underlying news is.

| News quality | Bonus |
|-------------|-------|
| Major catalyst (FDA approval, confirmed acquisition, strong earnings beat >5%) | +0.30 |
| Real but moderate news (earnings beat, analyst upgrade, insider buying) | +0.20 |
| Rumour or speculative article | +0.10 |
| No news — pure technical setup | +0.00 |

**The formula:**

```
confidence = (signals_passed / 3) + catalyst_bonus + volume_bonus
```

Where `signals_passed` is how many of the first 3 binary signals are true, `catalyst_bonus` is the additive news bonus above, and `volume_bonus` is +0.10 if volume is >3× average, +0.05 if 2–3×, zero otherwise.

This means **2 out of 3 technical signals (0.667) is enough to pass on its own**, even with no news. Strong news and volume push the score higher and help prioritise between multiple candidates.

### 4. AI decision

The top candidates (with all their signals and recent headlines) are sent to Claude (Anthropic's AI). Claude picks the best 1 or 2 trades and writes a short explanation for each. It's instructed to:
- Only go long (buy, not short)
- Skip the trade if it's not convinced — no forced trades
- Avoid picking two stocks from the same sector

### 5. Execution — 9:42 AM

Orders are placed via Alpaca (paper trading account). Position size is calculated live at order time using the formula:

```
position size = (current equity − $2,000) ÷ 2
```

The $2,000 is a permanent cash cushion that never gets invested — it covers fees, slippage, and acts as a last resort. Everything else is split equally between the two possible trades. The number of shares is always rounded down to whole shares. So on a $100k account: ($100,000 − $2,000) ÷ 2 = **$49,000 per trade**. If the account grows to $120k, each trade becomes $59,000 automatically.

### 6. Intraday monitoring

Every 5 minutes the bot checks each open position. It closes a trade if any of these triggers fires, checked in this exact order:

| Priority | Rule | Trigger | Why this rule exists |
|----------|------|---------|----------------------|
| 1 | **Hard stop** | Price falls ≥4.5% from entry | A fixed percentage floor. Simple, predictable, immune to data issues. With a $45k position, 4.5% = ~$2,025 max loss per trade. Always checked first. |
| 2 | **ATR stop** | Price falls ≥1.5× ATR14 from entry | ATR (Average True Range) measures how much a stock typically moves in a day over the past 14 days. Multiplying by 1.5 sets a stop that's "wider than normal noise" — so you don't get shaken out by ordinary volatility, only by a real move against you. On a calm stock (ATR = 1%) this stop is tighter than 4.5%; on a volatile one it might be looser. Whichever is higher (tighter) between rule 1 and rule 2 wins. |
| 3 | **VWAP take-profit** | Price drops below VWAP *and* profit ≥2.5% | This is a profit-protecting exit, not a stop loss. The idea: if the stock was running but has now fallen back below the average price of the day, momentum has likely shifted. The 2.5% minimum is there so we don't exit a trade that barely moved — we only lock in profit when there's real gain to protect. Calibrated via backtesting. |
| 4 | **End-of-day close** | 3:45 PM ET, no exceptions | We never hold overnight. Gaps at open, earnings after hours, macro news — too much can happen. Everything is flat before the close, every single day. |

The 2.5% minimum for the VWAP take-profit was chosen after testing different thresholds on 6 months of historical data — below that, it was cutting winners too early.

### 7. End-of-day recap — 4:05 PM

A Telegram message with a human-readable summary: market context, each trade's entry/exit/P&L, and the running account total.

---

## The numbers

| Parameter | Value |
|-----------|-------|
| Paper account size | $100,000 |
| Cash cushion (never invested) | $2,000 |
| Position size per trade | (equity − $2,000) ÷ 2, recalculated live each day |
| Example on $100k | ($100,000 − $2,000) ÷ 2 = $49,000/trade |
| Hard stop per trade | -4.5% from entry |
| VWAP take-profit threshold | 2.5% profit minimum |
| Real money equivalent (20:1 scale) | ~$2,450 per trade on $5k account |

---

## The watchlist (57 stocks)

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

## Tech stack

| What | How |
|------|-----|
| Market data & order execution | [Alpaca](https://alpaca.markets) (paper account, IEX data feed) |
| AI trade selection & recap | [Claude by Anthropic](https://anthropic.com) |
| News | Alpaca/Benzinga news API |
| Hosting & scheduling | [Railway](https://railway.app) — runs Mon–Fri at 9:00 AM ET |
| Notifications | Telegram |
| Code & CI | GitHub + GitHub Actions |

---

## Project layout

```
trading-system/
├── main.py              # Daily orchestrator — runs the full pipeline
├── config.py            # All parameters in one place
├── data/fetcher.py      # Everything that talks to Alpaca APIs
├── signals/
│   ├── eligibility.py   # Pre-market scan + L1 binary filters
│   └── triggers.py      # L2 signal scoring and confidence calculation
├── llm/analyst.py       # Claude API — trade selection and EOD recap
├── execution/trader.py  # Order placement, monitoring, stop logic
├── notify/telegram.py   # EOD Telegram message
├── backtest/engine.py   # Historical backtesting engine
└── logs/                # One JSON file per trading day (gitignored)
```

---

## Daily log

Every session saves a breakdown to `logs/YYYY-MM-DD.json` showing exactly how many stocks made it through each stage:

```json
{
  "date": "2026-05-29",
  "spy_pct": 0.0042,
  "blocked": null,
  "pipeline": [
    { "stage": "universe",          "count": 57 },
    { "stage": "premarket_scan",    "count": 4,  "tickers": ["NVDA", "TSLA", "AMD", "IONQ"] },
    { "stage": "binary_filters_L1", "count": 3,  "tickers": ["NVDA", "TSLA", "AMD"] },
    { "stage": "L2_signals_passed", "count": 2,  "tickers": ["NVDA", "TSLA"] }
  ],
  "llm_output": { "trade_1": { "ticker": "NVDA", "confidence": 0.82, "reason": "..." }, "trade_2": null },
  "trades": [ ... ]
}
```

---

## Environment variables (set in Railway)

| Variable | What it is |
|----------|-----------|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets/v2` |
| `ANTHROPIC_API_KEY` | Claude API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
