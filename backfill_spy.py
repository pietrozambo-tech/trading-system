#!/usr/bin/env python3
"""
Backfill del rendimento giornaliero UFFICIALE di SPY (S&P 500) nei daily log.

Il valore salvato intraday alle 09:35 è solo il movimento di apertura dell'indice, non
la performance dell'intera giornata. La chiusura ufficiale si conosce solo dopo le 16:00 ET,
quindi ogni giorno COMPLETATO viene corretto su una run successiva leggendo le barre daily
(prev_close → close). Idempotente: salta i log già marcati `spy_source="official_close"`.

Uso:
  python backfill_spy.py            # patcha i log locali (no push)
  from backfill_spy import backfill # uso programmatico dal bot all'avvio
"""
import glob
import json
import logging
import os
from datetime import datetime

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


def backfill(log_dir: str = "logs") -> list[str]:
    """Patcha spy_pct dei giorni completati con il rendimento ufficiale di SPY.

    Ritorna la lista delle date (YYYY-MM-DD) effettivamente aggiornate, così il
    chiamante può ri-pubblicarle (es. il bot via _push_log_to_github).
    """
    from datetime import datetime as _dt
    from data import fetcher

    base = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(base, log_dir, "*.json")))
    if not files:
        return []

    today_et = datetime.now(ET).date()

    # Carica i log e individua quelli da correggere (giorni passati, non già ufficiali)
    pending: list[tuple[str, str, dict]] = []  # (path, date_str, payload)
    for path in files:
        try:
            with open(path) as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"backfill: impossibile leggere {path}: {e}")
            continue
        date_str = payload.get("date")
        if not date_str:
            continue
        d = _dt.strptime(date_str, "%Y-%m-%d").date()
        if d >= today_et:
            continue  # giornata non ancora chiusa: nessuna chiusura ufficiale
        if payload.get("spy_source") == "official_close":
            continue  # già corretto
        pending.append((path, date_str, payload))

    if not pending:
        logger.info("backfill SPY: nessun giorno da correggere")
        return []

    dates = sorted(d for _, d, _ in pending)
    start = _dt.strptime(dates[0],  "%Y-%m-%d").date()
    end   = _dt.strptime(dates[-1], "%Y-%m-%d").date()
    returns = fetcher.get_spy_daily_returns(start, end)
    if not returns:
        logger.warning("backfill SPY: nessun dato daily ricevuto — log invariati")
        return []

    patched: list[str] = []
    for path, date_str, payload in pending:
        if date_str not in returns:
            continue  # nessuna barra per quel giorno (es. festività non rilevata altrove)
        payload["spy_pct"]    = round(returns[date_str], 6)
        payload["spy_source"] = "official_close"
        try:
            with open(path, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            patched.append(date_str)
            logger.info(f"backfill SPY: {date_str} → {returns[date_str]*100:+.2f}% (chiusura ufficiale)")
        except Exception as e:
            logger.warning(f"backfill: impossibile scrivere {path}: {e}")

    return patched


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    updated = backfill()
    print(f"\nGiorni aggiornati: {len(updated)} {updated}")
