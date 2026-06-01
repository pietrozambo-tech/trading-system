import html
import logging
import requests
from datetime import datetime

import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

EXIT_LABELS = {
    "eod_close":    "Fine giornata",
    "hard_blocker": "Stop loss",
    "dollar_stop":  "Stop loss",
    "atr_stop":     "Stop loss (ATR)",
    "vwap_exit":    "Profit taker",
}

DAYS_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return False
    url = TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id":    config.TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram message sent")
        return True
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False


def _no_trade_reason(pipeline: dict) -> str:
    """One-liner explaining why no trade was placed today."""
    blocked  = pipeline.get("blocked") or ""
    pre      = pipeline.get("premarket_count")
    l1       = pipeline.get("l1_count")
    l2       = pipeline.get("l2_count")

    if "SPY" in blocked:
        return "Mercato bloccato — SPY troppo negativo. Riproviamo domani."
    if pre == 0 or pre is None:
        return "Nessun titolo con gap ≥0.5% stamattina. Giornata piatta, capita."
    if l1 == 0:
        return f"{pre} titoli in pre-market, nessuno ha passato i filtri qualità (prezzo, liquidità, spread). Riproviamo domani."
    if l2 == 0:
        return f"{l1} titoli ai filtri, nessuno con segnali tecnici sufficienti. Meglio aspettare un setup pulito."
    return "Candidati trovati ma LLM non era convinto. Nessuna entry forzata."


def _fallback_message(
    trade_data: list[dict],
    spy_pct: float,
    daily_pnl: float,
    account_equity: float,
    date_str: str,
    pipeline_summary: dict | None = None,
) -> str:
    total_pnl = account_equity - config.PAPER_INITIAL_EQUITY
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        header = f"{DAYS_IT[d.weekday()]} {d.day}/{d.month}/{d.year}"
    except Exception:
        header = date_str

    spy_line = f"SPY {spy_pct:+.2%}"
    if spy_pct > 0.01:
        spy_line += " — giornata positiva"
    elif spy_pct < -0.01:
        spy_line += " — giornata negativa"
    else:
        spy_line += " — mercato piatto"

    lines = [f"📊 {header}", "", f"Mercato: {spy_line}", ""]

    executed = [t for t in trade_data if t.get("exit_price")]
    skipped  = [t for t in trade_data if not t.get("exit_price")]

    if not executed:
        reason = _no_trade_reason(pipeline_summary or {})
        lines += [f"Nessun trade. {reason}", ""]
    else:
        for i, t in enumerate(executed, 1):
            modalita   = EXIT_LABELS.get(t.get("exit_reason", ""), t.get("exit_reason", ""))
            pnl_usd    = t.get("pnl_usd", 0)
            pnl_pct    = t.get("pnl_pct", 0)
            sign       = "+" if pnl_usd >= 0 else ""
            confidence = t.get("confidence")
            score_str  = f" [Score: {confidence:.2f}]" if confidence is not None else ""
            trade_reason = t.get("reason") or ""

            lines.append(f"<b>Trade {i} — {t['ticker']} long{score_str}</b>")
            if trade_reason and trade_reason != "recovered after restart":
                lines.append(html.escape(trade_reason))
            lines += [
                f"  Entrata: ${t['entry_price']:.2f}",
                f"  Uscita:  ${t['exit_price']:.2f} ({modalita})",
                f"  P&L: {sign}{pnl_usd:.2f}$ ({sign}{pnl_pct:.2%})",
                "",
            ]
        if skipped and len(executed) == 1:
            lines += ["Trade 2 — nessun secondo segnale valido.", ""]

    sign_day = "+" if daily_pnl >= 0 else ""
    sign_tot = "+" if total_pnl >= 0 else ""
    lines += [
        f"Giornata:    {sign_day}{daily_pnl:.2f}$",
        f"P&L totale:  {sign_tot}{total_pnl:.2f}$",
        f"Saldo:       ${account_equity:,.2f}",
    ]
    return "\n".join(lines)


def send_shutdown_result(closed: list[dict], failed_tickers: list[str]) -> None:
    """Single message after SIGTERM close attempt — outcome per position."""
    lines = ["⚠️ Errore di sistema — chiusura forzata:"]
    for pos in closed:
        price = pos.get("exit_price")
        price_str = f" @ ${price:.2f}" if price else ""
        lines.append(f"✅ {pos['ticker']} chiusa{price_str}")
    for ticker in failed_tickers:
        lines.append(f"❌ {ticker} — chiusura fallita. Intervieni manualmente su Alpaca.")
    send_message("\n".join(lines))


def send_late_start_warning(triggered_at: str, date_str: str) -> None:
    """Notify when the bot is triggered manually after the entry window."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        header = f"{DAYS_IT[d.weekday()]} {d.day}/{d.month}/{d.year}"
    except Exception:
        header = date_str
    text = (
        f"⚠️ {header}\n\n"
        f"Algoritmo avviato manualmente alle {triggered_at} ET — "
        f"post apertura mercato. Nessun ordine inserito."
    )
    send_message(text)


def send_eod_recap(
    trade_data: list[dict],
    spy_pct: float,
    daily_pnl: float,
    account_equity: float,
    date_str: str,
    llm_text: str = "",
    pipeline_summary: dict | None = None,
) -> None:
    text = llm_text if llm_text else _fallback_message(
        trade_data, spy_pct, daily_pnl, account_equity, date_str, pipeline_summary
    )
    send_message(text)
