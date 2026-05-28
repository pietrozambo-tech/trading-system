"""
Backtesting engine.

Simulates the daily trading loop on historical data.
- No LLM calls: catalyst multiplier is proxied from earnings data.
- Confidence score computed on S1/S2/S3/S4 signals only.
- All parameters configurable for sensitivity analysis.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import pytz

from data import fetcher
from signals.triggers import (
    s1_above_vwap,
    s2_or_position,
    s3_gap_retention,
    calc_confidence,
)

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


@dataclass
class BacktestParams:
    confidence_threshold: float = 0.65
    hard_blocker_pct: float = 0.045
    atr_multiplier: float = 1.5
    or_position_threshold: float = 0.66
    gap_retention_threshold: float = 0.70
    position_size_usd: float = 500.0
    max_positions: int = 2
    catalyst_earnings: float = 1.0   # proxy: had earnings yesterday
    catalyst_default: float = 0.80   # all others assumed Tier 2


@dataclass
class TradeResult:
    ticker: str
    date: str
    entry_price: float
    exit_price: float
    exit_reason: str
    qty: int
    pnl_usd: float
    pnl_pct: float
    confidence: float
    above_vwap: bool
    or_position: float
    gap_retention: float


@dataclass
class BacktestResults:
    trades: list[TradeResult] = field(default_factory=list)
    daily_pnl: dict = field(default_factory=dict)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trades if t.pnl_usd > 0)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades else 0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_usd for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0)
        gross_loss   = abs(sum(t.pnl_usd for t in self.trades if t.pnl_usd < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_usd for t in self.trades if t.pnl_usd > 0]
        return sum(wins) / len(wins) if wins else 0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_usd for t in self.trades if t.pnl_usd < 0]
        return sum(losses) / len(losses) if losses else 0

    @property
    def avg_win_loss_ratio(self) -> float:
        return abs(self.avg_win / self.avg_loss) if self.avg_loss != 0 else float("inf")

    @property
    def max_drawdown(self) -> float:
        equity = [0.0]
        for pnl in [t.pnl_usd for t in self.trades]:
            equity.append(equity[-1] + pnl)
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def summary(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "avg_win_loss_ratio": round(self.avg_win_loss_ratio, 4),
            "total_pnl_usd": round(self.total_pnl, 2),
            "avg_win_usd": round(self.avg_win, 2),
            "avg_loss_usd": round(self.avg_loss, 2),
            "max_drawdown_usd": round(self.max_drawdown, 2),
        }


def _get_trading_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _simulate_day(
    ticker: str,
    session_date: date,
    params: BacktestParams,
    had_earnings_yesterday: bool = False,
) -> Optional[TradeResult]:
    """
    Simulate one ticker on one day.
    Returns TradeResult if a trade was taken, else None.
    """
    try:
        daily = fetcher.get_daily_bars(ticker, lookback_days=25)
        if len(daily) < 2:
            return None

        daily.index = pd.to_datetime(daily.index)
        daily_before = daily[daily.index.date < session_date]
        if len(daily_before) < 2:
            return None

        prev_close = float(daily_before["close"].iloc[-1])

        bars_or = fetcher.get_opening_range_bars(ticker, session_date)
        if bars_or.empty or len(bars_or) < 2:
            return None

        open_930  = float(bars_or["open"].iloc[0])
        price_945 = float(bars_or["close"].iloc[-1])

        if abs(open_930 - prev_close) / prev_close < 0.015:
            return None  # no meaningful gap

        catalyst_mult = params.catalyst_earnings if had_earnings_yesterday else params.catalyst_default

        above_vwap  = s1_above_vwap(bars_or, price_945)
        or_pos      = s2_or_position(bars_or, price_945)
        gap_ret     = s3_gap_retention(bars_or, open_930, prev_close)

        # No historical 15-min volume in backtest — use neutral boost
        vol_boost = 0.05
        confidence = calc_confidence(above_vwap, or_pos, gap_ret, catalyst_mult, vol_boost)

        if confidence < params.confidence_threshold:
            return None

        # Simulate entry
        entry_price = price_945
        qty = max(1, int(params.position_size_usd / entry_price))

        # Stops
        atr14 = fetcher.get_atr14(ticker)
        stop_atr  = entry_price - (atr14 * params.atr_multiplier) if atr14 > 0 else 0
        stop_hard = entry_price * (1 - params.hard_blocker_pct)
        stop_price = max(stop_atr, stop_hard)

        # Replay intraday bars after 9:45
        intraday = fetcher.get_intraday_bars(ticker, minutes=1, session_date=session_date)
        if intraday.empty:
            return None
        intraday.index = intraday.index.tz_convert(ET)

        eod_cutoff_hour, eod_cutoff_min = 15, 45
        exit_price  = entry_price
        exit_reason = "eod_close"

        cumvol = 0.0
        cumtpvol = 0.0

        for ts, bar in intraday.iterrows():
            if ts.hour < 9 or (ts.hour == 9 and ts.minute < 45):
                continue
            if ts.hour > eod_cutoff_hour or (ts.hour == eod_cutoff_hour and ts.minute >= eod_cutoff_min):
                exit_reason = "eod_close"
                exit_price  = float(bar["close"])
                break

            price = float(bar["close"])

            # Hard stop / ATR stop
            if price <= stop_price:
                exit_price  = stop_price
                exit_reason = "hard_blocker" if price <= entry_price * (1 - params.hard_blocker_pct) else "atr_stop"
                break

            # VWAP trailing exit (simplified: use cumulative VWAP from open)
            tp = (float(bar["high"]) + float(bar["low"]) + price) / 3
            cumvol   += float(bar["volume"])
            cumtpvol += tp * float(bar["volume"])
            vwap_now = cumtpvol / cumvol if cumvol > 0 else price

            if price < vwap_now and price > entry_price:
                exit_price  = price
                exit_reason = "vwap_exit"
                break

        pnl_usd = (exit_price - entry_price) * qty
        pnl_pct = (exit_price - entry_price) / entry_price

        return TradeResult(
            ticker=ticker,
            date=str(session_date),
            entry_price=round(entry_price, 4),
            exit_price=round(exit_price, 4),
            exit_reason=exit_reason,
            qty=qty,
            pnl_usd=round(pnl_usd, 2),
            pnl_pct=round(pnl_pct, 4),
            confidence=round(confidence, 4),
            above_vwap=above_vwap,
            or_position=round(or_pos, 4),
            gap_retention=round(gap_ret, 4),
        )

    except Exception as e:
        logger.warning(f"Backtest error {ticker} {session_date}: {e}")
        return None


def run_backtest(
    universe: list[str],
    start_date: date,
    end_date: date,
    params: Optional[BacktestParams] = None,
) -> BacktestResults:
    """
    Run backtest over date range for a universe of tickers.
    """
    if params is None:
        params = BacktestParams()

    results = BacktestResults()
    trading_days = _get_trading_days(start_date, end_date)

    for day in trading_days:
        day_pnl = 0.0
        day_trades = 0

        for ticker in universe:
            if day_trades >= params.max_positions:
                break
            trade = _simulate_day(ticker, day, params)
            if trade:
                results.trades.append(trade)
                day_pnl += trade.pnl_usd
                day_trades += 1

        results.daily_pnl[str(day)] = round(day_pnl, 2)

    logger.info(f"Backtest complete: {results.total_trades} trades | {results.summary()}")
    return results


def sensitivity_analysis(
    universe: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """
    Run backtest across a grid of parameter combinations.
    Returns a DataFrame ranked by profit factor.
    """
    import itertools

    confidence_thresholds = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
    hard_blocker_pcts     = [0.03, 0.035, 0.04, 0.045, 0.05, 0.055, 0.06]
    atr_multipliers       = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]

    rows = []
    combos = list(itertools.product(confidence_thresholds, hard_blocker_pcts, atr_multipliers))
    logger.info(f"Sensitivity: {len(combos)} combinations")

    for conf, hb, atr in combos:
        p = BacktestParams(
            confidence_threshold=conf,
            hard_blocker_pct=hb,
            atr_multiplier=atr,
        )
        res = run_backtest(universe, start_date, end_date, p)
        row = {"confidence_threshold": conf, "hard_blocker_pct": hb, "atr_multiplier": atr}
        row.update(res.summary())
        rows.append(row)

    df = pd.DataFrame(rows).sort_values("profit_factor", ascending=False)
    return df
