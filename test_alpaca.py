"""
Alpaca API connectivity test.

Run locally:   python test_alpaca.py
Run on GitHub: Actions → "Alpaca connectivity test" → Run workflow

Checks:
  1. Environment variables
  2. TradingClient — account info
  3. Daily bars (AAPL)
  4. Intraday bars IEX (AAPL)
  5. Latest quote bid/ask (AAPL)
  6. get_current_price — latest trade IEX (StockLatestTradeRequest)
  7. News feed (Benzinga)
  8. ADV calculation
  9. ATR14 calculation
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    icon = "✓" if ok else "✗"
    print(f"  {icon}  {label}" + (f"  →  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def main():
    print("\n=== Alpaca API connectivity test ===\n")

    # ------------------------------------------------------------------ #
    # 1. Config / env vars
    # ------------------------------------------------------------------ #
    print("[1] Environment variables")
    import config
    check("ALPACA_API_KEY set",    bool(config.ALPACA_API_KEY),    config.ALPACA_API_KEY[:8] + "…" if config.ALPACA_API_KEY else "MISSING")
    check("ALPACA_SECRET_KEY set", bool(config.ALPACA_SECRET_KEY), "***" if config.ALPACA_SECRET_KEY else "MISSING")
    check("ALPACA_BASE_URL set",   bool(config.ALPACA_BASE_URL),   config.ALPACA_BASE_URL or "MISSING")
    check("ANTHROPIC_API_KEY set", bool(config.ANTHROPIC_API_KEY), "***" if config.ANTHROPIC_API_KEY else "MISSING")
    icon = "✓" if config.TELEGRAM_BOT_TOKEN else "⚠"
    print(f"  {icon}  TELEGRAM_BOT_TOKEN  →  {'set' if config.TELEGRAM_BOT_TOKEN else 'non configurato (opzionale)'}")

    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        print("\n  ⛔ API keys mancanti — impossibile proseguire.\n")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 2. TradingClient — account
    # ------------------------------------------------------------------ #
    print("\n[2] TradingClient — account")
    from data.fetcher import get_account
    try:
        acct = get_account()
        check("Connection OK",  True)
        check("Equity > 0",     acct["equity"] > 0,        f"${acct['equity']:,.2f}")
        check("Cash >= 0",      acct["cash"] >= 0,         f"${acct['cash']:,.2f}")
        check("Buying power",   acct["buying_power"] >= 0, f"${acct['buying_power']:,.2f}")
    except Exception as e:
        check("TradingClient", False, str(e))

    # ------------------------------------------------------------------ #
    # 3. Daily bars
    # ------------------------------------------------------------------ #
    print("\n[3] Daily bars — AAPL (last 5 days)")
    from data.fetcher import get_daily_bars
    try:
        bars = get_daily_bars("AAPL", lookback_days=5)
        check("Bars received",   not bars.empty,              f"{len(bars)} bars")
        check("Has OHLCV cols",  "close" in bars.columns)
        last_close = float(bars["close"].iloc[-1])
        check("Last close > $0", last_close > 0,              f"${last_close:.2f}")
        for ts, row in bars.tail(3).iterrows():
            print(f"       {str(ts)[:10]}  close=${row['close']:.2f}  vol={int(row['volume']):,}")
    except Exception as e:
        check("Daily bars", False, str(e))

    # ------------------------------------------------------------------ #
    # 4. Intraday bars IEX
    # ------------------------------------------------------------------ #
    print("\n[4] Intraday bars IEX — AAPL (today, 1-min)")
    from data.fetcher import get_intraday_bars
    try:
        ibars = get_intraday_bars("AAPL", minutes=1)
        check("Intraday bars received", True, f"{len(ibars)} bars (0 fuori orario mercato è normale)")
    except Exception as e:
        check("Intraday bars IEX", False, str(e))

    # ------------------------------------------------------------------ #
    # 5. Latest quote
    # ------------------------------------------------------------------ #
    print("\n[5] Latest quote — AAPL bid/ask")
    from data.fetcher import get_latest_quote
    try:
        q = get_latest_quote("AAPL")
        check("Quote received",  q["bid"] > 0 or q["ask"] > 0, f"bid=${q['bid']:.2f}  ask=${q['ask']:.2f}")
        check("Spread < 1%",     q["spread_pct"] < 0.01,       f"{q['spread_pct']:.4%}")
    except Exception as e:
        check("Latest quote", False, str(e))

    # ------------------------------------------------------------------ #
    # 6. get_current_price — latest trade (StockLatestTradeRequest)
    # ------------------------------------------------------------------ #
    print("\n[6] get_current_price — latest trade IEX")
    from data.fetcher import get_current_price
    try:
        price = get_current_price("AAPL")
        check("Latest trade price > 0", price > 0, f"${price:.2f}")
    except Exception as e:
        check("get_current_price (StockLatestTradeRequest)", False, str(e))

    # ------------------------------------------------------------------ #
    # 7. News feed
    # ------------------------------------------------------------------ #
    print("\n[7] News feed — AAPL")
    from data.fetcher import get_news
    try:
        news = get_news("AAPL", limit=3)
        check("News feed reachable", True, f"{len(news)} articles")
        if news:
            print(f"       Latest: {news[0].get('headline','')[:80]}")
    except Exception as e:
        check("News feed", False, str(e))

    # ------------------------------------------------------------------ #
    # 8. ADV
    # ------------------------------------------------------------------ #
    print("\n[8] ADV — AAPL (20 days)")
    from data.fetcher import get_adv
    try:
        adv = get_adv("AAPL", lookback=20)
        check("ADV > 1M shares", adv > 1_000_000, f"{adv/1e6:.1f}M shares/day")
    except Exception as e:
        check("ADV", False, str(e))

    # ------------------------------------------------------------------ #
    # 9. ATR14
    # ------------------------------------------------------------------ #
    print("\n[9] ATR14 — AAPL")
    from data.fetcher import get_atr14
    try:
        atr = get_atr14("AAPL")
        check("ATR14 > 0", atr > 0, f"${atr:.2f}")
    except Exception as e:
        check("ATR14", False, str(e))

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 45)
    if failures:
        print(f"  ✗  {len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"       - {f}")
        print("=" * 45 + "\n")
        sys.exit(1)
    else:
        print("  ✓  All checks passed — pronti per domani.")
        print("=" * 45 + "\n")


if __name__ == "__main__":
    main()
