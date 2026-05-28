"""
Quick connectivity test for Alpaca API.
Run: python test_alpaca.py

Checks:
  1. Environment variables loaded correctly
  2. TradingClient connection (account info)
  3. StockHistoricalDataClient (daily bars for AAPL)
  4. Latest quote (bid/ask spread)
  5. News feed (Benzinga)
"""
import sys
import os
from datetime import datetime, timedelta

# Make sure we're in the project root
sys.path.insert(0, os.path.dirname(__file__))

def check(label: str, ok: bool, detail: str = "") -> None:
    icon = "✓" if ok else "✗"
    print(f"  {icon}  {label}" + (f"  →  {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def main():
    print("\n=== Alpaca API connectivity test ===\n")

    # ------------------------------------------------------------------ #
    # 1. Config / env vars
    # ------------------------------------------------------------------ #
    print("[1] Environment variables")
    import config
    check(".env loaded / ALPACA_API_KEY set",     bool(config.ALPACA_API_KEY),     config.ALPACA_API_KEY[:8] + "…" if config.ALPACA_API_KEY else "MISSING")
    check(".env loaded / ALPACA_SECRET_KEY set",  bool(config.ALPACA_SECRET_KEY),  "***" if config.ALPACA_SECRET_KEY else "MISSING")
    check("ALPACA_BASE_URL is paper",             "paper-api" in (config.ALPACA_BASE_URL or ""), config.ALPACA_BASE_URL)
    check("ANTHROPIC_API_KEY set",                bool(config.ANTHROPIC_API_KEY),  "***" if config.ANTHROPIC_API_KEY else "MISSING")
    check("TELEGRAM_BOT_TOKEN set",               bool(config.TELEGRAM_BOT_TOKEN), "set" if config.TELEGRAM_BOT_TOKEN else "MISSING (optional for now)")

    # ------------------------------------------------------------------ #
    # 2. Trading client — account
    # ------------------------------------------------------------------ #
    print("\n[2] TradingClient — account")
    from data.fetcher import get_trading_client, get_account
    try:
        client = get_trading_client()
        acct = get_account()
        check("Connection OK", True)
        check("Equity",        acct["equity"] > 0,  f"${acct['equity']:,.2f}")
        check("Cash",          acct["cash"] >= 0,   f"${acct['cash']:,.2f}")
        check("Buying power",  acct["buying_power"] >= 0, f"${acct['buying_power']:,.2f}")
    except Exception as e:
        check("TradingClient connection", False, str(e))

    # ------------------------------------------------------------------ #
    # 3. Historical data — daily bars
    # ------------------------------------------------------------------ #
    print("\n[3] StockHistoricalDataClient — AAPL daily bars (last 5 days)")
    from data.fetcher import get_daily_bars
    try:
        bars = get_daily_bars("AAPL", lookback_days=5)
        check("Bars received",   not bars.empty,        f"{len(bars)} bars")
        check("Has OHLCV cols",  "close" in bars.columns)
        last_close = float(bars["close"].iloc[-1])
        check("Last close > $0", last_close > 0,        f"${last_close:.2f}")
        print(f"\n     Last 3 AAPL daily closes:")
        for ts, row in bars.tail(3).iterrows():
            print(f"       {str(ts)[:10]}  close=${row['close']:.2f}  vol={int(row['volume']):,}")
    except Exception as e:
        check("Daily bars fetch", False, str(e))

    # ------------------------------------------------------------------ #
    # 4. Latest quote — bid/ask spread
    # ------------------------------------------------------------------ #
    print("\n[4] Latest quote — AAPL bid/ask")
    from data.fetcher import get_latest_quote
    try:
        q = get_latest_quote("AAPL")
        check("Quote received",    q["bid"] > 0,   f"bid=${q['bid']:.2f}  ask=${q['ask']:.2f}")
        check("Spread calculated", True,            f"{q['spread_pct']:.4%}")
        check("Spread < 0.6%",     q["spread_pct"] < 0.006, f"{'OK' if q['spread_pct'] < 0.006 else 'HIGH'}")
    except Exception as e:
        check("Latest quote fetch", False, str(e))

    # ------------------------------------------------------------------ #
    # 5. News feed (Benzinga)
    # ------------------------------------------------------------------ #
    print("\n[5] News feed — AAPL (last 24h)")
    from data.fetcher import get_news
    try:
        news = get_news("AAPL", limit=3)
        check("News feed reachable", True)
        check("Articles returned",   len(news) >= 0, f"{len(news)} articles")
        if news:
            print(f"\n     Latest headline: {news[0].get('headline','')[:80]}")
    except Exception as e:
        check("News feed fetch", False, str(e))

    # ------------------------------------------------------------------ #
    # 6. ADV calculation
    # ------------------------------------------------------------------ #
    print("\n[6] ADV (AAPL, 20 days)")
    from data.fetcher import get_adv
    try:
        adv = get_adv("AAPL", lookback=20)
        check("ADV > 1M", adv > 1_000_000, f"{adv/1e6:.1f}M shares/day")
    except Exception as e:
        check("ADV calculation", False, str(e))

    # ------------------------------------------------------------------ #
    # 7. ATR14
    # ------------------------------------------------------------------ #
    print("\n[7] ATR14 (AAPL)")
    from data.fetcher import get_atr14
    try:
        atr = get_atr14("AAPL")
        check("ATR14 > 0", atr > 0, f"${atr:.2f}")
    except Exception as e:
        check("ATR14 calculation", False, str(e))

    print("\n=== All checks passed — Alpaca connection is working ===\n")


if __name__ == "__main__":
    main()
