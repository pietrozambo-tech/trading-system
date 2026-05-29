import logging
from datetime import date
from typing import Optional

import pandas as pd

import config
from data import fetcher

logger = logging.getLogger(__name__)


def calc_vwap(bars: pd.DataFrame) -> float:
    """VWAP from 1-min bars using typical price × volume."""
    bars = bars.copy()
    bars["typical_price"] = (bars["high"] + bars["low"] + bars["close"]) / 3
    bars["tp_vol"] = bars["typical_price"] * bars["volume"]
    return float((bars["tp_vol"].cumsum() / bars["volume"].cumsum()).iloc[-1])


def s1_above_vwap(bars_or: pd.DataFrame, price_945: float) -> bool:
    """S1: Price at 9:45 above opening-range VWAP."""
    vwap = calc_vwap(bars_or)
    result = price_945 > vwap
    return result


def s2_or_position(bars_or: pd.DataFrame, price_945: float) -> float:
    """S2: Position in opening range. > 0.66 = strong."""
    or_high = float(bars_or["high"].max())
    or_low  = float(bars_or["low"].min())
    if or_high == or_low:
        return 0.5
    return (price_945 - or_low) / (or_high - or_low)


def s3_gap_retention(bars_or: pd.DataFrame, open_930: float, prev_close: float) -> float:
    """S3: Fraction of gap remaining after 15 min. > 0.70 = defended."""
    gap_size = open_930 - prev_close
    if abs(gap_size) < 0.0001:
        return 1.0  # effectively no gap
    gap_eaten = open_930 - float(bars_or["low"].min())
    return 1.0 - (gap_eaten / gap_size)


def s4_volume_boost(ticker: str, bars_or: pd.DataFrame, session_date: Optional[date] = None) -> float:
    """S4: Volume in opening range vs historical average same window."""
    vol_today = float(bars_or["volume"].sum())
    vol_avg   = fetcher.get_historical_15min_volume(ticker, lookback_days=20, session_date=session_date)
    if vol_avg == 0:
        return 0.0
    ratio = vol_today / vol_avg
    if ratio > config.VOL_RATIO_HIGH:
        return 0.10
    elif ratio > config.VOL_RATIO_MID:
        return 0.05
    return 0.0


def calc_confidence(
    above_vwap: bool,
    or_position: float,
    gap_retention: float,
    catalyst_bonus: float,
    vol_boost: float,
) -> float:
    direction_score = sum([
        above_vwap,
        or_position > config.OR_POSITION_THRESHOLD,
        gap_retention > config.GAP_RETENTION_THRESHOLD,
    ])
    confidence = (direction_score / 3) + catalyst_bonus + vol_boost
    return min(confidence, 1.0)


def compute_signals(
    ticker: str,
    prev_close: float,
    catalyst_multiplier: float,
    session_date: Optional[date] = None,
) -> dict:
    """
    Compute all L2 signals for a ticker.
    Returns a dict with signals, scores, and confidence.
    """
    bars_or = fetcher.get_opening_range_bars(ticker, session_date)
    if bars_or.empty or len(bars_or) < 2:
        logger.warning(f"{ticker}: insufficient opening range data")
        return {}

    open_930   = float(bars_or["open"].iloc[0])
    price_945  = float(bars_or["close"].iloc[-1])

    above_vwap   = s1_above_vwap(bars_or, price_945)
    or_pos       = s2_or_position(bars_or, price_945)
    gap_ret      = s3_gap_retention(bars_or, open_930, prev_close)
    vol_boost    = s4_volume_boost(ticker, bars_or, session_date)
    confidence   = calc_confidence(above_vwap, or_pos, gap_ret, catalyst_multiplier, vol_boost)

    signals = {
        "ticker": ticker,
        "price_945": price_945,
        "open_930": open_930,
        "prev_close": prev_close,
        "above_vwap": above_vwap,
        "or_position": round(or_pos, 4),
        "gap_retention": round(gap_ret, 4),
        "vol_boost": vol_boost,
        "catalyst_bonus": catalyst_multiplier,
        "confidence": round(confidence, 4),
        "passes_threshold": confidence >= config.CONFIDENCE_THRESHOLD,
    }
    logger.info(
        f"{ticker}: VWAP={above_vwap} OR={or_pos:.2f} GR={gap_ret:.2f} "
        f"boost={vol_boost} conf={confidence:.3f}"
    )
    return signals
