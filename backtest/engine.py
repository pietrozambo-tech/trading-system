"""
Backtesting engine v2 — bulk fetch + in-memory simulation.

Strategy:
  - Fetch all daily bars per ticker ONCE for the full period
  - Fetch all 1-min bars per ticker ONCE for the full period
  - Split intraday data by date in memory
  - Simulate each trading day from cached DataFrames

This reduces API calls from O(tickers × days × calls) → O(tickers × 2).
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import pytz
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

@dataclass
class BacktestParams:
    confidence_threshold:    float = 0.65
    hard_blocker_pct:        float = 0.020   # -2.0%
    vwap_exit_min_profit:    float = 0.015   # VWAP exit solo se profit >= 1.5%
    or_position_threshold:   float = 0.66
    gap_retention_threshold: float = 0.70
    min_gap_pct:             float = 0.005   # +0.5% long-only
    position_size_usd:       float = 49_000.0  # (100k - 2k cushion) / 2 trades
    max_positions:           int   = 2
    catalyst_bonus:          float = 0.10   # conservative proxy (Tier 3) — no real news in backtest


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    ticker:       str
    date:         str
    entry_price:  float
    exit_price:   float
    exit_reason:  str
    qty:          int
    pnl_usd:      float
    pnl_pct:      float
    confidence:   float
    above_vwap:   bool
    or_position:  float
    gap_retention: float


@dataclass
class BacktestResults:
    trades:    list[TradeResult] = field(default_factory=list)
    daily_pnl: dict              = field(default_factory=dict)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        wins = sum(1 for t in self.trades if t.pnl_usd > 0)
        return wins / self.total_trades if self.total_trades else 0

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
        equity, peak, max_dd = 0.0, 0.0, 0.0
        for t in self.trades:
            equity += t.pnl_usd
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    @property
    def trades_per_month(self) -> float:
        if not self.trades:
            return 0
        dates = sorted(set(t.date for t in self.trades))
        months = max(1, (pd.to_datetime(dates[-1]) - pd.to_datetime(dates[0])).days / 30)
        return self.total_trades / months

    def summary(self) -> dict:
        return {
            "total_trades":        self.total_trades,
            "win_rate":            round(self.win_rate, 4),
            "profit_factor":       round(self.profit_factor, 4),
            "avg_win_loss_ratio":  round(self.avg_win_loss_ratio, 4),
            "total_pnl_usd":       round(sum(t.pnl_usd for t in self.trades), 2),
            "avg_win_usd":         round(self.avg_win, 2),
            "avg_loss_usd":        round(self.avg_loss, 2),
            "max_drawdown_usd":    round(self.max_drawdown, 2),
            "trades_per_month":    round(self.trades_per_month, 1),
        }

    def to_dataframe(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.__dict__ for t in self.trades])


# ---------------------------------------------------------------------------
# Bulk data fetching
# ---------------------------------------------------------------------------

def _get_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
    )


def _fetch_daily_bars(client, ticker: str, start: date, end: date) -> pd.DataFrame:
    """All daily bars for a ticker in one API call."""
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start - timedelta(days=30), datetime.min.time()),
        end=datetime.combine(end, datetime.min.time()),
        feed="iex",
    )
    try:
        df = client.get_stock_bars(req).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(ticker, level="symbol")
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception as e:
        logger.warning(f"Daily bars fetch failed for {ticker}: {e}")
        return pd.DataFrame()


def _fetch_intraday_bars(client, ticker: str, start: date, end: date) -> dict[date, pd.DataFrame]:
    """
    All 1-min bars for a ticker for the full period in one API call.
    Returns dict keyed by session date.
    """
    start_dt = ET.localize(datetime.combine(start, datetime.strptime("09:28", "%H:%M").time()))
    end_dt   = ET.localize(datetime.combine(end,   datetime.strptime("16:01", "%H:%M").time()))
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute,
        start=start_dt,
        end=end_dt,
        feed="iex",
    )
    try:
        df = client.get_stock_bars(req).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(ticker, level="symbol")
        df.index = df.index.tz_convert(ET)
        by_date: dict[date, pd.DataFrame] = {}
        for d, group in df.groupby(df.index.date):
            by_date[d] = group
        return by_date
    except Exception as e:
        logger.warning(f"Intraday bars fetch failed for {ticker}: {e}")
        return {}


def prefetch_universe(
    universe: list[str],
    start: date,
    end: date,
) -> dict[str, dict]:
    """
    Pre-fetch all data for all tickers. Returns cache dict.
    Total API calls: len(universe) × 2.
    """
    client = _get_client()
    cache = {}
    for i, ticker in enumerate(universe, 1):
        logger.info(f"Fetching {ticker} ({i}/{len(universe)}) …")
        daily    = _fetch_daily_bars(client, ticker, start, end)
        intraday = _fetch_intraday_bars(client, ticker, start, end)
        cache[ticker] = {"daily": daily, "intraday": intraday}
    logger.info(f"Prefetch complete: {len(cache)} tickers")
    return cache


# ---------------------------------------------------------------------------
# Signal computation (from cached DataFrames)
# ---------------------------------------------------------------------------

def _calc_vwap(bars: pd.DataFrame) -> float:
    bars = bars.copy()
    bars["tp"] = (bars["high"] + bars["low"] + bars["close"]) / 3
    bars["tpv"] = bars["tp"] * bars["volume"]
    return float((bars["tpv"].cumsum() / bars["volume"].cumsum()).iloc[-1])


def _calc_atr14(daily: pd.DataFrame, as_of: date) -> float:
    hist = daily[daily.index.date < as_of].tail(15)
    if len(hist) < 2:
        return 0.0
    h, l, c = hist["high"], hist["low"], hist["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    val = tr.rolling(14).mean().iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def _calc_hist_15min_vol(intraday_by_date: dict, as_of: date, lookback: int = 20) -> float:
    """Compute rolling average of 9:30–9:40 volume from cached intraday data."""
    totals = []
    sorted_dates = sorted(d for d in intraday_by_date if d < as_of)
    for d in sorted_dates[-lookback:]:
        bars = intraday_by_date[d]
        or_bars = bars[(bars.index.hour == 9) & (bars.index.minute >= 30) & (bars.index.minute < 40)]
        if not or_bars.empty:
            totals.append(float(or_bars["volume"].sum()))
    return sum(totals) / len(totals) if totals else 0.0


# ---------------------------------------------------------------------------
# Single-day simulation
# ---------------------------------------------------------------------------

def _simulate_day(
    ticker: str,
    session_date: date,
    cache: dict,
    params: BacktestParams,
) -> Optional[TradeResult]:
    daily          = cache["daily"]
    intraday_cache = cache["intraday"]

    if session_date not in intraday_cache:
        return None

    # Previous close
    prev_daily = daily[daily.index.date < session_date]
    if len(prev_daily) < 2:
        return None
    prev_close = float(prev_daily["close"].iloc[-1])

    # Opening range bars (9:30–9:40)
    day_bars = intraday_cache[session_date]
    or_bars  = day_bars[
        (day_bars.index.hour == 9) &
        (day_bars.index.minute >= 30) &
        (day_bars.index.minute <= 40)
    ]
    if len(or_bars) < 2:
        return None

    open_930   = float(or_bars["open"].iloc[0])
    price_945  = float(or_bars["close"].iloc[-1])

    # L1 gap filter — long only, min +0.2%
    gap_pct = (open_930 - prev_close) / prev_close
    if gap_pct < params.min_gap_pct:
        return None

    # S1 — VWAP
    vwap      = _calc_vwap(or_bars)
    above_vwap = price_945 > vwap

    # S2 — Opening Range position
    or_high = float(or_bars["high"].max())
    or_low  = float(or_bars["low"].min())
    or_pos  = (price_945 - or_low) / (or_high - or_low) if or_high != or_low else 0.5

    # S3 — Gap retention
    gap_size  = open_930 - prev_close
    gap_eaten = open_930 - float(or_bars["low"].min())
    gap_ret   = 1.0 - (gap_eaten / gap_size) if abs(gap_size) > 0.001 else 1.0

    # S4 — Volume boost from cached historical 15min volume
    vol_today = float(or_bars["volume"].sum())
    vol_avg   = _calc_hist_15min_vol(intraday_cache, session_date, lookback=20)
    vol_ratio = vol_today / vol_avg if vol_avg > 0 else 0
    vol_boost = 0.10 if vol_ratio > 3 else (0.05 if vol_ratio > 2 else 0.0)

    # Catalyst proxy (no real news in backtest — use conservative Tier 3 bonus)
    direction_score = sum([
        above_vwap,
        or_pos > params.or_position_threshold,
        gap_ret > params.gap_retention_threshold,
    ])
    confidence = (direction_score / 3) + params.catalyst_bonus + vol_boost

    if confidence < params.confidence_threshold:
        return None

    # Entry
    entry_price = price_945
    qty = max(1, int(params.position_size_usd / entry_price))

    # Stops — tightest of ATR stop and hard blocker pct
    atr14      = _calc_atr14(daily, session_date)
    stop_atr   = entry_price - atr14 if atr14 > 0 else 0
    stop_pct   = entry_price * (1 - params.hard_blocker_pct)
    stop_price = max(stop_atr, stop_pct)

    # Replay bars from 9:40 to 15:45
    post_bars = day_bars[
        ((day_bars.index.hour == 9)  & (day_bars.index.minute >= 40)) |
        ((day_bars.index.hour > 9)   & (day_bars.index.hour < 15))    |
        ((day_bars.index.hour == 15) & (day_bars.index.minute <= 45))
    ]

    exit_price  = entry_price
    exit_reason = "eod_close"
    cumvol, cumtpvol = 0.0, 0.0

    for ts, bar in post_bars.iterrows():
        if ts.hour == 15 and ts.minute >= 45:
            exit_price  = float(bar["close"])
            exit_reason = "eod_close"
            break

        price = float(bar["close"])

        if price <= stop_price:
            exit_price = max(price, stop_price)
            exit_reason = "hard_blocker" if stop_pct >= stop_atr else "atr_stop"
            break

        tp = (float(bar["high"]) + float(bar["low"]) + price) / 3
        cumvol   += float(bar["volume"])
        cumtpvol += tp * float(bar["volume"])
        vwap_now  = cumtpvol / cumvol if cumvol > 0 else price

        profit_pct = (price - entry_price) / entry_price
        if price < vwap_now and profit_pct >= params.vwap_exit_min_profit:
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


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def _trading_days(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def run_backtest(
    universe: list[str],
    start_date: date,
    end_date: date,
    params: Optional[BacktestParams] = None,
) -> BacktestResults:
    if params is None:
        params = BacktestParams()

    logger.info(f"Backtest: {start_date} → {end_date} | {len(universe)} tickers")

    # Bulk fetch — O(tickers × 2) API calls
    all_cache = prefetch_universe(universe, start_date, end_date)

    results = BacktestResults()
    for day in _trading_days(start_date, end_date):
        day_pnl    = 0.0
        day_trades = 0

        for ticker in universe:
            if day_trades >= params.max_positions:
                break
            if ticker not in all_cache:
                continue
            trade = _simulate_day(ticker, day, all_cache[ticker], params)
            if trade:
                results.trades.append(trade)
                day_pnl += trade.pnl_usd
                day_trades += 1

        results.daily_pnl[str(day)] = round(day_pnl, 2)

    logger.info(f"Done: {results.total_trades} trades | {results.summary()}")
    return results


# ---------------------------------------------------------------------------
# Sensitivity analysis
# ---------------------------------------------------------------------------

def sensitivity_analysis(
    universe: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    import itertools

    logger.info("Pre-fetching data for sensitivity analysis …")
    all_cache = prefetch_universe(universe, start_date, end_date)
    days      = _trading_days(start_date, end_date)

    conf_thresholds  = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    hard_blockers    = [0.015, 0.020, 0.025, 0.030, 0.035, 0.040]

    combos = list(itertools.product(conf_thresholds, hard_blockers))
    logger.info(f"Sensitivity: {len(combos)} parameter combinations")

    rows = []
    for conf, hb in combos:
        p   = BacktestParams(confidence_threshold=conf, hard_blocker_pct=hb)
        res = BacktestResults()

        for day in days:
            day_trades = 0
            for ticker in universe:
                if day_trades >= p.max_positions or ticker not in all_cache:
                    continue
                trade = _simulate_day(ticker, day, all_cache[ticker], p)
                if trade:
                    res.trades.append(trade)
                    day_trades += 1

        row = {"confidence_threshold": conf, "hard_blocker_pct": hb}
        row.update(res.summary())
        rows.append(row)

    return pd.DataFrame(rows).sort_values("profit_factor", ascending=False)


def vwap_sensitivity_analysis(
    universe: list[str],
    start_date: date,
    end_date: date,
    thresholds: list[float] = None,
) -> pd.DataFrame:
    """
    Focused sensitivity analysis on vwap_exit_min_profit only.
    All other params fixed at default. Fast: N_thresholds runs total.
    """
    if thresholds is None:
        thresholds = [0.010, 0.015, 0.020, 0.025, 0.030]

    logger.info(f"VWAP sensitivity: {thresholds} — pre-fetching data …")
    all_cache = prefetch_universe(universe, start_date, end_date)
    days      = _trading_days(start_date, end_date)

    rows = []
    for threshold in thresholds:
        p   = BacktestParams(vwap_exit_min_profit=threshold)
        res = BacktestResults()

        for day in days:
            day_trades = 0
            for ticker in universe:
                if day_trades >= p.max_positions or ticker not in all_cache:
                    continue
                trade = _simulate_day(ticker, day, all_cache[ticker], p)
                if trade:
                    res.trades.append(trade)
                    day_trades += 1

        s = res.summary()
        # Exit reason breakdown
        if res.trades:
            import collections
            df_t = res.to_dataframe()
            exit_counts = df_t["exit_reason"].value_counts().to_dict()
        else:
            exit_counts = {}

        rows.append({
            "vwap_min_profit": f"{threshold:.1%}",
            "total_trades":    s["total_trades"],
            "win_rate":        f"{s['win_rate']:.1%}",
            "profit_factor":   s["profit_factor"],
            "avg_win_loss":    s["avg_win_loss_ratio"],
            "total_pnl_usd":   s["total_pnl_usd"],
            "avg_win_usd":     s["avg_win_usd"],
            "avg_loss_usd":    s["avg_loss_usd"],
            "max_dd_usd":      s["max_drawdown_usd"],
            "vwap_exits":      exit_counts.get("vwap_exit", 0),
            "eod_exits":       exit_counts.get("eod_close", 0),
            "stop_exits":      sum(v for k, v in exit_counts.items() if "stop" in k or "blocker" in k),
        })
        logger.info(f"  {threshold:.1%} → PF={s['profit_factor']:.2f} WR={s['win_rate']:.1%} PnL=${s['total_pnl_usd']:+.2f}")

    return pd.DataFrame(rows)
