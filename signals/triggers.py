import logging
import time
from datetime import date, datetime
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


def s1_post_open_advance(open_930: float, price_935: float) -> bool:
    """S1: Price at 9:35 above 9:30 open — stock moved up in its first 5 minutes."""
    return price_935 > open_930


def s2_or_position(bars_or: pd.DataFrame, price_935: float) -> float:
    """S2: Position in opening range. > 0.66 = strong."""
    or_high = float(bars_or["high"].max())
    or_low  = float(bars_or["low"].min())
    if or_high == or_low:
        return 0.5
    return (price_935 - or_low) / (or_high - or_low)


def s3_gap_retention(bars_or: pd.DataFrame, open_930: float, prev_close: float) -> float:
    """S3: Fraction of gap remaining after 5 min. > 0.70 = defended."""
    gap_size = open_930 - prev_close
    if abs(gap_size) < 0.0001:
        return 1.0  # effectively no gap
    gap_eaten = open_930 - float(bars_or["low"].min())
    return 1.0 - (gap_eaten / gap_size)


def s4_volume_boost(ticker: str, bars_or: pd.DataFrame, session_date: Optional[date] = None) -> tuple[float, Optional[float], float, float]:
    """S4: Volume in opening range vs historical average same window.
    Returns (boost, raw ratio, vol_today, vol_avg) — raw values go into the daily log
    so threshold calibration can be done offline. Ratio is None when historical data
    is unavailable — distinct from a genuine low-volume ratio, which previously
    looked identical."""
    vol_today = float(bars_or["volume"].sum())
    vol_avg   = fetcher.get_historical_or_volume(ticker, lookback_days=20, session_date=session_date)
    if vol_avg == 0:
        logger.warning(f"{ticker}: historical OR volume unavailable (vol_avg=0) — vol_boost skipped")
        return 0.0, None, vol_today, 0.0
    ratio = vol_today / vol_avg
    if ratio > config.VOL_RATIO_HIGH:
        return 0.10, ratio, vol_today, vol_avg
    elif ratio > config.VOL_RATIO_MID:
        return 0.05, ratio, vol_today, vol_avg
    return 0.0, ratio, vol_today, vol_avg


def calc_confidence(
    post_open_advance: bool,
    or_position: float,
    gap_retention: float,
    catalyst_bonus: float,
    vol_boost: float,
    short_float: Optional[float] = None,
    gap_pct: Optional[float] = None,
) -> float:
    direction_score = sum([
        post_open_advance,
        or_position > config.OR_POSITION_THRESHOLD,
        gap_retention > config.GAP_RETENTION_THRESHOLD,
    ])
    squeeze_bonus = (
        config.SHORT_SQUEEZE_BONUS
        if short_float is not None
        and short_float >= config.SHORT_SQUEEZE_THRESHOLD
        and (
            catalyst_bonus > 0
            or (gap_pct is not None and gap_pct >= config.SHORT_SQUEEZE_GAP_THRESHOLD)
        )
        else 0.0
    )
    return (direction_score / 3) + catalyst_bonus + vol_boost + squeeze_bonus


def compute_signals(
    ticker: str,
    prev_close: float,
    catalyst_bonus: float,
    short_float: Optional[float] = None,
    gap_pct: Optional[float] = None,
    session_date: Optional[date] = None,
) -> dict:
    """
    Compute all L2 signals for a ticker.
    Returns a dict with signals, scores, and confidence.
    """
    # Expected 1-min bars between 9:30 and ENTRY_TIME (5 with the 9:35 entry)
    entry_t = datetime.strptime(config.ENTRY_TIME, "%H:%M")
    expected_bars = (entry_t.hour * 60 + entry_t.minute) - (9 * 60 + 30)

    bars_or = fetcher.get_opening_range_bars(ticker, session_date)
    if session_date is None and 0 < len(bars_or) < expected_bars:
        # Seconds after ENTRY_TIME the last bar may not be published yet — without
        # this retry, price_935 silently becomes the previous minute's close and
        # vol_today is undercounted, with no trace in the dataset.
        logger.info(f"{ticker}: {len(bars_or)}/{expected_bars} OR bars — retrying fetch in 2s")
        time.sleep(2)
        bars_or = fetcher.get_opening_range_bars(ticker, session_date)
    if bars_or.empty or len(bars_or) < 2:
        logger.warning(f"{ticker}: insufficient opening range data")
        return {}

    or_bar_count = len(bars_or)
    or_last_bar  = bars_or.index[-1].strftime("%H:%M")
    if or_bar_count < expected_bars:
        logger.warning(
            f"{ticker}: only {or_bar_count}/{expected_bars} OR bars (last bar {or_last_bar}) "
            "— signals computed on truncated window"
        )

    open_930   = float(bars_or["open"].iloc[0])
    price_935  = float(bars_or["close"].iloc[-1])

    # Pre-market gap fully reversed before open — exclude immediately
    if open_930 < prev_close:
        logger.info(f"{ticker}: opened below prev_close (gap reversed at open) — excluding")
        return {"ticker": ticker, "excluded_reason": "gap_reversed_at_open"}

    # Pre-market gap mostly eaten before the open — skip faded setups
    if gap_pct and gap_pct > 0:
        pm_gap_open_retention = (open_930 - prev_close) / (prev_close * gap_pct)
        if pm_gap_open_retention < config.PM_OPEN_RETENTION:
            logger.info(
                f"{ticker}: pre-market gap eaten at open "
                f"({pm_gap_open_retention:.0%} of pm gap retained) — excluding"
            )
            return {"ticker": ticker, "excluded_reason": f"pm_gap_eaten_at_open_{pm_gap_open_retention:.0%}"}

    post_adv      = s1_post_open_advance(open_930, price_935)
    or_pos        = s2_or_position(bars_or, price_935)
    gap_ret       = s3_gap_retention(bars_or, open_930, prev_close)
    vol_boost, vol_ratio, vol_today, vol_avg = s4_volume_boost(ticker, bars_or, session_date)
    squeeze_bonus = (
        config.SHORT_SQUEEZE_BONUS
        if short_float is not None
        and short_float >= config.SHORT_SQUEEZE_THRESHOLD
        and (
            catalyst_bonus > 0
            or (gap_pct is not None and gap_pct >= config.SHORT_SQUEEZE_GAP_THRESHOLD)
        )
        else 0.0
    )
    confidence = calc_confidence(post_adv, or_pos, gap_ret, catalyst_bonus, vol_boost, short_float, gap_pct)

    passes   = confidence >= config.CONFIDENCE_THRESHOLD
    adv_str  = f"ADV={'✓' if post_adv else '✗'}"
    or_str   = f"OR={or_pos:.2f}{'✓' if or_pos > config.OR_POSITION_THRESHOLD else f'✗(need>{config.OR_POSITION_THRESHOLD})'}"
    gr_str   = f"GR={gap_ret:.2f}{'✓' if gap_ret > config.GAP_RETENTION_THRESHOLD else f'✗(need>{config.GAP_RETENTION_THRESHOLD})'}"
    vol_str  = f"vol={vol_ratio:.2f}x(+{vol_boost:.2f})" if vol_ratio is not None else f"vol=n/d(+{vol_boost:.2f})"
    cat_str  = f"catalyst=+{catalyst_bonus:.2f}"
    sq_str   = f" squeeze=+{squeeze_bonus:.2f}" if squeeze_bonus > 0 else ""
    conf_str = f"confidence={confidence:.3f}{'✓' if passes else f'✗(need≥{config.CONFIDENCE_THRESHOLD})'}"

    if passes:
        logger.info(f"L2 PASS  {ticker}: {adv_str} {or_str} {gr_str} {vol_str} {cat_str}{sq_str} → {conf_str}")
    else:
        logger.info(f"L2 REJECT {ticker}: {adv_str} {or_str} {gr_str} {vol_str} {cat_str}{sq_str} → {conf_str}")

    signals = {
        "ticker":               ticker,
        "price_935":            price_935,
        "open_930":             open_930,
        "prev_close":           prev_close,
        "post_open_advance":    post_adv,
        "post_open_advance_pct": round((price_935 - open_930) / open_930, 4),
        "or_position":          round(or_pos, 4),
        "gap_retention":        round(gap_ret, 4),
        "vol_boost":            vol_boost,
        "vol_ratio":            round(vol_ratio, 3) if vol_ratio is not None else None,
        "vol_today":            int(vol_today),
        "vol_avg":              int(vol_avg),
        "or_bar_count":         or_bar_count,
        "or_last_bar":          or_last_bar,
        "catalyst_bonus":       catalyst_bonus,
        "short_float":          short_float,
        "short_squeeze_bonus":  squeeze_bonus,
        "confidence":           round(confidence, 4),
        "passes_threshold":     passes,
    }
    return signals
