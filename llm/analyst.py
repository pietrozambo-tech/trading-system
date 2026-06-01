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
- Il campo "reason" deve essere scritto in italiano

TASSONOMIA CATALYST (bonus additivo):
- Tier 1 (+0.30): revenue beat, guidance raised/alzata, earnings beat confermato, FDA approval, acquisizione/merger confermati
- Tier 2 (+0.20): EPS beat modesto, analyst upgrade, price target raise, insider buying, Fed speak direzionale, partnership confermata
- Tier 3 (+0.10): rumor non confermati, articoli speculativi, sentiment settoriale generico
- Nessuno (+0.00): nessuna news identificabile — segnale puramente tecnico

FORMULA CONFIDENCE:
confidence = (direction_score/3) + catalyst_bonus + volume_boost
- direction_score = somma di [above_vwap, or_position>0.66, gap_retention>0.70]
- catalyst_bonus: Tier1=+0.30, Tier2=+0.20, Tier3=+0.10, Nessuno=+0.00
- volume_boost: vol_ratio>3x → +0.10, vol_ratio 2-3x → +0.05, <2x → +0.00
- 2/3 segnali tecnici (0.667) da soli superano già la soglia 0.65

CONTESTO AGGIUNTIVO:
Il campo dist_from_3m_high_pct indica quanto % il titolo è sotto al massimo degli ultimi 3 mesi.
0% = vicino ai massimi (poca resistenza sopra). -20% = 20% sotto i massimi (più resistenza).
Usalo come contesto qualitativo, non come criterio di esclusione.

Il campo post_open_advance_pct indica quanto il titolo è salito (o sceso) tra l'apertura alle 9:30 e le 9:40.
Valori positivi (es. +0.8%) = momentum reale post-gap, il titolo continua a salire dopo l'apertura.
Valori vicini a 0 = consolidamento piatto, stai comprando esattamente dove ha aperto — rischio di essere arrivato tardi.
Valori negativi = il titolo stava già ritracciando al momento dell'entry — segnale di debolezza.
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
    Returns the bonus for the strongest catalyst found.

    Tier 1: revenue beat, guidance raised, confirmed M&A, FDA approval, earnings beat
    Tier 2: modest EPS beat, analyst upgrade, price target raise, insider buying, macro
    Tier 3: rumours, speculative articles, generic sector sentiment
    """
    if not news:
        return config.CATALYST_NONE

    combined = " ".join(
        (n.get("headline", "") + " " + n.get("summary", "")).lower()
        for n in news
    )

    # Tier 1 — high-impact, confirmed catalysts
    tier1_phrases = [
        "fda approval", "fda approved",
        "acquisition confirmed", "merger confirmed", "buyout confirmed",
        "revenue beat", "top-line beat",
        "guidance raised", "raises guidance", "raised guidance",
        "guidance increase", "raised outlook", "raises outlook",
        "earnings beat",
    ]
    # Large EPS surprise qualifies for Tier 1; a modest beat goes to Tier 2
    eps_large_surprise = "eps beat" in combined and any(
        m in combined for m in ["10%", "15%", "20%", "25%", "30%", "40%", "50%"]
    )

    # Tier 2 — real but moderate positive catalysts
    tier2_phrases = [
        "eps beat",
        "acquisition", "merger",
        "fed ", "federal reserve", "trump", "white house",
        "partnership", "insider buying",
        "price target", "analyst upgrade",
    ]

    # Tier 3 — speculative / unconfirmed positive news only
    tier3_phrases = [
        "rumor", "rumour", "specul",
        "buzz", "whisper",
    ]

    if any(p in combined for p in tier1_phrases) or eps_large_surprise:
        return config.CATALYST_TIER1
    if any(p in combined for p in tier2_phrases):
        return config.CATALYST_TIER2
    if any(p in combined for p in tier3_phrases):
        return config.CATALYST_TIER3
    return config.CATALYST_NONE


def build_candidate_payload(candidates_with_signals: list[dict]) -> list[dict]:
    """Enrich candidates with news headlines for LLM context."""
    payload = []
    for c in candidates_with_signals:
        ticker = c["ticker"]
        news = c.get("news") or fetcher.get_news(ticker, limit=5)
        headlines = [n.get("headline", "") for n in news[:3]]
        dist = c.get("dist_from_3m_high")
        payload.append({
            "ticker": ticker,
            "gap_pct": round(c.get("gap_pct", 0), 4),
            "above_vwap": c.get("above_vwap"),
            "or_position": c.get("or_position"),
            "gap_retention": c.get("gap_retention"),
            "post_open_advance_pct": c.get("post_open_advance_pct"),
            "vol_boost": c.get("vol_boost"),
            "confidence_algo": c.get("confidence"),
            "catalyst_bonus": c.get("catalyst_bonus"),
            "dist_from_3m_high_pct": round(dist * 100, 1) if dist is not None else None,
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
Il messaggio usa HTML di Telegram: usa <b>...</b> per il grassetto dove indicato, nessun altro tag HTML.

REGOLE:
- Tono: diretto, amichevole, niente gergo tecnico
- Lunghezza: max 25 righe, leggibile in 20 secondi sul telefono
- Usa emoji ma poche (max 4 in tutto)
- Scrivi in italiano

STRUTTURA OBBLIGATORIA (esattamente in questo ordine, con le righe vuote indicate):

[Data e giorno della settimana]
[riga vuota]
Mercato: [una frase — SPY {spy_pct:+.2%} oggi, commenta in max 6 parole]
[riga vuota]
Per ogni trade (con riga vuota tra un trade e l'altro):
  <b>Trade N — TICKER long [Score: X.XX]</b>
  [UNA riga di contesto: riassumi in max 8 parole il catalyst o segnale chiave dal campo "reason". Usa SOLO fatti già presenti in "reason" — non inventare nulla.]
  Entrata: $X.XX
  Uscita: $X.XX (modalita_uscita)
  P&L: ±$XXX (±X.XX%)
[riga vuota]
Se nessun trade: una riga con il motivo
[riga vuota]
Giornata: {daily_pnl:+.2f}$
P&L totale: {total_pnl:+.2f}$
Saldo: ${account_equity:,.2f}

DATI:
{_json.dumps(clean_trades, indent=2, default=str)}
"""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()
