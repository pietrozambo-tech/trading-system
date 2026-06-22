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
    min_adv:                 float = 5_000_000  # 5M shares/day (SIP consolidated)
    vol_ratio_high:          float = 3.0
    vol_ratio_mid:           float = 2.0
    position_size_usd:       float = 49_000.0  # (100k - 2k cushion) / 2 trades
    max_positions:           int   = 2
    catalyst_bonus:          float = 0.10   # conservative proxy (Tier 3) — no real news in backtest
    # --- Exit-strategy experiments (None = disabled → identical to current live behavior) ---
    breakeven_trigger_pct:   Optional[float] = None   # once peak gain ≥ this, move stop up to entry
    trailing_trigger_pct:    Optional[float] = None   # once peak gain ≥ this, activate a trailing stop
    trailing_distance_pct:   float = 0.010             # trailing stop sits this far below the intraday peak
    # Multi-step ratchet: list of (peak_trigger_pct, stop_floor_pct) tuples, each relative to entry.
    # Steps are applied in ascending trigger order; each step raises dyn_stop only if the new floor
    # is higher than the current stop. Example: [(0.005, 0.0), (0.015, 0.005)] means:
    #   peak ≥ +0.5% → stop at entry (break-even)
    #   peak ≥ +1.5% → stop at entry +0.5%
    # When step_stops is set it is processed independently of breakeven_trigger_pct / trailing.
    step_stops:              Optional[list] = None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    ticker:            str
    date:              str
    entry_price:       float
    exit_price:        float
    exit_reason:       str
    qty:               int
    pnl_usd:           float
    pnl_pct:           float
    confidence:        float
    post_open_advance: bool
    or_position:       float
    gap_retention:     float
    entry_offset_min:  int = 10


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
        feed="iex",  # SIP historical requires paid subscription; IEX sufficient for backtesting
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


def _calc_hist_or_vol(intraday_by_date: dict, as_of: date, lookback: int = 20) -> float:
    """Rolling average of 9:30–9:34 OR volume (5 bars) from cached intraday data."""
    totals = []
    sorted_dates = sorted(d for d in intraday_by_date if d < as_of)
    for d in sorted_dates[-lookback:]:
        bars = intraday_by_date[d]
        or_bars = bars[(bars.index.hour == 9) & (bars.index.minute >= 30) & (bars.index.minute <= 34)]
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

    # ADV filter (rolling 20-day average)
    adv_hist = prev_daily.tail(20)
    if len(adv_hist) >= 10 and float(adv_hist["volume"].mean()) < params.min_adv:
        return None

    # Opening range bars (9:30–9:34) — 5 complete bars, matches live system at 09:35
    day_bars = intraday_cache[session_date]
    or_bars  = day_bars[
        (day_bars.index.hour == 9) &
        (day_bars.index.minute >= 30) &
        (day_bars.index.minute <= 34)
    ]
    if len(or_bars) < 2:
        return None

    open_930   = float(or_bars["open"].iloc[0])
    price_935  = float(or_bars["close"].iloc[-1])

    # L1 gap filter — long only, min +0.5%
    gap_pct = (open_930 - prev_close) / prev_close
    if gap_pct < params.min_gap_pct:
        return None

    # S1 — Post-open advance: price moved up in first 5 minutes
    post_open_advance = price_935 > open_930

    # S2 — Opening Range position
    or_high = float(or_bars["high"].max())
    or_low  = float(or_bars["low"].min())
    or_pos  = (price_935 - or_low) / (or_high - or_low) if or_high != or_low else 0.5

    # S3 — Gap retention
    gap_size  = open_930 - prev_close
    gap_eaten = open_930 - float(or_bars["low"].min())
    gap_ret   = 1.0 - (gap_eaten / gap_size) if abs(gap_size) > 0.001 else 1.0

    # S4 — Volume boost from cached historical OR volume
    vol_today = float(or_bars["volume"].sum())
    vol_avg   = _calc_hist_or_vol(intraday_cache, session_date, lookback=20)
    vol_ratio = vol_today / vol_avg if vol_avg > 0 else 0
    vol_boost = 0.10 if vol_ratio > params.vol_ratio_high else (0.05 if vol_ratio > params.vol_ratio_mid else 0.0)

    # Catalyst proxy (no real news in backtest — use conservative Tier 3 bonus)
    direction_score = sum([
        post_open_advance,
        or_pos > params.or_position_threshold,
        gap_ret > params.gap_retention_threshold,
    ])
    confidence = (direction_score / 3) + params.catalyst_bonus + vol_boost

    if confidence < params.confidence_threshold:
        return None

    # Entry
    entry_price = price_935
    qty = max(1, int(params.position_size_usd / entry_price))

    # Stops — tightest of ATR stop and hard blocker pct
    atr14      = _calc_atr14(daily, session_date)
    stop_atr   = entry_price - atr14 if atr14 > 0 else 0
    stop_pct   = entry_price * (1 - params.hard_blocker_pct)
    stop_price = max(stop_atr, stop_pct)

    # Replay bars from 9:35 to 15:45
    post_bars = day_bars[
        ((day_bars.index.hour == 9)  & (day_bars.index.minute >= 35)) |
        ((day_bars.index.hour > 9)   & (day_bars.index.hour < 15))    |
        ((day_bars.index.hour == 15) & (day_bars.index.minute <= 45))
    ]

    exit_price  = entry_price
    exit_reason = "eod_close"
    cumvol, cumtpvol = 0.0, 0.0
    # Dynamic stop ratchets UP only. Starts at the ATR/hard stop; break-even and
    # trailing (when enabled) can raise it. With both disabled, dyn_stop never
    # moves and the loop behaves exactly like the current live system.
    base_label = "hard_blocker" if stop_pct >= stop_atr else "atr_stop"
    dyn_stop   = stop_price
    stop_label = base_label
    peak       = entry_price

    for ts, bar in post_bars.iterrows():
        if ts.hour == 15 and ts.minute >= 45:
            exit_price  = float(bar["close"])
            exit_reason = "eod_close"
            break

        price = float(bar["close"])
        peak  = max(peak, price)
        peak_gain = (peak - entry_price) / entry_price

        # Break-even: once the trade has shown enough profit, never let the stop sit below entry
        if params.breakeven_trigger_pct is not None and peak_gain >= params.breakeven_trigger_pct:
            if entry_price > dyn_stop:
                dyn_stop, stop_label = entry_price, "breakeven_stop"
        # Trailing: pull the stop to peak − distance once activated (ratchets up with new highs)
        if params.trailing_trigger_pct is not None and peak_gain >= params.trailing_trigger_pct:
            trail = peak * (1 - params.trailing_distance_pct)
            if trail > dyn_stop:
                dyn_stop, stop_label = trail, "trailing_stop"
        # Multi-step ratchet: each step raises the stop floor as peak gain grows.
        # Steps are sorted ascending so each one can only raise dyn_stop, never lower it.
        if params.step_stops is not None:
            for trigger, floor in sorted(params.step_stops):
                if peak_gain >= trigger:
                    new_stop = entry_price * (1 + floor)
                    if new_stop > dyn_stop:
                        dyn_stop = new_stop
                        # floor ≤ 0 is still a protective break-even-type exit (incl. a
                        # small sub-entry buffer); only a positive floor locks in profit.
                        stop_label = "breakeven_stop" if floor <= 0.0 else "step_stop"

        if price <= dyn_stop:
            exit_price  = max(price, dyn_stop)
            exit_reason = stop_label
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
        post_open_advance=post_open_advance,
        or_position=round(or_pos, 4),
        gap_retention=round(gap_ret, 4),
        entry_offset_min=5,  # 9:34 bar close ≈ price at 9:35 = offset 5 from 9:30
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


def exit_strategy_analysis(
    universe: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """
    Compare exit-strategy variants on the SAME set of entries.

    Entries are identical across variants (same signal filter), so trade count is
    constant — only the exit logic differs. This isolates the effect of break-even
    and trailing stops on win rate, avg loss, and profit factor: exactly the
    "how many INTC-type trades get saved without cutting AMD-type winners" question.
    """
    logger.info("Exit strategy analysis — pre-fetching data …")
    all_cache = prefetch_universe(universe, start_date, end_date)
    days      = _trading_days(start_date, end_date)

    # Break-even buffer experiment (18 Jun): does placing the break-even stop a hair
    # BELOW entry (instead of exactly at entry) absorb a full round-trip-to-entry pullback
    # and survive the recovery (MRVL case), or does the extra room just cost more on the
    # losers? Buffer is expressed as a negative first-step floor: (0.005, -0.002) = arm at
    # +0.5% peak, stop at entry−0.2%. References kept inline for an apples-to-apples read.
    STEP_C = [(0.005, 0.0), (0.015, 0.010), (0.030, 0.020)]
    variants = [
        ("baseline (VWAP 1.5%)",            BacktestParams()),
        # — pure break-even +0.5%, buffer sweep —
        ("break-even +0.5% (no buffer)",    BacktestParams(step_stops=[(0.005,  0.000)])),
        ("break-even +0.5% / buffer −0.2%", BacktestParams(step_stops=[(0.005, -0.002)])),
        ("break-even +0.5% / buffer −0.3%", BacktestParams(step_stops=[(0.005, -0.003)])),
        # — Step C (live) + the same buffer on its break-even floor —
        ("Step C (live)",                   BacktestParams(step_stops=STEP_C)),
        ("Step C / buffer −0.2%",           BacktestParams(step_stops=[(0.005, -0.002), (0.015, 0.010), (0.030, 0.020)])),
        ("Step C / buffer −0.3%",           BacktestParams(step_stops=[(0.005, -0.003), (0.015, 0.010), (0.030, 0.020)])),
    ]

    rows = []
    for name, p in variants:
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

        s  = res.summary()
        df = res.to_dataframe() if res.trades else pd.DataFrame()
        ec = df["exit_reason"].value_counts().to_dict() if not df.empty else {}
        rows.append({
            "variant":       name,
            "trades":        s["total_trades"],
            "win_rate":      f"{s['win_rate']:.1%}",
            "profit_factor": s["profit_factor"],
            "avg_win_usd":   s["avg_win_usd"],
            "avg_loss_usd":  s["avg_loss_usd"],
            "total_pnl_usd": s["total_pnl_usd"],
            "max_dd_usd":    s["max_drawdown_usd"],
            "vwap_exits":      ec.get("vwap_exit", 0),
            "breakeven_exits": ec.get("breakeven_stop", 0),
            "step_exits":      ec.get("step_stop", 0),
            "trailing_exits":  ec.get("trailing_stop", 0),
            "hardstop_exits":  ec.get("hard_blocker", 0) + ec.get("atr_stop", 0),
            "eod_exits":       ec.get("eod_close", 0),
        })
        logger.info(f"  {name}: PF={s['profit_factor']:.2f} WR={s['win_rate']:.1%} PnL=${s['total_pnl_usd']:+.0f}")

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Entry timing analysis
# ---------------------------------------------------------------------------

def _precompute_vwap_series(day_bars: pd.DataFrame) -> dict:
    """Running intraday VWAP from first bar — returns {timestamp: vwap} dict for O(1) lookup."""
    tp     = (day_bars["high"] + day_bars["low"] + day_bars["close"]) / 3
    tpv    = tp * day_bars["volume"]
    cumvol = day_bars["volume"].cumsum()
    series = (tpv.cumsum() / cumvol.replace(0, float("nan")))
    return series.to_dict()


def _simulate_day_all_entries(
    ticker: str,
    session_date: date,
    cache: dict,
    params: BacktestParams,
    entry_offsets: list[int],
) -> list[TradeResult]:
    """
    Compute OR signals (9:30–9:34), then simulate a trade for each entry offset (minutes from 9:30).
    Entry offsets < 5 are 'oracle': they use signal data from bars not yet closed at entry.
    Offset=5 is live-equivalent (enters on 9:34 bar close ≈ price at 9:35).
    Returns one TradeResult per offset for setups that pass the confidence threshold.
    """
    daily          = cache["daily"]
    intraday_cache = cache["intraday"]

    if session_date not in intraday_cache:
        return []

    day_bars   = intraday_cache[session_date]
    prev_daily = daily[daily.index.date < session_date]
    if len(prev_daily) < 2:
        return []
    prev_close = float(prev_daily["close"].iloc[-1])

    # ADV filter
    adv_hist = prev_daily.tail(20)
    if len(adv_hist) >= 10 and float(adv_hist["volume"].mean()) < params.min_adv:
        return []

    # Opening range bars 9:30–9:34 (5 bars, matches live)
    or_bars = day_bars[
        (day_bars.index.hour == 9) &
        (day_bars.index.minute >= 30) &
        (day_bars.index.minute <= 34)
    ]
    if len(or_bars) < 2:
        return []

    open_930  = float(or_bars["open"].iloc[0])
    price_935 = float(or_bars["close"].iloc[-1])

    gap_pct = (open_930 - prev_close) / prev_close
    if gap_pct < params.min_gap_pct or open_930 < prev_close:
        return []

    # Signals at 9:35 (unchanged from live system)
    post_open_advance = price_935 > open_930
    or_high    = float(or_bars["high"].max())
    or_low     = float(or_bars["low"].min())
    or_pos     = (price_935 - or_low) / (or_high - or_low) if or_high != or_low else 0.5
    gap_eaten  = open_930 - float(or_bars["low"].min())
    gap_size   = open_930 - prev_close
    gap_ret    = 1.0 - (gap_eaten / gap_size) if abs(gap_size) > 0.001 else 1.0

    vol_today = float(or_bars["volume"].sum())
    vol_avg   = _calc_hist_or_vol(intraday_cache, session_date)
    vol_ratio = vol_today / vol_avg if vol_avg > 0 else 0
    vol_boost = (
        0.10 if vol_ratio > params.vol_ratio_high else
        0.05 if vol_ratio > params.vol_ratio_mid  else
        0.0
    )

    direction = sum([post_open_advance, or_pos > params.or_position_threshold, gap_ret > params.gap_retention_threshold])
    confidence = (direction / 3) + params.catalyst_bonus + vol_boost

    if confidence < params.confidence_threshold:
        return []

    atr14      = _calc_atr14(daily, session_date)
    vwap_dict  = _precompute_vwap_series(day_bars)
    results    = []

    for offset in entry_offsets:
        # Bar at minute M has close = price at end of minute M+1
        # offset=1  → bar at 9:30 → close ≈ price at 9:31
        # offset=5  → bar at 9:34 → close ≈ price at 9:35 (live-equivalent)
        # offset=15 → bar at 9:44 → close ≈ price at 9:45
        total_min      = 9 * 60 + 30 + offset - 1
        e_hour, e_min  = divmod(total_min, 60)
        entry_df       = day_bars[(day_bars.index.hour == e_hour) & (day_bars.index.minute == e_min)]
        if entry_df.empty:
            continue

        entry_ts    = entry_df.index[0]
        entry_price = float(entry_df.iloc[0]["close"])
        qty         = max(1, int(params.position_size_usd / entry_price))
        stop_atr    = entry_price - atr14 if atr14 > 0 else 0
        stop_pct    = entry_price * (1 - params.hard_blocker_pct)
        stop_price  = max(stop_atr, stop_pct)
        stop_label  = "hard_blocker" if stop_pct >= stop_atr else "atr_stop"

        post = day_bars[
            (day_bars.index > entry_ts) &
            ((day_bars.index.hour < 15) | ((day_bars.index.hour == 15) & (day_bars.index.minute <= 45)))
        ]

        exit_price  = float(post["close"].iloc[-1]) if not post.empty else entry_price
        exit_reason = "eod_close"

        for ts, bar in post.iterrows():
            price = float(bar["close"])
            if ts.hour == 15 and ts.minute >= 45:
                exit_price, exit_reason = price, "eod_close"
                break
            if float(bar["low"]) <= stop_price:
                exit_price  = max(float(bar["low"]), stop_price)
                exit_reason = stop_label
                break
            vwap_now = vwap_dict.get(ts, float("nan"))
            if not pd.isna(vwap_now) and (price - entry_price) / entry_price >= params.vwap_exit_min_profit and price < vwap_now:
                exit_price, exit_reason = price, "vwap_exit"
                break

        results.append(TradeResult(
            ticker           = ticker,
            date             = str(session_date),
            entry_price      = round(entry_price, 4),
            exit_price       = round(exit_price, 4),
            exit_reason      = exit_reason,
            qty              = qty,
            pnl_usd          = round((exit_price - entry_price) * qty, 2),
            pnl_pct          = round((exit_price - entry_price) / entry_price, 4),
            confidence       = round(confidence, 4),
            post_open_advance = post_open_advance,
            or_position      = round(or_pos, 4),
            gap_retention    = round(gap_ret, 4),
            entry_offset_min = offset,
        ))

    return results


def run_entry_timing_backtest(
    universe: list[str],
    start_date: date,
    end_date: date,
    params: Optional[BacktestParams] = None,
    entry_offsets: list[int] = None,
) -> dict[int, BacktestResults]:
    """
    Run backtest with multiple entry times. Returns dict: offset_min → BacktestResults.
    All offsets share the same signal filter (9:30–9:34 OR window).
    Offsets < 5 are 'oracle': they use signal data from bars not yet closed at entry.
    Offset=5 is live-equivalent (9:34 bar close ≈ price at 9:35).
    """
    if params is None:
        params = BacktestParams()
    if entry_offsets is None:
        entry_offsets = [1, 3, 5, 10, 15]

    logger.info(f"Entry timing backtest: {start_date}→{end_date} | {len(universe)} tickers | offsets={entry_offsets}")
    all_cache         = prefetch_universe(universe, start_date, end_date)
    results_by_offset = {o: BacktestResults() for o in entry_offsets}
    days              = _trading_days(start_date, end_date)

    for day in days:
        counts = {o: 0 for o in entry_offsets}
        for ticker in universe:
            if all(counts[o] >= params.max_positions for o in entry_offsets):
                break
            if ticker not in all_cache:
                continue
            trades = _simulate_day_all_entries(ticker, day, all_cache[ticker], params, entry_offsets)
            for trade in trades:
                o = trade.entry_offset_min
                if counts[o] < params.max_positions:
                    results_by_offset[o].trades.append(trade)
                    results_by_offset[o].daily_pnl[str(day)] = (
                        results_by_offset[o].daily_pnl.get(str(day), 0.0) + trade.pnl_usd
                    )
                    counts[o] += 1

    logger.info("Entry timing backtest complete.")
    return results_by_offset


# ---------------------------------------------------------------------------
# Max-positions analysis (1 vs 2 vs 3 slots, confidence-sorted selection)
# ---------------------------------------------------------------------------

def max_positions_analysis(
    universe: list[str],
    start_date: date,
    end_date: date,
) -> tuple["pd.DataFrame", dict]:
    """Compare taking 1, 2, or 3 positions per day.

    Selection is by confidence (highest first) so the "3rd trade" is the
    3rd-best signal that day — not the 3rd ticker in the universe list.
    Position size scales as $99k / N so total capital at risk stays constant.
    Exit strategy is the live Step C + buffer −0.2%.
    """
    EQUITY = 99_000  # $100k − $1k cushion
    STEP_C_BUFFER = [(0.005, -0.002), (0.015, 0.010), (0.030, 0.020)]

    logger.info("Max-positions analysis — pre-fetching data …")
    all_cache = prefetch_universe(universe, start_date, end_date)
    days      = _trading_days(start_date, end_date)

    # One pass: collect all qualifying trades per day with unit position size
    # (exit logic is position-size-independent; qty/pnl are rescaled per scenario).
    base_params = BacktestParams(step_stops=STEP_C_BUFFER, position_size_usd=1)
    daily_candidates: dict[date, list[TradeResult]] = {}
    for day in days:
        candidates = []
        for ticker in universe:
            if ticker not in all_cache:
                continue
            t = _simulate_day(ticker, day, all_cache[ticker], base_params)
            if t:
                candidates.append(t)
        # Sort best-first so [:n] always picks the top N by confidence
        daily_candidates[day] = sorted(candidates, key=lambda x: x.confidence, reverse=True)

    results_by_n: dict[int, BacktestResults] = {}
    summary_rows = []

    for n in [1, 2, 3]:
        size = EQUITY / n
        res  = BacktestResults()
        for day in days:
            for t in daily_candidates[day][:n]:
                qty = max(1, int(size / t.entry_price))
                pnl = (t.exit_price - t.entry_price) * qty
                res.trades.append(TradeResult(
                    ticker=t.ticker, date=t.date,
                    entry_price=t.entry_price, exit_price=t.exit_price,
                    exit_reason=t.exit_reason, qty=qty,
                    pnl_usd=round(pnl, 2), pnl_pct=t.pnl_pct,
                    confidence=t.confidence, post_open_advance=t.post_open_advance,
                    or_position=t.or_position, gap_retention=t.gap_retention,
                    entry_offset_min=t.entry_offset_min,
                ))
                res.daily_pnl[str(day)] = res.daily_pnl.get(str(day), 0.0) + pnl

        results_by_n[n] = res
        s  = res.summary()
        df = res.to_dataframe() if res.trades else pd.DataFrame()
        ec = df["exit_reason"].value_counts().to_dict() if not df.empty else {}
        nt = s["total_trades"]
        days_with_n = sum(1 for d in days if len(daily_candidates.get(d, [])) >= n)

        logger.info(
            f"  N={n}: {nt} trades | PF={s['profit_factor']:.2f} "
            f"WR={s['win_rate']:.1%} P&L=${s['total_pnl_usd']:+,.0f}"
        )
        summary_rows.append({
            "max_positions":    n,
            "position_size":    f"${size:,.0f}",
            "total_trades":     nt,
            "days_with_N_slots": days_with_n,
            "win_rate":         f"{s['win_rate']:.1%}",
            "profit_factor":    round(s["profit_factor"], 2),
            "total_pnl_usd":    s["total_pnl_usd"],
            "avg_win_usd":      s["avg_win_usd"],
            "avg_loss_usd":     s["avg_loss_usd"],
            "max_dd_usd":       s["max_drawdown_usd"],
            "trades_per_month": round(s["trades_per_month"], 1),
            "vwap_exits":       ec.get("vwap_exit", 0),
            "stop_exits":       sum(v for k, v in ec.items() if "stop" in k or "blocker" in k),
            "eod_exits":        ec.get("eod_close", 0),
        })

    return pd.DataFrame(summary_rows), results_by_n


# ---------------------------------------------------------------------------
# True (non-oracle) entry timing analysis
# ---------------------------------------------------------------------------

def _simulate_day_true_entry(
    ticker: str,
    session_date: date,
    cache: dict,
    params: BacktestParams,
    entry_offset_min: int,
) -> Optional[TradeResult]:
    """
    Non-oracle entry: signals computed from bars available at entry time only.
    entry_offset_min=1  → signals from 9:30 bar only (1 min of data)
    entry_offset_min=3  → signals from 9:30–9:32 (3 bars)
    entry_offset_min=5  → signals from 9:30–9:34 (5 bars) — same as live (9:35 entry)
    entry_offset_min=6  → signals from 9:30–9:35 (6 bars)
    """
    daily          = cache["daily"]
    intraday_cache = cache["intraday"]

    if session_date not in intraday_cache:
        return None

    day_bars   = intraday_cache[session_date]
    prev_daily = daily[daily.index.date < session_date]
    if len(prev_daily) < 2:
        return None
    prev_close = float(prev_daily["close"].iloc[-1])

    # ADV filter
    adv_hist = prev_daily.tail(20)
    if len(adv_hist) >= 10 and float(adv_hist["volume"].mean()) < params.min_adv:
        return None

    # OR bars available at entry time (9:30 bar through entry bar inclusive)
    total_min = 9 * 60 + 30 + entry_offset_min - 1
    e_hour, e_min = divmod(total_min, 60)

    bar_minutes = day_bars.index.hour * 60 + day_bars.index.minute
    or_bars = day_bars[(bar_minutes >= 9 * 60 + 30) & (bar_minutes <= total_min)]

    if len(or_bars) < 1:
        return None

    open_930        = float(or_bars["open"].iloc[0])
    price_at_entry  = float(or_bars["close"].iloc[-1])

    # Gap filter
    gap_pct = (open_930 - prev_close) / prev_close
    if gap_pct < params.min_gap_pct or open_930 < prev_close:
        return None

    # Signals from available bars only
    post_open_advance = price_at_entry > open_930
    or_high    = float(or_bars["high"].max())
    or_low     = float(or_bars["low"].min())
    or_pos     = (price_at_entry - or_low) / (or_high - or_low) if or_high != or_low else 0.5
    gap_eaten  = open_930 - or_low
    gap_size   = open_930 - prev_close
    gap_ret    = 1.0 - (gap_eaten / gap_size) if abs(gap_size) > 0.001 else 1.0

    # Volume: scale historical OR avg (5-bar baseline, 9:30–9:34) to the current entry window
    vol_today      = float(or_bars["volume"].sum())
    vol_avg_or     = _calc_hist_or_vol(intraday_cache, session_date)
    vol_avg_scaled = vol_avg_or * (entry_offset_min / 5.0) if vol_avg_or > 0 else 0
    vol_ratio      = vol_today / vol_avg_scaled if vol_avg_scaled > 0 else 0
    vol_boost      = (
        0.10 if vol_ratio > params.vol_ratio_high else
        0.05 if vol_ratio > params.vol_ratio_mid  else
        0.0
    )

    direction  = sum([post_open_advance, or_pos > params.or_position_threshold, gap_ret > params.gap_retention_threshold])
    confidence = (direction / 3) + params.catalyst_bonus + vol_boost

    if confidence < params.confidence_threshold:
        return None

    # Trade simulation (same exit logic for all offsets)
    entry_ts    = or_bars.index[-1]
    entry_price = price_at_entry
    qty         = max(1, int(params.position_size_usd / entry_price))
    atr14       = _calc_atr14(daily, session_date)
    stop_atr    = entry_price - atr14 if atr14 > 0 else 0
    stop_pct    = entry_price * (1 - params.hard_blocker_pct)
    stop_price  = max(stop_atr, stop_pct)
    stop_label  = "hard_blocker" if stop_pct >= stop_atr else "atr_stop"
    vwap_dict   = _precompute_vwap_series(day_bars)

    post = day_bars[
        (day_bars.index > entry_ts) &
        ((day_bars.index.hour < 15) | ((day_bars.index.hour == 15) & (day_bars.index.minute <= 45)))
    ]

    exit_price  = float(post["close"].iloc[-1]) if not post.empty else entry_price
    exit_reason = "eod_close"

    for ts, bar in post.iterrows():
        price = float(bar["close"])
        if ts.hour == 15 and ts.minute >= 45:
            exit_price, exit_reason = price, "eod_close"
            break
        if float(bar["low"]) <= stop_price:
            exit_price  = max(float(bar["low"]), stop_price)
            exit_reason = stop_label
            break
        vwap_now = vwap_dict.get(ts, float("nan"))
        if not pd.isna(vwap_now) and (price - entry_price) / entry_price >= params.vwap_exit_min_profit and price < vwap_now:
            exit_price, exit_reason = price, "vwap_exit"
            break

    return TradeResult(
        ticker           = ticker,
        date             = str(session_date),
        entry_price      = round(entry_price, 4),
        exit_price       = round(exit_price, 4),
        exit_reason      = exit_reason,
        qty              = qty,
        pnl_usd          = round((exit_price - entry_price) * qty, 2),
        pnl_pct          = round((exit_price - entry_price) / entry_price, 4),
        confidence       = round(confidence, 4),
        post_open_advance = post_open_advance,
        or_position      = round(or_pos, 4),
        gap_retention    = round(gap_ret, 4),
        entry_offset_min = entry_offset_min,
    )


def run_true_entry_timing_backtest(
    universe: list[str],
    start_date: date,
    end_date: date,
    params: Optional[BacktestParams] = None,
    entry_offsets: list[int] = None,
) -> dict[int, BacktestResults]:
    """
    Non-oracle entry timing backtest.
    Each offset uses only bars available at entry time for signal computation.
    Offsets run independently — a stock can appear in multiple offsets on the same day.
    """
    if params is None:
        params = BacktestParams()
    if entry_offsets is None:
        entry_offsets = [1, 3, 5, 6]  # 6 = live-equivalent (9:35 bar close)

    logger.info(f"True entry timing backtest: {start_date}→{end_date} | {len(universe)} tickers | offsets={entry_offsets}")
    all_cache         = prefetch_universe(universe, start_date, end_date)
    results_by_offset = {o: BacktestResults() for o in entry_offsets}
    days              = _trading_days(start_date, end_date)

    for day in days:
        counts = {o: 0 for o in entry_offsets}
        for ticker in universe:
            if all(counts[o] >= params.max_positions for o in entry_offsets):
                break
            if ticker not in all_cache:
                continue
            for offset in entry_offsets:
                if counts[offset] >= params.max_positions:
                    continue
                trade = _simulate_day_true_entry(ticker, day, all_cache[ticker], params, offset)
                if trade:
                    results_by_offset[offset].trades.append(trade)
                    results_by_offset[offset].daily_pnl[str(day)] = (
                        results_by_offset[offset].daily_pnl.get(str(day), 0.0) + trade.pnl_usd
                    )
                    counts[offset] += 1

    logger.info("True entry timing backtest complete.")
    return results_by_offset
