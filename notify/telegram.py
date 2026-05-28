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


def _fallback_message(
    trade_data: list[dict],
    spy_pct: float,
    daily_pnl: float,
    account_equity: float,
    date_str: str,
) -> str:
    """Fallback template se LLM non è disponibile."""
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
        reason = skipped[0].get("reason", "nessun segnale sufficiente") if skipped else "nessun segnale"
        lines += ["Nessun trade oggi.", f"Motivo: {reason}", ""]
    else:
        for i, t in enumerate(executed, 1):
            modalita = EXIT_LABELS.get(t.get("exit_reason", ""), t.get("exit_reason", ""))
            pnl_usd  = t.get("pnl_usd", 0)
            pnl_pct  = t.get("pnl_pct", 0)
            sign     = "+" if pnl_usd >= 0 else ""
            lines += [
                f"Trade {i} — {t['ticker']} long",
                f"  Entrata: ${t['entry_price']:.2f}",
                f"  Uscita:  ${t['exit_price']:.2f} ({modalita})",
                f"  P&L: {sign}{pnl_usd:.2f}$ ({sign}{pnl_pct:.2%})",
                "",
            ]
        if skipped:
            lines += [f"Trade 2 — non eseguito ({skipped[0].get('reason','nessun segnale')})", ""]

    sign_day = "+" if daily_pnl >= 0 else ""
    sign_tot = "+" if total_pnl >= 0 else ""
    lines += [
        f"Giornata:    {sign_day}{daily_pnl:.2f}$",
        f"P&L totale:  {sign_tot}{total_pnl:.2f}$",
        f"Saldo:       ${account_equity:,.2f}",
    ]
    return "\n".join(lines)


def send_eod_recap(
    trade_data: list[dict],
    spy_pct: float,
    daily_pnl: float,
    account_equity: float,
    date_str: str,
    llm_text: str = "",
) -> None:
    text = llm_text if llm_text else _fallback_message(
        trade_data, spy_pct, daily_pnl, account_equity, date_str
    )
    send_message(text)
