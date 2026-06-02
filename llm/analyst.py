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

Il campo post_open_advance_pct indica quanto il titolo è salito (o sceso) tra l'apertura alle 9:30 e le 9:35 (5 minuti di trading reale).
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
    Heuristic catalyst classification based on news headlines + summaries.
    Processes each article independently (no cross-article concatenation) to avoid
    false positives from unrelated articles contaminating each other.
    Returns the bonus for the strongest positive catalyst found.

    Tier 1: revenue beat, guidance raised, confirmed M&A, FDA approval, large EPS surprise
    Tier 2: modest EPS beat, analyst upgrade, price target raise, insider buying, partnership
    Tier 3: unconfirmed rumours, speculative articles
    """
    if not news:
        return config.CATALYST_NONE

    # Phrases that indicate the story is bearish — skip articles containing these
    # so they don't accidentally match a Tier keyword (e.g. "missed earnings beat consensus")
    negative_blockers = [
        "miss", "misses", "missed", "falls short", "fell short",
        "below estimates", "below expectations", "below consensus",
        "disappoints", "disappointing", "disappointed",
        "cuts guidance", "cut guidance", "lowers guidance", "slashes guidance",
        "reduces guidance", "guidance cut", "guidance reduced", "guidance lowered",
        "warns of", "cautious outlook", "weaker than expected",
    ]

    tier1_phrases = [
        "fda approv",                          # fda approval / fda approved
        "acquisition confirmed", "merger confirmed", "buyout confirmed",
        "agreed to acquire", "definitive agreement to acquire",
        "revenue beat", "top-line beat", "record revenue", "record sales",
        "beats revenue", "topped revenue estimates",
        "guidance raised", "raises guidance", "raised guidance", "raises its guidance",
        "raised outlook", "raises outlook", "raised its outlook",
        "guidance increase", "guidance raised",
        "earnings beat",                        # confirmed result, not rumour
    ]

    tier2_phrases = [
        "eps beat", "beats estimates", "beat estimates", "beat earnings estimates",
        "analyst upgrade", "upgraded to buy", "upgraded to outperform",
        "upgraded to overweight", "upgraded to strong buy",
        "price target raised", "price target increased", "raises price target",
        "boosts price target", "lifts price target",
        "insider buying", "insider purchase", "insider bought",
        "partnership", "strategic partnership", "collaboration agreement",
        "license agreement", "supply agreement",
    ]

    tier3_phrases = [
        "rumor", "rumour", "speculat",
        "buzz", "whisper",
        "could acquire", "might acquire", "exploring a sale", "considering a deal",
    ]

    best_tier = 0

    for article in news:
        text = (article.get("headline", "") + " " + article.get("summary", "")).lower()

        # Skip articles whose primary story is negative
        if any(neg in text for neg in negative_blockers):
            continue

        # Tier 1 — return immediately, can't do better
        if any(p in text for p in tier1_phrases):
            return config.CATALYST_TIER1

        # Large EPS surprise (% mentioned alongside beat) → also Tier 1
        if ("eps beat" in text or "beat estimates" in text) and any(
            m in text for m in ["10%", "15%", "20%", "25%", "30%", "40%", "50%"]
        ):
            return config.CATALYST_TIER1

        if any(p in text for p in tier2_phrases):
            best_tier = max(best_tier, 2)
        elif any(p in text for p in tier3_phrases):
            best_tier = max(best_tier, 3)

    if best_tier == 2:
        return config.CATALYST_TIER2
    if best_tier == 3:
        return config.CATALYST_TIER3
    return config.CATALYST_NONE


def build_candidate_payload(candidates_with_signals: list[dict]) -> list[dict]:
    """Enrich candidates with news articles (headline + summary) for LLM context."""
    payload = []
    for c in candidates_with_signals:
        ticker = c["ticker"]
        news = c.get("news") or fetcher.get_news(ticker, limit=5)
        recent_news = [
            {
                "headline": n.get("headline", ""),
                "summary":  n.get("summary", ""),
            }
            for n in news[:5]
            if n.get("headline")
        ]
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
            "recent_news": recent_news,
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
