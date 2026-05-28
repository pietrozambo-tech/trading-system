"""
Daily trading orchestrator — entry point for Railway cron job.

Schedule: 30 13 * * 1-5  (9:25 ET = 13:25 UTC, weekdays only)

Timeline (all times ET):
  09:25  Build pre-market watchlist
  09:45  Apply binary L1 filters + compute L2 signals → LLM decision
  09:47  Place orders
  intra  Monitor every 5 min
  15:45  Force-close all positions
  16:05  Send Telegram EOD recap
"""
import logging
import time
from datetime import datetime

import pytz

import config
from data import fetcher
from signals import eligibility, triggers
from llm import analyst
from execution import trader
from notify import telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# Universe — replace with your actual screener output or a broader list
# ---------------------------------------------------------------------------
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX",
    "CRM", "ORCL", "ADBE", "INTC", "QCOM", "MU", "AVGO", "TXN", "AMAT",
    "JPM", "BAC", "GS", "MS", "C", "WFC", "BLK", "SCHW",
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "BMY",
    "XOM", "CVX", "SLB", "HAL", "OXY",
    "SPY", "QQQ", "IWM",
]


def wait_until(target_time_str: str, session_now: datetime) -> None:
    """Block until the target ET time (HH:MM)."""
    h, m = map(int, target_time_str.split(":"))
    target = session_now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (target - datetime.now(ET)).total_seconds()
    if delta > 0:
        logger.info(f"Waiting {delta:.0f}s until {target_time_str} ET …")
        time.sleep(delta)


def current_et_str() -> str:
    return datetime.now(ET).strftime("%H:%M")


def run() -> None:
    now_et = datetime.now(ET)
    today_str = now_et.strftime("%Y-%m-%d")
    logger.info(f"=== Trading session start: {today_str} ===")

    daily_pnl    = 0.0
    open_positions: list[dict] = []
    all_trades:    list[dict]  = []

    # ------------------------------------------------------------------
    # 09:25 — Pre-market watchlist
    # ------------------------------------------------------------------
    wait_until(config.WATCHLIST_TIME, now_et)
    logger.info("Building pre-market watchlist …")
    watchlist = eligibility.build_premarket_watchlist(UNIVERSE)
    logger.info(f"Watchlist: {[c['ticker'] for c in watchlist]}")

    # ------------------------------------------------------------------
    # 09:45 — Binary L1 filters + L2 signals + LLM
    # ------------------------------------------------------------------
    wait_until(config.ENTRY_TIME, now_et)

    # SPY macro block check
    if eligibility.check_spy_block():
        logger.warning("SPY block triggered — no trades today")
        _send_eod(all_trades, daily_pnl, today_str)
        return

    # Apply binary filters
    candidates = eligibility.apply_binary_filters(watchlist)
    candidates = eligibility.filter_earnings_tonight(candidates)
    logger.info(f"Candidates after L1: {[c['ticker'] for c in candidates]}")

    if not candidates:
        logger.info("No candidates after filtering — no trades today")
        _send_eod(all_trades, daily_pnl, today_str)
        return

    # Compute L2 signals for each candidate
    candidates_with_signals = []
    for c in candidates:
        ticker = c["ticker"]
        news = fetcher.get_news(ticker, limit=5)
        catalyst_mult = analyst.classify_catalyst_from_news(news)
        signals = triggers.compute_signals(ticker, c["prev_close"], catalyst_mult)
        if signals and signals.get("passes_threshold"):
            merged = {**c, **signals}
            candidates_with_signals.append(merged)

    logger.info(f"Above confidence threshold: {[c['ticker'] for c in candidates_with_signals]}")

    if not candidates_with_signals:
        logger.info("No candidates passed L2 — no trades today")
        _send_eod(all_trades, daily_pnl, today_str)
        return

    # LLM analysis
    spy_pct = fetcher.get_spy_change()
    llm_result = analyst.analyze_candidates(candidates_with_signals, spy_pct, today_str)
    logger.info(f"LLM decision: {llm_result}")

    # ------------------------------------------------------------------
    # 09:47 — Place orders
    # ------------------------------------------------------------------
    wait_until(config.ORDER_TIME, now_et)

    for key in ("trade_1", "trade_2"):
        decision = llm_result.get(key)
        if not decision:
            if llm_result.get("no_trade_reason"):
                all_trades.append({"reason": llm_result["no_trade_reason"]})
            continue
        if len(open_positions) >= config.MAX_POSITIONS:
            break
        position = trader.open_position(decision["ticker"], decision)
        if position:
            open_positions.append(position)
            all_trades.append(position)

    # ------------------------------------------------------------------
    # Intraday monitoring loop (every 5 min until 15:45 ET)
    # ------------------------------------------------------------------
    logger.info("Starting monitoring loop …")
    while current_et_str() < config.EOD_CLOSE_TIME:
        if not open_positions:
            break
        if trader.daily_loss_limit_reached(daily_pnl):
            logger.warning(f"Daily loss limit reached (${daily_pnl:.2f}) — closing all")
            break
        time.sleep(config.MONITORING_INTERVAL)
        open_positions, just_closed, daily_pnl = trader.monitor_positions(open_positions, daily_pnl)
        for closed_pos in just_closed:
            logger.info(f"Closed: {closed_pos['ticker']} {closed_pos['exit_reason']} P&L=${closed_pos['pnl_usd']:.2f}")

    # ------------------------------------------------------------------
    # 15:45 — EOD hard close
    # ------------------------------------------------------------------
    if open_positions:
        logger.info("EOD close — forcing all positions")
        closed_eod, daily_pnl = trader.close_all_positions_eod(open_positions, daily_pnl)
        open_positions = []

    logger.info(f"Day P&L: ${daily_pnl:.2f}")

    # ------------------------------------------------------------------
    # 16:05 — Telegram EOD recap
    # ------------------------------------------------------------------
    wait_until(config.TELEGRAM_NOTIFY_TIME, now_et)
    _send_eod(all_trades, daily_pnl, today_str, spy_pct)


def _send_eod(
    all_trades: list[dict],
    daily_pnl: float,
    today_str: str,
    spy_pct: float = 0.0,
) -> None:
    try:
        account = fetcher.get_account()
        equity = account["equity"]
    except Exception:
        equity = 0.0

    try:
        llm_text = analyst.generate_eod_recap(all_trades, spy_pct, equity)
    except Exception as e:
        logger.warning(f"LLM EOD recap failed: {e}")
        llm_text = ""

    telegram.send_eod_recap(
        trade_data=all_trades,
        spy_pct=spy_pct,
        daily_pnl=daily_pnl,
        account_equity=equity,
        date_str=today_str,
        llm_text=llm_text,
    )


if __name__ == "__main__":
    run()
