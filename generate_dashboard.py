#!/usr/bin/env python3
"""
Generate self-contained dashboard.html from logs/*.json

Usage:
    git pull && python generate_dashboard.py
    open dashboard.html
"""
import json
import glob
import os
from datetime import datetime

# ── Load logs ─────────────────────────────────────────────────────────────────
log_files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "logs/*.json")))
logs = []
for f in log_files:
    with open(f) as fp:
        logs.append(json.load(fp))

if not logs:
    print("No logs found in logs/. Run 'git pull' first.")
    exit(1)

# ── Helpers ───────────────────────────────────────────────────────────────────
def stage_count(log, name):
    for s in log.get("pipeline", []):
        if s["stage"] == name:
            return s["count"]
    return 0

EXIT_LABELS = {
    "hard_blocker": "Hard stop",
    "atr_stop":     "ATR stop",
    "vwap_exit":    "VWAP take-profit",
    "eod_close":    "EOD close",
    "manual_close": "Manual close",
}

# ── Build rows ────────────────────────────────────────────────────────────────
rows = []
for log in logs:
    trades = [t for t in log.get("trades", []) if t.get("exit_price")]
    daily_pnl = sum((t.get("pnl_usd") or 0) for t in trades)
    rows.append({
        "date":                 log["date"],
        "spy_pct":              log.get("spy_pct", 0),
        "blocked":              log.get("blocked"),
        "daily_pnl":            round(daily_pnl, 2),
        "trades":               trades,
        "signals":              log.get("signals", []),
        "premarket_candidates": log.get("premarket_candidates", []),
        "llm_output":           log.get("llm_output", {}),
        "universe_count":       stage_count(log, "universe"),
        "premarket_count":      stage_count(log, "premarket_scan"),
        "l1_count":             stage_count(log, "binary_filters_L1"),
        "l2_count":             stage_count(log, "L2_signals_passed"),
        "llm_input":            log.get("llm_input", []),
    })

# ── Aggregate stats ───────────────────────────────────────────────────────────
all_trades  = [t for r in rows for t in r["trades"]]
total_pnl   = round(sum((t.get("pnl_usd") or 0) for t in all_trades), 2)
wins        = [t for t in all_trades if (t.get("pnl_usd") or 0) > 0]
losses      = [t for t in all_trades if (t.get("pnl_usd") or 0) <= 0]
win_rate    = round(len(wins) / len(all_trades) * 100, 1) if all_trades else 0
avg_conf    = round(sum((t.get("confidence") or 0) for t in all_trades) / len(all_trades), 2) if all_trades else 0
avg_win     = round(sum((t.get("pnl_usd") or 0) for t in wins)   / len(wins),   2) if wins   else 0
avg_loss    = round(sum((t.get("pnl_usd") or 0) for t in losses) / len(losses), 2) if losses else 0
trade_days  = sum(1 for r in rows if r["trades"])
cumulative  = []
running     = 0
for r in rows:
    running += r["daily_pnl"]
    cumulative.append(round(running, 2))

stats = {
    "total_pnl":  total_pnl,
    "win_rate":   win_rate,
    "n_trades":   len(all_trades),
    "n_wins":     len(wins),
    "n_losses":   len(losses),
    "avg_win":    avg_win,
    "avg_loss":   avg_loss,
    "avg_conf":   avg_conf,
    "trade_days": trade_days,
    "total_days": len(rows),
    "cumulative": cumulative,
}

updated = datetime.now().strftime("%d/%m/%Y %H:%M")

# ── HTML ──────────────────────────────────────────────────────────────────────
DATA_JS  = json.dumps(rows,  ensure_ascii=False, default=str)
STATS_JS = json.dumps(stats, ensure_ascii=False)

HTML = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Dashboard — Gap &amp; Go</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:       #0f1117;
    --surface:  #1a1d27;
    --border:   #2a2d3a;
    --text:     #e2e8f0;
    --muted:    #8892a4;
    --green:    #22c55e;
    --red:      #ef4444;
    --blue:     #3b82f6;
    --yellow:   #f59e0b;
    --radius:   8px;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; padding: 24px; }}
  h2 {{ font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; }}
  .header {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 28px; }}
  .header h1 {{ font-size: 20px; font-weight: 700; }}
  .header .updated {{ font-size: 12px; color: var(--muted); }}
  .section {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; margin-bottom: 20px; }}

  /* KPI cards */
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; }}
  .kpi .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }}
  .kpi .value {{ font-size: 22px; font-weight: 700; }}
  .kpi .sub {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}
  .pos {{ color: var(--green); }}
  .neg {{ color: var(--red); }}
  .neu {{ color: var(--text); }}

  /* Charts row */
  .charts-row {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 20px; }}
  @media (max-width: 800px) {{ .charts-row {{ grid-template-columns: 1fr; }} }}
  .chart-wrap {{ position: relative; height: 220px; }}

  /* Tables */
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 10px; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(255,255,255,.03); }}
  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .badge-green {{ background: rgba(34,197,94,.15); color: var(--green); }}
  .badge-red   {{ background: rgba(239,68,68,.15);  color: var(--red);   }}
  .badge-gray  {{ background: rgba(136,146,164,.15); color: var(--muted); }}
  .badge-blue  {{ background: rgba(59,130,246,.15); color: var(--blue); }}
  .tick  {{ color: var(--green); font-weight: 700; }}
  .cross {{ color: var(--red);   font-weight: 700; }}
  .conf-bar {{ display: inline-block; height: 6px; background: var(--blue); border-radius: 3px; vertical-align: middle; margin-right: 6px; }}
</style>
</head>
<body>

<div class="header">
  <h1>📈 Trading Dashboard — Gap &amp; Go</h1>
  <span class="updated">Aggiornato: {updated}</span>
</div>

<!-- KPIs -->
<div class="kpis" id="kpis"></div>

<!-- Charts -->
<div class="charts-row">
  <div class="section">
    <h2>P&amp;L giornaliero</h2>
    <div class="chart-wrap"><canvas id="chartPnl"></canvas></div>
  </div>
  <div class="section">
    <h2>Exit reasons</h2>
    <div class="chart-wrap"><canvas id="chartExit"></canvas></div>
  </div>
</div>

<!-- Trade log -->
<div class="section">
  <h2>Trade log</h2>
  <table id="tableTrades">
    <thead>
      <tr>
        <th>Data</th><th>Ticker</th><th>Entry</th><th>Exit</th>
        <th>Shares</th><th>P&amp;L $</th><th>P&amp;L %</th>
        <th>Uscita</th><th>Conf</th><th>Gap %</th><th>Catalyst</th><th>Vol</th><th>Short float</th>
      </tr>
    </thead>
    <tbody id="tradeRows"></tbody>
  </table>
</div>

<!-- Pipeline funnel -->
<div class="section">
  <h2>Pipeline funnel — giornaliero</h2>
  <table id="tableFunnel">
    <thead>
      <tr>
        <th>Data</th><th>SPY</th><th>Universe</th><th>Pre-market</th>
        <th>L1 pass</th><th>L2 pass</th><th>→ LLM</th><th>Trade</th><th>P&amp;L</th><th>Note</th>
      </tr>
    </thead>
    <tbody id="funnelRows"></tbody>
  </table>
</div>

<!-- L2 signals -->
<div class="section">
  <h2>Segnali L2 — tutti i candidati</h2>
  <table id="tableSignals">
    <thead>
      <tr>
        <th>Data</th><th>Ticker</th><th>Confidence</th><th>Adv</th>
        <th>OR pos</th><th>Gap ret</th><th>Vol boost</th><th>Catalyst</th>
        <th>Short float</th><th>Squeeze</th><th>Gap %</th><th>Esito</th>
      </tr>
    </thead>
    <tbody id="signalRows"></tbody>
  </table>
</div>

<!-- Pre-market candidates -->
<div class="section">
  <h2>Candidati pre-market — tutti i giorni</h2>
  <table id="tablePm">
    <thead>
      <tr>
        <th>Data</th><th>Ticker</th><th>Gap %</th><th>ADV (M)</th>
        <th>Short float</th><th>Dist. 3M high</th><th>Avanzato a L2</th>
      </tr>
    </thead>
    <tbody id="pmRows"></tbody>
  </table>
</div>

<script>
const LOGS  = {DATA_JS};
const STATS = {STATS_JS};

const EXIT_LABELS = {{
  hard_blocker: "Hard stop",
  atr_stop:     "ATR stop",
  vwap_exit:    "VWAP take-profit",
  eod_close:    "EOD close",
  manual_close: "Manual close",
}};

// ── Helpers ──────────────────────────────────────────────────────────────────
const fmt  = (n, d=2) => n == null ? "—" : (n >= 0 ? "+" : "") + n.toFixed(d);
const fmtU = (n, d=2) => n == null ? "—" : n.toFixed(d);
const pct  = n => n == null ? "—" : (n*100).toFixed(1) + "%";
const tick = v => v ? '<span class="tick">✓</span>' : '<span class="cross">✗</span>';
const col  = n => n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "neu";

function badge(text, cls) {{
  return `<span class="badge badge-${{cls}}">${{text}}</span>`;
}}

// ── KPIs ─────────────────────────────────────────────────────────────────────
const kpiDefs = [
  {{ label: "P&L totale",   value: (STATS.total_pnl >= 0 ? "+" : "") + "$" + STATS.total_pnl.toFixed(2), cls: col(STATS.total_pnl), sub: `${{STATS.trade_days}} giorni con trade / ${{STATS.total_days}} totali` }},
  {{ label: "Win rate",      value: STATS.win_rate + "%",  cls: "neu", sub: `${{STATS.n_wins}}W / ${{STATS.n_losses}}L (${{STATS.n_trades}} trade)` }},
  {{ label: "Avg win",       value: STATS.n_wins   ? "+$" + STATS.avg_win.toFixed(2)  : "—", cls: "pos", sub: "per trade vincente" }},
  {{ label: "Avg loss",      value: STATS.n_losses ? "$"  + STATS.avg_loss.toFixed(2) : "—", cls: STATS.n_losses ? "neg" : "muted", sub: "per trade perdente" }},
  {{ label: "Avg confidence",value: STATS.avg_conf.toFixed(2), cls: "neu", sub: "soglia: 0.65" }},
];

document.getElementById("kpis").innerHTML = kpiDefs.map(k =>
  `<div class="kpi">
    <div class="label">${{k.label}}</div>
    <div class="value ${{k.cls}}">${{k.value}}</div>
    <div class="sub">${{k.sub}}</div>
  </div>`
).join("");

// ── P&L bar chart ────────────────────────────────────────────────────────────
new Chart(document.getElementById("chartPnl"), {{
  type: "bar",
  data: {{
    labels: LOGS.map(r => r.date.slice(5)),
    datasets: [{{
      label: "P&L ($)",
      data: LOGS.map(r => r.daily_pnl),
      backgroundColor: LOGS.map(r => r.daily_pnl > 0 ? "rgba(34,197,94,.7)" : r.daily_pnl < 0 ? "rgba(239,68,68,.7)" : "rgba(136,146,164,.4)"),
      borderRadius: 4,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: "#8892a4" }}, grid: {{ color: "#2a2d3a" }} }},
      y: {{ ticks: {{ color: "#8892a4", callback: v => "$"+v }}, grid: {{ color: "#2a2d3a" }} }}
    }}
  }}
}});

// ── Exit reasons donut ────────────────────────────────────────────────────────
const exitCounts = {{}};
LOGS.forEach(r => r.trades.forEach(t => {{
  const k = EXIT_LABELS[t.exit_reason] || t.exit_reason || "Unknown";
  exitCounts[k] = (exitCounts[k] || 0) + 1;
}}));
const exitKeys = Object.keys(exitCounts);
new Chart(document.getElementById("chartExit"), {{
  type: "doughnut",
  data: {{
    labels: exitKeys.length ? exitKeys : ["Nessun trade"],
    datasets: [{{
      data: exitKeys.length ? exitKeys.map(k => exitCounts[k]) : [1],
      backgroundColor: ["#3b82f6","#22c55e","#f59e0b","#ef4444","#8b5cf6","#8892a4"],
      borderWidth: 0,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: "right", labels: {{ color: "#e2e8f0", font: {{ size: 12 }} }} }}
    }}
  }}
}});

// ── Trade log table ───────────────────────────────────────────────────────────
const tradeRows = [];
LOGS.forEach(r => {{
  r.trades.forEach(t => {{
    const pnl   = t.pnl_usd != null ? t.pnl_usd : null;
    const pnlPct = t.pnl_pct != null ? t.pnl_pct * 100 : null;
    const sign  = pnl != null ? (pnl >= 0 ? "pos" : "neg") : "neu";
    const cat   = t.catalyst_bonus >= 0.30 ? "T1" : t.catalyst_bonus >= 0.20 ? "T2" : t.catalyst_bonus >= 0.10 ? "T3" : "—";
    const catCls = t.catalyst_bonus >= 0.20 ? "blue" : t.catalyst_bonus >= 0.10 ? "gray" : "gray";
    tradeRows.push(`<tr>
      <td>${{r.date.slice(5)}}</td>
      <td><strong>${{t.ticker}}</strong></td>
      <td>${{t.entry_price != null ? "$"+fmtU(t.entry_price) : "—"}}</td>
      <td>${{t.exit_price  != null ? "$"+fmtU(t.exit_price)  : "—"}}</td>
      <td>${{t.qty ?? "—"}}</td>
      <td class="${{sign}}">${{pnl != null ? (pnl>=0?"+":"") + "$" + Math.abs(pnl).toFixed(2) : "—"}}</td>
      <td class="${{sign}}">${{pnlPct != null ? (pnlPct>=0?"+":"") + pnlPct.toFixed(2)+"%" : "—"}}</td>
      <td>${{badge(EXIT_LABELS[t.exit_reason] || t.exit_reason || "—", sign === "pos" ? "green" : sign === "neg" ? "red" : "gray")}}</td>
      <td>
        <span class="conf-bar" style="width:${{Math.min((t.confidence||0)/1.53*60,60)}}px"></span>
        ${{fmtU(t.confidence)}}
      </td>
      <td>${{t.gap_pct != null ? fmt(t.gap_pct*100,1)+"%" : "—"}}</td>
      <td>${{badge(cat, catCls)}}</td>
      <td>${{t.vol_boost ? "+"+t.vol_boost.toFixed(2) : "—"}}</td>
      <td>${{t.short_float != null ? (t.short_float*100).toFixed(1)+"%" : "—"}}</td>
    </tr>`);
  }});
}});
document.getElementById("tradeRows").innerHTML = tradeRows.length
  ? tradeRows.join("")
  : '<tr><td colspan="13" style="color:var(--muted);text-align:center;padding:20px">Nessun trade ancora</td></tr>';

// ── Funnel table ──────────────────────────────────────────────────────────────
document.getElementById("funnelRows").innerHTML = LOGS.map(r => {{
  const pnl   = r.daily_pnl;
  const sign  = pnl > 0 ? "pos" : pnl < 0 ? "neg" : "neu";
  const note  = r.blocked || (r.trades.length ? "✓ trade" : "LLM: no entry");
  return `<tr>
    <td>${{r.date.slice(5)}}</td>
    <td class="${{r.spy_pct > 0 ? "pos" : r.spy_pct < 0 ? "neg" : "neu"}}">${{fmt(r.spy_pct*100,2)}}%</td>
    <td>60</td>
    <td>${{r.premarket_count}}</td>
    <td>${{r.l1_count}}</td>
    <td>${{r.l2_count}}</td>
    <td>${{r.llm_input.length}}</td>
    <td>${{r.trades.length}}</td>
    <td class="${{sign}}">${{pnl !== 0 ? (pnl>0?"+":"")+"$"+Math.abs(pnl).toFixed(2) : "—"}}</td>
    <td style="color:var(--muted);font-size:12px">${{note}}</td>
  </tr>`;
}}).join("");

// ── L2 signals table ──────────────────────────────────────────────────────────
const signalRows = [];
LOGS.forEach(r => {{
  r.signals.forEach(s => {{
    const pass   = s.passes_threshold;
    const traded = r.trades.some(t => t.ticker === s.ticker);
    signalRows.push(`<tr>
      <td>${{r.date.slice(5)}}</td>
      <td><strong>${{s.ticker}}</strong></td>
      <td>
        <span class="conf-bar" style="width:${{Math.min((s.confidence||0)/1.53*60,60)}}px"></span>
        ${{fmtU(s.confidence,3)}}
      </td>
      <td>${{tick(s.post_open_advance)}}</td>
      <td class="${{(s.or_position||0) > 0.66 ? "pos" : "neg"}}">${{fmtU(s.or_position,2)}}</td>
      <td class="${{(s.gap_retention||0) > 0.70 ? "pos" : "neg"}}">${{fmtU(s.gap_retention,2)}}</td>
      <td>${{s.vol_boost ? "+"+s.vol_boost.toFixed(2) : "—"}}</td>
      <td>${{s.catalyst_bonus ? "+"+s.catalyst_bonus.toFixed(2) : "—"}}</td>
      <td>${{s.short_float != null ? (s.short_float*100).toFixed(1)+"%" : "—"}}</td>
      <td>${{s.short_squeeze_bonus ? badge("+"+s.short_squeeze_bonus.toFixed(2), "blue") : "—"}}</td>
      <td>${{s.gap_pct != null ? fmt(s.gap_pct*100,1)+"%" : "—"}}</td>
      <td>
        ${{badge(pass ? "PASS" : "REJECT", pass ? "green" : "gray")}}
        ${{traded ? badge("TRADED", "blue") : ""}}
      </td>
    </tr>`);
  }});
}});
document.getElementById("signalRows").innerHTML = signalRows.length
  ? signalRows.join("")
  : '<tr><td colspan="12" style="color:var(--muted);text-align:center;padding:20px">Nessun segnale L2</td></tr>';

// ── Pre-market candidates table ───────────────────────────────────────────────
const pmRows = [];
LOGS.forEach(r => {{
  const l2tickers = r.signals.map(s => s.ticker);
  r.premarket_candidates.forEach(c => {{
    const advanced = l2tickers.includes(c.ticker);
    pmRows.push(`<tr>
      <td>${{r.date.slice(5)}}</td>
      <td><strong>${{c.ticker}}</strong></td>
      <td class="pos">+${{fmtU(c.gap_pct,2)}}%</td>
      <td>${{fmtU(c.adv_m,1)}}M</td>
      <td>${{c.short_float_pct != null ? c.short_float_pct.toFixed(1)+"%" : "—"}}</td>
      <td class="${{(c.dist_from_3m_high_pct||0) > -5 ? "pos" : "neg"}}">${{fmt(c.dist_from_3m_high_pct,1)}}%</td>
      <td>${{badge(advanced ? "Sì" : "No (dati OR insufficienti)", advanced ? "green" : "gray")}}</td>
    </tr>`);
  }});
}});
document.getElementById("pmRows").innerHTML = pmRows.length
  ? pmRows.join("")
  : '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">Nessun candidato pre-market</td></tr>';

</script>
</body>
</html>"""

out = os.path.join(os.path.dirname(__file__), "dashboard.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(HTML)

print(f"✓ dashboard.html generato ({len(logs)} giorni, {len(all_trades)} trade)")
