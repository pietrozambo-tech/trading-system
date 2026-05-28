import logging
from datetime import date
from typing import Optional

import config
from data import fetcher

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
            vol_vs_adv = pm_volume / adv if adv > 0 else 0

            if abs(gap_pct) >= config.MIN_PREMARKET_GAP and vol_vs_adv >= config.MIN_PREMARKET_VOL_ADV:
                candidates.append({
                    "ticker": ticker,
                    "prev_close": prev_close,
                    "premarket_price": pm_price,
                    "premarket_volume": pm_volume,
                    "gap_pct": gap_pct,
                    "vol_vs_adv": vol_vs_adv,
                    "adv": adv,
                })
                logger.info(f"Watchlist: {ticker} gap={gap_pct:.2%} vol_adv={vol_vs_adv:.1f}x")
        except Exception as e:
            logger.warning(f"Watchlist error for {ticker}: {e}")

    candidates.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
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
                logger.debug(f"{ticker}: price ${price} < ${config.MIN_PRICE} — skip")
                continue

            # 2. ADV > 1M
            adv = c.get("adv") or fetcher.get_adv(ticker)
            if adv < config.MIN_ADV:
                logger.debug(f"{ticker}: ADV {adv:.0f} < {config.MIN_ADV} — skip")
                continue

            # 3. Tradable (no halt)
            if not fetcher.is_asset_tradable(ticker):
                logger.debug(f"{ticker}: not tradable — skip")
                continue

            # 4. Bid-ask spread < 0.6% (real-time)
            quote = fetcher.get_latest_quote(ticker)
            if quote["spread_pct"] >= config.MAX_BID_ASK_SPREAD:
                logger.debug(f"{ticker}: spread {quote['spread_pct']:.3%} >= {config.MAX_BID_ASK_SPREAD:.3%} — skip")
                continue

            c["current_price"] = quote["ask"]
            c["bid_ask_spread"] = quote["spread_pct"]
            passed.append(c)
            logger.info(f"L1 pass: {ticker}")

        except Exception as e:
            logger.warning(f"Binary filter error for {ticker}: {e}")

    return passed


def filter_earnings_tonight(candidates: list[dict]) -> list[dict]:
    """
    Remove tickers with earnings scheduled after close today.
    Alpaca does not expose earnings calendar directly — we rely on news heuristics.
    Tickers with earnings yesterday are KEPT (good catalyst).
    """
    safe = []
    for c in candidates:
        ticker = c["ticker"]
        news = fetcher.get_news(ticker, limit=5)
        earnings_tonight = any(
            "earnings" in (n.get("headline", "") + n.get("summary", "")).lower()
            and "after" in (n.get("headline", "") + n.get("summary", "")).lower()
            and "close" in (n.get("headline", "") + n.get("summary", "")).lower()
            for n in news
        )
        if earnings_tonight:
            logger.info(f"{ticker}: earnings tonight — skip")
        else:
            safe.append(c)
    return safe
