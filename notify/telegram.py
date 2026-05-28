import logging
import requests

import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return False
    url = TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
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


def format_eod_message(
    trade_data: list[dict],
    spy_pct: float,
    daily_pnl: float,
    account_equity: float,
    date_str: str,
    llm_text: str = "",
) -> str:
    """
    Build EOD recap. Uses LLM text if provided, otherwise falls back to template.
    """
    if llm_text:
        return llm_text

    spy_status = "operativo" if spy_pct > config.SPY_BLOCK_THRESHOLD else f"⛔ bloccato SPY < {config.SPY_BLOCK_THRESHOLD:.1%}"
    lines = [f"📊 Daily recap — {date_str}", f"", f"Mercato: SPY {spy_pct:+.2%} — {spy_status}", ""]

    for i, trade in enumerate(trade_data, 1):
        if trade.get("exit_price"):
            lines += [
                f"Trade {i} — {trade['ticker']} long",
                f"Entrata: ${trade['entry_price']:.2f} @ {trade.get('entry_time','?')} ET",
                f"Uscita:  ${trade['exit_price']:.2f} @ {trade.get('exit_time','?')} ET ({trade.get('exit_reason','')})",
                f"P&L: {trade.get('pnl_usd',0):+.2f}$ ({trade.get('pnl_pct',0):+.2%})",
                f"Perché: {trade.get('reason','')} [conf {trade.get('confidence',0):.2f}]",
                "",
            ]
        else:
            lines += [f"Trade {i} — nessun trade ({trade.get('reason','')})", ""]

    lines += [f"Giornata: {daily_pnl:+.2f}$", f"Conto paper: ${account_equity:,.0f}"]
    return "\n".join(lines)


def send_eod_recap(
    trade_data: list[dict],
    spy_pct: float,
    daily_pnl: float,
    account_equity: float,
    date_str: str,
    llm_text: str = "",
) -> None:
    text = format_eod_message(trade_data, spy_pct, daily_pnl, account_equity, date_str, llm_text)
    send_message(text)
