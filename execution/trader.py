import logging
import math
import time
from datetime import datetime
from typing import Optional

_RECONCILE_GRACE_SECONDS = 180  # skip manual-close check for positions younger than 3 min

import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, ClosePositionRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

import config
from data import fetcher
from notify import telegram
from signals.triggers import calc_vwap

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# Set by main.py at startup — lets long polls (fill confirmation) abort on SIGTERM
# instead of sleeping through it and dying with a live order on the book.
_shutdown_event = None

# Reconciliation miss counters per ticker (module-level so they survive across
# monitor cycles without polluting the position dicts that end up in the log).
_reconcile_misses: dict[str, int] = {}


def register_shutdown_event(event) -> None:
    global _shutdown_event
    _shutdown_event = event


def _trading_client() -> TradingClient:
    return fetcher.get_trading_client()


def cancel_all_open_orders() -> list[str]:
    """Cancel every open order at startup. A crash during fill confirmation leaves an
    orphan DAY limit order that nothing tracks — it can fill hours later, unmonitored,
    and the position-based recovery never sees it. Returns descriptions of cancelled orders."""
    client = _trading_client()
    try:
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as e:
        logger.error(f"Cannot list open orders at startup: {e}")
        return []
    cancelled = []
    for o in open_orders:
        try:
            client.cancel_order_by_id(str(o.id))
            desc = f"{o.symbol} {o.side.value if hasattr(o.side, 'value') else o.side} qty={o.qty}"
            cancelled.append(desc)
            logger.warning(f"Orphan order cancelled at startup: {desc}")
        except Exception as e:
            logger.error(f"Cancel failed for order {o.id} ({o.symbol}): {e}")
    return cancelled


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


def place_limit_order(ticker: str, qty: int, limit_price: float, side: OrderSide = OrderSide.BUY) -> Optional[dict]:
    """Submit a limit order and return order info."""
    client = _trading_client()
    req = LimitOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    try:
        order = client.submit_order(req)
        logger.info(f"Limit order placed: {side.value} {qty} {ticker} @ ${limit_price:.2f} | id={order.id}")
        return {
            "order_id": str(order.id),
            "ticker": ticker,
            "qty": qty,
            "side": side.value,
            "status": order.status,
        }
    except Exception as e:
        logger.error(f"Limit order error for {ticker}: {e}")
        return None


def _fill_price_from_position(client: TradingClient, ticker: str) -> Optional[float]:
    """Position endpoint fallback — on paper trading it can reflect avg_entry_price
    before the order endpoint exposes filled_avg_price."""
    try:
        pos = client.get_open_position(ticker)
        if pos and pos.avg_entry_price:
            return float(pos.avg_entry_price)
    except Exception:
        pass
    return None


def place_entry_order(ticker: str, llm_decision: dict) -> Optional[dict]:
    """Phase 1 of entry: size and submit the limit order without waiting for the fill.
    Returns an order context for confirm_entry_fill(). Keeping the two phases separate
    lets multiple trades poll their fills concurrently instead of back-to-back."""
    account = fetcher.get_account()
    equity = account["equity"]

    # price_935 is the 9:34 bar close from intraday bar data (real trades).
    # Fall back to latest trade if not provided (e.g. recovery/late entry paths).
    ref_price: float = llm_decision.get("price_935") or 0.0
    if not ref_price:
        ref_price = fetcher.get_current_price(ticker)

    qty = calc_qty(ref_price, equity)
    if qty == 0:
        logger.error(f"Cannot compute qty for {ticker} at ${ref_price:.2f}")
        return None

    # Limit buy at ref_price +0.5% — caps the fill at the real market price,
    # blocking Alpaca from filling at stale IEX pre-market asks (+1-2% above market).
    limit_price = round(ref_price * 1.005, 2)
    order = place_limit_order(ticker, qty, limit_price, OrderSide.BUY)
    if not order:
        return None
    return {
        "ticker": ticker,
        "order_id": order["order_id"],
        "qty": qty,
        "limit_price": limit_price,
        "ref_price": ref_price,
        "placed_ts": time.time(),
        "llm_decision": llm_decision,
    }


def confirm_entry_fill(ctx: dict) -> Optional[dict]:
    """Phase 2 of entry: poll the order until filled, cancel on timeout.

    The position dict is created ONLY after the fill is confirmed. A limit order can
    sit unfilled for minutes when the IEX ask is above our limit (June 12: 2m18s) —
    creating the position before the fill produces phantom positions, wrong entry
    prices/stops, and a race with the reconciliation check in monitor_positions().
    If the order is not fully filled within FILL_CONFIRM_TIMEOUT_S of placement it is
    cancelled: a partial fill is kept with the actual executed qty, a zero fill skips
    the trade. The deadline is anchored to placed_ts, so with two pending orders the
    waits overlap instead of adding up.
    """
    ticker       = ctx["ticker"]
    order_id     = ctx["order_id"]
    qty          = ctx["qty"]
    limit_price  = ctx["limit_price"]
    ref_price    = ctx["ref_price"]
    llm_decision = ctx["llm_decision"]

    client = _trading_client()
    entry_price = None
    filled_qty = 0
    deadline = ctx["placed_ts"] + config.FILL_CONFIRM_TIMEOUT_S
    # Always poll at least once: with two pending orders this one may have filled
    # (deadline even expired) while we were confirming the previous one.
    first_check = True
    while first_check or time.time() < deadline:
        first_check = False
        if _shutdown_event is not None:
            # Interruptible sleep: on SIGTERM stop waiting and fall through to the
            # cancel path — never die with a live order on the book.
            if _shutdown_event.wait(timeout=config.FILL_POLL_INTERVAL_S):
                logger.warning(f"{ticker}: shutdown during fill confirmation — cancelling order")
                break
        else:
            time.sleep(config.FILL_POLL_INTERVAL_S)
        try:
            o = client.get_order_by_id(order_id)
        except Exception as e:
            logger.warning(f"{ticker}: order poll error: {e}")
            continue
        status = str(getattr(o, "status", "")).lower()
        if any(s in status for s in ("canceled", "rejected", "expired")):
            logger.error(f"{ticker}: order {status} before fill")
            break
        filled_qty = int(float(o.filled_qty or 0))
        if filled_qty >= qty:
            entry_price = float(o.filled_avg_price) if o.filled_avg_price else _fill_price_from_position(client, ticker)
            if entry_price:
                logger.info(f"{ticker}: fill confirmed @ ${entry_price:.2f} ({filled_qty} shares)")
                break

    if entry_price is None:
        # Not (fully) filled in time — cancel so no orphan order can fill later, unmonitored.
        try:
            client.cancel_order_by_id(order_id)
            logger.warning(f"{ticker}: limit ${limit_price:.2f} not filled within {config.FILL_CONFIRM_TIMEOUT_S}s — order cancelled")
        except Exception as e:
            logger.warning(f"{ticker}: order cancel failed (may already be terminal): {e}")
        # Re-check final state: the cancel can race with a (partial) fill.
        time.sleep(2)
        try:
            o = client.get_order_by_id(order_id)
            filled_qty = int(float(o.filled_qty or 0))
            if filled_qty > 0:
                entry_price = float(o.filled_avg_price) if o.filled_avg_price else _fill_price_from_position(client, ticker)
                if entry_price is None:
                    # We OWN these shares — never abandon them unmonitored. The limit
                    # price is the worst possible fill, so it's a conservative estimate.
                    entry_price = limit_price
                    logger.error(f"{ticker}: {filled_qty} shares filled but no price available — assuming limit ${limit_price:.2f}")
                qty = filled_qty
                logger.warning(f"{ticker}: partial fill kept — {qty} shares @ ${entry_price:.2f}")
        except Exception as e:
            logger.error(f"{ticker}: final order check failed: {e}")

    if entry_price is None:
        logger.warning(f"{ticker}: entry skipped — limit ${limit_price:.2f} never filled (ref ${ref_price:.2f})")
        telegram.send_message(
            f"⚠️ <b>{ticker}</b>: entry saltata — limit ${limit_price:.2f} non eseguito entro "
            f"{config.FILL_CONFIRM_TIMEOUT_S}s (ref ${ref_price:.2f}). Nessuna posizione aperta."
        )
        return None

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
        "order_id": order_id,
        **stops,
        "peak_price": entry_price,      # highest price seen — drives the step ratchet
        "breakeven_armed": False,       # True once the stop has been raised to entry or above
        "stop_label": None,             # label of the active ratchet ("breakeven_stop"/"step_stop"); None = base ATR/hard stop
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "pnl_usd": None,
        "pnl_pct": None,
    }
    logger.info(
        f"Position opened: {ticker} @ ${entry_price:.2f} (limit ${limit_price:.2f} | ref ${ref_price:.2f})"
        f" qty={qty} stop=${stops['stop_price']:.2f}"
    )
    return position


def open_position(ticker: str, llm_decision: dict) -> Optional[dict]:
    """Single-trade entry: place the order and wait for the confirmed fill."""
    ctx = place_entry_order(ticker, llm_decision)
    if not ctx:
        return None
    return confirm_entry_fill(ctx)


def close_position(ticker: str, qty: int, reason: str, fallback_price: Optional[float] = None) -> Optional[dict]:
    """Close position at market. Returns exit info with the ACTUAL fill price.

    Returns None ONLY if the close order itself failed. Once Alpaca accepts the
    close, a price-lookup failure must not be reported as a failed close — the
    caller would mis-book the trade and reconciliation would later drop it with
    no PnL. In that case exit_price falls back to `fallback_price` (typically the
    entry price) flagged with exit_price_estimated.

    The close order's own filled_avg_price is the ONLY price that reflects the real
    execution. A market order can take a few seconds to report it, so we POLL the
    order before ever falling back to market-data snapshots — those lag the real
    fill and silently mis-state the realised PnL (June 15: AMD booked at 548.12 from
    a snapshot vs the real 547.81 fill, hiding ~$28 of loss; CRWV 107.59 vs 107.66).
    Snapshot/quote are used only if the order never reports a fill price.
    """
    client = _trading_client()
    try:
        order = client.close_position(ticker)
    except Exception as e:
        logger.error(f"Close position error for {ticker}: {e}")
        return None

    order_id = str(getattr(order, "id", "") or "")

    # Authoritative exit price = the close order's filled_avg_price, read ONLY once the
    # order is FULLY filled (filled_qty >= qty). Polling to completion — instead of bailing
    # on the first non-null price — mirrors confirm_entry_fill, which is exactly why entries
    # always book at the real fill. Reading too early books a partial-fill average; bailing
    # to a market snapshot books a print that never matches the real execution. Both silently
    # mis-state PnL (18 Jun: AMAT booked 614.60 from a snapshot vs the real 614.71 fill,
    # ~$9 of loss hidden; MRVL 307.26 vs 307.24; INTC 133.5715 partial-avg vs 133.5766 final).
    exit_price = None
    last_avg = None  # most recent filled_avg_price seen, even before the fill completes
    for attempt in range(config.CLOSE_FILL_POLL_ATTEMPTS):
        try:
            o = client.get_order_by_id(order_id) if order_id else order
            status = str(getattr(o, "status", "")).lower()
            filled_qty = int(float(getattr(o, "filled_qty", 0) or 0))
            if o.filled_avg_price:
                last_avg = float(o.filled_avg_price)
                if filled_qty >= qty:
                    exit_price = last_avg  # complete fill — authoritative
                    break
            if any(s in status for s in ("canceled", "rejected", "expired")):
                logger.error(f"{ticker}: close order {status} (filled {filled_qty}/{qty})")
                break
        except Exception as e:
            logger.warning(f"{ticker}: close order poll error (attempt {attempt + 1}): {e}")
        time.sleep(config.CLOSE_FILL_POLL_INTERVAL_S)

    # A partial-fill average still reflects real execution far better than a market print.
    if exit_price is None and last_avg is not None:
        logger.warning(
            f"{ticker}: close order not fully filled in {config.CLOSE_FILL_POLL_ATTEMPTS} polls "
            f"— booking the partial-fill avg ${last_avg:.4f}"
        )
        exit_price = last_avg

    # Market-data fallback ONLY if the order never reported ANY fill price.
    if exit_price is None:
        logger.warning(
            f"{ticker}: close order reported no filled_avg_price after "
            f"{config.CLOSE_FILL_POLL_ATTEMPTS} polls — falling back to market data (may mis-state PnL)"
        )
        try:
            snap = fetcher.get_snapshot(ticker)
            if snap.latest_trade:
                exit_price = float(snap.latest_trade.price)
        except Exception:
            pass
    if exit_price is None:
        try:
            exit_price = float(fetcher.get_latest_quote(ticker)["ask"])
        except Exception:
            pass

    estimated = False
    if exit_price is None:
        estimated = True
        exit_price = fallback_price if fallback_price else 0.0
        logger.error(f"{ticker}: closed on Alpaca but NO exit price available — booked at estimate ${exit_price:.2f}")
        telegram.send_message(
            f"⚠️ <b>{ticker}</b> chiusa su Alpaca ma prezzo di uscita non disponibile — "
            f"registrata a ${exit_price:.2f} (stima). Verifica il fill reale su Alpaca."
        )

    exit_time = datetime.now(ET).strftime("%H:%M:%S")
    logger.info(f"Position closed: {ticker} @ ${exit_price:.2f} reason={reason}")
    info = {"exit_price": exit_price, "exit_time": exit_time, "exit_reason": reason}
    if estimated:
        info["exit_price_estimated"] = True
    return info


def check_stop_triggered(position: dict, current_price: float) -> Optional[str]:
    """Return exit reason if any stop is triggered, else None.

    Once a ratchet step has armed, `stop_label` holds the distinct exit reason
    ("breakeven_stop" or "step_stop") so the dashboard/stats don't mis-book a
    protected exit as a full hard/ATR stop. When no step has armed yet (stop_label
    is None) the stop is still the base ATR/hard stop, classified by the drop size.
    """
    if current_price <= position["stop_price"]:
        label = position.get("stop_label")
        if label:
            return label
        entry = position["entry_price"]
        if (entry - current_price) / entry >= config.HARD_BLOCKER_PCT:
            return "hard_blocker"
        return "atr_stop"
    return None


def update_dynamic_stop(position: dict, current_price: float) -> None:
    """Ratchet the stop up through the STEP_STOPS gradini as peak gain grows.

    Mirrors the backtested 'Step C' variant exactly. STEP_STOPS is a list of
    (peak_trigger_pct, stop_floor_pct) tuples relative to entry. When peak gain
    reaches a step's trigger, the stop is raised to entry*(1+floor) — but only if
    that is higher than the current stop. The stop only ever ratchets UP. The first
    step (floor 0.0) is plain break-even; later steps lock in a growing slice of profit.

    Mutates `position` in place (peak_price, stop_price, stop_label, breakeven_armed).
    The caller must pass a validated, positive current_price (monitor_positions guards
    this) so a stale/garbage print can never set a phantom peak or arm a step early.
    """
    if config.STEP_STOPS is None:
        return
    entry = position["entry_price"]
    if entry <= 0:
        return

    # Track the running peak (defensive .get for positions recovered after a restart,
    # which are reconstructed without this field).
    peak = max(position.get("peak_price", entry), current_price)
    position["peak_price"] = peak
    peak_gain = (peak - entry) / entry

    # Find the highest step floor the peak has unlocked, then ratchet up to it.
    # Sorted ascending so we always end on the highest qualifying floor.
    new_stop = position["stop_price"]
    new_label = position.get("stop_label")
    for trigger, floor in sorted(config.STEP_STOPS):
        if peak_gain >= trigger:
            candidate = round(entry * (1 + floor), 4)
            if candidate > new_stop:
                new_stop = candidate
                new_label = "breakeven_stop" if floor == 0.0 else "step_stop"

    if new_stop > position["stop_price"]:
        old_stop = position["stop_price"]
        position["stop_price"] = new_stop
        position["stop_label"] = new_label
        position["breakeven_armed"] = True
        ticker = position["ticker"]
        locked_pct = (new_stop - entry) / entry * 100
        logger.info(
            f"{ticker}: stop ratcheted ({new_label}) — peak gain {peak_gain * 100:.2f}%, "
            f"stop ${old_stop:.2f} → ${new_stop:.2f} (locks {locked_pct:+.1f}% vs entry)"
        )


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
            for p in mature:
                t = p["ticker"]
                if t not in actual_tickers:
                    _reconcile_misses[t] = _reconcile_misses.get(t, 0) + 1
                    if _reconcile_misses[t] >= config.RECONCILE_MISS_LIMIT:
                        logger.warning(
                            f"{t}: absent from Alpaca for {_reconcile_misses[t]} consecutive cycles "
                            "— treating as manually closed"
                        )
                        telegram.send_message(
                            f"⚠️ <b>{t}</b>: posizione non trovata su Alpaca per "
                            f"{_reconcile_misses[t]} cicli consecutivi — rimossa dal monitoring. "
                            "Verifica su Alpaca."
                        )
                    else:
                        logger.warning(
                            f"{t}: absent from Alpaca (miss {_reconcile_misses[t]}/{config.RECONCILE_MISS_LIMIT}) "
                            "— will remove next cycle if still missing"
                        )
                else:
                    _reconcile_misses.pop(t, None)
            to_remove = {t for t, n in _reconcile_misses.items() if n >= config.RECONCILE_MISS_LIMIT}
            if to_remove:
                open_positions = [p for p in open_positions if p["ticker"] not in to_remove]
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

            # Price sanity gate: a non-positive or non-finite print must never drive a
            # stop, a VWAP exit, or the break-even ratchet. We had real incidents where
            # bad/stale data mis-booked trades — hold the position and retry next cycle.
            if not (isinstance(current_price, (int, float)) and current_price > 0 and math.isfinite(current_price)):
                logger.warning(f"{ticker}: invalid current price ({current_price!r}) — skipping checks this cycle")
                still_open.append(position)
                continue

            # Ratchet the stop up through the step gradini before evaluating it, so a
            # price that both arms a step and then dips this same cycle is handled correctly.
            update_dynamic_stop(position, current_price)

            # Stop check
            stop_reason = check_stop_triggered(position, current_price)
            if stop_reason:
                exit_info = close_position(ticker, position["qty"], stop_reason, fallback_price=position["entry_price"])
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
                exit_info = close_position(ticker, position["qty"], "vwap_exit", fallback_price=position["entry_price"])
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
    """Force-close all open positions at 15:45 ET. Retries up to EOD_CLOSE_ATTEMPTS times."""
    closed = []
    for position in open_positions:
        ticker = position["ticker"]
        exit_info = None
        for attempt in range(config.EOD_CLOSE_ATTEMPTS):
            exit_info = close_position(ticker, position["qty"], "eod_close", fallback_price=position["entry_price"])
            if exit_info:
                break
            if attempt < config.EOD_CLOSE_ATTEMPTS - 1:
                logger.warning(f"{ticker}: EOD close attempt {attempt + 1} failed — retrying in 2s")
                time.sleep(2)
        if exit_info:
            pnl = (exit_info["exit_price"] - position["entry_price"]) * position["qty"]
            position.update(exit_info)
            position["pnl_usd"] = round(pnl, 2)
            position["pnl_pct"] = round(
                (exit_info["exit_price"] - position["entry_price"]) / position["entry_price"], 4
            )
            daily_pnl += pnl
            closed.append(position)
        else:
            logger.error(f"{ticker}: EOD close FAILED after {config.EOD_CLOSE_ATTEMPTS} attempts — manual action required")
            telegram.send_message(
                f"🚨 <b>{ticker}</b>: chiusura EOD fallita dopo {config.EOD_CLOSE_ATTEMPTS} tentativi! "
                "Chiudi manualmente su Alpaca."
            )
    return closed, daily_pnl


def daily_loss_limit_reached(daily_pnl: float) -> bool:
    if config.MAX_DAILY_LOSS_USD is None:
        return False
    return daily_pnl <= -config.MAX_DAILY_LOSS_USD
