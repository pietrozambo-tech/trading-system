"""
Dry-run diagnostico — replica la pipeline di una sessione senza piazzare ordini.

Uso:
  python diagnose.py                     # usa la data di oggi
  python diagnose.py --date 2026-06-03   # replay di una data specifica

Output: connessioni, segnali L2, decisione LLM. Nessun ordine.
"""
import argparse
import json
import logging
from datetime import date, datetime

import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("diagnose")

ET = pytz.timezone("America/New_York")


def run(session_date: date) -> None:
    from data import fetcher
    from signals import eligibility, triggers
    from llm import analyst
    import config
    import main as _main

    sep = "=" * 60

    # ── 1. CONNESSIONI ────────────────────────────────────────────
    print(f"\n{sep}\n1. CONNESSIONI\n{sep}")

    ok = True
    try:
        acct = fetcher.get_account()
        print(f"  ✓ Trading client — equity: ${acct['equity']:,.2f}")
    except Exception as e:
        print(f"  ✗ Trading client: {e}")
        ok = False

    try:
        snap = fetcher.get_snapshot("SPY")
        prev = float(snap.previous_daily_bar.close)
        last = float(snap.latest_trade.price)
        spy_pct = (last - prev) / prev
        print(f"  ✓ Snapshot (IEX) — SPY: {spy_pct:+.2%}")
    except Exception as e:
        print(f"  ✗ Snapshot: {e}")
        ok = False

    try:
        bars = fetcher.get_daily_bars("MRVL", lookback_days=3)
        print(f"  ✓ Daily bars — MRVL close: ${float(bars['close'].iloc[-1]):.2f}")
    except Exception as e:
        print(f"  ✗ Daily bars: {e}")
        ok = False

    try:
        bars_or = fetcher.get_opening_range_bars("MRVL", session_date=session_date)
        print(f"  ✓ Intraday bars IEX — MRVL OR bars: {len(bars_or)} (attesi 5)")
    except Exception as e:
        print(f"  ✗ Intraday bars IEX: {e}")
        ok = False

    try:
        q = fetcher.get_latest_quote("MRVL")
        print(f"  ✓ Latest quote — MRVL ask: ${q['ask']:.2f}")
    except Exception as e:
        print(f"  ✗ Latest quote: {e}")
        ok = False

    if not ok:
        print("\n  ⚠️  Alcune connessioni hanno fallito — verifica le API key e il feed.")
        return

    # ── 2. PRE-MARKET SCAN ────────────────────────────────────────
    print(f"\n{sep}\n2. PRE-MARKET SCAN (universe completo)\n{sep}")

    universe = _main.UNIVERSE
    watchlist = eligibility.build_premarket_watchlist(universe, session_date=session_date)
    for c in watchlist:
        print(f"  {c['ticker']:6} gap={c['gap_pct']:+.2%}  premarket=${c['premarket_price']:.2f}")

    if not watchlist:
        print("  Nessun ticker in gap — sessione vuota.")
        return

    # ── 3. L1 FILTERS ────────────────────────────────────────────
    print(f"\n{sep}\n3. FILTRI L1\n{sep}")

    spy_pct = fetcher.get_spy_change(session_date)
    print(f"  SPY: {spy_pct:+.2%}  (blocco < {config.SPY_BLOCK_THRESHOLD:.1%})")

    if spy_pct < config.SPY_BLOCK_THRESHOLD:
        print("  ⛔ SPY block — nessun trade.")
        return

    for c in watchlist:
        try:
            c["news"] = fetcher.get_news(c["ticker"], limit=5)
        except Exception:
            c["news"] = []

    candidates, l1_rejects = eligibility.apply_binary_filters(watchlist, session_date=session_date)
    safe_candidates, earn_rejects = eligibility.filter_earnings_tonight(candidates, session_date=session_date)

    for r in l1_rejects + earn_rejects:
        print(f"  ✗ {r['ticker']:6} → {r['reason']}")
    for c in safe_candidates:
        print(f"  ✓ {c['ticker']:6} → passa L1")

    if not safe_candidates:
        print("  Nessun candidato dopo L1.")
        return

    # ── 4. SEGNALI L2 ────────────────────────────────────────────
    print(f"\n{sep}\n4. SEGNALI L2\n{sep}")

    candidates_with_signals = []
    for c in safe_candidates:
        ticker = c["ticker"]
        catalyst_bonus = analyst.classify_catalyst_from_news(c["news"])
        signals = triggers.compute_signals(ticker, c["prev_close"], catalyst_bonus, c.get("short_float"), c.get("gap_pct"), session_date=session_date)
        if not signals:
            print(f"  {ticker}: nessun dato OR")
            continue
        flag = "✓ PASS" if signals["passes_threshold"] else "✗ NO  "
        sf   = signals.get("short_float")
        sf_str = f"  short={sf*100:.1f}%" if sf is not None else "  short=n/a"
        sq_str = f"  squeeze=+{signals['short_squeeze_bonus']:.2f}" if signals.get("short_squeeze_bonus") else ""
        print(
            f"  {flag} {ticker:6}  conf={signals['confidence']:.3f}"
            f"  adv={'✓' if signals['post_open_advance'] else '✗'}"
            f"  OR={signals['or_position']:.2f}"
            f"  GR={signals['gap_retention']:.2f}"
            f"  vol=+{signals['vol_boost']:.2f}"
            f"  cat=+{catalyst_bonus:.2f}"
            f"{sf_str}{sq_str}"
            f"  post_open={signals['post_open_advance_pct']:+.2%}"
        )
        if signals["passes_threshold"]:
            candidates_with_signals.append({**c, **signals})

    if not candidates_with_signals:
        print("\n  Nessun candidato sopra soglia — nessun trade.")
        return

    # ── 5. DECISIONE LLM ─────────────────────────────────────────
    print(f"\n{sep}\n5. LLM — {len(candidates_with_signals)} candidati\n{sep}")

    result = analyst.analyze_candidates(candidates_with_signals, spy_pct, str(session_date))
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # ── RIEPILOGO ─────────────────────────────────────────────────
    print(f"\n{sep}\nRIEPILOGO\n{sep}")
    trades_picked = [v for k, v in result.items() if k.startswith("trade_") and v]
    if trades_picked:
        for t in trades_picked:
            print(f"  → TRADE: {t['ticker']}  confidence={t['confidence']:.2f}")
            print(f"    motivo: {t.get('reason', '')}")
    else:
        print(f"  → NESSUN TRADE: {result.get('no_trade_reason', '—')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="Data sessione YYYY-MM-DD (default: oggi)")
    args = parser.parse_args()

    if args.date:
        session_date = date.fromisoformat(args.date)
    else:
        session_date = datetime.now(ET).date()

    print(f"Diagnosi pipeline — sessione: {session_date}")
    run(session_date)
