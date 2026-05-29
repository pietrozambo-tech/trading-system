"""
Daily trading orchestrator — entry point for Railway cron job.

Schedule: 0 13 * * 1-5  (UTC — sempre prima delle 9:25 ET estate/inverno)
Il cron parte presto, wait_until() gestisce il timing esatto via pytz.

Timeline (all times ET):
  09:25  Build pre-market watchlist
  09:40  Apply binary L1 filters + compute L2 signals → LLM decision
  09:42  Place orders
  intra  Monitor every 5 min
  15:45  Force-close all positions
  16:05  Send Telegram EOD recap
"""
import json
import logging
import os
import signal
import time
from datetime import datetime

import pytz

import config
from data import fetcher
from signals import eligibility, triggers
from llm import analyst
from execution import trader
from notify import telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# Graceful shutdown — set to True when Railway sends SIGTERM so the monitoring
# loop exits cleanly, closes positions, and sends the Telegram recap before dying.
_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    logger.warning("Shutdown signal received — closing positions before exit …")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

UNIVERSE = [
    # Tech / Growth
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX",
    "CRM", "ORCL", "ADBE", "INTC", "QCOM", "MU", "AVGO", "TXN", "AMAT",
    # Finance
    "JPM", "BAC", "GS", "MS", "C", "WFC", "BLK", "SCHW",
    # Healthcare
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "BMY",
    # Energy
    "XOM", "CVX", "SLB", "HAL", "OXY",
    # Airlines / Crociere
    "DAL", "AAL", "NCLH", "CCL",
    # Space
    "RKLB", "ASTS", "BKSY", "RDW", "LUNR",
    # Nucleare / Uranio
    "UUUU", "CCJ", "NNE", "SMR",
    # Quantum Computing
    "IONQ", "QBTS", "QUBT", "RGTI",
    # ETF
    "SPY", "QQQ", "IWM",
]


# ---------------------------------------------------------------------------
# Pipeline logger — traccia i ticker ad ogni stadio del filtro
# ---------------------------------------------------------------------------

class PipelineLog:
    """Accumula un log strutturato dell'intera sessione di trading."""

    def __init__(self, date: str):
        self.date = date
        self.stages: list[dict] = []
        self.signals: list[dict] = []
        self.l1_rejects: list[dict] = []
        self.llm_input: list[str] = []
        self.llm_output: dict = {}
        self.trades: list[dict] = []
        self.spy_pct: float = 0.0
        self.blocked: str | None = None

    def log_stage(self, name: str, tickers: list[str], note: str = "") -> None:
        entry = {"stage": name, "count": len(tickers), "tickers": tickers}
        if note:
            entry["note"] = note
        self.stages.append(entry)
        logger.info(f"[PIPELINE] {name}: {len(tickers)} ticker {tickers}")

    def log_signals(self, signals: dict) -> None:
        self.signals.append({
            "ticker":            signals.get("ticker"),
            "confidence":        signals.get("confidence"),
            "passes_threshold":  signals.get("passes_threshold"),
            "above_vwap":        signals.get("above_vwap"),
            "or_position":       signals.get("or_position"),
            "gap_retention":     signals.get("gap_retention"),
            "vol_boost":         signals.get("vol_boost"),
            "catalyst_bonus":    signals.get("catalyst_bonus"),
            "gap_pct":           signals.get("gap_pct"),
        })

    def log_l1_rejects(self, rejects: list[dict]) -> None:
        self.l1_rejects.extend(rejects)

    def log_llm(self, candidates: list[dict], result: dict) -> None:
        self.llm_input  = [c["ticker"] for c in candidates]
        self.llm_output = result

    def log_trade(self, trade: dict) -> None:
        self.trades.append(trade)

    def save(self) -> None:
        os.makedirs("logs", exist_ok=True)
        path = f"logs/{self.date}.json"
        payload = {
            "date":        self.date,
            "spy_pct":     self.spy_pct,
            "blocked":     self.blocked,
            "pipeline":    self.stages,
            "l1_rejects":  self.l1_rejects,
            "signals":     self.signals,
            "llm_input":   self.llm_input,
            "llm_output":  self.llm_output,
            "trades":      self.trades,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        logger.info(f"[PIPELINE] Log salvato in {path}")

    def summary_text(self) -> str:
        lines = [f"📋 Pipeline {self.date}"]
        for s in self.stages:
            lines.append(f"  {s['stage']}: {s['count']} → {s['tickers']}")
        if self.signals:
            lines.append("Segnali L2:")
            for sig in sorted(self.signals, key=lambda x: -(x["confidence"] or 0)):
                lines.append(
                    f"  {sig['ticker']}: conf={sig['confidence']:.2f} "
                    f"vwap={'✓' if sig['above_vwap'] else '✗'} "
                    f"OR={sig['or_position']:.2f} GR={sig['gap_retention']:.2f}"
                )
        if self.blocked:
            lines.append(f"⛔ Bloccato: {self.blocked}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_until(target_time_str: str, session_now: datetime) -> None:
    h, m = map(int, target_time_str.split(":"))
    target = session_now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (target - datetime.now(ET)).total_seconds()
    if delta > 0:
        logger.info(f"Waiting {delta:.0f}s until {target_time_str} ET …")
        time.sleep(delta)


def current_et_str() -> str:
    return datetime.now(ET).strftime("%H:%M")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    now_et    = datetime.now(ET)
    today_str = now_et.strftime("%Y-%m-%d")
    logger.info(f"=== Trading session start: {today_str} ===")

    # Guard 1: if started after the entry window, the session is over — abort.
    # Scheduled Railway runs land at 09:00; anything past 10:00 is a manual/late start.
    cutoff_dt = ET.localize(datetime.combine(now_et.date(), datetime.strptime("10:00", "%H:%M").time()))
    if now_et >= cutoff_dt:
        triggered_at = now_et.strftime("%H:%M")
        logger.warning(
            f"Bot started at {triggered_at} ET — past entry window (cut-off 10:00). "
            "No orders will be placed. Use the scheduled Railway run for live trading."
        )
        telegram.send_late_start_warning(triggered_at, today_str)
        return

    # Guard 2: if positions are already open, skip the pipeline and jump straight
    # to the monitoring loop — handles crash-and-restart without leaving positions unmonitored.
    try:
        existing = fetcher.get_open_positions()
        if len(existing) >= config.MAX_POSITIONS:
            tickers = [p["ticker"] for p in existing]
            logger.warning(f"Already {len(existing)} open position(s) {tickers} — skipping pipeline, resuming monitoring.")
            telegram.send_message(
                f"⚠️ Riavvio rilevato: {len(existing)} posizioni già aperte "
                f"({', '.join(tickers)}). Pipeline saltata, monitoring ripreso."
            )
            # Reconstruct position dicts with fresh stop prices so the monitor loop works correctly
            recovered: list[dict] = []
            for p in existing:
                stops = trader.calc_stop_prices(p["ticker"], p["entry_price"])
                recovered.append({
                    **p,
                    **stops,
                    "direction": "long",
                    "confidence": None,
                    "reason": "recovered after restart",
                    "order_id": None,
                    "exit_price": None,
                    "exit_time": None,
                    "exit_reason": None,
                    "pnl_usd": None,
                    "pnl_pct": None,
                })
            pl        = PipelineLog(today_str)
            daily_pnl = 0.0
            open_positions = recovered
            all_trades     = []
            spy_pct        = fetcher.get_spy_change()
            # Jump directly to monitoring loop — skip pre-market, filters, LLM, orders
            logger.info("Starting monitoring loop (recovered) …")
            while current_et_str() < config.EOD_CLOSE_TIME and not _shutdown:
                if not open_positions:
                    break
                time.sleep(config.MONITORING_INTERVAL)
                open_positions, just_closed, daily_pnl = trader.monitor_positions(open_positions, daily_pnl)
                for pos in just_closed:
                    logger.info(f"Closed: {pos['ticker']} {pos['exit_reason']} P&L=${pos['pnl_usd']:.2f}")
                    pl.log_trade(pos)
            eod_forced = bool(open_positions) and not _shutdown
            if open_positions:
                logger.info("EOD close — forcing all positions (recovered)")
                closed_eod, daily_pnl = trader.close_all_positions_eod(open_positions, daily_pnl)
                for pos in closed_eod:
                    pl.log_trade(pos)
            pl.save()
            if eod_forced:
                wait_until(config.TELEGRAM_NOTIFY_TIME, now_et)
            spy_pct_final = fetcher.get_spy_change()
            _send_eod(pl.trades, daily_pnl, today_str, spy_pct_final, pl)
            return
    except Exception as e:
        logger.warning(f"Could not check open positions at startup: {e}")

    pl           = PipelineLog(today_str)
    daily_pnl    = 0.0
    open_positions: list[dict] = []
    all_trades:    list[dict]  = []

    # ------------------------------------------------------------------
    # 09:25 — Pre-market watchlist
    # ------------------------------------------------------------------
    wait_until(config.WATCHLIST_TIME, now_et)
    pl.log_stage("universe", UNIVERSE)

    watchlist = eligibility.build_premarket_watchlist(UNIVERSE)
    pl.log_stage("premarket_scan", [c["ticker"] for c in watchlist],
                 f"gap>+{config.MIN_PREMARKET_GAP:.1%}")

    # ------------------------------------------------------------------
    # 09:45 — Binary L1 filters + SPY check + L2 signals + LLM
    # ------------------------------------------------------------------
    wait_until(config.ENTRY_TIME, now_et)
    spy_pct  = fetcher.get_spy_change()
    pl.spy_pct = spy_pct

    if eligibility.check_spy_block():
        pl.blocked = f"SPY {spy_pct:+.2%} < {config.SPY_BLOCK_THRESHOLD:.1%}"
        logger.warning(f"SPY block triggered ({spy_pct:+.2%}) — no trades today")
        pl.save()
        _send_eod(all_trades, daily_pnl, today_str, spy_pct, pl)
        return

    candidates, l1_rejects = eligibility.apply_binary_filters(watchlist)
    safe_candidates, earnings_rejects = eligibility.filter_earnings_tonight(candidates)
    candidates = safe_candidates
    pl.log_l1_rejects(l1_rejects + earnings_rejects)
    pl.log_stage("binary_filters_L1", [c["ticker"] for c in candidates])

    if not candidates:
        pl.blocked = "nessun candidato dopo L1"
        pl.save()
        _send_eod(all_trades, daily_pnl, today_str, spy_pct, pl)
        return

    # L2 signals
    candidates_with_signals = []
    for c in candidates:
        ticker        = c["ticker"]
        news          = fetcher.get_news(ticker, limit=5)
        catalyst_mult = analyst.classify_catalyst_from_news(news)
        signals       = triggers.compute_signals(ticker, c["prev_close"], catalyst_mult)
        if signals:
            pl.log_signals({**signals, "gap_pct": c.get("gap_pct")})
            if signals.get("passes_threshold"):
                candidates_with_signals.append({**c, **signals})

    pl.log_stage("L2_signals_passed", [c["ticker"] for c in candidates_with_signals],
                 f"confidence>={config.CONFIDENCE_THRESHOLD}")

    if not candidates_with_signals:
        pl.blocked = "nessun candidato sopra soglia confidence"
        pl.save()
        _send_eod(all_trades, daily_pnl, today_str, spy_pct, pl)
        return

    # LLM
    llm_result = analyst.analyze_candidates(candidates_with_signals, spy_pct, today_str)
    pl.log_llm(candidates_with_signals, llm_result)
    logger.info(f"LLM decision: {llm_result}")

    # ------------------------------------------------------------------
    # 09:47 — Place orders (skip if already past EOD cut-off)
    # ------------------------------------------------------------------
    wait_until(config.ORDER_TIME, now_et)

    eod_dt = ET.localize(datetime.combine(datetime.now(ET).date(),
                          datetime.strptime(config.EOD_CLOSE_TIME, "%H:%M").time()))
    if datetime.now(ET) >= eod_dt:
        logger.warning("Skipping order placement — already past EOD close time")
        pl.blocked = "ordini saltati: orario di entrata superato"
        pl.save()
        _send_eod(all_trades, daily_pnl, today_str, spy_pct, pl)
        return

    for key in ("trade_1", "trade_2"):
        decision = llm_result.get(key)
        if not decision:
            if llm_result.get("no_trade_reason"):
                all_trades.append({"reason": llm_result["no_trade_reason"]})
            continue
        if len(open_positions) >= config.MAX_POSITIONS:
            break
        position = trader.open_position(decision["ticker"], decision)
        if position:
            open_positions.append(position)
            all_trades.append(position)
            pl.log_trade(position)

    # ------------------------------------------------------------------
    # Intraday monitoring loop
    # ------------------------------------------------------------------
    logger.info("Starting monitoring loop …")
    while current_et_str() < config.EOD_CLOSE_TIME and not _shutdown:
        if not open_positions:
            break
        if trader.daily_loss_limit_reached(daily_pnl):
            logger.warning(f"Daily loss limit reached (${daily_pnl:.2f}) — closing all")
            break
        time.sleep(config.MONITORING_INTERVAL)
        open_positions, just_closed, daily_pnl = trader.monitor_positions(open_positions, daily_pnl)
        for pos in just_closed:
            logger.info(f"Closed: {pos['ticker']} {pos['exit_reason']} P&L=${pos['pnl_usd']:.2f}")
            pl.log_trade(pos)
    if _shutdown:
        logger.warning("Shutdown flag set — forcing EOD close immediately")

    # ------------------------------------------------------------------
    # 15:45 — EOD hard close
    # ------------------------------------------------------------------
    # Track whether we hit the hard EOD close (vs all positions closing naturally
    # earlier in the day). Determines whether Telegram waits until 16:05.
    eod_forced = bool(open_positions) and not _shutdown
    if open_positions:
        logger.info("EOD close — forcing all positions")
        closed_eod, daily_pnl = trader.close_all_positions_eod(open_positions, daily_pnl)
        for pos in closed_eod:
            pl.log_trade(pos)
        open_positions = []

    logger.info(f"Day P&L: ${daily_pnl:.2f}")
    pl.save()

    # ------------------------------------------------------------------
    # Telegram EOD recap
    # If positions closed naturally before EOD (or on shutdown) send now.
    # If we had to force-close at 15:45, wait until 16:05 for prices to settle.
    # ------------------------------------------------------------------
    if eod_forced:
        wait_until(config.TELEGRAM_NOTIFY_TIME, now_et)
    spy_pct_final = fetcher.get_spy_change()
    _send_eod(all_trades, daily_pnl, today_str, spy_pct_final, pl)


def _send_eod(
    all_trades: list[dict],
    daily_pnl:  float,
    today_str:  str,
    spy_pct:    float = 0.0,
    pl:         "PipelineLog | None" = None,
) -> None:
    try:
        account = fetcher.get_account()
        equity  = account["equity"]
    except Exception:
        equity = 0.0

    try:
        llm_text = analyst.generate_eod_recap(all_trades, spy_pct, equity, daily_pnl)
    except Exception as e:
        logger.warning(f"LLM EOD recap failed: {e}")
        llm_text = ""

    def _stage_count(name):
        if not pl:
            return None
        for s in pl.stages:
            if s["stage"] == name:
                return s["count"]
        return None

    pipeline_summary = {
        "blocked":        pl.blocked if pl else None,
        "premarket_count": _stage_count("premarket_scan"),
        "l1_count":        _stage_count("binary_filters_L1"),
        "l2_count":        _stage_count("L2_signals_passed"),
    }

    telegram.send_eod_recap(
        trade_data=all_trades,
        spy_pct=spy_pct,
        daily_pnl=daily_pnl,
        account_equity=equity,
        date_str=today_str,
        llm_text=llm_text,
        pipeline_summary=pipeline_summary,
    )


if __name__ == "__main__":
    run()
