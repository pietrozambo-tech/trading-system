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
    Filters by pre-market gap >= MIN_PREMARKET_GAP (0.5%). No volume filter here —
    pre-market volume is unreliable; volume is assessed properly in L2.
    Returns candidates with metadata.
    """
    candidates = []
    for ticker in universe:
        try:
            daily_bars = fetcher.get_daily_bars(ticker, lookback_days=65)
            if len(daily_bars) < 2:
                continue
            prev_close = float(daily_bars["close"].iloc[-1])
            adv = float(daily_bars["volume"].mean())
            high_3m = float(daily_bars["high"].max())

            pm = fetcher.get_premarket_data(ticker, session_date)
            pm_price = pm["premarket_price"]

            if pm_price is None or prev_close == 0:
                continue

            gap_pct = (pm_price - prev_close) / prev_close

            # Solo gap positivi (long only)
            if gap_pct < config.MIN_PREMARKET_GAP:
                continue

            candidates.append({
                "ticker": ticker,
                "prev_close": prev_close,
                "premarket_price": pm_price,
                "gap_pct": gap_pct,
                "adv": adv,
                "dist_from_3m_high": round((pm_price - high_3m) / high_3m, 4),
            })
            logger.info(f"Watchlist: {ticker} gap={gap_pct:.2%}")
        except Exception as e:
            logger.warning(f"Watchlist error for {ticker}: {e}")

    candidates.sort(key=lambda x: x["gap_pct"], reverse=True)
    return candidates


def apply_binary_filters(
    candidates: list[dict], session_date: Optional[date] = None
) -> tuple[list[dict], list[dict]]:
    """
    Phase 2 — 9:35 ET binary filters. Applied cheapest first.
    All must pass (fail = discard).
    Returns (passed, rejects) where rejects = [{"ticker": ..., "reason": ...}].
    """
    passed  = []
    rejects = []
    for c in candidates:
        ticker = c["ticker"]
        try:
            # 1. ADV >= 200k shares/day on IEX (~5–10M real ADV)
            adv = c.get("adv") or fetcher.get_adv(ticker)
            if adv < config.MIN_ADV:
                logger.info(f"L1 REJECT {ticker}: ADV {adv:,.0f} < {config.MIN_ADV:,} min")
                rejects.append({"ticker": ticker, "reason": f"adv_{adv:,.0f}<min_{config.MIN_ADV:,}"})
                continue

            # 2. Tradable (no halt)
            if not fetcher.is_asset_tradable(ticker):
                logger.info(f"L1 REJECT {ticker}: not tradable on Alpaca")
                rejects.append({"ticker": ticker, "reason": "not_tradable"})
                continue

            quote = fetcher.get_latest_quote(ticker)
            c["current_price"] = quote["ask"]
            passed.append(c)
            logger.info(f"L1 pass: {ticker}")

        except Exception as e:
            logger.warning(f"Binary filter error for {ticker}: {e}")
            rejects.append({"ticker": ticker, "reason": f"error_{e}"})

    return passed, rejects


def filter_earnings_tonight(
    candidates: list[dict], session_date: Optional[date] = None
) -> tuple[list[dict], list[dict]]:
    """
    Remove tickers with earnings scheduled after close TODAY.
    Tickers that already reported yesterday are KEPT — that's the catalyst we want.

    Logic: only block if the article (a) is from today, and (b) does not contain
    past-tense result words — distinguishes "reports tonight" from "reported last night".
    Returns (safe, rejects) where rejects = [{"ticker": ..., "reason": ...}].
    """
    if session_date is None:
        session_date = datetime.now(ET).date()

    # Words that indicate earnings already happened (past tense)
    past_tense = ["beat", "miss", "reported", "exceeded", "results", "fell short", "topped", "surpassed"]

    safe    = []
    rejects = []
    for c in candidates:
        ticker = c["ticker"]
        news = c.get("news") or fetcher.get_news(ticker, limit=5)
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
            rejects.append({"ticker": ticker, "reason": "earnings_tonight"})
        else:
            safe.append(c)
    return safe, rejects
