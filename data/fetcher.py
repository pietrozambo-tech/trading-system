import logging
import time
from datetime import datetime, timedelta, date
from typing import Optional

import pandas as pd
import pytz
from alpaca.data import StockHistoricalDataClient
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
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


def _feed() -> DataFeed:
    """Return configured data feed. IEX = free tier; SIP = paid tier (set ALPACA_DATA_FEED=sip)."""
    return DataFeed.SIP if config.ALPACA_DATA_FEED.upper() == "SIP" else DataFeed.IEX


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
_short_float_cache: dict[str, Optional[float]] = {}


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
    """Daily OHLCV bars for ATR and ADV calculation.

    adjustment=ALL: raw bars across a split/dividend produce fake gaps, garbage ATR
    for 14 days, and ADV in the wrong share count.
    Buffer is 1.6x calendar days: lookback_days are TRADING days (the old +10 buffer
    returned ~50 rows when 65 were requested).
    """
    client = get_data_client()
    end = datetime.now(ET).date()
    start = end - timedelta(days=int(lookback_days * 1.6) + 7)
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
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
    end   = ET.localize(datetime.combine(session_date, datetime.strptime("16:01", "%H:%M").time()))
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute if minutes == 1 else TimeFrame(minutes, "Min"),
        start=start,
        end=end,
        feed=_feed(),
    )
    bars = _with_retry(client.get_stock_bars, req).df
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(ticker, level="symbol")
    return bars


def get_opening_range_bars(ticker: str, session_date: Optional[date] = None) -> pd.DataFrame:
    """1-min bars from 9:30 to ENTRY_TIME ET (opening range)."""
    bars = get_intraday_bars(ticker, minutes=1, session_date=session_date)
    if bars.empty:
        return bars
    bars.index = bars.index.tz_convert(ET)
    cutoff = ET.localize(datetime.combine(
        session_date or datetime.now(ET).date(),
        datetime.strptime(config.ENTRY_TIME, "%H:%M").time()
    ))
    return bars[bars.index < cutoff]


def _age_and_et(ts) -> tuple[Optional[float], Optional[str]]:
    """(età in secondi, ora ET 'HH:MM:SS') di un timestamp Alpaca. (None, None) se assente."""
    if ts is None:
        return None, None
    ts_utc = ts if ts.tzinfo else pytz.UTC.localize(ts)
    age_s = (datetime.now(pytz.UTC) - ts_utc).total_seconds()
    try:
        return age_s, ts_utc.astimezone(ET).strftime("%H:%M:%S")
    except Exception:
        return age_s, None


def _bid_for_exit(ticker: str) -> Optional[dict]:
    """Bid corrente, solo se la quotazione è affidabile. None altrimenti.

    Deliberatamente NON riusa get_latest_quote(): quella, se la quotazione non arriva,
    ripiega sul close dell'ultima barra restituendo bid=ask e spread 0 — che qui
    sembrerebbe una quotazione perfetta e ci farebbe decidere su un prezzo di barra
    spacciato per denaro. Qui un fallimento deve restare un fallimento.

    Scarta le quotazioni con spread largo: sono stub o stantie, e un bid stub farebbe
    scattare uno stop che il mercato non ha toccato.
    """
    try:
        client = get_data_client()
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=_feed())
        q = _with_retry(client.get_stock_latest_quote, req)[ticker]
        bid = float(q.bid_price) if q.bid_price else 0.0
        ask = float(q.ask_price) if q.ask_price else 0.0
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        spread = (ask - bid) / ask
        if spread > config.MAX_QUOTE_SPREAD_PCT:
            return None
        age_s, tick_time = _age_and_et(getattr(q, "timestamp", None))
        if age_s is not None and age_s > config.PRICE_MAX_AGE_S:
            return None
        return {"price": bid, "age_s": age_s, "tick_time": tick_time, "spread_pct": spread}
    except Exception as e:
        logger.debug(f"{ticker}: quotazione non disponibile ({e})")
        return None


def get_price_detail(ticker: str) -> dict:
    """Prezzo per le decisioni di uscita, CON la sua provenienza.

    Returns {price, source, age_s, tick_time}:
      source 'quote_bid'     — bid corrente (spread sano, quotazione fresca) ← preferito
      source 'latest_trade'  — ultimo print IEX (età ≤ PRICE_MAX_AGE_S)
      source 'bar_fallback'  — print troppo vecchio, close dell'ultima barra 1-min
      source 'stale_trade'   — print vecchio E nessuna barra disponibile (caso peggiore)

    Perché il BID per primo: su una posizione long è il prezzo a cui possiamo davvero
    uscire, e soprattutto le quotazioni si aggiornano molto più spesso dei print. Il
    16/09 il feed IEX non ha pubblicato un solo print su DELL per oltre 5 minuti: il
    monitor ha continuato a leggere lo stesso prezzo vecchio mentre il titolo scendeva,
    e lo stop è stato rilevato 0,655 punti sotto il livello (−2,71% invece di −2,00%,
    ~$370). Il fallback sulla barra 1-min non aiutava: legge lo stesso feed, quindi era
    congelato anch'esso. Pollare più spesso non avrebbe cambiato nulla — il dato era fermo.

    Nota: il bid è tipicamente ≤ ultimo scambio, quindi gli stop scattano marginalmente
    prima (di uno spread). Sui nomi liquidi sono pochi centesimi; in cambio si vede il
    mercato muoversi quando i print mancano.

    Esiste con la provenienza perché prima la decisione di stop era un float nudo: al
    trigger si registrava solo il FILL, e "quel prezzo era reale?" non era rispondibile
    dal log (AMD 9/09 richiese un grafico al minuto a mano).
    """
    bid = _bid_for_exit(ticker)
    if bid is not None:
        return {"price": bid["price"], "source": "quote_bid",
                "age_s": bid["age_s"], "tick_time": bid["tick_time"]}

    client = get_data_client()
    req = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=_feed())
    trade = _with_retry(client.get_stock_latest_trade, req)[ticker]
    age_s, tick_time = _age_and_et(trade.timestamp)
    if age_s is None or age_s <= config.PRICE_MAX_AGE_S:
        return {"price": float(trade.price), "source": "latest_trade", "age_s": age_s, "tick_time": tick_time}
    try:
        bars = get_intraday_bars(ticker, minutes=1)
        if not bars.empty:
            logger.warning(f"{ticker}: latest trade {age_s:.0f}s old — using last 1-min bar close")
            return {"price": float(bars["close"].iloc[-1]), "source": "bar_fallback",
                    "age_s": age_s, "tick_time": tick_time}
    except Exception:
        pass
    logger.warning(f"{ticker}: latest trade {age_s:.0f}s old and no bar fallback — using stale price")
    return {"price": float(trade.price), "source": "stale_trade", "age_s": age_s, "tick_time": tick_time}


def get_current_price(ticker: str) -> float:
    """Latest trade price, validated for staleness. Thin wrapper over get_price_detail()
    for callers that only need the number (entry reference price, tests)."""
    return get_price_detail(ticker)["price"]


def get_latest_quote(ticker: str) -> dict:
    """Latest bid/ask for entry price estimation."""
    client = get_data_client()
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=_feed())
        quote = _with_retry(client.get_stock_latest_quote, req)[ticker]
        bid = float(quote.bid_price) if quote.bid_price else 0.0
        ask = float(quote.ask_price) if quote.ask_price else 0.0
        if bid > 0 and ask > 0:
            return {"bid": bid, "ask": ask, "spread_pct": (ask - bid) / ask}
    except Exception as e:
        logger.warning(f"Quote unavailable for {ticker}: {e} — falling back to bar close")

    # Fallback: latest bar close as ask proxy
    req = StockLatestBarRequest(symbol_or_symbols=ticker, feed=_feed())
    bar = _with_retry(client.get_stock_latest_bar, req)[ticker]
    close = float(bar.close)
    return {"bid": close, "ask": close, "spread_pct": 0.0}


def get_snapshot(ticker: str):
    """Full snapshot: latest trade, quote, daily + minute bars."""
    client = get_data_client()
    req = StockSnapshotRequest(symbol_or_symbols=ticker, feed=_feed())
    snap = _with_retry(client.get_stock_snapshot, req)[ticker]
    return snap


def get_adv(ticker: str, lookback: int = 65) -> float:
    """Average daily volume over last N trading days.
    Default 65 to match the watchlist ADV definition (eligibility uses 65-day bars) —
    the L1 filter must not use two different windows depending on the code path."""
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
    """Pre-market price at ~9:25 ET.
    Primary: yfinance (multi-exchange coverage).
    Fallback: Alpaca IEX snapshot (today's trades only).
    """
    if session_date is None:
        session_date = datetime.now(ET).date()

    # Primary: yfinance — aggregates pre-market prints from all exchanges.
    # NOTE: the field is `.info["preMarketPrice"]`. The previous code read
    # `fast_info.pre_market_price`, an attribute FastInfo does not have, so it was ALWAYS
    # None: this "primary" source silently never fired and every pre-market gap came from
    # the IEX print (~15-20% of volume) — exactly the weakness it was added to fix.
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        pm_price = info.get("preMarketPrice")
        prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
        if pm_price and float(pm_price) > 0:
            pm_price = float(pm_price)
            # A pre-market price identical to the previous close means no pre-market trade
            # has printed yet — fall through to IEX rather than log a 0% gap.
            if not prev_close or abs(pm_price - float(prev_close)) > 1e-9:
                logger.debug(f"{ticker}: pre-market price from yfinance ${pm_price:.2f}")
                # Restituisce anche la chiusura UFFICIALE di ieri (asta di chiusura), così il
                # chiamante calcola il gap con la STESSA sorgente. La daily bar Alpaca sul feed
                # IEX è l'ultimo print IEX, non la chiusura ufficiale: mischiare Yahoo/IEX
                # nella stessa frazione sposta il gap di qualche decimo — sulla soglia +0.5% conta.
                return {"premarket_price": pm_price,
                        "prev_close": float(prev_close) if prev_close else None,
                        "source": "yahoo"}
    except Exception as e:
        logger.debug(f"{ticker}: yfinance pre-market failed ({e}) — trying Alpaca")

    # Fallback: Alpaca IEX snapshot — only accept trades from today
    try:
        snap = get_snapshot(ticker)
        if snap.latest_trade:
            trade_ts = snap.latest_trade.timestamp
            trade_date = trade_ts.astimezone(ET).date() if trade_ts.tzinfo else trade_ts.date()
            if trade_date == session_date:
                pm_price = float(snap.latest_trade.price)
                logger.debug(f"{ticker}: pre-market price from Alpaca IEX ${pm_price:.2f}")
                # prev_close None → il chiamante usa la daily bar IEX: stessa sorgente del print.
                return {"premarket_price": pm_price, "prev_close": None, "source": "iex"}
            else:
                logger.debug(f"{ticker}: Alpaca latest_trade from {trade_date}, not today — skipping")
    except Exception as e:
        logger.warning(f"{ticker}: snapshot price error ({e})")

    return {"premarket_price": None, "prev_close": None, "source": None}


def get_news(ticker: str, start: Optional[datetime] = None, limit: int = 10) -> list[dict]:
    """Recent news via Alpaca News API (Benzinga)."""
    import requests
    if start is None:
        now = datetime.now(ET)
        # On Monday, look back 72h to catch Friday evening earnings/news
        lookback_days = 3 if now.weekday() == 0 else 1
        start = now - timedelta(days=lookback_days)
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
    try:
        snap = get_snapshot("SPY")
        prev_close = float(snap.previous_daily_bar.close)
        current_price = float(snap.latest_trade.price)
        if prev_close == 0:
            return 0.0
        return (current_price - prev_close) / prev_close
    except Exception as e:
        logger.warning(f"SPY change error: {e}")
        return 0.0


def get_spy_daily_returns(start: date, end: date) -> dict[str, float]:
    """Official SPY daily returns (prev_close → close) keyed by 'YYYY-MM-DD'.

    Used to backfill the dashboard benchmark: the value stored intraday at 09:35 is
    only SPY's opening move, not the full-day performance. The official close is known
    only after 16:00 ET, so each completed day is patched on a later run from daily bars.
    Returns an empty dict on error (caller leaves existing values untouched).
    """
    try:
        # Anchor lookback to today→start (not end→start): get_daily_bars always anchors at
        # today, so lookback must span from today back to start regardless of how old start is.
        # Convert calendar days to trading days (~5/7) and add 20 for the prior-close buffer.
        _today = datetime.now(ET).date()
        bars = get_daily_bars("SPY", lookback_days=int((_today - start).days * 5 / 7) + 20)
    except Exception as e:
        logger.warning(f"SPY daily returns fetch error: {e}")
        return {}
    if bars.empty or "close" not in bars:
        return {}
    closes = bars["close"]
    rets = closes.pct_change()
    out: dict[str, float] = {}
    for ts, val in rets.items():
        d = ts.date() if hasattr(ts, "date") else ts
        if start <= d <= end and pd.notna(val):
            out[d.strftime("%Y-%m-%d")] = float(val)
    return out


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
    except Exception as e:
        # Fail OPEN: an API blip must never reject a valid candidate. An asset that truly
        # isn't tradable simply gets its order rejected downstream, which is handled.
        logger.warning(f"{ticker}: is_asset_tradable check failed ({e}) — assuming tradable")
        return True


def get_short_float(ticker: str) -> Optional[float]:
    """Short interest as a fraction of float (e.g. 0.18 = 18%).

    Source: Yahoo Finance via yfinance — data comes from FINRA biweekly reports,
    so it's not real-time but is stable enough for our daily pre-market scan.
    Result is cached per process to avoid redundant HTTP calls within one session.
    Returns None if the data is unavailable (no exception raised).
    """
    if ticker in _short_float_cache:
        return _short_float_cache[ticker]
    result = None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        val  = info.get("shortPercentOfFloat")
        if val is not None:
            result = float(val)
    except Exception as e:
        logger.warning(f"Short float unavailable for {ticker}: {e}")
    _short_float_cache[ticker] = result
    return result


def is_market_open_today(session_date: Optional[date] = None) -> bool:
    """Return True if NYSE is a regular trading session today.

    Uses Alpaca's market calendar — authoritative for all NYSE holidays and
    early-close days. Fails open (returns True) so that a transient API error
    never silently blocks a real trading day.
    """
    from alpaca.trading.requests import GetCalendarRequest
    if session_date is None:
        session_date = datetime.now(ET).date()
    client = get_trading_client()
    try:
        calendar = client.get_calendar(GetCalendarRequest(start=session_date, end=session_date))
        return len(calendar) > 0
    except Exception as e:
        logger.warning(f"Market calendar check failed: {e} — assuming market is open")
        return True


def get_session_close_today(session_date: Optional[date] = None):
    """Today's NYSE session close (a datetime.time, ET) from the Alpaca calendar, or None
    if it's not a trading day / the lookup failed.

    is_market_open_today() only checks that a calendar entry EXISTS — it never read the
    entry's close time, so EARLY-CLOSE days (13:00: day after Thanksgiving, Christmas Eve,
    Jul 3) were treated as full sessions: the 15:45 EOD close fired into a closed market,
    the sell got queued for the next session and the position was held over the (long)
    weekend while the recap said "closed". The caller uses this to skip half-days.
    """
    from alpaca.trading.requests import GetCalendarRequest
    if session_date is None:
        session_date = datetime.now(ET).date()
    try:
        cal = get_trading_client().get_calendar(GetCalendarRequest(start=session_date, end=session_date))
        if not cal:
            return None
        close = getattr(cal[0], "close", None)
        if isinstance(close, str):                      # defensive: "13:00"
            h, m = close.split(":")[:2]
            return datetime.strptime(f"{int(h):02d}:{int(m):02d}", "%H:%M").time()
        return close
    except Exception as e:
        logger.warning(f"Session close lookup failed: {e}")
        return None


# Media del volume dell'opening range, precalcolata alle 9:25. Chiave (ticker, giorno).
# È il DENOMINATORE del vol_ratio: dato storico puro (i 20 giorni precedenti), nessuna
# dipendenza da oggi. Il numeratore (volume 9:30–9:34 di OGGI) resta misurato alle 9:35
# da bars_or, che scarichiamo comunque per gli altri segnali.
_or_vol_cache: dict[tuple[str, date], float] = {}


def prefetch_historical_or_volumes(tickers: list[str], lookback_days: int = 20,
                                   session_date: Optional[date] = None) -> int:
    """Precalcola la media del volume dell'opening range per TUTTI i ticker in una volta.

    Perché esiste: get_historical_or_volume() fa UNA richiesta per ogni giorno di storico,
    quindi 20 chiamate per ticker. Nel percorso critico delle 9:35, con 46 candidati, sono
    920 chiamate sequenziali — 243 secondi misurati il 17/09, l'83% del traffico totale e
    il motivo per cui l'ordine partiva 4,6 minuti dopo le 9:35 (LUNR: riempita a +0,41%
    dal prezzo di riferimento, sul tetto del limit).

    Qui si sfrutta il fatto che Alpaca accetta PIÙ SIMBOLI in una sola richiesta: una
    chiamata per giorno copre tutti i ticker insieme → 20 chiamate totali invece di 920,
    con payload minuscoli (5 barre × N ticker). Chiamata alle 9:25, durante l'attesa
    morta prima dell'apertura, esce del tutto dal percorso critico.

    Stessa finestra e stessa semantica del loop per-ticker: 9:30 → ENTRY_TIME con estremo
    finale ESCLUSIVO, weekend saltati, giorni vuoti che non consumano uno slot.
    Ritorna il numero di ticker per cui è stata popolata una media.
    """
    if not tickers:
        return 0
    if session_date is None:
        session_date = datetime.now(ET).date()
    client = get_data_client()
    entry_time = datetime.strptime(config.ENTRY_TIME, "%H:%M").time()
    per_ticker: dict[str, list[float]] = {t: [] for t in tickers}

    check_date = session_date - timedelta(days=1)
    attempts = days_used = 0
    while attempts < lookback_days * 2 and days_used < lookback_days:
        if check_date.weekday() >= 5:
            check_date -= timedelta(days=1)
            continue
        attempts += 1
        start = ET.localize(datetime.combine(check_date, datetime.strptime("09:30", "%H:%M").time()))
        end   = ET.localize(datetime.combine(check_date, entry_time))
        req = StockBarsRequest(
            symbol_or_symbols=list(tickers),
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=_feed(),
        )
        try:
            df = _with_retry(client.get_stock_bars, req).df
            if not df.empty:
                df = df.reset_index()
                if "timestamp" in df.columns and "symbol" in df.columns:
                    df["timestamp"] = df["timestamp"].dt.tz_convert(ET)
                    df = df[df["timestamp"] < end]          # estremo finale esclusivo
                    if not df.empty:
                        for sym, vol in df.groupby("symbol")["volume"].sum().items():
                            if sym in per_ticker:
                                per_ticker[sym].append(float(vol))
                        days_used += 1
        except Exception as e:
            logger.warning(f"Prefetch OR volume {check_date}: {e}")
        check_date -= timedelta(days=1)

    filled = 0
    for tk, totals in per_ticker.items():
        if totals:
            _or_vol_cache[(tk, session_date)] = sum(totals) / len(totals)
            filled += 1
    short = [tk for tk, v in per_ticker.items() if 0 < len(v) < lookback_days]
    logger.info(
        f"[PREFETCH] media volume opening range: {filled}/{len(tickers)} ticker su {days_used} giorni "
        f"({attempts} richieste multi-simbolo invece di ~{len(tickers) * lookback_days})"
        + (f" | storico parziale per {len(short)} ticker" if short else "")
    )
    return filled


def get_historical_or_volume(ticker: str, lookback_days: int = 20, session_date: Optional[date] = None) -> float:
    """Average volume in the 9:30–ENTRY_TIME opening-range window over past N trading days (for S4).

    The end bound is EXCLUSIVE (bars.index < end): Alpaca includes both endpoints in
    the request, so without the filter the historical window contained one extra
    minute (the ENTRY_TIME bar) vs today's window — inflating vol_avg ~15-25% and
    systematically deflating the vol_ratio.
    Holidays/empty days don't consume a sample slot (bounded at 2x lookback attempts).

    Legge prima la cache popolata da prefetch_historical_or_volumes() alle 9:25: in quel
    caso costa ZERO chiamate. Il loop per-giorno qui sotto resta come fallback per i
    ticker non coperti dal prefetch (o se il prefetch è fallito) — corretto ma lento,
    20 chiamate per ticker.
    """
    if session_date is None:
        session_date = datetime.now(ET).date()
    cached = _or_vol_cache.get((ticker, session_date))
    if cached is not None:
        return cached
    logger.debug(f"{ticker}: media volume OR non in cache — fallback al fetch per-giorno")
    client = get_data_client()
    if session_date is None:
        session_date = datetime.now(ET).date()
    totals = []
    attempts = 0
    check_date = session_date - timedelta(days=1)
    while attempts < lookback_days * 2 and len(totals) < lookback_days:
        if check_date.weekday() >= 5:
            check_date -= timedelta(days=1)
            continue
        attempts += 1
        start = ET.localize(datetime.combine(check_date, datetime.strptime("09:30", "%H:%M").time()))
        end   = ET.localize(datetime.combine(check_date, datetime.strptime(config.ENTRY_TIME, "%H:%M").time()))
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=_feed(),
        )
        try:
            bars = _with_retry(client.get_stock_bars, req).df
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(ticker, level="symbol")
            if not bars.empty:
                bars.index = bars.index.tz_convert(ET)
                bars = bars[bars.index < end]
                if not bars.empty:
                    totals.append(int(bars["volume"].sum()))
        except Exception:
            pass
        check_date -= timedelta(days=1)
    if len(totals) < lookback_days:
        logger.debug(f"{ticker}: historical OR volume from {len(totals)}/{lookback_days} days")
    return float(sum(totals) / len(totals)) if totals else 0.0
