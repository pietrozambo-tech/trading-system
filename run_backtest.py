"""
Standalone backtest runner.

Usage:
  python run_backtest.py                # YTD + 2025, full universe, default params
  python run_backtest.py --timing       # entry timing comparison (1/3/5/10/15 min)
  python run_backtest.py --sensitivity  # griglia completa di parametri
  python run_backtest.py --vwap         # sensitivity solo su VWAP exit threshold
  python run_backtest.py --hardstop     # sensitivity solo su hard stop (1.0%–2.5%)
"""
import argparse
import logging
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

from backtest.engine import (
    BacktestParams, BacktestResults,
    run_backtest, sensitivity_analysis, vwap_sensitivity_analysis,
    run_entry_timing_backtest,
    prefetch_universe, _trading_days, _simulate_day,
)

# ---------------------------------------------------------------------------
# Universe — full production universe (same as main.py)
# ---------------------------------------------------------------------------
BACKTEST_UNIVERSE = [
    # Tech / Growth
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX",
    "CRM", "ORCL", "ADBE", "INTC", "QCOM", "MU", "AVGO", "TXN", "AMAT", "MRVL",
    # Finance
    "JPM", "BAC", "GS", "MS", "C", "WFC", "BLK", "SCHW",
    # Healthcare
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "BMY", "MRNA",
    # Energy
    "XOM", "CVX", "SLB", "HAL", "OXY",
    # Clean Energy
    "ENPH",
    # Consumer
    "NKE",
    # Defense
    "LMT",
    # Crypto Proxy
    "MSTR",
    # Airlines / Crociere
    "DAL", "AAL", "NCLH", "CCL",
    # Space
    "RKLB", "ASTS", "BKSY", "RDW", "LUNR",
    # Nucleare / Uranio
    "UUUU", "CCJ", "NNE", "SMR",
    # Quantum Computing
    "IONQ", "QBTS", "QUBT", "RGTI",
]

START_DATE = date(2025, 1, 1)
END_DATE   = date(2026, 6, 2)


def print_summary(results) -> None:
    s = results.summary()
    print("\n" + "=" * 50)
    print("BACKTEST RESULTS")
    print("=" * 50)
    print(f"  Periodo:           {START_DATE} → {END_DATE}")
    print(f"  Universe:          {len(BACKTEST_UNIVERSE)} ticker")
    print(f"  Trade totali:      {s['total_trades']}")
    print(f"  Win rate:          {s['win_rate']:.1%}")
    print(f"  Profit factor:     {s['profit_factor']:.2f}  (target > 1.5)")
    print(f"  Avg Win/Loss:      {s['avg_win_loss_ratio']:.2f}  (target > 1.5)")
    print(f"  P&L totale:        ${s['total_pnl_usd']:+,.2f}")
    print(f"  Avg win:           ${s['avg_win_usd']:+.2f}")
    print(f"  Avg loss:          ${s['avg_loss_usd']:+.2f}")
    print(f"  Max drawdown:      ${s['max_drawdown_usd']:.2f}")
    print(f"  Trade/mese:        {s['trades_per_month']:.1f}  (target > 20)")
    print("=" * 50)

    if results.trades:
        df = results.to_dataframe()
        exit_counts = df["exit_reason"].value_counts()
        print("\nUscite per tipo:")
        for reason, count in exit_counts.items():
            print(f"  {reason:<20} {count:>4} ({count/len(df):.0%})")

        print("\nTop 5 trade (P&L):")
        top = df.nlargest(5, "pnl_usd")[["ticker", "date", "pnl_usd", "pnl_pct", "exit_reason", "confidence"]]
        print(top.to_string(index=False))

        print("\nBottom 5 trade (P&L):")
        bot = df.nsmallest(5, "pnl_usd")[["ticker", "date", "pnl_usd", "pnl_pct", "exit_reason", "confidence"]]
        print(bot.to_string(index=False))


def save_results(results, sensitivity_df=None) -> None:
    os.makedirs("backtest/results", exist_ok=True)
    if results.trades:
        trades_path = "backtest/results/trades.csv"
        results.to_dataframe().to_csv(trades_path, index=False)
        print(f"\nTrade salvati in: {trades_path}")

    equity = []
    cumulative = 0.0
    for d, pnl in sorted(results.daily_pnl.items()):
        cumulative += pnl
        equity.append({"date": d, "daily_pnl": pnl, "equity_curve": cumulative})
    eq_path = "backtest/results/equity_curve.csv"
    pd.DataFrame(equity).to_csv(eq_path, index=False)
    print(f"Equity curve salvata in: {eq_path}")

    if sensitivity_df is not None:
        sens_path = "backtest/results/sensitivity.csv"
        sensitivity_df.to_csv(sens_path, index=False)
        print(f"Sensitivity analysis salvata in: {sens_path}")


def hardstop_sensitivity() -> None:
    """Focused sensitivity on hard_blocker_pct only. All other params at current defaults."""
    hard_stops = [0.010, 0.015, 0.020, 0.025]

    print(f"Hard-stop sensitivity: {[f'{h:.1%}' for h in hard_stops]}")
    print(f"Periodo: {START_DATE} → {END_DATE} | {len(BACKTEST_UNIVERSE)} ticker")
    print(f"VWAP exit min: 1.5% | ATR stop: 1× ATR14\n")

    all_cache = prefetch_universe(BACKTEST_UNIVERSE, START_DATE, END_DATE)
    days      = _trading_days(START_DATE, END_DATE)

    hdr = f"{'Stop':>6}  {'Trades':>7}  {'Win%':>6}  {'PF':>5}  {'W/L':>5}  {'PnL $':>10}  {'AvgWin':>8}  {'AvgLoss':>9}  {'MaxDD':>9}  {'VWAP':>5}  {'EOD':>5}  {'STOP':>5}"
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for hb in hard_stops:
        p   = BacktestParams(hard_blocker_pct=hb)
        res = BacktestResults()
        for day in days:
            day_trades = 0
            for ticker in BACKTEST_UNIVERSE:
                if day_trades >= p.max_positions or ticker not in all_cache:
                    continue
                trade = _simulate_day(ticker, day, all_cache[ticker], p)
                if trade:
                    res.trades.append(trade)
                    day_trades += 1

        s  = res.summary()
        df = res.to_dataframe() if res.trades else pd.DataFrame()
        ec = df["exit_reason"].value_counts().to_dict() if not df.empty else {}
        vwap_x = ec.get("vwap_exit", 0)
        eod_x  = ec.get("eod_close", 0)
        stop_x = sum(v for k, v in ec.items() if "stop" in k or "blocker" in k)

        print(f"{hb:>5.1%}  {s['total_trades']:>7}  {s['win_rate']:>6.1%}  {s['profit_factor']:>5.2f}  {s['avg_win_loss_ratio']:>5.2f}  {s['total_pnl_usd']:>+10,.0f}  {s['avg_win_usd']:>+8.0f}  {s['avg_loss_usd']:>+9.0f}  {s['max_drawdown_usd']:>9,.0f}  {vwap_x:>5}  {eod_x:>5}  {stop_x:>5}")
        rows.append({"hard_stop_pct": hb, **s, "vwap_exits": vwap_x, "eod_exits": eod_x, "stop_exits": stop_x})

    os.makedirs("backtest/results", exist_ok=True)
    path = "backtest/results/hardstop_sensitivity.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"\nRisultati salvati in: {path}")


def print_timing_summary(results_by_offset: dict, universe_size: int) -> None:
    """Print comparison table for entry timing analysis."""
    offsets    = sorted(results_by_offset.keys())
    entry_time = {1: "9:31", 3: "9:33", 5: "9:35", 10: "9:40", 15: "9:45"}
    oracle_msg = "(oracle — before 9:40 confirmation)" if any(o < 10 for o in offsets) else ""

    print("\n" + "=" * 100)
    print(f"ENTRY TIMING ANALYSIS  {START_DATE} → {END_DATE} | {universe_size} tickers | $49k/trade")
    if oracle_msg:
        print(f"Note: offsets 1/3/5 are {oracle_msg}")
    print("=" * 100)
    hdr = f"{'Entry':>7}  {'Trades':>7}  {'Win%':>6}  {'PF':>5}  {'W/L':>5}  {'AvgWin$':>8}  {'AvgLoss$':>9}  {'TotalPnL$':>11}  {'MaxDD$':>8}  {'VWAP%':>6}  {'EOD%':>5}  {'Stop%':>6}"
    print(hdr)
    print("-" * 100)

    rows = []
    for o in offsets:
        res = results_by_offset[o]
        s   = res.summary()
        df  = res.to_dataframe() if res.trades else pd.DataFrame()
        ec  = df["exit_reason"].value_counts().to_dict() if not df.empty else {}
        n   = s["total_trades"]
        vwap_n  = ec.get("vwap_exit", 0)
        eod_n   = ec.get("eod_close", 0)
        stop_n  = sum(v for k, v in ec.items() if "stop" in k or "blocker" in k)
        marker  = " ←" if o == 10 else ""
        label   = entry_time.get(o, f"9:{30+o}")
        print(
            f"{label:>7}  {n:>7}  {s['win_rate']:>6.1%}  {s['profit_factor']:>5.2f}  "
            f"{s['avg_win_loss_ratio']:>5.2f}  {s['avg_win_usd']:>+8.0f}  {s['avg_loss_usd']:>+9.0f}  "
            f"{s['total_pnl_usd']:>+11,.0f}  {s['max_drawdown_usd']:>8,.0f}  "
            f"{vwap_n/n:>6.0%}  {eod_n/n:>5.0%}  {stop_n/n:>6.0%}{marker}"
        )
        rows.append({"entry_time": label, "entry_offset_min": o, **s,
                     "vwap_pct": vwap_n/n if n else 0,
                     "eod_pct": eod_n/n if n else 0,
                     "stop_pct_exits": stop_n/n if n else 0})
    print("=" * 100)

    os.makedirs("backtest/results", exist_ok=True)
    path = "backtest/results/entry_timing.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"\nRisultati salvati in: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timing",      action="store_true", help="Entry timing comparison (1/3/5/10/15 min)")
    parser.add_argument("--sensitivity", action="store_true", help="Griglia completa parametri")
    parser.add_argument("--vwap",        action="store_true", help="Sensitivity solo VWAP exit threshold")
    parser.add_argument("--hardstop",    action="store_true", help="Sensitivity solo hard stop (1.0%–2.5%)")
    args = parser.parse_args()

    if args.timing:
        print(f"Entry timing analysis: {START_DATE} → {END_DATE} | {len(BACKTEST_UNIVERSE)} tickers")
        print("Signals always computed at 9:40. Entry prices tested at 9:31, 9:33, 9:35, 9:40, 9:45.\n")
        results_by_offset = run_entry_timing_backtest(
            BACKTEST_UNIVERSE, START_DATE, END_DATE,
            entry_offsets=[1, 3, 5, 10, 15],
        )
        print_timing_summary(results_by_offset, len(BACKTEST_UNIVERSE))

    elif args.hardstop:
        hardstop_sensitivity()

    elif args.vwap:
        thresholds = [0.010, 0.015, 0.020, 0.025, 0.030]
        print(f"VWAP sensitivity: {[f'{t:.1%}' for t in thresholds]}")
        print(f"Periodo: {START_DATE} → {END_DATE} | {len(BACKTEST_UNIVERSE)} ticker\n")
        vwap_df = vwap_sensitivity_analysis(BACKTEST_UNIVERSE, START_DATE, END_DATE, thresholds)

        print("\n" + "=" * 90)
        print("VWAP EXIT MIN PROFIT — SENSITIVITY RESULTS")
        print("=" * 90)
        print(vwap_df.to_string(index=False))
        print("=" * 90)

        os.makedirs("backtest/results", exist_ok=True)
        path = "backtest/results/vwap_sensitivity.csv"
        vwap_df.to_csv(path, index=False)
        print(f"\nRisultati salvati in: {path}")

    elif args.sensitivity:
        print(f"Avvio sensitivity analysis su {len(BACKTEST_UNIVERSE)} ticker × {START_DATE} → {END_DATE} …")
        sens_df = sensitivity_analysis(BACKTEST_UNIVERSE, START_DATE, END_DATE)
        print("\nTop 10 combinazioni parametri (per profit factor):")
        print(sens_df.head(10).to_string(index=False))
        save_results(run_backtest(BACKTEST_UNIVERSE, START_DATE, END_DATE), sens_df)

    else:
        params = BacktestParams()
        print(f"Avvio backtest su {len(BACKTEST_UNIVERSE)} ticker × {START_DATE} → {END_DATE} …")
        results = run_backtest(BACKTEST_UNIVERSE, START_DATE, END_DATE, params)
        print_summary(results)
        save_results(results)


if __name__ == "__main__":
    main()
