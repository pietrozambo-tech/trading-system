# Automated Trading System — Gap-and-Go

A fully automated day trading bot for US stocks. Every weekday morning it wakes up, scans the market, picks the best 1-2 trades, executes them, manages risk throughout the day, and sends a summary to Telegram at the end of the session.

No manual intervention needed.

---

## The universe: 60 curated stocks

The bot doesn't scan the entire US stock market — it operates on a fixed watchlist of **60 hand-picked stocks**. Scanning thousands of tickers is technically possible but strategically counterproductive.

**Why a curated universe instead of the full market:**

- **Institutional volume.** Every stock on the list trades at least 5 million shares per day on average. A $50k position doesn't move the price, and the bid-ask spread is tight enough that slippage is negligible. Thinly traded names are excluded entirely.
- **Sectors with momentum.** Tech (semis, cloud, AI), healthcare, energy, financials, space, and nuclear — sectors with active institutional participation and frequent catalyst-driven moves. Consumer staples and utilities are intentionally excluded: they gap rarely, and when they do, the moves are small.
- **Pipeline speed.** With 60 stocks, the full pre-market scan completes in under 90 seconds. Scaling to 5,000 stocks would require hours of API calls — far past the 9:25 AM window when pre-market data is still relevant.

The complete list is in the [Appendix — Watchlist (60 stocks)](#the-watchlist-60-stocks).

---

## What it does, step by step

### 1. Pre-market scan — 9:25 AM New York time

The bot scans all 60 stocks looking for ones **gapping up at least +0.5%** above yesterday's close. That's the only filter here — a meaningful overnight move signals that something happened (earnings, news, an upgrade) worth investigating further. Stocks that drifted up 0.2% on no news don't qualify.

Pre-market prices are fetched from **Yahoo Finance** (primary), with Alpaca IEX as fallback. Yahoo Finance aggregates prints from all exchanges (NYSE, NASDAQ, CBOE, etc.), giving full pre-market coverage. Alpaca's free IEX feed only sees ~15–20% of pre-market volume — relying on it alone would miss gappers that trade on other venues.

Pre-market volume is intentionally not filtered here — it's noisy and unreliable in thin pre-market hours. Volume gets measured properly in Stage 3 using the first 5 minutes of real market trading.

### 2. Quality check — 9:35 AM

Once the market opens, the bot waits exactly 5 minutes before running any filters. Backtesting over 18 months showed that 9:35 is the sweet spot: enough price action for reliable signals (VWAP, gap retention, opening range), while still catching most of the opening move. Earlier entries generate too many false signals; waiting until 9:40 misses the best entries.

The bot then applies a set of hard filters to remove anything that doesn't meet the bar:

| Check | Requirement | Why |
|-------|-------------|-----|
| Daily volume | At least 5M shares/day | A $50k order in a thinly traded stock moves the price. This filter keeps out names too small to absorb our position size without slippage. |
| Earnings tonight | Excluded | Overnight risk is unpredictable — but stocks that *already* reported earnings yesterday are kept, as that's the catalyst we want |
| Market mood | SPY not down >2.0% | Circuit breaker for real panic days only — strong individual setups still trade in a mildly negative market |

### 3. Signal scoring

For each stock that passes the quality check, the bot scores 4 signals based on what happened in the first 5 minutes of trading:

| Signal | What it means | How it's calculated |
|--------|--------------|---------------------|
| **Post-open advance** | Did the stock actually move up in the first 5 minutes? | Compare the 9:35 price (last bar close) to the 9:30 open. True if the stock is higher than where it opened — confirming that the gap led to real continuation, not an immediate fade. |
| **Opening range position** | Is the stock pushing toward the top of its early range, not the bottom? | Take the highest and lowest price between 9:30 and 9:35. Calculate where the current price sits within that range as a percentage (0% = at the low, 100% = at the high). We require ≥66% — meaning the stock is in the upper third. |
| **Gap retention** | Is the open gap holding, or is it already being sold off? | Compare the size of the gap at open (today's open minus yesterday's close) with how much of it has been "eaten" by sellers during the first 5 minutes (measured by how far the price dipped from the open). We require ≥70% of the gap still intact. |
| **Volume boost** | Is today unusually active in the first 5 minutes? | Total shares traded 9:30–9:35 today, divided by the average of the same 9:30–9:35 window over the past 20 trading days. >3× average = +0.10 bonus, 2–3× = +0.05, below 2× = no bonus. |

Before these four signals are computed, there is an additional **pre-open gate**: if less than 50% of the pre-market gap is still intact at the 9:30 open, the stock is excluded immediately. A stock gapping +5% pre-market but opening at only +1% has already lost most of its momentum before the market even opened — that is not a gap-and-go setup, and entering it would mean chasing a faded move.

The first three signals (Post-open advance, Opening Range, Gap Retention) are **binary and equally weighted** — each one is either true or false, and each contributes exactly 1/3 to the base score. Volume boost and catalyst are additive bonuses on top.

These combine into a **confidence score**. Only stocks scoring 0.65 or above go to the next step.

```
confidence = (signals_passed / 3) + catalyst_bonus + volume_boost + short_squeeze_bonus
```

| Component | Max contribution | Example |
|-----------|-----------------|---------|
| Post-open advance ✓ | +0.333 | Price at 9:35 > open at 9:30 |
| Opening range ✓ | +0.333 | Price in top third |
| Gap retention ✓ | +0.333 | Gap still 70%+ intact |
| Catalyst bonus | +0.30 | Major earnings beat |
| Volume boost | +0.10 | Volume >3× average |
| Short squeeze bonus | +0.10 | Short float >15% and (catalyst present or pre-market gap ≥10%) |
| **Theoretical max** | **1.53** (3/3 + 0.30 + 0.10 + 0.10) | Not capped — higher scores help LLM prioritise between multiple candidates |

Minimum to pass: **2 out of 3 signals** (0.667) with no news and no volume boost is already above the 0.65 threshold. Scores above 1.0 are valid and meaningful — a 1.3 beats a 1.0 when the LLM has to choose.

The confidence score factors in two additive bonuses on top of the binary signals:

**Catalyst bonus** — based on the strength of the underlying news:

| News quality | Bonus |
|-------------|-------|
| **Tier 1** — Revenue beat, guidance raised, large EPS surprise (>10%), FDA approval, confirmed acquisition/merger | +0.30 |
| **Tier 2** — Modest EPS beat, analyst upgrade, price target raise, insider buying, confirmed partnership | +0.20 |
| **Tier 3** — Rumours, speculative articles, unconfirmed buzz | +0.10 |
| No news — pure technical setup | +0.00 |

The distinction between Tier 1 and Tier 2 matters: a **revenue beat** or **guidance raise** signals that the business is genuinely accelerating — the gap is likely to sustain. A modest EPS beat (which can come from cost cuts or buybacks) is real news but less likely to drive continuation throughout the day.

**Short squeeze bonus** — applied when short float >15% (data from FINRA biweekly reports via Yahoo Finance) and at least one of:
- A catalyst is present (Tier 1, 2, or 3) — news forces short sellers to cover
- Pre-market gap ≥10% — at that magnitude the covering is already happening regardless of whether the news scraper identified a catalyst; the price action is the signal

The bonus is +0.10 and never stacks — it's either on or off, regardless of how many conditions fire simultaneously.

This means **2 out of 3 technical signals (0.667) is enough to pass on its own**, even with no news. Strong news, high volume, and a squeeze setup push the score higher and help prioritise between multiple candidates.

### 4. AI decision

The top candidates — with their confidence scores, individual signal results, catalyst tier, and recent headlines — are sent to Claude (Anthropic's AI model). Claude reads the full picture for each and picks the best 1 or 2 trades.

**What Claude looks at:**
- Which of the 3 technical signals passed and the overall confidence score
- The catalyst: what news triggered the gap and its quality (revenue beat vs. EPS beat vs. rumour)
- Recent news for each stock — up to 5 articles, each with headline and full summary
- The overall market tone that morning (SPY % change)
- How far the stock is from its 3-month high — stocks near their highs have less overhead resistance (context only, not a filter)
- **Post-open advance** — how much the stock moved between the 9:30 open and 9:35 entry. A positive value (+0.8%) means buyers pushed higher after the gap opened — real continuation momentum. Near zero means the stock is flat at the opening high, risking buying the peak. Negative means it was already fading at the time of entry. Claude uses this to distinguish "arrived early" setups from "late to the party" ones.
- **Short float** — the percentage of shares sold short. When combined with a catalyst, Claude knows the move could be amplified by forced short covering. Presented as raw context so Claude can weigh it against the catalyst strength.

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

**Order type: limit, not market (paper-trading specific).** Alpaca paper simulates market orders at the IEX ask, which at 9:35 can still be the stale pre-market quote, +1–2% above the real market (observed June 10: AMD filled ~$2–3 above the actual price). The entry is therefore a limit buy at `price_935 × 1.005` — the 9:34 bar close (real trade data) plus 0.5% of room. This caps the fill at the real market price.

**The position exists only after the fill is confirmed.** A limit order can sit unfilled for minutes while the stale IEX ask stays above our limit (June 12: 2m18s to fill). The bot polls the order status every 5 seconds for up to 4 minutes (`FILL_CONFIRM_TIMEOUT_S`); the position — entry price, stops, monitoring — is created exclusively from the confirmed fill. If the deadline expires the order is cancelled: a partial fill is kept with the actual executed quantity, a zero fill skips the trade entirely (with a Telegram alert). No phantom positions, no orphan orders left alive until the close.

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

**Example — 1 trade, profit on VWAP exit:**
```
📊 Venerdì 29 maggio 2026

Mercato: SPY +0.42% — mercato positivo

Trade 1 — NVDA long [Score: 1.07]
Gap pre-market confermato, earnings beat Q1
  Entrata: $135.20
  Uscita:  $137.85 (VWAP take-profit)
  P&L: +$959.30 (+1.96%)

Giornata: +$959.30 (+0.96%)
P&L totale: +$959.30 (+0.96%)
Saldo: $100,959.30
```

**Example — 2 trades, end of day:**
```
📊 Venerdì 29 maggio 2026

Mercato: SPY +0.28% — mercato positivo

Trade 1 — NVDA long [Score: 1.07]
Gap pre-market confermato, volumi forti
  Entrata: $135.40
  Uscita:  $137.90 (Fine giornata)
  P&L: +$905.00 (+1.84%)

Trade 2 — TSLA long [Score: 0.87]
Setup tecnico pulito, prezzo sopra VWAP in apertura
  Entrata: $318.50
  Uscita:  $315.80 (Fine giornata)
  P&L: -$413.10 (-0.85%)

Giornata: +$491.90 (+0.49%)
P&L totale: +$491.90 (+0.49%)
Saldo: $100,491.90
```

**Example — no trade (SPY too negative):**
```
📊 Martedì 2 giugno 2026

Mercato: SPY -2.31% — mercato in calo

Nessun trade. Mercato bloccato — SPY troppo negativo. Riproviamo domani.

Giornata: +$0.00 (+0.00%)
P&L totale: +$0.00 (+0.00%)
Saldo: $100,000.00
```

The trade header (`Trade N — TICKER long [Score: X.XX]`) is **bold** in Telegram. The score is the algorithmic confidence, uncapped — 1.07 means all 3 signals passed plus a Tier 3 catalyst bonus. The context line describes setup and catalyst only; the exit reason appears once on the Uscita line.

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

## Appendix — The watchlist (60 stocks)

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

Every session saves a breakdown to `logs/YYYY-MM-DD.json`. When `GITHUB_LOG_TOKEN` is set in Railway, the file is also pushed directly to this repository after each session — so logs accumulate in `logs/` on GitHub and can be reviewed or analysed at any time.

The JSON contains:

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

## Live trading — first week (June 2026)

The system went live on **June 4, 2026** (paper trading). Notes from the first two sessions:

**June 4 — UNH long, +$223.93 (+0.45%)**
UNH gapped +3.1% on multiple analyst upgrades (KeyBanc, BofA PT $450). Confidence 1.30 — all three signals, Tier 2 catalyst, vol boost. Three bugs surfaced and were fixed same day:

- **False manual-close detection:** Alpaca paper trading can take up to 3 minutes to reflect a fill in `get_all_positions()`. The first monitoring cycle (60s after entry) found the position "missing" and removed it from tracking. Fix: 3-minute grace period before reconciliation runs on any newly opened position.
- **Wrong entry price in log:** After placing a market order, the bot waited only 1 second for `filled_avg_price` via API — not enough. The actual fill ($397.35) was logged as the pre-order IEX ask ($396.00), so the stop was anchored to the wrong price. Fix: retry up to 5 times (1s, 2s, 3s, 4s, 5s) before falling back to the IEX estimate.
- **Telegram 400 error:** LLM-generated `no_trade_reason` text contained `(0.57 < 0.70)` — the `<` character breaks Telegram's HTML parse mode. Fix: `html.escape()` on all LLM-generated text before sending.

**June 5 — no trade**
SPY −0.58%, only RDW (+3.0%) and SMR (+2.2%) in pre-market. Both failed L2 — SMR gap fully reversed at open (gap_retention −1.6), RDW had insufficient IEX bar data. Two structural improvements shipped:

- **Pre-market data source:** switched from Alpaca IEX (15–20% of volume) to Yahoo Finance as primary source, IEX as fallback. Yahoo aggregates all exchanges, eliminating the risk of missing gappers that trade on non-IEX venues.
- **Pre-open gate:** new filter in `compute_signals` — if less than 50% of the pre-market gap is intact at the 9:30 open, the ticker is excluded before L2 signals are computed. Catches setups where the gap faded in the minutes before the open.

---

## Next steps

Ideas discussed and parked — revisit when there's time.

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

### ADV + OR: segnali troppo correlati — revisione in corso (giugno 2026)

**Osservazione emersa il 8 giugno 2026:** 19 candidati con gap significativi (INTC +12%, MRVL +8.6%, MU +8%, AMD +4.8%, NVDA +2.7% ecc.), 0 passano L2. Il motivo: `ADV=✗` su tutti e 19 — ogni titolo alle 9:34 era sotto l'apertura delle 9:30. Post-analisi: INTC e AMD hanno rimbalzato dopo i 5 minuti e avrebbero generato trade validi.

**Il problema strutturale:** `post_open_advance` e `OR position` misurano entrambi lo stesso istante (close del bar delle 9:34) visto da angolazioni diverse. Sono altamente correlati: se il prezzo è sceso dall'open, ADV è falso E OR position è bassa. Si comportano come un singolo segnale con peso 2/3, non due segnali indipendenti da 1/3 ciascuno. Un setup "open → pullback sano in 5 minuti → rimbalzo" viene escluso anche se il gap è forte e il catalyst è reale.

**Opzione A — Opening Range Breakout (ORB):** anziché misurare dove si trova il prezzo alle 9:34, si entra quando il prezzo *rompe sopra il massimo* del range 9:30–9:35. Cattura esattamente il pattern "pullback poi breakout". Pro: più robusto, cattura il momentum reale. Contro: richiede monitoraggio attivo dopo le 9:35, timeout gestibile, riscrittura sostanziale della logica di entry.

**Opzione B — ADV diventa peso morbido (preferita):** `post_open_advance` smette di essere binario con peso 1/3 e diventa un bonus addizionale: `+0.15` se vero, `0.00` se falso (non penalità). `OR position` idem — diventa bonus (es. `+0.15` se > 0.66) invece di componente del direction score. Il `direction_score` si riduce a un solo segnale binario: `gap_retention` (il più stabile e indipendente dagli altri due). Semplice da implementare, meno selettivo ma meno rigido su "sell the news opening".

**Decisione:** Opzione B quando ci sono abbastanza dati per calibrare i nuovi pesi. **Prerequisito:** almeno 3–4 settimane di log reali per confrontare quanti trade buoni sarebbero stati catturati con la nuova formula vs. quanti falsi positivi sarebbero entrati. Nel frattempo il sistema rimane invariato.

### Strategia di uscita in profitto — backtest necessario (giugno 2026)

**Osservazione emersa l'11 giugno 2026 (trade INTC):** il sistema ha due soli meccanismi di uscita intraday: hard blocker (−2% dall'entry) e VWAP exit (attivo solo se profit ≥ 1.5%). Quando un trade va brevemente in profitto (+0.5–1%) e poi inverte senza raggiungere la soglia VWAP, non scatta nessuna protezione — il trade continua a deteriorarsi fino all'hard blocker. Risultato: un potenziale +0.8% si trasforma in un −2.1%.

**Tre opzioni da valutare con backtest:**

- **Break-even stop:** una volta raggiunto un profitto soglia (es. +0.5%), sposta lo stop al prezzo di entry. Costo zero sui trade che continuano a salire (AMD dell'11/6 non sarebbe stato impattato), elimina il rischio di trasformare un vincitore in perdente.

- **Abbassare la soglia VWAP da 1.5% a ~0.8–1.0%:** ~~più aggressivo nel prendere profitti parziali~~ — **TESTATA E RESPINTA (14 giugno 2026).**

- **Trailing stop post-profitto:** una volta superato un profitto soglia (es. +0.8%), trascina lo stop a −X% dal massimo intraday raggiunto. Il più sofisticato: richiede di tracciare `peak_price` per posizione nel monitoring loop.

#### Risultati backtest VWAP exit (14 giugno 2026)

Backtest su `--vwap` (gen 2025 → giu 2026, 60 ticker, 624 trade, stesse entry — cambia solo la soglia):

| VWAP exit | Win rate | Profit factor | Avg win | Avg loss | P&L totale |
|-----------|----------|---------------|---------|----------|------------|
| 0.8%  | 51.8% | 1.14 | $668 | −$643 | $26.559 |
| 1.0%  | 50.0% | 1.19 | $741 | −$641 | $36.884 |
| 1.2%  | 48.7% | 1.18 | $774 | −$644 | $35.075 |
| **1.5% (attuale)** | 48.4% | 1.25 | $832 | −$641 | $50.548 |
| 2.0%  | 48.4% | 1.30 | $866 | −$641 | $60.839 |
| 2.5%  | 48.1% | 1.30 | $878 | −$643 | $60.984 |
| 3.0%  | 48.1% | 1.33 | $899 | −$643 | $67.238 |

**Conclusione:** abbassare la soglia **peggiora** il sistema. Da 1.5% → 0.8% il profit factor scende 1.25 → 1.14 e il P&L quasi si dimezza ($50.5k → $26.6k). Meccanismo: soglie più basse triplicano le uscite VWAP (55 → 148) e alzano il win rate (48.4% → 51.8%), ma l'avg win crolla ($832 → $668) — si tagliano i vincitori sul nascere. L'effetto è monotòno nella direzione opposta: alzare la soglia migliora PF e P&L fino a 3.0% (dove però la VWAP exit è quasi vestigiale, 14 trade su 624). **La soglia attuale dell'1.5% resta ragionevole; semmai i dati suggeriscono di alzarla verso 2.0%, non abbassarla.** **Decisione (14 giugno 2026): si tiene 1.5%.** Il guadagno teorico di P&L alzando a 2.0% deriva dal tenere le posizioni più a lungo in un backtest in-sample senza costi — non abbastanza solido da giustificare il cambio ora. Eventuale revisione futura con più dati live.

**Implicazione per il caso INTC:** la VWAP exit **non** è lo strumento per il problema "piccolo profitto → inversione" — abbassarla per catturarlo costa troppo sul resto del portafoglio. Quel caso va risolto col **break-even stop**, che protegge il downside senza forzare l'uscita a un piccolo profitto (quindi senza tagliare i vincitori). Prossimo backtest: `--exit` (confronto break-even e trailing).

**Nota metodologica:** il backtest è in-sample, con stop valutati sul close del minuto, dati IEX (15–20% del volume reale), senza costi di transazione/slippage. La direzione del risultato è netta e monotòna, ma i valori assoluti vanno presi come indicativi.

### Soglie vol_boost — ricalibrare coi dati (giugno 2026)

**Osservazione del 12 giugno:** il vol_boost è scattato su 4 candidati su 8 il 4 giugno, poi 0.0 su 35 dei 36 candidati successivi — incluso INTC l'8 giugno con gap +12%, dove un volume d'apertura sotto il doppio della media è improbabile. Le soglie attuali (ratio > 2× per +0.05, > 3× per +0.10) sono severe, e sui volumi IEX (15–20% del volume reale, rumorosi su finestre di 5 minuti) probabilmente doppiamente severe.

**Proposta in valutazione:** abbassare a **+0.05 se ratio > 1.5×** e **+0.10 se ratio > 2×**.

**Criterio di verifica prima di cambiare:** dal 12 giugno il `vol_ratio` grezzo è loggato per ogni candidato L2 (e un warning segnala quando lo storico volumi non è disponibile — prima era uno 0.0 silenzioso). Guardare la distribuzione dei ratio della settimana del 15–19 giugno: (1) se i ratio sono spesso `null` → problema di feed, le soglie non c'entrano; (2) se i ratio reali si concentrano sotto 2× anche su giornate con gap forti → adottare le nuove soglie, eventualmente tarandole sul quartile alto della distribuzione osservata. Il vol_boost entra nella confidence che decide chi passa L2, quindi il cambio va trattato come modifica di strategia, non cosmetica.

### Migrazione a live trading — cambi all'esecuzione (giugno 2026)

I limit order con conferma fill (sezione 5) sono un workaround per un problema specifico del **paper trading**: Alpaca paper simula i market order all'ask IEX, che alle 9:35 può essere ancora la quote pre-market stantia (+1–2% sopra il mercato reale). Su un account live gli ordini vanno al mercato vero e il problema non esiste. Quando si passa a live:

- **Tornare ai market order per l'entry.** Su titoli liquidissimi (AMD, NVDA, GOOGL…) alle 9:35 lo spread è di pochi centesimi e un market order riempie in ~1 secondo. Si elimina il rischio di entry ritardata (12 giugno: fill 2m18s dopo l'invio) o saltata, che su un setup momentum costa più dello spread.
- **Tenere un sanity check pre-ordine:** se `ask > price_935 × 1.01`, non inviare e notificare su Telegram — protegge da spike anomali o dati errati anche su live.
- **Tenere la conferma fill obbligatoria:** la regola "la posizione esiste solo dopo il fill confermato" resta valida anche con market order — il polling convergerà in pochi secondi invece che minuti, ma stop e P&L devono sempre nascere dal prezzo di fill reale, mai da un prezzo di riferimento.


Alpaca's built-in reporting is too limited for meaningful analysis. The plan is a self-contained HTML dashboard — generated by a Python script that reads the daily JSON logs from GitHub — with four panels:

1. **Equity & KPIs** — daily P&L bar chart, win rate, avg win/loss, total trades, avg confidence
2. **Trade log** — filterable table: date, ticker, entry→exit price & time, P&L $/%,  exit reason, confidence, catalyst bonus, vol boost, short float, gap %
3. **Pipeline funnel** — per-day drill-down showing how many tickers passed each stage (universe → pre-market → L1 filters → L2 signals → LLM → trade) and why each was rejected
4. **Insights** — confidence vs P&L scatter, exit reason breakdown, top tickers, day-of-week performance

**Tech stack:** HTML + Chart.js (CDN, no build step). One `generate_dashboard.py` script reads `logs/*.json` and writes a single `dashboard.html` with all data embedded as `const DATA = [...]`. No server, no hosting — just open in the browser.

**Pre-requisites before building:** at least 2–3 days of real trading logs in `logs/` on GitHub. The daily log already captures everything needed: all L2 signal fields (`post_open_advance`, `or_position`, `gap_retention`, `vol_boost`, `catalyst_bonus`, `short_float`, `short_squeeze_bonus`, `confidence`), trade fields (entry/exit price & time, shares, `pnl_usd`, `exit_reason`), the pipeline funnel stages, and the LLM reasoning. Resume this task once real data is available.

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
| `GITHUB_LOG_TOKEN` | GitHub PAT (contents:write) — daily logs pushed to `logs/` in this repo |
