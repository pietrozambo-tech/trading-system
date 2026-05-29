import logging
from datetime import date, datetime
from typing import Optional

import pytz

import config
from data import fetcher

ET = pytz.timezone("America/New_York")

logger = logging.getLogger(__name__)


def check_spy_block(session_date: Optional[date] = None) -> bool:
    """Return True if SPY is down more than threshold — block all trading."""
    spy_pct = fetcher.get_spy_change(session_date)
    if spy_pct < config.SPY_BLOCK_THRESHOLD:
        logger.warning(f"SPY block triggered: {spy_pct:.2%} < {config.SPY_BLOCK_THRESHOLD:.2%}")
        return True
    return False


def build_premarket_watchlist(universe: list[str], session_date: Optional[date] = None) -> list[dict]:
    """
    Phase 1 — 9:25 ET scan.
    Filters by pre-market gap > 1.5% and pre-market volume > 100% ADV.
    Returns candidates with metadata.
    """
    candidates = []
    for ticker in universe:
        try:
            daily_bars = fetcher.get_daily_bars(ticker, lookback_days=22)
            if len(daily_bars) < 2:
                continue
            prev_close = float(daily_bars["close"].iloc[-1])
            adv = float(daily_bars["volume"].mean())

            pm = fetcher.get_premarket_data(ticker, session_date)
            pm_price  = pm["premarket_price"]
            pm_volume = pm["premarket_volume"]

            if pm_price is None or prev_close == 0:
                continue

            gap_pct = (pm_price - prev_close) / prev_close

            # Solo gap positivi (long only)
            if gap_pct < config.MIN_PREMARKET_GAP:
                continue

            # Volume pre-market vs media pre-market ultimi 10gg (apple-to-apple)
            pm_vol_avg = fetcher.get_historical_premarket_volume_avg(
                ticker,
                lookback_days=config.PREMARKET_VOL_LOOKBACK,
                session_date=session_date,
            )
            vol_ratio = pm_volume / pm_vol_avg if pm_vol_avg > 0 else 0

            if vol_ratio >= config.MIN_PREMARKET_VOL_RATIO:
                candidates.append({
                    "ticker": ticker,
                    "prev_close": prev_close,
                    "premarket_price": pm_price,
                    "premarket_volume": pm_volume,
                    "premarket_vol_avg": pm_vol_avg,
                    "gap_pct": gap_pct,
                    "premarket_vol_ratio": vol_ratio,
                    "adv": adv,
                })
                logger.info(f"Watchlist: {ticker} gap={gap_pct:.2%} pm_vol_ratio={vol_ratio:.1f}x")
        except Exception as e:
            logger.warning(f"Watchlist error for {ticker}: {e}")

    candidates.sort(key=lambda x: x["gap_pct"], reverse=True)
    return candidates


def apply_binary_filters(candidates: list[dict], session_date: Optional[date] = None) -> list[dict]:
    """
    Phase 2 — 9:45 ET binary filters. Applied cheapest first.
    All must pass (fail = discard).
    """
    passed = []
    for c in candidates:
        ticker = c["ticker"]
        try:
            # 1. Price >= $5 and market cap > $2B (use ADV as proxy if no cap data)
            daily = fetcher.get_daily_bars(ticker, lookback_days=5)
            if daily.empty:
                logger.debug(f"{ticker}: no daily data — skip")
                continue
            price = float(daily["close"].iloc[-1])
            if price < config.MIN_PRICE:
                logger.info(f"L1 REJECT {ticker}: price ${price:.2f} < ${config.MIN_PRICE} min")
                continue

            # 2. ADV > 1M
            adv = c.get("adv") or fetcher.get_adv(ticker)
            if adv < config.MIN_ADV:
                logger.info(f"L1 REJECT {ticker}: ADV {adv:,.0f} < {config.MIN_ADV:,} min")
                continue

            # 3. Tradable (no halt)
            if not fetcher.is_asset_tradable(ticker):
                logger.info(f"L1 REJECT {ticker}: not tradable on Alpaca")
                continue

            # 4. Bid-ask spread < 0.6% (real-time)
            quote = fetcher.get_latest_quote(ticker)
            if quote["spread_pct"] >= config.MAX_BID_ASK_SPREAD:
                logger.info(f"L1 REJECT {ticker}: spread {quote['spread_pct']:.3%} >= {config.MAX_BID_ASK_SPREAD:.3%} max")
                continue

            c["current_price"] = quote["ask"]
            c["bid_ask_spread"] = quote["spread_pct"]
            passed.append(c)
            logger.info(f"L1 pass: {ticker}")

        except Exception as e:
            logger.warning(f"Binary filter error for {ticker}: {e}")

    return passed


def filter_earnings_tonight(candidates: list[dict], session_date: Optional[date] = None) -> list[dict]:
    """
    Remove tickers with earnings scheduled after close TODAY.
    Tickers that already reported yesterday are KEPT — that's the catalyst we want.

    Logic: only block if the article (a) is from today, and (b) does not contain
    past-tense result words — distinguishes "reports tonight" from "reported last night".
    """
    if session_date is None:
        session_date = datetime.now(ET).date()

    # Words that indicate earnings already happened (past tense)
    past_tense = ["beat", "miss", "reported", "exceeded", "results", "fell short", "topped", "surpassed"]

    safe = []
    for c in candidates:
        ticker = c["ticker"]
        news = fetcher.get_news(ticker, limit=5)
        earnings_tonight = False
        for n in news:
            text = (n.get("headline", "") + " " + n.get("summary", "")).lower()

            if not ("earnings" in text and "after" in text and "close" in text):
                continue

            # If the article talks about results already out, it's past earnings — keep the stock
            if any(w in text for w in past_tense):
                continue

            # Only block if the article was published today (future earnings)
            created_at = n.get("created_at", "")
            if created_at:
                try:
                    article_date = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
                    if article_date < session_date:
                        continue  # article from yesterday — earnings already happened
                except Exception:
                    pass

            earnings_tonight = True
            break

        if earnings_tonight:
            logger.info(f"L1 REJECT {ticker}: earnings scheduled tonight — skip")
        else:
            safe.append(c)
    return safe
