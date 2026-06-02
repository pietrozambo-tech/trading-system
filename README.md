# Automated Trading System — Gap-and-Go

A fully automated day trading bot for US stocks. Every weekday morning it wakes up, scans the market, picks the best 1-2 trades, executes them, manages risk throughout the day, and sends a summary to Telegram at the end of the session.

No manual intervention needed.

---

## What it does, step by step

### 1. Pre-market scan — 9:25 AM New York time

The bot scans all 57 stocks looking for ones **gapping up at least +0.5%** above yesterday's close. That's the only filter here — a meaningful overnight move signals that something happened (earnings, news, an upgrade) worth investigating further. Stocks that drifted up 0.2% on no news don't qualify.

Pre-market volume is intentionally not filtered here — it's noisy and unreliable in thin pre-market hours. Volume gets measured properly in Stage 3 using the first 5 minutes of real market trading.

### 2. Quality check — 9:35 AM

Once the market opens and the first 5 minutes settle, the bot applies a set of hard filters to remove anything that doesn't meet the bar:

| Check | Requirement | Why |
|-------|-------------|-----|
| Daily volume | At least 5M shares/day | A $50k order in a thinly traded stock moves the price. This filter keeps out names too small to absorb our position size without slippage. |
| Earnings tonight | Excluded | Overnight risk is unpredictable — but stocks that *already* reported earnings yesterday are kept, as that's the catalyst we want |
| Market mood | SPY not down >2.0% | Circuit breaker for real panic days only — strong individual setups still trade in a mildly negative market |

### 3. Signal scoring

For each stock that passes the quality check, the bot scores 4 signals based on what happened in the first 5 minutes of trading:

| Signal | What it means | How it's calculated |
|--------|--------------|---------------------|
| **VWAP position** | Are buyers in control right now? | VWAP (Volume Weighted Average Price) is the average price of every trade so far, weighted by how many shares were traded at each price. If the current price is above it, most people who traded today are sitting on a profit — a sign of strength. Computed from all 1-minute bars since 9:30. |
| **Opening range position** | Is the stock pushing toward the top of its early range, not the bottom? | Take the highest and lowest price between 9:30 and 9:35. Calculate where the current price sits within that range as a percentage (0% = at the low, 100% = at the high). We require ≥66% — meaning the stock is in the upper third. |
| **Gap retention** | Is the pre-market gap holding, or is it already being sold off? | Compare the size of the gap at open (today's open minus yesterday's close) with how much of it has been "eaten" by sellers during the first 5 minutes (measured by how far the price dipped from the open). We require ≥70% of the gap still intact. |
| **Volume boost** | Is today unusually active in the first 5 minutes? | Total shares traded 9:30–9:35 today, divided by the average of the same 9:30–9:35 window over the past 20 trading days. >3× average = +0.10 bonus, 2–3× = +0.05, below 2× = no bonus. |

The first three signals (VWAP, Opening Range, Gap Retention) are **binary and equally weighted** — each one is either true or false, and each contributes exactly 1/3 to the base score. Volume boost and catalyst are additive bonuses on top.

These combine into a **confidence score** between 0 and 1. Only stocks scoring 0.65 or above go to the next step.

```
confidence = (signals_passed / 3) + catalyst_bonus + volume_boost
```

| Component | Max contribution | Example |
|-----------|-----------------|---------|
| VWAP ✓ | +0.333 | Price above VWAP |
| Opening range ✓ | +0.333 | Price in top third |
| Gap retention ✓ | +0.333 | Gap still 70%+ intact |
| Catalyst bonus | +0.30 | Major earnings beat |
| Volume boost | +0.10 | Volume >3× average |
| **Theoretical max** | **1.43** (3/3 + 0.30 + 0.10) | Not capped — higher scores help LLM prioritise between multiple candidates |

Minimum to pass: **2 out of 3 signals** (0.667) with no news and no volume boost is already above the 0.65 threshold. Scores above 1.0 are valid and meaningful — a 1.3 beats a 1.0 when the LLM has to choose.

The confidence score also factors in a **catalyst bonus** — an additive bump based on how strong the underlying news is.

| News quality | Bonus |
|-------------|-------|
| **Tier 1** — Revenue beat, guidance raised, large EPS surprise (>10%), FDA approval, confirmed acquisition/merger | +0.30 |
| **Tier 2** — Modest EPS beat, analyst upgrade, price target raise, insider buying, Fed/macro news | +0.20 |
| **Tier 3** — Rumours, speculative articles, unconfirmed buzz | +0.10 |
| No news — pure technical setup | +0.00 |

The distinction between Tier 1 and Tier 2 matters: a **revenue beat** or **guidance raise** signals that the business is genuinely accelerating — the gap is likely to sustain. A modest EPS beat (which can come from cost cuts or buybacks) is real news but less likely to drive continuation throughout the day.

**The formula:**

```
confidence = (signals_passed / 3) + catalyst_bonus + volume_bonus
```

Where `signals_passed` is how many of the first 3 binary signals are true, `catalyst_bonus` is the additive news bonus above, and `volume_bonus` is +0.10 if volume is >3× average, +0.05 if 2–3×, zero otherwise.

This means **2 out of 3 technical signals (0.667) is enough to pass on its own**, even with no news. Strong news and volume push the score higher and help prioritise between multiple candidates.

### 4. AI decision

The top candidates — with their confidence scores, individual signal results, catalyst tier, and recent headlines — are sent to Claude (Anthropic's AI model). Claude reads the full picture for each and picks the best 1 or 2 trades.

**What Claude looks at:**
- Which of the 3 technical signals passed and the overall confidence score
- The catalyst: what news triggered the gap and its quality (revenue beat vs. EPS beat vs. rumour)
- Recent headlines for each stock
- The overall market tone that morning (SPY % change)
- How far the stock is from its 3-month high — stocks near their highs have less overhead resistance (context only, not a filter)
- **Post-open advance** — how much the stock moved between the 9:30 open and 9:35 entry. A positive value (+0.8%) means buyers pushed higher after the gap opened — real continuation momentum. Near zero means the stock is flat at the opening high, risking buying the peak. Negative means it was already fading at the time of entry. Claude uses this to distinguish "arrived early" setups from "late to the party" ones.

**What Claude decides:**
- Which 1 or 2 stocks to trade, or none if it's not convinced
- A short explanation for each pick, in Italian — this becomes the context line in the Telegram recap

**Rules Claude follows:**
- Only go long (buy, never short)
- No forced trades — if the setup isn't convincing, it passes
- Never pick two stocks from the same sector (e.g. two semis on the same day)

The AI step exists because the algorithmic score captures *whether* the signals are there, but not *which* setup has the clearest story. Two stocks can both score 1.1 — Claude reads the news and decides which one has the more credible catalyst behind it.

### 5. Execution — immediately after step 4

Orders are placed via Alpaca (paper trading account) as soon as the LLM returns its decision — no fixed delay. Position size is calculated live at order time using the formula:

```
position size = (current equity − $1,000) ÷ 2
```

The $1,000 is a permanent cash cushion that never gets invested — it covers fees, slippage, and acts as a last resort. Everything else is split equally between the two possible trades. The number of shares is always rounded down to whole shares. So on a $100k account: ($100,000 − $1,000) ÷ 2 = **$49,500 per trade**. If the account grows to $120k, each trade becomes $59,500 automatically.

### 6. Intraday monitoring

Every minute the bot checks each open position. It closes a trade if any of these triggers fires, checked in this exact order:

| Priority | Rule | Trigger | Why this rule exists |
|----------|------|---------|----------------------|
| 1 | **Hard stop** | Price falls ≥2.0% from entry | The absolute floor — simple, predictable, immune to data issues. On a ~$49.5k position, 2% = ~$990 max loss per trade. Always checked first. |
| 2 | **ATR stop** | Price falls ≥1× ATR14 from entry | ATR (Average True Range) measures how much a stock typically moves in a day over the past 14 days. Setting the stop exactly at ATR14 below entry means you exit if the move against you exceeds the stock's typical daily range — a signal that something is genuinely wrong, not just noise. On calm stocks (ATR ~1%) this fires at -1%, tighter than the hard stop. On volatile stocks (ATR >2%) the hard stop at -2% fires first. Whichever is tighter (higher price) wins. |
| 3 | **VWAP take-profit** | Price drops below VWAP *and* profit ≥1.5% | This is a profit-protecting exit, not a stop loss. If the stock was running but has now fallen back below the average price of the day, momentum has likely shifted. The 1.5% minimum is there so we don't exit a trade that barely moved — we only lock in profit when there's real gain to protect. |
| 4 | **End-of-day close** | 3:45 PM ET, no exceptions | We never hold overnight. Gaps at open, earnings after hours, macro news — too much can happen. Everything is flat before the close, every single day. |

The 1.5% minimum for the VWAP take-profit gives the condition room to fire: during a reversal VWAP lags the current price, so by the time price crosses below VWAP the profit has often already eroded — a 2.5% threshold was too tight and never fired in backtesting.

### 7. Daily recap

Two types of Telegram notifications are sent throughout the day:

- **System error alert** — if Railway stops the container mid-session (SIGTERM), the bot attempts to close all open positions and sends a single message with the outcome per position. Example:
  ```
  ⚠️ Errore di sistema — chiusura forzata:
  ✅ NVDA chiusa @ $221.40
  ```
  If the close fails: `❌ NVDA — chiusura fallita. Intervieni manualmente su Alpaca.` — so you always know whether to act.
- **Daily recap** — the full summary: market context, each trade's entry/exit/P&L, running account total.

The recap is sent **as soon as the last position closes** — whether that's a stop at 10:30 AM, a VWAP exit at 2:00 PM, or the forced 3:45 PM liquidation. No artificial delays. SPY performance is always measured at the exact moment the message is sent. On SIGTERM the recap uses the plain-text fallback format (no LLM generation — not enough time before Railway's SIGKILL).

**Example — 1 trade closed early via profit-taker:**
```
📈 Venerdì 29/5/2026

Mercato: SPY +0.42% — seduta tranquilla, indici in leggero rialzo.

Trade 1 — NVDA long [Score: 1.07]
Earnings beat Q1, gap ben difeso con volumi 3× la media.
  Entrata: $135.20
  Uscita:  $137.85 (Profit taker)
  P&L: +$959.30 (+1.96%)

Trade 2 — nessun secondo segnale valido.

Giornata:    +$959.30$
P&L totale:  +$959.30$
Saldo:       $100,959.30
```

**Example — 2 trades closed at end of day:**
```
📊 Venerdì 29/5/2026

Mercato: SPY +0.28% — chiusura piatta, nessuna direzionalità.

Trade 1 — NVDA long [Score: 1.07]
Gap retention all'82%, volumi forti, catalyst earnings.
  Entrata: $135.40
  Uscita:  $137.90 (Fine giornata)
  P&L: +$905.00 (+1.84%)

Trade 2 — TSLA long [Score: 0.87]
Setup tecnico pulito, prezzo sopra VWAP in apertura.
  Entrata: $318.50
  Uscita:  $315.80 (Fine giornata)
  P&L: -$413.10 (-0.85%)

Giornata:    +$491.90$
P&L totale:  +$491.90$
Saldo:       $100,491.90
```

**Example — no trade (SPY too negative):**
```
📊 Venerdì 29/5/2026

Mercato: SPY -2.31% — giornata negativa.

Nessun trade. Mercato bloccato — SPY troppo negativo. Riproviamo domani.

Giornata:    +$0.00$
P&L totale:  +$0.00$
Saldo:       $100,000.00
```

The trade header (`Trade N — TICKER long [Score: X.XX]`) is **bold** in Telegram. The score is the algorithmic confidence from signal scoring, uncapped — a score of 1.07 means all 3 technical signals passed (1.0) plus a Tier 3 catalyst bonus (+0.10) with no volume boost. Maximum theoretical score is 1.43.

---

## The numbers

| Parameter | Value |
|-----------|-------|
| Paper account size | $100,000 |
| Cash cushion (never invested) | $1,000 |
| Position size per trade | (equity − $1,000) ÷ 2, recalculated live each day |
| Example on $100k | ($100,000 − $1,000) ÷ 2 = $49,500/trade |
| Hard stop per trade | -2.0% from entry (~$990 on $49.5k position) |
| ATR stop per trade | -1× ATR14 from entry (tighter than hard stop on low-vol stocks) |
| VWAP take-profit threshold | 1.5% profit minimum |

---

## The watchlist (60 stocks)

| Sector | Tickers |
|--------|---------|
| Tech / Growth | AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AMD, NFLX, CRM, ORCL, ADBE, INTC, QCOM, MU, AVGO, TXN, AMAT, MRVL |
| Finance | JPM, BAC, GS, MS, C, WFC, BLK, SCHW |
| Healthcare | UNH, JNJ, PFE, ABBV, MRK, BMY, MRNA |
| Energy | XOM, CVX, SLB, HAL, OXY |
| Clean Energy | ENPH |
| Consumer | NKE |
| Defense | LMT |
| Crypto Proxy | MSTR |
| Airlines / Cruises | DAL, AAL, NCLH, CCL |
| Space | RKLB, ASTS, BKSY, RDW, LUNR |
| Nuclear / Uranium | UUUU, CCJ, NNE, SMR |
| Quantum Computing | IONQ, QBTS, QUBT, RGTI |

---

## Tech stack

| What | How |
|------|-----|
| Market data & order execution | [Alpaca](https://alpaca.markets) (paper account, SIP consolidated data feed) |
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

## Next steps

Ideas discussed and parked — revisit when there's time.

### Short interest signal
Stocks with high short interest (15%+) that receive a positive catalyst don't just gap — they can squeeze: short sellers are forced to cover, amplifying the move. This is one of the most powerful momentum multipliers for gap-and-go setups. Adding short float as a bonus to the confidence score (or as context for Claude) could meaningfully improve signal quality.

Requires an external data source (Finviz, IEX Cloud, or similar) since Alpaca doesn't provide short interest data.

### 2 vs 3 max positions
The capital deployed is the same regardless: 2 × $49.5k or 3 × $33k both put $99k to work. The question is whether the 3rd-best setup on a given day is genuinely good or just marginal. The daily log now records `passes_threshold` per ticker — after a few weeks of data, count how many days had 3+ viable candidates above the confidence threshold and decide from there.

### Entry timing — implemented (June 2026)
Backtested entry times at 9:31, 9:33, 9:35, and 9:40 over the full 2025–2026 universe. Both oracle (signals computed at 9:40, entries at earlier times) and non-oracle (signals computed from bars available at entry time only) variants were run.

Non-oracle results (profit factor, YTD 2025–2026):

| Entry | PF | Notes |
|-------|----|-------|
| 9:31 | 1.05 | Very thin signal — 1 bar, noisy |
| 9:33 | 1.18 | Improving but OR still unreliable |
| **9:35** | **1.37** | Best: captures opening momentum, full 5-min OR |
| 9:40 | 1.21 | Baseline — later entry means inflated price |

**Decision: entry changed to 9:35.** The 5-minute opening range is a well-established institutional reference and it captures more of the initial move while retaining enough bars to compute reliable VWAP and gap retention signals.

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
