import logging
import time
from datetime import datetime
from typing import Optional

_RECONCILE_GRACE_SECONDS = 180  # skip manual-close check for positions younger than 3 min

import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

import config
from data import fetcher
from signals.triggers import calc_vwap

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


def _trading_client() -> TradingClient:
    return fetcher.get_trading_client()


def calc_stop_prices(ticker: str, entry_price: float) -> dict:
    """ATR stop and hard blocker stop — use the tighter (higher) one."""
    atr14 = fetcher.get_atr14(ticker)
    stop_atr   = entry_price - atr14 if atr14 > 0 else 0
    stop_pct   = entry_price * (1 - config.HARD_BLOCKER_PCT)
    stop_price = max(stop_atr, stop_pct)
    return {
        "stop_atr":   round(stop_atr, 4),
        "stop_pct":   round(stop_pct, 4),
        "stop_price": round(stop_price, 4),
        "atr14":      round(atr14, 4),
    }


def calc_qty(entry_price: float, equity: float) -> int:
    """Shares to buy: split investable capital (equity minus $1k cushion) across max positions."""
    if entry_price <= 0:
        return 0
    investable = max(0, equity - config.CASH_CUSHION_USD)
    position_usd = investable / config.MAX_POSITIONS
    return max(1, int(position_usd / entry_price))  # floor division → whole shares


def place_market_order(ticker: str, qty: int, side: OrderSide = OrderSide.BUY) -> Optional[dict]:
    """Submit a market order and return order info."""
    client = _trading_client()
    req = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.submit_order(req)
        logger.info(f"Order placed: {side.value} {qty} {ticker} | id={order.id}")
        return {
            "order_id": str(order.id),
            "ticker": ticker,
            "qty": qty,
            "side": side.value,
            "status": order.status,
        }
    except Exception as e:
        logger.error(f"Order error for {ticker}: {e}")
        return None


def open_position(ticker: str, llm_decision: dict) -> Optional[dict]:
    """
    Open a long position based on LLM decision.
    Returns position metadata including stop prices.
    """
    account = fetcher.get_account()
    equity = account["equity"]
    # IEX ask used only to estimate qty before the order; actual fill may differ.
    estimated_price = fetcher.get_latest_quote(ticker).get("ask") or 0.0
    qty = calc_qty(estimated_price, equity)
    if qty == 0:
        logger.error(f"Cannot compute qty for {ticker} at ${estimated_price}")
        return None

    order = place_market_order(ticker, qty, OrderSide.BUY)
    if not order:
        return None

    # Fetch actual Alpaca fill price — market orders route via NBBO smart routing,
    # often filling below IEX ask. Stop must be anchored to real fill, not IEX ask.
    client = _trading_client()
    entry_price = None
    time.sleep(1)
    try:
        filled = client.get_order_by_id(order["order_id"])
        if filled.filled_avg_price:
            entry_price = float(filled.filled_avg_price)
    except Exception:
        pass
    if entry_price is None:
        entry_price = estimated_price  # fallback if fill not yet reflected
        logger.warning(f"{ticker}: fill price not available yet — using IEX ask ${estimated_price:.2f} as fallback")

    stops = calc_stop_prices(ticker, entry_price)
    position = {
        "ticker": ticker,
        "qty": qty,
        "entry_price": entry_price,
        "entry_time": datetime.now(ET).strftime("%H:%M:%S"),
        "entry_ts": time.time(),
        "direction": "long",
        "confidence": llm_decision.get("confidence"),
        "reason": llm_decision.get("reason", ""),
        "order_id": order["order_id"],
        **stops,
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "pnl_usd": None,
        "pnl_pct": None,
    }
    logger.info(
        f"Position opened: {ticker} @ ${entry_price:.2f} (est ${estimated_price:.2f})"
        f" qty={qty} stop=${stops['stop_price']:.2f}"
    )
    return position


def close_position(ticker: str, qty: int, reason: str) -> Optional[dict]:
    """Close position at market. Returns exit info with actual fill price."""
    client = _trading_client()
    try:
        order = client.close_position(ticker)
        # Prefer actual fill price from the order object
        exit_price = float(order.filled_avg_price) if order.filled_avg_price else None
        if exit_price is None:
            # Market order may not be reflected immediately — wait briefly and retry
            time.sleep(1)
            try:
                refreshed = client.get_order_by_id(str(order.id))
                if refreshed.filled_avg_price:
                    exit_price = float(refreshed.filled_avg_price)
            except Exception:
                pass
        if exit_price is None:
            # Last resort: snapshot latest trade (our sell order should be the most recent)
            try:
                snap = fetcher.get_snapshot(ticker)
                if snap.latest_trade:
                    exit_price = float(snap.latest_trade.price)
            except Exception:
                pass
        if exit_price is None:
            exit_price = float(fetcher.get_latest_quote(ticker)["ask"])
        exit_time = datetime.now(ET).strftime("%H:%M:%S")
        logger.info(f"Position closed: {ticker} @ ${exit_price:.2f} reason={reason}")
        return {"exit_price": exit_price, "exit_time": exit_time, "exit_reason": reason}
    except Exception as e:
        logger.error(f"Close position error for {ticker}: {e}")
        return None


def check_stop_triggered(position: dict, current_price: float) -> Optional[str]:
    """Return exit reason if any stop is triggered, else None."""
    if current_price <= position["stop_price"]:
        entry = position["entry_price"]
        if (entry - current_price) / entry >= config.HARD_BLOCKER_PCT:
            return "hard_blocker"
        return "atr_stop"
    return None


def check_vwap_exit(ticker: str, position: dict, current_price: float) -> bool:
    """Return True if price crossed below intraday VWAP with profit >= 1.5%."""
    profit_pct = (current_price - position["entry_price"]) / position["entry_price"]
    if profit_pct < config.VWAP_EXIT_MIN_PROFIT_PCT:
        return False  # not enough profit to trigger VWAP exit
    try:
        bars = fetcher.get_intraday_bars(ticker, minutes=1)
        if bars.empty:
            return False
        bars.index = bars.index.tz_convert(ET)
        vwap_now = calc_vwap(bars)
        return current_price < vwap_now
    except Exception as e:
        logger.warning(f"VWAP exit check error for {ticker}: {e}")
        return False


def check_trading_halt(ticker: str) -> bool:
    """Return True if ticker is currently halted."""
    return not fetcher.is_asset_tradable(ticker)


def monitor_positions(open_positions: list[dict], daily_pnl: float) -> tuple[list[dict], list[dict], float]:
    """
    Single monitoring cycle. Checks stops and VWAP exits.
    Returns (still_open, just_closed, updated_daily_pnl).
    """
    still_open = []
    just_closed = []

    # Reconcile against Alpaca's actual positions — detect manual closes.
    # Skip positions opened within the last 3 minutes: Alpaca paper trading can lag
    # in reflecting fills in get_all_positions(), causing false manual-close detections.
    mature = [p for p in open_positions if time.time() - p.get("entry_ts", 0) >= _RECONCILE_GRACE_SECONDS]
    if mature:
        try:
            actual_tickers = {p["ticker"] for p in fetcher.get_open_positions()}
            orphans = [p["ticker"] for p in mature if p["ticker"] not in actual_tickers]
            if orphans:
                logger.warning(f"Manual close detected for {orphans} — removing from monitoring")
                open_positions = [p for p in open_positions if p["ticker"] in actual_tickers]
        except Exception as e:
            logger.warning(f"Alpaca position reconciliation failed: {e} — skipping check")

    for position in open_positions:
        ticker = position["ticker"]
        try:
            # Halt check
            if check_trading_halt(ticker):
                logger.warning(f"{ticker}: trading halt detected — waiting to reopen")
                still_open.append(position)
                continue

            current_price = fetcher.get_current_price(ticker)

            # Stop check
            stop_reason = check_stop_triggered(position, current_price)
            if stop_reason:
                exit_info = close_position(ticker, position["qty"], stop_reason)
                if exit_info:
                    pnl = (exit_info["exit_price"] - position["entry_price"]) * position["qty"]
                    position.update(exit_info)
                    position["pnl_usd"] = round(pnl, 2)
                    position["pnl_pct"] = round(
                        (exit_info["exit_price"] - position["entry_price"]) / position["entry_price"], 4
                    )
                    daily_pnl += pnl
                    just_closed.append(position)
                    continue

            # VWAP trailing exit
            if check_vwap_exit(ticker, position, current_price):
                exit_info = close_position(ticker, position["qty"], "vwap_exit")
                if exit_info:
                    pnl = (exit_info["exit_price"] - position["entry_price"]) * position["qty"]
                    position.update(exit_info)
                    position["pnl_usd"] = round(pnl, 2)
                    position["pnl_pct"] = round(
                        (exit_info["exit_price"] - position["entry_price"]) / position["entry_price"], 4
                    )
                    daily_pnl += pnl
                    just_closed.append(position)
                    continue

            still_open.append(position)

        except Exception as e:
            logger.error(f"Monitor error for {ticker}: {e}")
            still_open.append(position)

    return still_open, just_closed, daily_pnl


def close_all_positions_eod(open_positions: list[dict], daily_pnl: float) -> tuple[list[dict], float]:
    """Force-close all open positions at 15:45 ET. No exceptions."""
    closed = []
    for position in open_positions:
        ticker = position["ticker"]
        exit_info = close_position(ticker, position["qty"], "eod_close")
        if exit_info:
            pnl = (exit_info["exit_price"] - position["entry_price"]) * position["qty"]
            position.update(exit_info)
            position["pnl_usd"] = round(pnl, 2)
            position["pnl_pct"] = round(
                (exit_info["exit_price"] - position["entry_price"]) / position["entry_price"], 4
            )
            daily_pnl += pnl
            closed.append(position)
    return closed, daily_pnl


def daily_loss_limit_reached(daily_pnl: float) -> bool:
    if config.MAX_DAILY_LOSS_USD is None:
        return False
    return daily_pnl <= -config.MAX_DAILY_LOSS_USD
