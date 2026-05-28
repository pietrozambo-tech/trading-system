import logging
import time
from datetime import datetime, timedelta, date
from typing import Optional

import pandas as pd
import pytz
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
    StockLatestBarRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus

import config

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


def _with_retry(fn, *args, retries: int = 3, **kwargs):
    """Esegue fn con retry automatico su rate limit (429) o errori temporanei."""
    wait = 5
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            is_rate_limit = "429" in msg or "too many requests" in msg.lower()
            is_last = attempt == retries - 1
            if is_last:
                raise
            sleep_time = wait * (2 ** attempt) if is_rate_limit else wait
            logger.warning(f"API error (attempt {attempt+1}/{retries}): {msg[:80]} — retry in {sleep_time}s")
            time.sleep(sleep_time)


_data_client: Optional[StockHistoricalDataClient] = None
_trading_client: Optional[TradingClient] = None


def get_data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
    return _data_client


def get_trading_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        paper = "paper-api" in config.ALPACA_BASE_URL
        _trading_client = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=paper,
        )
    return _trading_client


def get_daily_bars(ticker: str, lookback_days: int = 25) -> pd.DataFrame:
    """Daily OHLCV bars for ATR and ADV calculation."""
    client = get_data_client()
    end = datetime.now(ET).date()
    start = end - timedelta(days=lookback_days + 10)
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )
    bars = _with_retry(client.get_stock_bars, req).df
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(ticker, level="symbol")
    return bars.tail(lookback_days)


def get_intraday_bars(ticker: str, minutes: int = 1, session_date: Optional[date] = None) -> pd.DataFrame:
    """1-minute bars for the current or specified trading session."""
    client = get_data_client()
    if session_date is None:
        session_date = datetime.now(ET).date()
    start = ET.localize(datetime.combine(session_date, datetime.strptime("09:30", "%H:%M").time()))
    end   = ET.localize(datetime.combine(session_date, datetime.strptime("16:00", "%H:%M").time()))
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute if minutes == 1 else TimeFrame(minutes, "Min"),
        start=start,
        end=end,
        feed="iex",
    )
    bars = _with_retry(client.get_stock_bars, req).df
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(ticker, level="symbol")
    return bars


def get_opening_range_bars(ticker: str, session_date: Optional[date] = None) -> pd.DataFrame:
    """1-min bars from 9:30 to 9:45 ET (opening range)."""
    bars = get_intraday_bars(ticker, minutes=1, session_date=session_date)
    if bars.empty:
        return bars
    bars.index = bars.index.tz_convert(ET)
    cutoff = ET.localize(datetime.combine(
        session_date or datetime.now(ET).date(),
        datetime.strptime("09:45", "%H:%M").time()
    ))
    return bars[bars.index <= cutoff]


def get_latest_quote(ticker: str) -> dict:
    """Latest bid/ask for spread calculation."""
    client = get_data_client()
    req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
    quote = _with_retry(client.get_stock_latest_quote, req)[ticker]
    return {
        "bid": float(quote.bid_price),
        "ask": float(quote.ask_price),
        "spread_pct": (float(quote.ask_price) - float(quote.bid_price)) / float(quote.ask_price)
        if float(quote.ask_price) > 0 else 1.0,
    }


def get_snapshot(ticker: str) -> dict:
    """Full snapshot: latest trade, quote, daily + minute bars."""
    client = get_data_client()
    req = StockSnapshotRequest(symbol_or_symbols=ticker)
    snap = _with_retry(client.get_stock_snapshot, req)[ticker]
    return snap


def get_latest_bar(ticker: str) -> dict:
    """Most recent 1-min bar."""
    client = get_data_client()
    req = StockLatestBarRequest(symbol_or_symbols=ticker)
    bar = _with_retry(client.get_stock_latest_bar, req)[ticker]
    return {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume}


def get_adv(ticker: str, lookback: int = 20) -> float:
    """Average daily volume over last N trading days."""
    bars = get_daily_bars(ticker, lookback_days=lookback)
    if bars.empty:
        return 0.0
    return float(bars["volume"].mean())


def get_atr14(ticker: str) -> float:
    """ATR14 on daily bars."""
    bars = get_daily_bars(ticker, lookback_days=20)
    if len(bars) < 15:
        return 0.0
    high  = bars["high"]
    low   = bars["low"]
    close = bars["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(14).mean().iloc[-1])


def get_premarket_data(ticker: str, session_date: Optional[date] = None) -> dict:
    """Volume and price at ~9:25 ET using extended-hours bars."""
    client = get_data_client()
    if session_date is None:
        session_date = datetime.now(ET).date()
    start = ET.localize(datetime.combine(session_date, datetime.strptime("04:00", "%H:%M").time()))
    end   = ET.localize(datetime.combine(session_date, datetime.strptime("09:26", "%H:%M").time()))
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed="iex",
    )
    try:
        bars = _with_retry(client.get_stock_bars, req).df
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(ticker, level="symbol")
        if bars.empty:
            return {"premarket_volume": 0, "premarket_price": None}
        return {
            "premarket_volume": int(bars["volume"].sum()),
            "premarket_price": float(bars["close"].iloc[-1]),
        }
    except Exception as e:
        logger.warning(f"Premarket data error for {ticker}: {e}")
        return {"premarket_volume": 0, "premarket_price": None}


def get_historical_premarket_volume_avg(ticker: str, lookback_days: int = 10, session_date: Optional[date] = None) -> float:
    """
    Average pre-market volume (04:00–09:25 ET) over last N trading days.
    Apple-to-apple comparison for the pre-market scan.
    """
    client = get_data_client()
    if session_date is None:
        session_date = datetime.now(ET).date()
    totals = []
    check_date = session_date - timedelta(days=1)
    attempts = 0
    while len(totals) < lookback_days and attempts < lookback_days * 3:
        attempts += 1
        if check_date.weekday() >= 5:
            check_date -= timedelta(days=1)
            continue
        start = ET.localize(datetime.combine(check_date, datetime.strptime("04:00", "%H:%M").time()))
        end   = ET.localize(datetime.combine(check_date, datetime.strptime("09:26", "%H:%M").time()))
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed="iex",
        )
        try:
            bars = _with_retry(client.get_stock_bars, req).df
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(ticker, level="symbol")
            if not bars.empty:
                totals.append(int(bars["volume"].sum()))
        except Exception:
            pass
        check_date -= timedelta(days=1)
    return float(sum(totals) / len(totals)) if totals else 0.0


def get_news(ticker: str, start: Optional[datetime] = None, limit: int = 10) -> list[dict]:
    """Recent news via Alpaca News API (Benzinga)."""
    import requests
    if start is None:
        start = datetime.now(ET) - timedelta(days=1)
    url = "https://data.alpaca.markets/v1beta1/news"
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }
    params = {
        "symbols": ticker,
        "start": start.isoformat(),
        "limit": limit,
        "sort": "desc",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("news", [])
    except Exception as e:
        logger.warning(f"News fetch error for {ticker}: {e}")
        return []


def get_spy_change(session_date: Optional[date] = None) -> float:
    """SPY % change vs previous close at current time."""
    client = get_data_client()
    if session_date is None:
        session_date = datetime.now(ET).date()
    daily = get_daily_bars("SPY", lookback_days=5)
    if len(daily) < 2:
        return 0.0
    prev_close = float(daily["close"].iloc[-2])
    snap = get_snapshot("SPY")
    current_price = float(snap.latest_trade.price)
    return (current_price - prev_close) / prev_close


def get_account() -> dict:
    """Account info: cash, equity, etc."""
    client = get_trading_client()
    acct = client.get_account()
    return {
        "equity": float(acct.equity),
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
    }


def get_open_positions() -> list[dict]:
    """All currently open positions."""
    client = get_trading_client()
    positions = client.get_all_positions()
    return [
        {
            "ticker": p.symbol,
            "qty": float(p.qty),
            "entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in positions
    ]


def is_asset_tradable(ticker: str) -> bool:
    """Check if asset is active and tradable on Alpaca."""
    try:
        client = get_trading_client()
        asset = client.get_asset(ticker)
        return asset.tradable and asset.status == AssetStatus.ACTIVE
    except Exception:
        return False


def get_historical_15min_volume(ticker: str, lookback_days: int = 20, session_date: Optional[date] = None) -> float:
    """Average volume in 9:30–9:45 window over past N trading days (for S4)."""
    client = get_data_client()
    if session_date is None:
        session_date = datetime.now(ET).date()
    totals = []
    days_checked = 0
    check_date = session_date - timedelta(days=1)
    while days_checked < lookback_days and len(totals) < lookback_days:
        if check_date.weekday() >= 5:
            check_date -= timedelta(days=1)
            continue
        start = ET.localize(datetime.combine(check_date, datetime.strptime("09:30", "%H:%M").time()))
        end   = ET.localize(datetime.combine(check_date, datetime.strptime("09:45", "%H:%M").time()))
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed="iex",
        )
        try:
            bars = _with_retry(client.get_stock_bars, req).df
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(ticker, level="symbol")
            if not bars.empty:
                totals.append(int(bars["volume"].sum()))
        except Exception:
            pass
        check_date -= timedelta(days=1)
        days_checked += 1
    return float(sum(totals) / len(totals)) if totals else 0.0
