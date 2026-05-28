"""
Standalone backtest runner.

Usage:
  python run_backtest.py                # 6 mesi, parametri di default
  python run_backtest.py --sensitivity  # griglia completa di parametri
  python run_backtest.py --vwap         # sensitivity solo su VWAP exit threshold
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

from backtest.engine import BacktestParams, run_backtest, sensitivity_analysis, vwap_sensitivity_analysis

# ---------------------------------------------------------------------------
# Universe — 15 ticker liquidi, diversificati per settore
# ---------------------------------------------------------------------------
BACKTEST_UNIVERSE = [
    # Tech
    "AAPL", "MSFT", "NVDA", "AMD", "META",
    # Consumer / E-commerce
    "AMZN", "TSLA", "NFLX",
    # Finance
    "JPM", "GS",
    # Healthcare
    "UNH", "LLY",
    # Energy
    "XOM",
    # Semis
    "AVGO", "QCOM",
]

START_DATE = date(2025, 11, 28)   # 6 mesi
END_DATE   = date(2026, 5, 27)


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sensitivity", action="store_true", help="Griglia completa parametri")
    parser.add_argument("--vwap",        action="store_true", help="Sensitivity solo VWAP exit threshold")
    args = parser.parse_args()

    if args.vwap:
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
