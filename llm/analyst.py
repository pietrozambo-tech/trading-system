import json
import logging
from datetime import datetime

import anthropic
import pytz

import config
from data import fetcher

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

SYSTEM_PROMPT = """
Sei un sistema di analisi per day trading intraday su US equities.
Ricevi una lista di candidati già filtrati algoritmicamente.
Per ognuno hai: segnali tecnici calcolati, news recenti, score di qualità.

REGOLE ASSOLUTE:
- Solo posizioni LONG
- Max 2 trade — puoi restituire 0 o 1 se i segnali sono deboli
- NON forzare trade se confidence < 0.65
- Penalizza se entrambi i trade sono nello stesso settore GICS
- Rispondi SOLO con JSON valido, zero testo aggiuntivo

TASSONOMIA CATALYST (bonus additivo):
- Tier 1 (+0.30): earnings beat >5%, upgrade broker primario >10% target, FDA approval, acquisizione confermata
- Tier 2 (+0.20): earnings beat modesto, Fed speak direzionale, Trump tweet/White House su settore specifico, upgrade target price, partnership confermata, insider buying
- Tier 3 (+0.10): rumor non confermati, articoli speculativi, sentiment settoriale
- Nessuno (+0.00): nessuna news identificabile — segnale puramente tecnico

FORMULA CONFIDENCE:
confidence = (direction_score/3) + catalyst_bonus + volume_boost
- direction_score = somma di [above_vwap, or_position>0.66, gap_retention>0.70]
- catalyst_bonus: Tier1=+0.30, Tier2=+0.20, Tier3=+0.10, Nessuno=+0.00
- volume_boost: vol_ratio>3x → +0.10, vol_ratio 2-3x → +0.05, <2x → +0.00
- 2/3 segnali tecnici (0.667) da soli superano già la soglia 0.65
"""

USER_PROMPT_TEMPLATE = """\
Data: {date}
SPY oggi: {spy_pct:+.2f}%

Candidati ({n} titoli):

{candidates_json}

Restituisci JSON con questa struttura esatta:
{{
  "trade_1": {{
    "ticker": "XXXX",
    "direction": "long",
    "confidence": 0.00,
    "reason": "max 2 frasi — catalyst + segnale tecnico dominante"
  }},
  "trade_2": null,
  "no_trade_reason": null
}}
"""


def classify_catalyst_from_news(news: list[dict]) -> float:
    """
    Heuristic catalyst classification based on news headlines.
    Returns the multiplier for the strongest catalyst found.
    """
    if not news:
        return config.CATALYST_NONE

    combined = " ".join(
        (n.get("headline", "") + " " + n.get("summary", "")).lower()
        for n in news
    )

    # Tier 1
    tier1_keywords = [
        "earnings beat", "eps beat", "revenue beat", "guidance raised",
        "fda approval", "acquisition", "merger confirmed", "upgrade",
    ]
    # Tier 2
    tier2_keywords = [
        "earnings", "fed", "federal reserve", "trump", "white house",
        "partnership", "insider buying", "price target",
    ]
    # Tier 3
    tier3_keywords = [
        "rumor", "report", "specul", "analyst", "sector", "industry",
    ]

    # Check upgrade with meaningful target raise as Tier 1
    if any(k in combined for k in ["fda approval", "acquisition confirmed", "merger confirmed"]):
        return config.CATALYST_TIER1
    if "earnings beat" in combined or ("eps beat" in combined and "5%" in combined):
        return config.CATALYST_TIER1
    if any(k in combined for k in tier1_keywords):
        return config.CATALYST_TIER2
    if any(k in combined for k in tier2_keywords):
        return config.CATALYST_TIER2
    if any(k in combined for k in tier3_keywords):
        return config.CATALYST_TIER3

    return config.CATALYST_NONE


def build_candidate_payload(candidates_with_signals: list[dict]) -> list[dict]:
    """Enrich candidates with news headlines for LLM context."""
    payload = []
    for c in candidates_with_signals:
        ticker = c["ticker"]
        news = fetcher.get_news(ticker, limit=5)
        headlines = [n.get("headline", "") for n in news[:3]]
        payload.append({
            "ticker": ticker,
            "gap_pct": round(c.get("gap_pct", 0), 4),
            "above_vwap": c.get("above_vwap"),
            "or_position": c.get("or_position"),
            "gap_retention": c.get("gap_retention"),
            "vol_boost": c.get("vol_boost"),
            "confidence_algo": c.get("confidence"),
            "catalyst_multiplier": c.get("catalyst_multiplier"),
            "recent_headlines": headlines,
        })
    return payload


def analyze_candidates(
    candidates_with_signals: list[dict],
    spy_pct: float,
    today: str,
) -> dict:
    """
    Call Claude API with up to MAX_CANDIDATES_TO_LLM candidates.
    Returns parsed JSON: {trade_1, trade_2, no_trade_reason}.
    """
    limited = candidates_with_signals[: config.MAX_CANDIDATES_TO_LLM]
    payload = build_candidate_payload(limited)

    user_prompt = USER_PROMPT_TEMPLATE.format(
        date=today,
        spy_pct=spy_pct * 100,
        n=len(payload),
        candidates_json=json.dumps(payload, indent=2),
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()
    logger.info(f"LLM raw response: {raw}")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON block if model added surrounding text
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            logger.error("Could not parse LLM JSON response")
            result = {"trade_1": None, "trade_2": None, "no_trade_reason": "LLM parse error"}

    return result


def generate_eod_recap(
    trade_data: list[dict],
    spy_pct: float,
    account_equity: float,
    daily_pnl: float,
) -> str:
    """Generate the EOD Telegram message via LLM. Returns empty string if no meaningful data."""
    import json as _json

    # Se non ci sono dati reali, lascia che il fallback template gestisca il messaggio
    has_real_data = any(t.get("ticker") for t in trade_data)
    if not has_real_data:
        return ""

    total_pnl = account_equity - config.PAPER_INITIAL_EQUITY

    exit_labels = {
        "eod_close":    "Fine giornata",
        "hard_blocker": "Stop loss",
        "dollar_stop":  "Stop loss",
        "atr_stop":     "Stop loss (ATR)",
        "vwap_exit":    "Profit taker",
    }

    # Traduci exit_reason in label user-friendly
    clean_trades = []
    for t in trade_data:
        ct = dict(t)
        raw_reason = ct.get("exit_reason", "")
        ct["modalita_uscita"] = exit_labels.get(raw_reason, raw_reason)
        clean_trades.append(ct)

    prompt = f"""Sei un assistente di trading. Scrivi un messaggio Telegram EOD in italiano.

REGOLE:
- Tono: diretto, amichevole, niente gergo tecnico
- Lunghezza: max 20 righe, leggibile in 20 secondi sul telefono
- Usa emoji ma poche (max 5 in tutto)
- Scrivi in italiano

STRUTTURA OBBLIGATORIA (esattamente in questo ordine):
1. Data e giorno della settimana
2. Mercato: una frase sul contesto generale (SPY {spy_pct:+.2f}% oggi — commenta brevemente)
3. Per ogni trade eseguito:
   - Ticker e direzione (long)
   - Prezzo entrata
   - Prezzo uscita + modalità (usa il campo "modalita_uscita": Fine giornata / Stop loss / Profit taker)
   - P&L del trade in $ e %
4. Se nessun trade: una riga con il motivo
5. P&L giornata: {daily_pnl:+.2f}$
6. P&L totale conto dall'inizio: {total_pnl:+.2f}$
7. Saldo disponibile: {account_equity:,.2f}$

DATI:
{_json.dumps(clean_trades, indent=2, default=str)}
"""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()
