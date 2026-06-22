#!/usr/bin/env python3
"""
Generate self-contained dashboard.html from logs/*.json
No external dependencies — pure HTML/CSS/SVG/JS.

Usage:
    git pull && python generate_dashboard.py
    open dashboard.html   (or send to phone)
"""
import json
import glob
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Load logs ─────────────────────────────────────────────────────────────────
log_files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "logs/*.json")))
logs = []
for f in log_files:
    with open(f) as fp:
        logs.append(json.load(fp))

if not logs:
    print("No logs found in logs/. Run 'git pull' first.")
    exit(1)

INITIAL_EQUITY = 100_000  # config.PAPER_INITIAL_EQUITY

# ── Helpers ───────────────────────────────────────────────────────────────────
def stage_count(log, name):
    for s in log.get("pipeline", []):
        if s["stage"] == name:
            return s["count"]
    return 0

EXIT_LABELS = {
    "hard_blocker":   "Hard stop",
    "atr_stop":       "ATR stop",
    "breakeven_stop": "Break-even stop",
    "step_stop":      "Gradino di profitto",
    "vwap_exit":      "VWAP take-profit",
    "eod_close":      "EOD close",
    "manual_close":   "Manual close",
}

# ── Build rows ────────────────────────────────────────────────────────────────
rows = []
for log in logs:
    trades = [t for t in log.get("trades", []) if t.get("exit_price")]
    daily_pnl = round(sum((t.get("pnl_usd") or 0) for t in trades), 2)
    rows.append({
        "date":                 log["date"],
        "spy_pct":              log.get("spy_pct", 0),
        "blocked":              log.get("blocked"),
        "daily_pnl":            daily_pnl,
        "trades":               trades,
        "signals":              log.get("signals", []),
        "l1_rejects":           log.get("l1_rejects", []),
        "premarket_candidates": log.get("premarket_candidates", []),
        "llm_output":           log.get("llm_output", {}),
        "universe_count":       stage_count(log, "universe"),
        "premarket_count":      stage_count(log, "premarket_scan"),
        "l1_count":             stage_count(log, "binary_filters_L1"),
        "l2_count":             stage_count(log, "L2_signals_passed"),
        "llm_input":            log.get("llm_input", []),
    })

# ── Aggregate stats ───────────────────────────────────────────────────────────
all_trades = [t for r in rows for t in r["trades"]]
total_pnl  = round(sum((t.get("pnl_usd") or 0) for t in all_trades), 2)
wins       = [t for t in all_trades if (t.get("pnl_usd") or 0) > 0]
losses     = [t for t in all_trades if (t.get("pnl_usd") or 0) <= 0]
win_rate   = round(len(wins) / len(all_trades) * 100, 1) if all_trades else 0
avg_conf   = round(sum((t.get("confidence") or 0) for t in all_trades) / len(all_trades), 2) if all_trades else 0
avg_win    = round(sum((t.get("pnl_usd") or 0) for t in wins)   / len(wins),   2) if wins   else 0
avg_loss   = round(sum((t.get("pnl_usd") or 0) for t in losses) / len(losses), 2) if losses else 0
trade_days = sum(1 for r in rows if r["trades"])

# Madrid local time — the GitHub Actions runner is UTC, so anchor explicitly
# (ZoneInfo handles CET/CEST DST automatically).
updated = datetime.now(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y %H:%M %Z")

DATA_JS  = json.dumps(rows,  ensure_ascii=False, default=str)
STATS_JS = json.dumps({
    "total_pnl":  total_pnl,
    "win_rate":   win_rate,
    "n_trades":   len(all_trades),
    "n_wins":     len(wins),
    "n_losses":   len(losses),
    "avg_win":    avg_win,
    "avg_loss":   avg_loss,
    "avg_conf":   avg_conf,
    "trade_days":    trade_days,
    "total_days":    len(rows),
    "total_pnl_pct": round(total_pnl / INITIAL_EQUITY * 100, 2),
    "initial_equity": INITIAL_EQUITY,
}, ensure_ascii=False)

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Dashboard</title>
<style>
:root {{
  --bg:      #0f1117;
  --surface: #1a1d27;
  --border:  #2a2d3a;
  --text:    #e2e8f0;
  --muted:   #8892a4;
  --green:   #22c55e;
  --red:     #ef4444;
  --blue:    #3b82f6;
  --yellow:  #f59e0b;
  --r:       8px;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; padding: 20px; }}
h2 {{ font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 14px; }}
.header {{ display: flex; align-items: baseline; gap: 14px; margin-bottom: 24px; flex-wrap: wrap; }}
.header h1 {{ font-size: 18px; font-weight: 700; }}
.updated {{ font-size: 12px; color: var(--muted); }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--r); padding: 18px; margin-bottom: 16px; }}

/* KPI grid */
.kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 16px; }}
.kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--r); padding: 14px 16px; }}
.kpi .lbl {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 5px; }}
.kpi .val {{ font-size: 20px; font-weight: 700; line-height: 1.2; }}
.kpi .sub {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}

/* Charts row */
.charts {{ display: grid; grid-template-columns: 3fr 2fr; gap: 16px; margin-bottom: 16px; }}
@media (max-width: 700px) {{ .charts {{ grid-template-columns: 1fr; }} }}

/* Tables */
.tbl-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; white-space: nowrap; }}
th {{ padding: 8px 10px; font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; border-bottom: 1px solid var(--border); text-align: left; }}
td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); }}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: rgba(255,255,255,.025); }}

/* Scrollable table body — fixed height, sticky header */
.scroll-wrap {{ max-height: 370px; overflow-y: auto; }}
.scroll-wrap::-webkit-scrollbar {{ width: 6px; }}
.scroll-wrap::-webkit-scrollbar-track {{ background: var(--bg); }}
.scroll-wrap::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
.scroll-wrap thead th {{ position: sticky; top: 0; background: var(--surface); z-index: 1; box-shadow: 0 1px 0 var(--border); }}

/* Colors */
.pos {{ color: var(--green); }} .neg {{ color: var(--red); }} .neu {{ color: var(--text); }}
.mut {{ color: var(--muted); }}

/* Badge */
.b {{ display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
.bg  {{ background: rgba(34,197,94,.15);  color: var(--green); }}
.br  {{ background: rgba(239,68,68,.15);  color: var(--red); }}
.bb  {{ background: rgba(59,130,246,.15); color: var(--blue); }}
.by  {{ background: rgba(245,158,11,.15); color: var(--yellow); }}
.bmu {{ background: rgba(136,146,164,.12); color: var(--muted); }}

/* Signal tick */
.tk {{ color: var(--green); font-weight: 700; }}
.cx {{ color: var(--red); font-weight: 700; }}

/* Info icon on column headers — padded for a comfortable tap target on touch devices */
.iico {{ font-size: 12px; color: var(--blue); cursor: pointer; opacity: .85; vertical-align: middle; padding: 3px 5px; margin: -3px -2px; display: inline-block; }}
[data-tip] {{ cursor: pointer; }}

/* Tap/hover tooltip popover (works on iPad — no native title hover needed) */
.tip-pop {{ position: fixed; z-index: 1000; max-width: 270px; background: #0b0d13; border: 1px solid var(--border); color: var(--text); font-size: 12px; line-height: 1.45; padding: 9px 12px; border-radius: 7px; box-shadow: 0 6px 22px rgba(0,0,0,.55); display: none; }}

/* Conf bar */
.cb {{ display: inline-block; height: 5px; background: var(--blue); border-radius: 3px; vertical-align: middle; margin-right: 5px; opacity: .8; }}

/* SVG charts */
svg text {{ font-family: -apple-system, sans-serif; }}

/* Filter bar */
.filter-bar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; padding:12px 16px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r); }}
.filter-label {{ font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.05em; }}
.qbtn {{ background:transparent; border:1px solid var(--border); color:var(--muted); padding:4px 13px; border-radius:20px; font-size:12px; cursor:pointer; transition:all .15s; white-space:nowrap; }}
.qbtn:hover {{ border-color:var(--blue); color:var(--text); }}
.qbtn.active {{ background:var(--blue); border-color:var(--blue); color:#fff; font-weight:600; }}
.filter-sep {{ width:1px; height:20px; background:var(--border); margin:0 4px; }}
.filter-bar label {{ font-size:12px; color:var(--muted); display:flex; align-items:center; gap:6px; }}
.filter-bar input[type=date] {{ background:var(--bg); border:1px solid var(--border); color:var(--text); padding:4px 8px; border-radius:6px; font-size:12px; cursor:pointer; }}
.apply-btn {{ background:var(--blue); border:none; color:#fff; padding:5px 13px; border-radius:6px; font-size:12px; cursor:pointer; font-weight:500; }}
.apply-btn:hover {{ opacity:.85; }}
</style>
</head>
<body>

<div class="header">
  <h1>📈 Trading Dashboard — Gap &amp; Go</h1>
  <span class="updated">Aggiornato: {updated}</span>
</div>

<!-- Filter bar -->
<div class="filter-bar">
  <span class="filter-label">Periodo</span>
  <button class="qbtn active" data-days="0">Tutto</button>
  <button class="qbtn" data-days="7">7 giorni</button>
  <button class="qbtn" data-days="30">30 giorni</button>
  <button class="qbtn" data-days="90">90 giorni</button>
  <div class="filter-sep"></div>
  <label>Dal <input type="date" id="dateFrom"></label>
  <label>Al&nbsp; <input type="date" id="dateTo"></label>
  <button class="apply-btn" id="applyRange">Applica</button>
  <div class="filter-sep"></div>
  <span class="filter-label">Exit</span>
  <button class="qbtn active" data-exit="">Tutti</button>
  <button class="qbtn" data-exit="hard_blocker">Hard stop</button>
  <button class="qbtn" data-exit="atr_stop">ATR stop</button>
  <button class="qbtn" data-exit="breakeven_stop">Break-even</button>
  <button class="qbtn" data-exit="step_stop">Gradino profitto</button>
  <button class="qbtn" data-exit="vwap_exit">VWAP</button>
  <button class="qbtn" data-exit="eod_close">EOD close</button>
</div>

<!-- KPI cards -->
<div class="kpis" id="kpis"></div>

<!-- Charts -->
<div class="charts">
  <div class="card">
    <h2>P&amp;L giornaliero ($)</h2>
    <div id="chartPnl"></div>
  </div>
  <div class="card">
    <h2>Exit reasons</h2>
    <div id="chartExit"></div>
  </div>
</div>

<!-- Pipeline funnel -->
<div class="card">
  <h2>Pipeline funnel — giornaliero</h2>
  <div class="tbl-wrap scroll-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>SPY</th><th>Universe</th>
      <th>Pre-mkt <span data-tip="Ticker con gap ≥ 0.5% in pre-market rispetto alla chiusura precedente" class="iico">ⓘ</span></th>
      <th>L1 ✓ <span data-tip="Superano i filtri binari di qualità: liquidità (ADV), spread, prezzo minimo, asset tradabile su Alpaca" class="iico">ⓘ</span></th>
      <th>L2 ✓ <span data-tip="Superano la soglia di confidence algoritmica (≥ 0.65) basata su segnali tecnici: post-open advance, OR position, gap retention, vol boost, catalyst" class="iico">ⓘ</span></th>
      <th>→ LLM <span data-tip="Candidati inviati al modello LLM per la selezione finale del trade, dopo aver superato tutti i filtri algoritmici" class="iico">ⓘ</span></th>
      <th>Trade</th><th>P&amp;L</th><th>P&amp;L %</th><th>Note</th>
    </tr></thead>
    <tbody id="funnelRows"></tbody>
  </table>
  </div>
</div>

<!-- Trade log -->
<div class="card">
  <h2>Trade log</h2>
  <div class="tbl-wrap scroll-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>Ticker</th><th>Entry</th><th>Exit</th>
      <th>Shares</th><th>P&amp;L $</th><th>P&amp;L %</th>
      <th>Uscita</th><th>Conf.</th><th>Gap %</th>
      <th>Catalyst</th><th>Vol boost</th><th>Short float</th>
    </tr></thead>
    <tbody id="tradeRows"></tbody>
  </table>
  </div>
</div>

<!-- L2 signals -->
<div class="card">
  <h2>Segnali L2 — tutti i candidati</h2>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">
    <span class="filter-label" style="align-self:center">Esito</span>
    <button class="qbtn active" data-esito="">Tutti</button>
    <button class="qbtn" data-esito="TRADED">TRADED</button>
    <button class="qbtn" data-esito="LLM_ALTRO">LLM: altro scelto</button>
    <button class="qbtn" data-esito="LLM_NOENTRY">LLM: no entry</button>
    <button class="qbtn" data-esito="REJECT">REJECT</button>
    <button class="qbtn" data-esito="PASS">PASS</button>
  </div>
  <div class="tbl-wrap scroll-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>Ticker</th><th>Confidence</th>
      <th>Post-open advance <span data-tip="Prezzo alle 9:35 superiore all'apertura delle 9:30 — conferma che il gap tiene nei primi 5 minuti di trading" class="iico">ⓘ</span></th>
      <th>OR position <span data-tip="Posizione nel range 9:30–9:35: 1.0 = massimo del range, 0.0 = minimo. Sopra 0.66 = titolo nel terzo superiore, segnale di forza" class="iico">ⓘ</span></th>
      <th>Gap retention <span data-tip="Frazione del gap pre-market ancora intatta alle 9:35. 1.0 = gap invariato, 0.0 = gap completamente colmato. Sopra 0.70 = gap difeso" class="iico">ⓘ</span></th>
      <th>Vol boost <span data-tip="Volume nei primi 5 minuti (9:30–9:35) rapportato alla media storica della stessa finestra. >3× → +0.10, 2–3× → +0.05. Passa il mouse sulla cella per i volumi grezzi (oggi vs media)" class="iico">ⓘ</span></th><th>Catalyst</th>
      <th data-tip="Percentuale del flottante venduta allo scoperto">Short float</th>
      <th>Squeeze</th><th>Gap %</th><th>Esito</th>
    </tr></thead>
    <tbody id="signalRows"></tbody>
  </table>
  </div>
</div>

<!-- Pre-market -->
<div class="card">
  <h2>Candidati pre-market</h2>
  <div class="tbl-wrap scroll-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>Ticker</th><th>Gap %</th><th>ADV (M)</th>
      <th>Short float</th><th>Dist. 3M high</th><th>→ L2</th>
    </tr></thead>
    <tbody id="pmRows"></tbody>
  </table>
  </div>
</div>

<!-- Pre-open gate exclusions -->
<div class="card">
  <h2>Esclusioni pre-open gate <span data-tip="Candidati scartati alle 9:35 prima del calcolo dei segnali: gap invertito all'apertura, o gap pre-market eroso sotto la soglia di ritenzione. Non raggiungono mai lo scoring L2" class="iico">ⓘ</span></h2>
  <div class="tbl-wrap scroll-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>Ticker</th><th>Motivo</th>
    </tr></thead>
    <tbody id="gateRows"></tbody>
  </table>
  </div>
</div>

<script>
const LOGS  = {DATA_JS};
const STATS = {STATS_JS};
const EXIT_LABELS = {{
  hard_blocker:"Hard stop", atr_stop:"ATR stop", breakeven_stop:"Break-even stop",
  step_stop:"Gradino di profitto",
  vwap_exit:"VWAP take-profit", eod_close:"EOD close", manual_close:"Manual close"
}};

// ── Tap/hover tooltips (iPad-friendly) ──────────────────────────────────────────
// Native title= tooltips never appear on touch devices. We replace them with a
// popover: tap an element with data-tip to pin it (tap again or elsewhere to close);
// on desktop it also follows mouse hover when nothing is pinned.
const tipPop = document.createElement("div");
tipPop.className = "tip-pop";
document.body.appendChild(tipPop);
let tipPinned = false, tipCurrent = null;
function placeTip(el) {{
  const txt = el.getAttribute("data-tip");
  if (!txt) return false;
  tipPop.innerHTML = txt;
  tipPop.style.display = "block";
  const r = el.getBoundingClientRect();
  const pw = tipPop.offsetWidth, ph = tipPop.offsetHeight;
  let left = Math.max(8, Math.min(r.left, window.innerWidth - pw - 8));
  let top = r.bottom + 6;
  if (top + ph > window.innerHeight - 8) top = r.top - ph - 6;  // flip above if no room below
  tipPop.style.left = left + "px";
  tipPop.style.top = Math.max(8, top) + "px";
  tipCurrent = el;
  return true;
}}
function hideTip() {{ tipPop.style.display = "none"; tipCurrent = null; tipPinned = false; }}
document.addEventListener("click", e => {{
  const el = e.target.closest("[data-tip]");
  if (el) {{
    e.preventDefault(); e.stopPropagation();
    if (tipPinned && tipCurrent === el) {{ hideTip(); }}
    else {{ placeTip(el); tipPinned = true; }}
  }} else if (!e.target.closest(".tip-pop")) {{
    hideTip();
  }}
}});
document.addEventListener("mouseover", e => {{
  if (tipPinned) return;
  const el = e.target.closest("[data-tip]");
  if (el && el !== tipCurrent) placeTip(el);
}});
document.addEventListener("mouseout", e => {{
  if (tipPinned) return;
  const el = e.target.closest("[data-tip]");
  if (el && tipCurrent === el) hideTip();
}});
window.addEventListener("scroll", () => {{ if (!tipPinned) hideTip(); }}, true);

// ── Helpers ───────────────────────────────────────────────────────────────────
const fu  = (n,d=2) => n==null?"—":parseFloat(n).toFixed(d);
const fpm = (n,d=2) => n==null?"—":(n>=0?"+":"")+parseFloat(n).toFixed(d);
const tk  = v => v?'<span class="tk">✓</span>':'<span class="cx">✗</span>';
const cls = n => n>0?"pos":n<0?"neg":"neu";
const badge = (t,c) => `<span class="b ${{c}}">${{t}}</span>`;
const confBar = c => {{
  const w=Math.min(Math.round((c||0)/1.53*56),56);
  return `<span class="cb" style="width:${{w}}px"></span>${{fu(c,3)}}`;
}};
const fint = n => n==null?"—":Math.round(n).toLocaleString("en-US");
const MONTHS_EN = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const WDAYS_EN  = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const fmtDate = s => {{ const d=new Date(s+"T12:00:00Z"); return d.getUTCDate()+" "+MONTHS_EN[d.getUTCMonth()]+", "+WDAYS_EN[d.getUTCDay()]; }};
// Money with thousands separator (comma) + 2 decimals: 1,234.56
const money = (n,d=2) => Math.abs(n||0).toLocaleString("en-US",{{minimumFractionDigits:d,maximumFractionDigits:d}});
const volTip = s => {{
  if(s.vol_today==null && s.vol_avg==null) return "Volume storico non disponibile";
  return `Volume 9:30–9:35 oggi: ${{fint(s.vol_today)}} · media storica: ${{fint(s.vol_avg)}}`;
}};

// Pre-open gate exclusion reasons — surfaced separately from L1 binary rejects
const GATE_REASONS = ["gap_reversed_at_open", "pm_gap_eaten_at_open"];
const isGateReject = r => GATE_REASONS.some(g => (r.reason||"").startsWith(g));
function gateLabel(reason) {{
  if(reason==="gap_reversed_at_open") return "Gap invertito all'apertura (open &lt; prev close)";
  const m=(reason||"").match(/^pm_gap_eaten_at_open_(.+)$/);
  if(m) return `Gap pre-market eroso all'apertura (${{m[1]}} rimasto)`;
  return reason||"—";
}}

// ── Stats (recomputed on filtered subset) ─────────────────────────────────────
function computeStats(logs) {{
  const trades = logs.flatMap(r=>r.trades);
  const totalPnl = +trades.reduce((s,t)=>s+(t.pnl_usd||0),0).toFixed(2);
  const wins     = trades.filter(t=>(t.pnl_usd||0)>0);
  const losses   = trades.filter(t=>(t.pnl_usd||0)<=0);
  return {{
    total_pnl:      totalPnl,
    total_pnl_pct:  +(totalPnl/STATS.initial_equity*100).toFixed(2),
    win_rate:       trades.length?+(wins.length/trades.length*100).toFixed(1):0,
    n_trades:       trades.length,
    n_wins:         wins.length,
    n_losses:       losses.length,
    avg_win:        wins.length  ?+(wins.reduce((s,t)=>s+(t.pnl_usd||0),0)/wins.length).toFixed(2):0,
    avg_loss:       losses.length?+(losses.reduce((s,t)=>s+(t.pnl_usd||0),0)/losses.length).toFixed(2):0,
    avg_conf:       trades.length?+(trades.reduce((s,t)=>s+(t.confidence||0),0)/trades.length).toFixed(2):0,
    trade_days:     logs.filter(r=>r.trades.length>0).length,
    total_days:     logs.length,
    initial_equity: STATS.initial_equity,
  }};
}}

// ── KPIs ──────────────────────────────────────────────────────────────────────
function renderKpis(st) {{
  const kpis = [
    {{ l:"P&L totale",        v:(st.total_pnl>=0?"+$":"−$")+money(st.total_pnl),                                   c:cls(st.total_pnl),       s:`${{st.trade_days}} trade days / ${{st.total_days}} giorni` }},
    {{ l:"P&L % portafoglio", v:st.n_trades?(st.total_pnl_pct>=0?"+":"")+st.total_pnl_pct.toFixed(2)+"%":"—",     c:cls(st.total_pnl_pct),   s:`su equity iniziale ${{(st.initial_equity/1000).toFixed(0)}}k` }},
    {{ l:"Win rate",          v:st.n_trades?st.win_rate+"%":"—",                                                    c:"neu",                   s:`${{st.n_wins}}W · ${{st.n_losses}}L · ${{st.n_trades}} trade` }},
    {{ l:"Avg win",           v:st.n_wins  ?"+$"+money(st.avg_win):"—",                                            c:"pos",                   s:"per trade vincente" }},
    {{ l:"Avg loss",          v:st.n_losses?"−$"+money(st.avg_loss):"—",                                           c:st.n_losses?"neg":"mut", s:"per trade perdente" }},
    {{ l:"Avg confidence",    v:st.n_trades?st.avg_conf.toFixed(2):"—",                                            c:"neu",                   s:"soglia min: 0.65" }},
  ];
  document.getElementById("kpis").innerHTML=kpis.map(k=>
    `<div class="kpi"><div class="lbl">${{k.l}}</div><div class="val ${{k.c}}">${{k.v}}</div><div class="sub">${{k.s}}</div></div>`
  ).join("");
}}

// ── P&L bar chart ─────────────────────────────────────────────────────────────
function renderPnlChart(logs) {{
  const el=document.getElementById("chartPnl");
  if(!logs.length){{el.innerHTML='<p style="color:var(--muted);padding:20px 0;font-size:12px">Nessun dato</p>';return;}}
  const W=560,H=160,pad={{t:10,r:10,b:30,l:52}};
  const iW=W-pad.l-pad.r,iH=H-pad.t-pad.b;
  const vals=logs.map(r=>r.daily_pnl), dates=logs.map(r=>r.date.slice(5));
  const maxAbs=Math.max(...vals.map(Math.abs),1);
  const barW=Math.max(6,Math.floor(iW/vals.length*0.6));
  const zero=pad.t+iH/2;
  const yLabels=[-maxAbs,-maxAbs/2,0,maxAbs/2,maxAbs].map(v=>{{
    const y=pad.t+iH/2-(v/maxAbs)*(iH/2);
    const lbl=v===0?"$0":(v>0?"+$":"-$")+money(v,0);
    return `<text x="${{pad.l-6}}" y="${{y+4}}" text-anchor="end" fill="#8892a4" font-size="10">${{lbl}}</text>
            <line x1="${{pad.l}}" y1="${{y}}" x2="${{W-pad.r}}" y2="${{y}}" stroke="#2a2d3a" stroke-width="1"/>`;
  }}).join("");
  const bars=vals.map((v,i)=>{{
    const x=pad.l+(i+0.5)*iW/vals.length-barW/2;
    const bH=Math.abs(v)/maxAbs*(iH/2);
    const y=v>=0?zero-bH:zero;
    return `<rect x="${{x.toFixed(1)}}" y="${{y.toFixed(1)}}" width="${{barW}}" height="${{Math.max(bH,1).toFixed(1)}}" fill="${{v>0?"#22c55e":v<0?"#ef4444":"#8892a4"}}" rx="3"/>`;
  }}).join("");
  const xLabels=dates.map((d,i)=>{{
    const x=pad.l+(i+0.5)*iW/vals.length;
    return `<text x="${{x.toFixed(1)}}" y="${{H-4}}" text-anchor="middle" fill="#8892a4" font-size="10">${{d}}</text>`;
  }}).join("");
  el.innerHTML=`<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;max-width:${{W}}px;display:block">
    ${{yLabels}}
    <line x1="${{pad.l}}" y1="${{zero}}" x2="${{W-pad.r}}" y2="${{zero}}" stroke="#4a4d5a" stroke-width="1"/>
    ${{bars}}${{xLabels}}</svg>`;
}}

// ── Exit donut ────────────────────────────────────────────────────────────────
function renderExitDonut(logs) {{
  const counts={{}}, pnlTotals={{}}, pnlPcts={{}};
  logs.forEach(r=>r.trades.forEach(t=>{{
    const k=EXIT_LABELS[t.exit_reason]||t.exit_reason||"Unknown";
    counts[k]=(counts[k]||0)+1;
    pnlTotals[k]=(pnlTotals[k]||0)+(t.pnl_usd||0);
    if(!pnlPcts[k]) pnlPcts[k]=[];
    pnlPcts[k].push(t.pnl_pct||0);
  }}));
  const keys=Object.keys(counts);
  if(!keys.length){{document.getElementById("chartExit").innerHTML='<p style="color:var(--muted);font-size:12px;padding:20px 0">Nessun trade chiuso</p>';return;}}
  const total=keys.reduce((s,k)=>s+counts[k],0);
  const colors=["#3b82f6","#22c55e","#f59e0b","#ef4444","#8b5cf6","#8892a4"];
  const R=60,cx=80,cy=80; let angle=-Math.PI/2;
  const slices=keys.map((k,i)=>{{
    const frac=counts[k]/total,a1=angle,a2=angle+frac*2*Math.PI; angle=a2;
    const x1=cx+R*Math.cos(a1),y1=cy+R*Math.sin(a1),x2=cx+R*Math.cos(a2),y2=cy+R*Math.sin(a2);
    return `<path d="M${{cx}},${{cy}} L${{x1.toFixed(1)}},${{y1.toFixed(1)}} A${{R}},${{R}} 0 ${{frac>0.5?1:0}},1 ${{x2.toFixed(1)}},${{y2.toFixed(1)}} Z" fill="${{colors[i%colors.length]}}"/>`;
  }}).join("");
  const legend=keys.map((k,i)=>
    `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
      <span style="width:10px;height:10px;border-radius:2px;background:${{colors[i%colors.length]}};flex-shrink:0"></span>
      <span style="font-size:11px">${{k}} (${{counts[k]}})</span></div>`).join("");
  const tableRows=keys.map(k=>{{
    const n=counts[k], tot=+(pnlTotals[k]||0).toFixed(2), avg=+(tot/n).toFixed(2);
    const avgPct=pnlPcts[k].reduce((s,v)=>s+v,0)/n*100;
    const sc=tot>=0?"pos":"neg", sa=avg>=0?"pos":"neg", sp=avgPct>=0?"pos":"neg";
    return `<tr>
      <td style="padding:4px 8px;font-size:11px;white-space:nowrap;border-bottom:1px solid var(--border)">${{k}}</td>
      <td style="padding:4px 8px;text-align:right;border-bottom:1px solid var(--border)" class="${{sc}}">${{tot>=0?"+$":"−$"}}${{money(Math.abs(tot))}}</td>
      <td style="padding:4px 8px;text-align:right;border-bottom:1px solid var(--border)" class="${{sa}}">${{avg>=0?"+$":"−$"}}${{money(Math.abs(avg))}}</td>
      <td style="padding:4px 8px;text-align:right;border-bottom:1px solid var(--border)" class="${{sp}}">${{(avgPct>=0?"+":"")+avgPct.toFixed(1)}}%</td>
      <td style="padding:4px 8px;text-align:right;border-bottom:1px solid var(--border);color:var(--muted)">${{n}}</td>
    </tr>`;
  }}).join("");
  document.getElementById("chartExit").innerHTML=
    `<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
      <div style="flex-shrink:0">
        <svg viewBox="0 0 160 160" style="width:110px;display:block">
          <circle cx="${{cx}}" cy="${{cy}}" r="${{R}}" fill="var(--border)"/>
          ${{slices}}<circle cx="${{cx}}" cy="${{cy}}" r="30" fill="var(--surface)"/>
        </svg>
        <div style="margin-top:8px">${{legend}}</div>
      </div>
      <div style="flex:1;min-width:220px;overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr>
            <th style="font-size:10px;font-weight:600;color:var(--muted);padding:4px 8px;text-align:left;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.04em">Strategia</th>
            <th style="font-size:10px;font-weight:600;color:var(--muted);padding:4px 8px;text-align:right;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.04em">Tot P&amp;L</th>
            <th style="font-size:10px;font-weight:600;color:var(--muted);padding:4px 8px;text-align:right;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.04em">Avg P&amp;L</th>
            <th style="font-size:10px;font-weight:600;color:var(--muted);padding:4px 8px;text-align:right;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.04em">Avg %</th>
            <th style="font-size:10px;font-weight:600;color:var(--muted);padding:4px 8px;text-align:right;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.04em">#</th>
          </tr></thead>
          <tbody>${{tableRows}}</tbody>
        </table>
      </div>
    </div>`;
}}

// ── Trade log ─────────────────────────────────────────────────────────────────
function renderTradeLog(logs) {{
  const rows=[];
  [...logs].reverse().forEach(r=>r.trades.forEach(t=>{{
    const pnl=t.pnl_usd??null,pp=t.pnl_pct??null,sc=cls(pnl);
    const cat=t.catalyst_bonus>=0.30?"T1":t.catalyst_bonus>=0.20?"T2":t.catalyst_bonus>=0.10?"T3":"—";
    rows.push(`<tr>
      <td>${{fmtDate(r.date)}}</td><td><strong>${{t.ticker}}</strong></td>
      <td>${{t.entry_price!=null?"$"+fu(t.entry_price):"—"}}</td>
      <td>${{t.exit_price !=null?"$"+fu(t.exit_price) :"—"}}</td>
      <td>${{t.qty??"—"}}</td>
      <td class="${{sc}}">${{pnl!=null?(pnl>=0?"+$":"−$")+money(pnl):"—"}}</td>
      <td class="${{sc}}">${{pp!=null?(pp>=0?"+":"")+(pp*100).toFixed(2)+"%":"—"}}</td>
      <td>${{badge(EXIT_LABELS[t.exit_reason]||t.exit_reason||"—",sc==="pos"?"bg":sc==="neg"?"br":"bmu")}}</td>
      <td>${{confBar(t.confidence)}}</td>
      <td class="pos">${{t.gap_pct!=null?fpm(t.gap_pct*100,1)+"%":"—"}}</td>
      <td>${{badge(cat,t.catalyst_bonus>=0.20?"bb":t.catalyst_bonus>=0.10?"bb":"bmu")}}</td>
      <td>${{t.vol_boost?"+"+parseFloat(t.vol_boost).toFixed(2):"—"}}</td>
      <td>${{t.short_float!=null?(t.short_float*100).toFixed(1)+"%":"—"}}</td>
    </tr>`);
  }}));
  document.getElementById("tradeRows").innerHTML=rows.length
    ?rows.join("")
    :'<tr><td colspan="13" style="color:var(--muted);text-align:center;padding:24px">Nessun trade ancora</td></tr>';
}}

// ── Funnel ────────────────────────────────────────────────────────────────────
function renderFunnel(logs) {{
  document.getElementById("funnelRows").innerHTML=[...logs].reverse().map(r=>{{
    const sc=cls(r.daily_pnl);
    const note=r.blocked||(r.trades.length?"✓ trade eseguito":"LLM: nessuna entry");
    const totalInvested=r.trades.reduce((s,t)=>s+(t.entry_price||0)*(t.qty||0),0);
    const dailyPct=totalInvested>0?r.daily_pnl/totalInvested*100:null;
    return `<tr>
      <td>${{fmtDate(r.date)}}</td>
      <td class="${{cls(r.spy_pct)}}">${{fpm(r.spy_pct*100,2)}}%</td>
      <td>60</td><td>${{r.premarket_count}}</td><td>${{r.l1_count}}</td>
      <td>${{r.l2_count}}</td><td>${{r.llm_input.length}}</td><td>${{r.trades.length}}</td>
      <td class="${{sc}}">${{r.daily_pnl!==0?(r.daily_pnl>0?"+$":"−$")+money(r.daily_pnl):"—"}}</td>
      <td class="${{sc}}">${{dailyPct!=null?(dailyPct>=0?"+":"")+dailyPct.toFixed(2)+"%":"—"}}</td>
      <td class="mut" style="font-size:12px;white-space:normal;max-width:200px">${{note}}</td>
    </tr>`;
  }}).join("");
}}

// ── L2 signals ────────────────────────────────────────────────────────────────
function getEsitoType(s, tradedTickers, llmTickers, llm_output) {{
  if (!s.passes_threshold) return "REJECT";
  if (tradedTickers.includes(s.ticker)) return "TRADED";
  if (llmTickers.length>0 && !llmTickers.includes(s.ticker)) return "LLM_ALTRO";
  if (llm_output?.no_trade_reason) return "LLM_NOENTRY";
  return "PASS";
}}
function esitoDisplay(type) {{
  switch(type) {{
    case "REJECT":     return badge("REJECT","br");
    case "TRADED":     return badge("TRADED","bb");
    case "LLM_ALTRO":  return badge("LLM: altro scelto","by");
    case "LLM_NOENTRY":return badge("LLM: no entry","bmu");
    default:           return badge("PASS","bg");
  }}
}}
function renderSignals(logs) {{
  const rows=[];
  [...logs].reverse().forEach(r=>{{
    const tradedTickers=r.trades.map(t=>t.ticker);
    const llmTickers=[r.llm_output?.trade_1?.ticker,r.llm_output?.trade_2?.ticker].filter(Boolean);
    r.signals.forEach(s=>{{
      const esitoType=getEsitoType(s,tradedTickers,llmTickers,r.llm_output);
      if (activeSignalEsitoFilter && esitoType!==activeSignalEsitoFilter) return;
      const esito=esitoDisplay(esitoType);
      rows.push(`<tr>
        <td>${{fmtDate(r.date)}}</td><td><strong>${{s.ticker}}</strong></td>
        <td>${{confBar(s.confidence)}}</td>
        <td>${{tk(s.post_open_advance)}}</td>
        <td class="${{(s.or_position||0)>0.66?"pos":"neg"}}">${{fu(s.or_position,2)}}</td>
        <td class="${{(s.gap_retention||0)>0.70?"pos":"neg"}}" data-tip="${{fu(s.gap_retention,2)}}">${{(s.gap_retention??0)<-1?"≤ −1":fu(s.gap_retention,2)}}</td>
        <td data-tip="${{volTip(s)}}">${{s.vol_ratio!=null?parseFloat(s.vol_ratio).toFixed(1)+"×"+(s.vol_boost?" (+"+parseFloat(s.vol_boost).toFixed(2)+")":""):(s.vol_boost?"+"+parseFloat(s.vol_boost).toFixed(2):"—")}}</td>
        <td>${{s.catalyst_bonus?"+"+parseFloat(s.catalyst_bonus).toFixed(2):"—"}}</td>
        <td>${{s.short_float!=null?(s.short_float*100).toFixed(1)+"%":"—"}}</td>
        <td>${{s.short_squeeze_bonus?badge("+"+parseFloat(s.short_squeeze_bonus).toFixed(2),"bb"):"—"}}</td>
        <td class="pos">${{s.gap_pct!=null?fpm(s.gap_pct*100,1)+"%":"—"}}</td>
        <td>${{esito}}</td>
      </tr>`);
    }});
  }});
  document.getElementById("signalRows").innerHTML=rows.length
    ?rows.join("")
    :'<tr><td colspan="12" style="color:var(--muted);text-align:center;padding:24px">Nessun segnale L2</td></tr>';
}}

// ── Pre-market ────────────────────────────────────────────────────────────────
function renderPremarket(logs) {{
  const rows=[];
  [...logs].reverse().forEach(r=>{{
    const l2t=r.signals.map(s=>s.ticker);
    r.premarket_candidates.forEach(c=>{{
      const adv=l2t.includes(c.ticker);
      rows.push(`<tr>
        <td>${{fmtDate(r.date)}}</td><td><strong>${{c.ticker}}</strong></td>
        <td class="pos">+${{fu(c.gap_pct,2)}}%</td>
        <td>${{fu(c.adv_m,1)}}M</td>
        <td>${{c.short_float_pct!=null?c.short_float_pct.toFixed(1)+"%":"—"}}</td>
        <td class="${{(c.dist_from_3m_high_pct||0)>-5?"pos":"neg"}}">${{fpm(c.dist_from_3m_high_pct,1)}}%</td>
        <td>${{badge(adv?"Sì":"No",adv?"bg":"bmu")}}</td>
      </tr>`);
    }});
  }});
  document.getElementById("pmRows").innerHTML=rows.length
    ?rows.join("")
    :'<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:24px">Nessun candidato pre-market</td></tr>';
}}

// ── Pre-open gate exclusions ────────────────────────────────────────────────────
function renderGateExclusions(logs) {{
  const rows=[];
  [...logs].reverse().forEach(r=>{{
    (r.l1_rejects||[]).filter(isGateReject).forEach(rj=>{{
      rows.push(`<tr>
        <td>${{fmtDate(r.date)}}</td><td><strong>${{rj.ticker}}</strong></td>
        <td>${{badge(gateLabel(rj.reason),"by")}}</td>
      </tr>`);
    }});
  }});
  document.getElementById("gateRows").innerHTML=rows.length
    ?rows.join("")
    :'<tr><td colspan="3" style="color:var(--muted);text-align:center;padding:24px">Nessuna esclusione pre-open gate</td></tr>';
}}

// ── Master render ─────────────────────────────────────────────────────────────
let activeLogs = LOGS;
let activeExitFilter = null;
let activeSignalEsitoFilter = null;

function applyExitFilter(logs, exitFilter) {{
  if (!exitFilter) return logs;
  return logs.map(r=>({{...r, trades:r.trades.filter(t=>t.exit_reason===exitFilter)}}));
}}

function renderAll(logs) {{
  activeLogs = logs;
  const ef = applyExitFilter(logs, activeExitFilter);
  renderKpis(computeStats(ef));
  renderPnlChart(ef);
  renderExitDonut(ef);
  renderTradeLog(ef);
  renderFunnel(logs);     // funnel: pipeline counts unaffected by exit filter
  renderSignals(logs);    // signals: uses own esito filter
  renderPremarket(logs);
  renderGateExclusions(logs);
}}

// ── Filter logic ──────────────────────────────────────────────────────────────
function filterLogs(from, to) {{
  return LOGS.filter(r => (!from || r.date >= from) && (!to || r.date <= to));
}}

function applyDateFilter() {{
  const from=document.getElementById("dateFrom").value||null;
  const to  =document.getElementById("dateTo").value  ||null;
  document.querySelectorAll(".qbtn[data-days]").forEach(b=>b.classList.remove("active"));
  renderAll(filterLogs(from, to));
}}

// Date quick buttons
document.querySelectorAll(".qbtn[data-days]").forEach(btn=>{{
  btn.addEventListener("click", ()=>{{
    document.querySelectorAll(".qbtn[data-days]").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    const days=+btn.dataset.days;
    if(days===0){{
      document.getElementById("dateFrom").value="";
      document.getElementById("dateTo").value="";
      renderAll(LOGS);
    }} else {{
      const from=new Date(); from.setDate(from.getDate()-days);
      const fromStr=from.toISOString().slice(0,10);
      document.getElementById("dateFrom").value=fromStr;
      document.getElementById("dateTo").value="";
      renderAll(filterLogs(fromStr,null));
    }}
  }});
}});

// Exit reason filter buttons
document.querySelectorAll(".qbtn[data-exit]").forEach(btn=>{{
  btn.addEventListener("click", ()=>{{
    document.querySelectorAll(".qbtn[data-exit]").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    activeExitFilter = btn.dataset.exit || null;
    renderAll(activeLogs);
  }});
}});

// L2 signal esito filter buttons
document.querySelectorAll(".qbtn[data-esito]").forEach(btn=>{{
  btn.addEventListener("click", ()=>{{
    document.querySelectorAll(".qbtn[data-esito]").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    activeSignalEsitoFilter = btn.dataset.esito || null;
    renderSignals(activeLogs);
  }});
}});

document.getElementById("applyRange").addEventListener("click", applyDateFilter);
["dateFrom","dateTo"].forEach(id=>document.getElementById(id).addEventListener("change", applyDateFilter));

// Initial render
renderAll(LOGS);
</script>
</body>
</html>"""

out = os.path.join(os.path.dirname(__file__), "dashboard.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(HTML)

print(f"✓ dashboard.html generato ({len(logs)} giorni, {len(all_trades)} trade)")
