#!/usr/bin/env python3
"""
Analisi dei candidati REALI dei daily log non eseguiti per via del cap a 2 posizioni.

Domanda: stiamo lasciando sul tavolo ingressi di qualità limitando a MAX_POSITIONS=2?
A differenza di `run_backtest.py --positions` (che ri-simula l'intero universo con i
segnali ricalcolati), questo script parte dai candidati VERI che il pipeline live ha
fatto passare la soglia L2 ogni giorno — con la loro confidence reale e il loro
price_935 reale — e simula SOLO l'uscita con la config live (Step C + buffer −0.2%,
VWAP take-profit, ATR/hard stop, EOD).

Logica:
  1. Per ogni daily log non bloccato: pool = signals con passes_threshold=True,
     ordinati per confidence. I primi N realmente tradati sono il baseline (P&L reale
     dal log = ground truth). Il "3° marginale" = miglior candidato passato ma NON tradato.
  2. Si fetchano le barre 1-min reali e si simula l'uscita a partire da entry=price_935.
  3. VALIDAZIONE: per i nomi realmente eseguiti, confronto pnl_pct simulato vs reale.
     Se combaciano, la simulazione sui candidati mancati è affidabile.
  4. Si aggregano le statistiche del 3° trade marginale (size-independent + a $33k e $49.5k).

Richiede accesso ad Alpaca (gira su GitHub Actions). Usage: python analyze_missed_candidates.py
"""
import glob
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

import config
from backtest.engine import prefetch_universe, _calc_atr14, ET

# Config di uscita LIVE (deve restare allineata a config.STEP_STOPS)
STEP_STOPS        = config.STEP_STOPS
VWAP_MIN_PROFIT   = config.VWAP_EXIT_MIN_PROFIT_PCT
HARD_BLOCKER_PCT  = config.HARD_BLOCKER_PCT
EQUITY            = 99_000   # $100k − $1k cushion


def simulate_exit(ticker: str, session_date: date, entry_price: float, cache: dict) -> dict:
    """Replica l'uscita live a partire da un'entry fissata a 9:35.

    Stessa logica di backtest.engine._simulate_day (righe 342-411): stop = max(ATR, hard%),
    ratchet a gradini, VWAP take-profit ≥ soglia, altrimenti EOD. Ritorna None se mancano barre.
    """
    daily          = cache["daily"]
    intraday_cache = cache["intraday"]
    if session_date not in intraday_cache:
        return None

    day_bars = intraday_cache[session_date]
    # Barre da 9:35 a 15:45 (post-entry)
    post_bars = day_bars[
        ((day_bars.index.hour == 9)  & (day_bars.index.minute >= 35)) |
        ((day_bars.index.hour > 9)   & (day_bars.index.hour < 15))    |
        ((day_bars.index.hour == 15) & (day_bars.index.minute <= 45))
    ]
    if post_bars.empty:
        return None

    atr14    = _calc_atr14(daily, session_date)
    stop_atr = entry_price - atr14 if atr14 > 0 else 0
    stop_pct = entry_price * (1 - HARD_BLOCKER_PCT)
    dyn_stop = max(stop_atr, stop_pct)
    stop_label = "hard_blocker" if stop_pct >= stop_atr else "atr_stop"

    exit_price, exit_reason = entry_price, "eod_close"
    cumvol, cumtpvol = 0.0, 0.0
    peak = entry_price

    for ts, bar in post_bars.iterrows():
        if ts.hour == 15 and ts.minute >= 45:
            exit_price, exit_reason = float(bar["close"]), "eod_close"
            break
        price = float(bar["close"])
        peak  = max(peak, price)
        peak_gain = (peak - entry_price) / entry_price

        if STEP_STOPS is not None:
            for trigger, floor in sorted(STEP_STOPS):
                if peak_gain >= trigger:
                    new_stop = entry_price * (1 + floor)
                    if new_stop > dyn_stop:
                        dyn_stop   = new_stop
                        stop_label = "breakeven_stop" if floor <= 0.0 else "step_stop"

        if price <= dyn_stop:
            exit_price, exit_reason = max(price, dyn_stop), stop_label
            break

        tp = (float(bar["high"]) + float(bar["low"]) + price) / 3
        cumvol   += float(bar["volume"])
        cumtpvol += tp * float(bar["volume"])
        vwap_now  = cumtpvol / cumvol if cumvol > 0 else price
        profit_pct = (price - entry_price) / entry_price
        if price < vwap_now and profit_pct >= VWAP_MIN_PROFIT:
            exit_price, exit_reason = price, "vwap_exit"
            break

    return {
        "exit_price":  round(exit_price, 4),
        "exit_reason": exit_reason,
        "pnl_pct":     round((exit_price - entry_price) / entry_price, 4),
    }


def load_candidates() -> list[dict]:
    """Estrae da ogni daily log i candidati passati L2, con flag traded e P&L reale."""
    rows = []
    for f in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "logs/*.json"))):
        with open(f) as fp:
            log = json.load(fp)
        if log.get("blocked"):
            continue
        d = log["date"]
        real = {t["ticker"]: t for t in log.get("trades", []) if t.get("exit_price")}
        for s in log.get("signals", []):
            if not s.get("passes_threshold"):
                continue
            tk = s["ticker"]
            rows.append({
                "date":         d,
                "ticker":       tk,
                "confidence":   s.get("confidence"),
                "price_935":    s.get("price_935"),
                "traded":       tk in real,
                "real_pnl_pct": real[tk].get("pnl_pct") if tk in real else None,
                "real_pnl_usd": real[tk].get("pnl_usd") if tk in real else None,
                "real_exit":    real[tk].get("exit_reason") if tk in real else None,
            })
    return rows


def main() -> None:
    cands = load_candidates()
    if not cands:
        print("Nessun candidato trovato nei daily log.")
        return

    dates    = sorted({c["date"] for c in cands})
    tickers  = sorted({c["ticker"] for c in cands})
    start    = datetime.strptime(dates[0], "%Y-%m-%d").date()
    end      = datetime.strptime(dates[-1], "%Y-%m-%d").date()

    print(f"Daily log: {len(dates)} giorni ({dates[0]} → {dates[-1]}) | {len(tickers)} ticker | {len(cands)} candidati L2")
    print(f"Exit config: STEP_STOPS={STEP_STOPS}, VWAP≥{VWAP_MIN_PROFIT:.1%}, hard={HARD_BLOCKER_PCT:.1%}\n")

    # Prefetch barre reali (serve un buffer per ATR su barre daily precedenti)
    cache = prefetch_universe(tickers, start - timedelta(days=40), end)

    # Simula l'uscita per ogni candidato con entry = price_935
    for c in cands:
        sd = datetime.strptime(c["date"], "%Y-%m-%d").date()
        ep = c["price_935"]
        sim = simulate_exit(c["ticker"], sd, ep, cache.get(c["ticker"], {})) if ep else None
        c["sim_pnl_pct"]   = sim["pnl_pct"]     if sim else None
        c["sim_exit"]      = sim["exit_reason"] if sim else None

    # ── VALIDAZIONE: sim vs reale sui trade eseguiti ────────────────────────────
    print("=" * 92)
    print("VALIDAZIONE — P&L simulato vs reale sui trade ESEGUITI (ground truth)")
    print("=" * 92)
    print(f"{'Data':<12}{'Ticker':<7}{'Entry':>9}{'Real %':>9}{'Sim %':>9}{'Δ pp':>8}  {'Real exit':<16}{'Sim exit':<16}")
    print("-" * 92)
    val_traded = [c for c in cands if c["traded"] and c["sim_pnl_pct"] is not None and c["real_pnl_pct"] is not None]
    deltas = []
    for c in sorted(val_traded, key=lambda x: (x["date"], x["ticker"])):
        delta_pp = (c["sim_pnl_pct"] - c["real_pnl_pct"]) * 100
        deltas.append(abs(delta_pp))
        print(f"{c['date']:<12}{c['ticker']:<7}{c['price_935']:>9.2f}"
              f"{c['real_pnl_pct']*100:>8.2f}%{c['sim_pnl_pct']*100:>8.2f}%{delta_pp:>+8.2f}  "
              f"{(c['real_exit'] or ''):<16}{(c['sim_exit'] or ''):<16}")
    if deltas:
        print("-" * 92)
        print(f"Errore medio assoluto simulazione: {sum(deltas)/len(deltas):.2f} punti percentuali "
              f"({len(deltas)} trade). Più è basso, più la simulazione sui mancati è affidabile.")

    # ── MARGINALE: per ogni giorno, il miglior candidato passato ma NON tradato ──
    by_day: dict[str, list[dict]] = {}
    for c in cands:
        by_day.setdefault(c["date"], []).append(c)

    marginals = []
    for d, lst in by_day.items():
        missed = [c for c in lst if not c["traded"] and c["sim_pnl_pct"] is not None]
        if missed:
            best = max(missed, key=lambda x: (x["confidence"] or 0))
            marginals.append(best)

    print("\n" + "=" * 92)
    print("3° TRADE MARGINALE — miglior candidato passato L2 ma NON eseguito, per giorno")
    print("(simulato con la config di uscita live; il P&L reale eseguito è il baseline noto)")
    print("=" * 92)
    print(f"{'Data':<12}{'Marginale':<11}{'Conf':>6}{'Entry':>9}{'Sim %':>9}  {'Sim exit':<16}")
    print("-" * 92)
    for c in sorted(marginals, key=lambda x: x["date"]):
        print(f"{c['date']:<12}{c['ticker']:<11}{(c['confidence'] or 0):>6.2f}{c['price_935']:>9.2f}"
              f"{c['sim_pnl_pct']*100:>8.2f}%  {(c['sim_exit'] or ''):<16}")

    if marginals:
        pcts   = [c["sim_pnl_pct"] for c in marginals]
        wins   = [p for p in pcts if p > 0]
        losses = [p for p in pcts if p <= 0]
        gross_w = sum(wins)
        gross_l = abs(sum(losses))
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        print("-" * 92)
        print(f"  Giorni con un 3° candidato:  {len(marginals)}")
        print(f"  Win rate:                    {len(wins)/len(marginals):.1%}")
        print(f"  Profit factor (su %):        {pf:.2f}")
        print(f"  Avg P&L:                     {sum(pcts)/len(pcts)*100:+.2f}%")
        print(f"  Avg win / Avg loss:          {(gross_w/len(wins) if wins else 0)*100:+.2f}% / "
              f"{(sum(losses)/len(losses) if losses else 0)*100:+.2f}%")
        # Conversione in dollari ai due framing di sizing
        usd_33 = sum(p * (EQUITY/3)  for p in pcts)   # capitale costante: 3 slot da $33k
        usd_49 = sum(p * (EQUITY/2)  for p in pcts)   # additivo: 3° slot alla size attuale $49.5k
        print(f"\n  P&L $ del 3° slot @ $33k (capitale costante, $99k/3): {usd_33:+,.0f}")
        print(f"  P&L $ del 3° slot @ $49.5k (additivo, size attuale):  {usd_49:+,.0f}")
        print(f"  NB: a capitale costante i primi 2 slot scendono da $49.5k a $33k —")
        print(f"      il confronto netto vero è nella tabella `--positions`.")

    # Salva CSV
    os.makedirs("backtest/results", exist_ok=True)
    pd.DataFrame(cands).to_csv("backtest/results/missed_candidates_all.csv", index=False)
    pd.DataFrame(marginals).to_csv("backtest/results/missed_candidates_marginal.csv", index=False)
    print(f"\nSalvati: backtest/results/missed_candidates_all.csv, missed_candidates_marginal.csv")


if __name__ == "__main__":
    main()
