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

SETUP "RIMBALZO DA SELLOFF" (prev_day_return_pct <= -3%):
Se il titolo nella sessione PRECEDENTE ha chiuso a -3% o peggio, il gap di oggi è con alta
probabilità un rimbalzo tecnico da ipervenduto, NON una continuazione di trend — i segnali
tecnici a 5 minuti (or_position, gap_retention) appaiono identici a un gap genuino ma il setup
è una potenziale bull trap. In questo caso approva il trade SOLO se:
  - catalyst Tier 1 confermato (revenue/earnings beat, M&A, FDA approval), OPPURE
  - catalyst Tier 2 SENZA news negative o contrastanti sullo stesso titolo nelle recent_news
    delle ultime 24h (es. un altro analista che taglia il price target, un revenue miss).
Un price target raise di un singolo analista subito dopo un earnings miss NON è sufficiente.
In dubbio → no_trade. Spiega l'esclusione nel campo no_trade_reason citando prev_day_return_pct.

TASSONOMIA CATALYST (bonus additivo):
- Tier 1 (+0.30): revenue beat, guidance raised/alzata, earnings beat confermato, FDA approval, acquisizione/merger confermati
- Tier 2 (+0.20): EPS beat modesto, analyst upgrade, price target raise, insider buying, Fed speak direzionale, partnership confermata
- Tier 3 (+0.10): rumor non confermati, articoli speculativi, sentiment settoriale generico
- Nessuno (+0.00): nessuna news identificabile — segnale puramente tecnico

FORMULA CONFIDENCE:
confidence = (direction_score/3) + catalyst_bonus + volume_boost + short_squeeze_bonus
- direction_score = somma di [post_open_advance>0, or_position>0.66, gap_retention>0.70]
- catalyst_bonus: Tier1=+0.30, Tier2=+0.20, Tier3=+0.10, Nessuno=+0.00
- volume_boost: vol_ratio>3x → +0.10, vol_ratio 2-3x → +0.05, <2x → +0.00
- short_squeeze_bonus: short_float_pct>15% E (catalyst>0 OPPURE gap_pct>10%) → +0.10, altrimenti +0.00
- 2/3 segnali tecnici (0.667) da soli superano già la soglia 0.65

PRIORITÀ NELLA SCELTA:
- Non ribaltare la classifica di confidence_algo senza una ragione esplicita nelle news
- Se due candidati hanno confidence_algo simile (differenza ≤ 0.10): scegli quello con catalyst_bonus più alto; a parità di catalyst_bonus, scegli quello con vol_boost più alto
- La classifica algoritmica è già corretta — il tuo contributo è leggere le news, non ri-pesare i segnali tecnici

CONTESTO AGGIUNTIVO:
Il campo prev_day_return_pct indica la performance del titolo nella sessione PRECEDENTE.
Valori molto negativi (<= -3%) attivano la regola "RIMBALZO DA SELLOFF" sopra: oggi stai
comprando un rimbalzo dopo un calo, non un trend in continuazione. null = dato non disponibile.

Il campo dist_from_3m_high_pct indica quanto % il titolo è sotto al massimo degli ultimi 3 mesi.
0% = vicino ai massimi (poca resistenza sopra). -20% = 20% sotto i massimi (più resistenza).
Usalo come contesto qualitativo, non come criterio di esclusione.

Il campo post_open_advance_pct indica quanto il titolo è salito (o sceso) tra l'apertura alle 9:30 e le 9:35 (5 minuti di trading reale).
Valori positivi (es. +0.8%) = momentum reale post-gap, il titolo continua a salire dopo l'apertura.
Valori vicini a 0 = consolidamento piatto, stai comprando esattamente dove ha aperto — rischio di essere arrivato tardi.
Valori negativi = il titolo stava già ritracciando al momento dell'entry — segnale di debolezza.

Il campo short_float_pct indica la percentuale del float venduta allo scoperto (fonte: FINRA, aggiornamento bisettimanale).
>15%: squeeze potential elevato — se c'è un catalyst, i venditori allo scoperto sono forzati a coprire amplificando il movimento verso l'alto.
5-15%: moderato, nessun impatto particolare. <5%: normale. null: dato non disponibile.
Alto short float senza catalyst non è un segnale — il catalyst è la scintilla che innesca la copertura.
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
    # so they don't accidentally match a Tier keyword (e.g. "missed earnings beat consensus").
    # Patterns starting with \b use re.search() to enforce word boundaries, preventing
    # "miss" from matching inside "commissioner", "mission", "emissions", "dismissal", etc.
    import re
    negative_blockers = [
        r"\bmiss\b", r"\bmisses\b", r"\bmissed\b",
        "falls short", "fell short",
        "below estimates", "below expectations", "below consensus",
        r"\bdisappoints\b", r"\bdisappointing\b", r"\bdisappointed\b",
        "cuts guidance", "cut guidance", "lowers guidance", "slashes guidance",
        "reduces guidance", "guidance cut", "guidance reduced", "guidance lowered",
        "warns of", "cautious outlook", "weaker than expected",
    ]

    def _is_negative(text: str) -> bool:
        for pat in negative_blockers:
            if pat.startswith("\\b"):
                if re.search(pat, text):
                    return True
            elif pat in text:
                return True
        return False

    # Azioni di analisti che CONTRADDICONO un catalyst rialzista (es. un PT raise da un
    # analista mentre un altro taglia il PT lo stesso giorno — CCL 24/06). Non sono
    # "negative" in senso assoluto (non bloccano l'articolo), ma se coesistono con un
    # catalyst Tier 2 (anch'esso guidato da analisti) il segnale netto è incerto: si
    # declassa a Tier 3 invece di prendere solo il lato positivo.
    conflict_phrases = [
        "lowers price target", "lowered price target", "cuts price target",
        "cut price target", "reduces price target", "price target lowered",
        "price target cut", "lowers pt", "cuts pt",
        "downgrade", "downgraded", "downgrades",
    ]
    conflict_seen = False

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
        "merger talks", "merger discussions", "exploring a merger", "in talks to merge",
        "takeover talks", "buyout talks",
    ]

    best_tier = 0
    best_headline = ""

    for article in news:
        headline = article.get("headline", "")
        text = (headline + " " + article.get("summary", "")).lower()

        # Conflitto rilevato su QUALSIASI articolo (anche negativi/skippati): un'azione
        # ribassista di un analista sullo stesso titolo.
        if any(p in text for p in conflict_phrases):
            conflict_seen = True

        # Skip articles whose primary story is negative
        if _is_negative(text):
            continue

        # Tier 1 — return immediately, can't do better
        matched_t1 = next((p for p in tier1_phrases if p in text), None)
        if matched_t1:
            logger.info(f"Catalyst Tier1 | phrase='{matched_t1}' | headline='{headline}'")
            return config.CATALYST_TIER1

        # Large EPS surprise (% mentioned alongside beat) → also Tier 1
        if ("eps beat" in text or "beat estimates" in text) and any(
            m in text for m in ["10%", "15%", "20%", "25%", "30%", "40%", "50%"]
        ):
            logger.info(f"Catalyst Tier1 (large EPS) | headline='{headline}'")
            return config.CATALYST_TIER1

        matched_t2 = next((p for p in tier2_phrases if p in text), None)
        if matched_t2:
            if best_tier < 2:
                best_headline = headline
            best_tier = max(best_tier, 2)
        else:
            matched_t3 = next((p for p in tier3_phrases if p in text), None)
            if matched_t3:
                if best_tier < 3:
                    best_headline = headline
                best_tier = max(best_tier, 3)

    if best_tier == 2:
        # Catalyst Tier 2 (guidato da analisti) contraddetto da un'azione ribassista di
        # un altro analista → segnale netto incerto, declassa a Tier 3.
        if conflict_seen:
            logger.info(f"Catalyst Tier2→Tier3 (azione analista contrastante) | headline='{best_headline}'")
            return config.CATALYST_TIER3
        logger.info(f"Catalyst Tier2 | headline='{best_headline}'")
        return config.CATALYST_TIER2
    if best_tier == 3:
        logger.info(f"Catalyst Tier3 | headline='{best_headline}'")
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
        dist        = c.get("dist_from_3m_high")
        short_float = c.get("short_float")
        pdr         = c.get("prev_day_return")
        payload.append({
            "ticker":               ticker,
            "gap_pct":              round(c.get("gap_pct", 0), 4),
            "post_open_advance":    c.get("post_open_advance"),
            "or_position":          c.get("or_position"),
            "gap_retention":        c.get("gap_retention"),
            "post_open_advance_pct": c.get("post_open_advance_pct"),
            "vol_boost":            c.get("vol_boost"),
            "confidence_algo":      c.get("confidence"),
            "catalyst_bonus":       c.get("catalyst_bonus"),
            "short_float_pct":      round(short_float * 100, 1) if short_float is not None else None,
            "dist_from_3m_high_pct": round(dist * 100, 1) if dist is not None else None,
            "prev_day_return_pct":  round(pdr * 100, 1) if pdr is not None else None,
            "recent_news":          recent_news,
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
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                logger.error("Could not parse LLM JSON response (regex match also invalid)")
                result = {"trade_1": None, "trade_2": None, "no_trade_reason": "LLM parse error"}
        else:
            logger.error("Could not parse LLM JSON response")
            result = {"trade_1": None, "trade_2": None, "no_trade_reason": "LLM parse error"}

    return result


def generate_eod_recap(
    trade_data: list[dict],
    spy_pct: float,
    account_equity: float,
    daily_pnl: float,
    date_str: str = "",
) -> str:
    """Generate the EOD Telegram message via LLM. Returns empty string if no meaningful data."""
    import json as _json
    from datetime import datetime as _dt

    executed = [t for t in trade_data if t.get("exit_price")]
    if not executed:
        return ""  # let _fallback_message handle no-trade days

    total_pnl = account_equity - config.PAPER_INITIAL_EQUITY

    # Build the Italian date string so the LLM never has to guess it.
    _MONTHS_IT = ["gennaio","febbraio","marzo","aprile","maggio","giugno",
                  "luglio","agosto","settembre","ottobre","novembre","dicembre"]
    _DAYS_IT   = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"]
    try:
        _d = _dt.strptime(date_str, "%Y-%m-%d")
        date_it = f"{_DAYS_IT[_d.weekday()]} {_d.day} {_MONTHS_IT[_d.month - 1]} {_d.year}"
    except Exception:
        date_it = date_str  # fallback: raw string, still better than nothing

    exit_labels = {
        "eod_close":      "chiuso a fine giornata",
        "hard_blocker":   "stop loss",
        "atr_stop":       "stop loss",
        "vwap_exit":      "profit preso",
        "breakeven_stop": "break-even",
        "step_stop":      "profit bloccato (trailing a gradini)",
    }

    exit_human = {
        "hard_blocker":   "Hard stop",
        "atr_stop":       "ATR stop",
        "vwap_exit":      "VWAP take-profit",
        "eod_close":      "End-of-day close",
        "breakeven_stop": "Break-even stop",
        "step_stop":      "Trailing a gradini",
    }

    trade_rows = []
    for t in executed:
        pnl_usd = t.get("pnl_usd") or 0
        pnl_pct = (t.get("pnl_pct") or 0) * 100
        catalyst_bonus = t.get("catalyst_bonus") or 0
        # Classify catalyst tier for the LLM — without any internal field names
        if catalyst_bonus >= 0.30:
            catalyst_label = "tier1"
        elif catalyst_bonus >= 0.20:
            catalyst_label = "tier2"
        elif catalyst_bonus >= 0.10:
            catalyst_label = "tier3 (speculativo)"
        else:
            catalyst_label = "nessuno"

        row = {
            "ticker":        t.get("ticker"),
            "gap_pct":       f"{(t.get('gap_pct') or 0)*100:+.1f}%",
            "segnali_attivi": sum([
                bool(t.get("post_open_advance")),
                (t.get("or_position") or 0) > 0.66,
                (t.get("gap_retention") or 0) > 0.70,
            ]),
            "volumi_forti":  (t.get("vol_boost") or 0) >= 0.10,
            "catalyst":      catalyst_label,
            "entry":         t.get("entry_price"),
            "exit":          t.get("exit_price"),
            "uscita":        exit_human.get(t.get("exit_reason", ""), "chiuso"),
            "pnl":           f"{'+'if pnl_usd>=0 else ''}${pnl_usd:.2f} ({pnl_pct:+.2f}%)",
            "score":         round(t.get("confidence") or 0, 2),
        }
        # Include news headlines only when a catalyst was identified
        if catalyst_bonus > 0:
            news = t.get("news") or []
            row["notizie_principali"] = [n.get("headline", "") for n in news[:2] if n.get("headline")]
        trade_rows.append(row)

    spy_pct_str = f"{spy_pct:+.2%}"
    if spy_pct > 0.005:
        spy_comment = "mercato positivo"
    elif spy_pct < -0.005:
        spy_comment = "mercato in calo"
    else:
        spy_comment = "mercato piatto"

    sign_day = "+" if daily_pnl >= 0 else ""
    sign_tot = "+" if total_pnl >= 0 else ""
    eod = account_equity - daily_pnl
    daily_pct = daily_pnl / eod if eod else 0
    total_pct = total_pnl / config.PAPER_INITIAL_EQUITY if config.PAPER_INITIAL_EQUITY else 0

    prompt = (
        "Scrivi un messaggio Telegram di fine giornata per un bot di trading. Segui queste regole:\n"
        "- Italiano, tono diretto e amichevole\n"
        "- HTML Telegram: solo <b>...</b> per il grassetto, nessun altro tag HTML\n"
        "- VIETATO usare nomi di variabili o campi tecnici\n"
        "- Emoji: usa SOLO il carattere Unicode 📊 nell'intestazione, ZERO altri emoji\n"
        "- VIETATO usare la sintassi :nome_emoji: (es. :bar_chart:, :briefcase:) — solo Unicode diretto\n"
        "- VIETATO usare i caratteri < e > salvo nei tag <b> e </b>\n"
        "- Date in italiano: '4 giugno 2026', non '4/6/2026'\n\n"
        "STRUTTURA ESATTA:\n\n"
        f"<b>📊 {date_it}</b>\n\n"
        f"Mercato: {spy_pct_str} — {spy_comment}\n\n"
        "[Per ogni trade — riga vuota tra blocchi diversi:]\n"
        "<b>Trade [N] — [TICKER] long [Score: [score]]</b>\n"
        "[RIGA CONTESTO — max 10 parole, solo setup e catalyst, NON menzionare come/quando è uscito:\n"
        "  • Se volumi_forti: 'Gap pre-market confermato, volumi forti'\n"
        "  • Se catalyst != 'nessuno': 'Gap pre-market confermato, [notizia in 3-4 parole]'\n"
        "  • Se né volumi forti né catalyst: 'Setup tecnico pulito, prezzo sopra VWAP in apertura'\n"
        "  • Non menzionare mai le news se catalyst == 'nessuno']\n"
        "  Entrata: $[entry]\n"
        "  Uscita:  $[exit] ([uscita])\n"
        "  P&L: [pnl]\n\n"
        f"Giornata: {sign_day}{daily_pnl:.2f}$ ({daily_pct:+.2%})\n"
        f"P&L totale: {sign_tot}{total_pnl:.2f}$ ({total_pct:+.2%})\n"
        f"Saldo: ${account_equity:,.2f}\n\n"
        f"DATI TRADE:\n{_json.dumps(trade_rows, indent=2, default=str)}"
    )
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()
