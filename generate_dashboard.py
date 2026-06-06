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
    daily_pnl = round(sum((t.get("pnl_usd") or 0) for t in trades), 2)
    rows.append({
        "date":                 log["date"],
        "spy_pct":              log.get("spy_pct", 0),
        "blocked":              log.get("blocked"),
        "daily_pnl":            daily_pnl,
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
all_trades = [t for r in rows for t in r["trades"]]
total_pnl  = round(sum((t.get("pnl_usd") or 0) for t in all_trades), 2)
wins       = [t for t in all_trades if (t.get("pnl_usd") or 0) > 0]
losses     = [t for t in all_trades if (t.get("pnl_usd") or 0) <= 0]
win_rate   = round(len(wins) / len(all_trades) * 100, 1) if all_trades else 0
avg_conf   = round(sum((t.get("confidence") or 0) for t in all_trades) / len(all_trades), 2) if all_trades else 0
avg_win    = round(sum((t.get("pnl_usd") or 0) for t in wins)   / len(wins),   2) if wins   else 0
avg_loss   = round(sum((t.get("pnl_usd") or 0) for t in losses) / len(losses), 2) if losses else 0
trade_days = sum(1 for r in rows if r["trades"])

updated = datetime.now().strftime("%d/%m/%Y %H:%M")

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
    "trade_days": trade_days,
    "total_days": len(rows),
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

/* Colors */
.pos {{ color: var(--green); }} .neg {{ color: var(--red); }} .neu {{ color: var(--text); }}
.mut {{ color: var(--muted); }}

/* Badge */
.b {{ display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
.bg  {{ background: rgba(34,197,94,.15);  color: var(--green); }}
.br  {{ background: rgba(239,68,68,.15);  color: var(--red); }}
.bb  {{ background: rgba(59,130,246,.15); color: var(--blue); }}
.bmu {{ background: rgba(136,146,164,.12); color: var(--muted); }}

/* Signal tick */
.tk {{ color: var(--green); font-weight: 700; }}
.cx {{ color: var(--red); font-weight: 700; }}

/* Conf bar */
.cb {{ display: inline-block; height: 5px; background: var(--blue); border-radius: 3px; vertical-align: middle; margin-right: 5px; opacity: .8; }}

/* SVG charts */
svg text {{ font-family: -apple-system, sans-serif; }}
</style>
</head>
<body>

<div class="header">
  <h1>📈 Trading Dashboard — Gap &amp; Go</h1>
  <span class="updated">Aggiornato: {updated}</span>
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

<!-- Trade log -->
<div class="card">
  <h2>Trade log</h2>
  <div class="tbl-wrap">
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

<!-- Pipeline funnel -->
<div class="card">
  <h2>Pipeline funnel — giornaliero</h2>
  <div class="tbl-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>SPY</th><th>Universe</th><th>Pre-mkt</th>
      <th>L1 ✓</th><th>L2 ✓</th><th>→ LLM</th><th>Trade</th><th>P&amp;L</th><th>Note</th>
    </tr></thead>
    <tbody id="funnelRows"></tbody>
  </table>
  </div>
</div>

<!-- L2 signals -->
<div class="card">
  <h2>Segnali L2 — tutti i candidati</h2>
  <div class="tbl-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>Ticker</th><th>Confidence</th><th>Post-adv</th>
      <th>OR pos</th><th>Gap ret.</th><th>Vol</th><th>Catalyst</th>
      <th>Short float</th><th>Squeeze</th><th>Gap %</th><th>Esito</th>
    </tr></thead>
    <tbody id="signalRows"></tbody>
  </table>
  </div>
</div>

<!-- Pre-market -->
<div class="card">
  <h2>Candidati pre-market</h2>
  <div class="tbl-wrap">
  <table>
    <thead><tr>
      <th>Data</th><th>Ticker</th><th>Gap %</th><th>ADV (M)</th>
      <th>Short float</th><th>Dist. 3M high</th><th>→ L2</th>
    </tr></thead>
    <tbody id="pmRows"></tbody>
  </table>
  </div>
</div>

<script>
const LOGS  = {DATA_JS};
const STATS = {STATS_JS};
const EXIT_LABELS = {{
  hard_blocker:"Hard stop", atr_stop:"ATR stop",
  vwap_exit:"VWAP take-profit", eod_close:"EOD close", manual_close:"Manual close"
}};

// ── Micro helpers ─────────────────────────────────────────────────────────────
const fu  = (n,d=2) => n==null ? "—" : parseFloat(n).toFixed(d);
const fpm = (n,d=2) => n==null ? "—" : (n>=0?"+":"")+parseFloat(n).toFixed(d);
const pct = n       => n==null ? "—" : (n*100).toFixed(1)+"%";
const tk  = v       => v ? '<span class="tk">✓</span>' : '<span class="cx">✗</span>';
const cls = n       => n>0?"pos":n<0?"neg":"neu";

function badge(t,c){{ return `<span class="b ${{c}}">${{t}}</span>`; }}
function confBar(c){{
  const w = Math.min(Math.round((c||0)/1.53*56),56);
  return `<span class="cb" style="width:${{w}}px"></span>${{fu(c,3)}}`;
}}

// ── KPIs ──────────────────────────────────────────────────────────────────────
const kpis = [
  {{ l:"P&L totale",    v:(STATS.total_pnl>=0?"+$":"−$")+Math.abs(STATS.total_pnl).toFixed(2), c:cls(STATS.total_pnl), s:`${{STATS.trade_days}} trade days / ${{STATS.total_days}} giorni` }},
  {{ l:"Win rate",      v:STATS.n_trades?STATS.win_rate+"%":"—",                                c:"neu", s:`${{STATS.n_wins}}W · ${{STATS.n_losses}}L · ${{STATS.n_trades}} trade` }},
  {{ l:"Avg win",       v:STATS.n_wins  ?"+$"+STATS.avg_win.toFixed(2):"—",                    c:"pos", s:"per trade vincente" }},
  {{ l:"Avg loss",      v:STATS.n_losses?"−$"+Math.abs(STATS.avg_loss).toFixed(2):"—",         c:STATS.n_losses?"neg":"mut", s:"per trade perdente" }},
  {{ l:"Avg confidence",v:STATS.n_trades?STATS.avg_conf.toFixed(2):"—",                        c:"neu", s:"soglia min: 0.65" }},
];
document.getElementById("kpis").innerHTML = kpis.map(k=>
  `<div class="kpi"><div class="lbl">${{k.l}}</div><div class="val ${{k.c}}">${{k.v}}</div><div class="sub">${{k.s}}</div></div>`
).join("");

// ── SVG bar chart — P&L giornaliero ──────────────────────────────────────────
(function(){{
  const W=560, H=160, pad={{t:10,r:10,b:30,l:52}};
  const iW=W-pad.l-pad.r, iH=H-pad.t-pad.b;
  const vals = LOGS.map(r=>r.daily_pnl);
  const dates = LOGS.map(r=>r.date.slice(5));
  const maxAbs = Math.max(...vals.map(Math.abs), 1);
  const barW = Math.max(6, Math.floor(iW/vals.length*0.6));
  const zero = pad.t + iH/2;

  // y-axis labels
  const yLabels = [-maxAbs, -maxAbs/2, 0, maxAbs/2, maxAbs].map(v=>{{
    const y = pad.t + iH/2 - (v/maxAbs)*(iH/2);
    const label = v===0?"$0":(v>0?"+$":"-$")+Math.abs(v).toFixed(0);
    return `<text x="${{pad.l-6}}" y="${{y+4}}" text-anchor="end" fill="#8892a4" font-size="10">${{label}}</text>
            <line x1="${{pad.l}}" y1="${{y}}" x2="${{W-pad.r}}" y2="${{y}}" stroke="#2a2d3a" stroke-width="1"/>`;
  }}).join("");

  const bars = vals.map((v,i)=>{{
    const x = pad.l + (i+0.5)*iW/vals.length - barW/2;
    const bH = Math.abs(v)/maxAbs*(iH/2);
    const y  = v>=0 ? zero-bH : zero;
    const fill = v>0?"#22c55e":v<0?"#ef4444":"#8892a4";
    return `<rect x="${{x.toFixed(1)}}" y="${{y.toFixed(1)}}" width="${{barW}}" height="${{Math.max(bH,1).toFixed(1)}}" fill="${{fill}}" rx="3"/>`;
  }}).join("");

  const xLabels = dates.map((d,i)=>{{
    const x = pad.l + (i+0.5)*iW/vals.length;
    return `<text x="${{x.toFixed(1)}}" y="${{H-4}}" text-anchor="middle" fill="#8892a4" font-size="10">${{d}}</text>`;
  }}).join("");

  const el = document.getElementById("chartPnl");
  el.innerHTML = `<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;max-width:${{W}}px;display:block">
    ${{yLabels}}
    <line x1="${{pad.l}}" y1="${{zero}}" x2="${{W-pad.r}}" y2="${{zero}}" stroke="#4a4d5a" stroke-width="1"/>
    ${{bars}}${{xLabels}}
  </svg>`;
}})();

// ── SVG donut — Exit reasons ──────────────────────────────────────────────────
(function(){{
  const counts = {{}};
  LOGS.forEach(r=>r.trades.forEach(t=>{{
    const k = EXIT_LABELS[t.exit_reason]||t.exit_reason||"Unknown";
    counts[k]=(counts[k]||0)+1;
  }}));
  const keys = Object.keys(counts);
  if(!keys.length){{
    document.getElementById("chartExit").innerHTML='<p style="color:var(--muted);font-size:12px;padding:20px 0">Nessun trade chiuso</p>';
    return;
  }}
  const total = keys.reduce((s,k)=>s+counts[k],0);
  const colors=["#3b82f6","#22c55e","#f59e0b","#ef4444","#8b5cf6","#8892a4"];
  const R=60,cx=80,cy=80,r2=30;
  let angle=-Math.PI/2;
  const slices = keys.map((k,i)=>{{
    const frac = counts[k]/total;
    const a1=angle, a2=angle+frac*2*Math.PI;
    angle=a2;
    const x1=cx+R*Math.cos(a1),y1=cy+R*Math.sin(a1);
    const x2=cx+R*Math.cos(a2),y2=cy+R*Math.sin(a2);
    const large=frac>0.5?1:0;
    return `<path d="M${{cx}},${{cy}} L${{x1.toFixed(1)}},${{y1.toFixed(1)}} A${{R}},${{R}} 0 ${{large}},1 ${{x2.toFixed(1)}},${{y2.toFixed(1)}} Z" fill="${{colors[i%colors.length]}}"/>`;
  }}).join("");
  // hole
  const hole=`<circle cx="${{cx}}" cy="${{cy}}" r="${{r2}}" fill="var(--surface)"/>`;
  // legend
  const legend = keys.map((k,i)=>
    `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
      <span style="width:10px;height:10px;border-radius:2px;background:${{colors[i%colors.length]}};flex-shrink:0"></span>
      <span style="font-size:12px;color:var(--text)">${{k}} (${{counts[k]}})</span>
    </div>`
  ).join("");
  document.getElementById("chartExit").innerHTML=
    `<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <svg viewBox="0 0 160 160" style="width:120px;flex-shrink:0"><circle cx="${{cx}}" cy="${{cy}}" r="${{R}}" fill="var(--border)"/>${{slices}}${{hole}}</svg>
      <div>${{legend}}</div>
    </div>`;
}})();

// ── Trade log ─────────────────────────────────────────────────────────────────
const tradeRows=[];
LOGS.forEach(r=>{{
  r.trades.forEach(t=>{{
    const pnl=t.pnl_usd??null, pp=t.pnl_pct??null;
    const sc=cls(pnl);
    const cat=t.catalyst_bonus>=0.30?"T1":t.catalyst_bonus>=0.20?"T2":t.catalyst_bonus>=0.10?"T3":"—";
    const catC=t.catalyst_bonus>=0.20?"bb":t.catalyst_bonus>=0.10?"bb":"bmu";
    tradeRows.push(`<tr>
      <td>${{r.date.slice(5)}}</td>
      <td><strong>${{t.ticker}}</strong></td>
      <td>${{t.entry_price!=null?"$"+fu(t.entry_price):"—"}}</td>
      <td>${{t.exit_price !=null?"$"+fu(t.exit_price) :"—"}}</td>
      <td>${{t.qty??"—"}}</td>
      <td class="${{sc}}">${{pnl!=null?(pnl>=0?"+$":"−$")+Math.abs(pnl).toFixed(2):"—"}}</td>
      <td class="${{sc}}">${{pp!=null?(pp>=0?"+":"")+( pp*100).toFixed(2)+"%":"—"}}</td>
      <td>${{badge(EXIT_LABELS[t.exit_reason]||t.exit_reason||"—", sc==="pos"?"bg":sc==="neg"?"br":"bmu")}}</td>
      <td>${{confBar(t.confidence)}}</td>
      <td class="pos">${{t.gap_pct!=null?fpm(t.gap_pct*100,1)+"%":"—"}}</td>
      <td>${{badge(cat,catC)}}</td>
      <td>${{t.vol_boost?"+"+parseFloat(t.vol_boost).toFixed(2):"—"}}</td>
      <td>${{t.short_float!=null?(t.short_float*100).toFixed(1)+"%":"—"}}</td>
    </tr>`);
  }});
}});
document.getElementById("tradeRows").innerHTML=tradeRows.length
  ?tradeRows.join("")
  :'<tr><td colspan="13" style="color:var(--muted);text-align:center;padding:24px">Nessun trade ancora</td></tr>';

// ── Funnel ────────────────────────────────────────────────────────────────────
document.getElementById("funnelRows").innerHTML=LOGS.map(r=>{{
  const sc=cls(r.daily_pnl);
  const note=r.blocked||(r.trades.length?"✓ trade eseguito":"LLM: nessuna entry");
  return `<tr>
    <td>${{r.date.slice(5)}}</td>
    <td class="${{cls(r.spy_pct)}}">${{fpm(r.spy_pct*100,2)}}%</td>
    <td>60</td>
    <td>${{r.premarket_count}}</td>
    <td>${{r.l1_count}}</td>
    <td>${{r.l2_count}}</td>
    <td>${{r.llm_input.length}}</td>
    <td>${{r.trades.length}}</td>
    <td class="${{sc}}">${{r.daily_pnl!==0?(r.daily_pnl>0?"+$":"−$")+Math.abs(r.daily_pnl).toFixed(2):"—"}}</td>
    <td class="mut" style="font-size:12px;white-space:normal;max-width:200px">${{note}}</td>
  </tr>`;
}}).join("");

// ── L2 signals ────────────────────────────────────────────────────────────────
const sigRows=[];
LOGS.forEach(r=>{{
  const tradedTickers=r.trades.map(t=>t.ticker);
  r.signals.forEach(s=>{{
    const pass=s.passes_threshold;
    const traded=tradedTickers.includes(s.ticker);
    sigRows.push(`<tr>
      <td>${{r.date.slice(5)}}</td>
      <td><strong>${{s.ticker}}</strong></td>
      <td>${{confBar(s.confidence)}}</td>
      <td>${{tk(s.post_open_advance)}}</td>
      <td class="${{(s.or_position||0)>0.66?"pos":"neg"}}">${{fu(s.or_position,2)}}</td>
      <td class="${{(s.gap_retention||0)>0.70?"pos":"neg"}}">${{fu(s.gap_retention,2)}}</td>
      <td>${{s.vol_boost?"+"+parseFloat(s.vol_boost).toFixed(2):"—"}}</td>
      <td>${{s.catalyst_bonus?"+"+parseFloat(s.catalyst_bonus).toFixed(2):"—"}}</td>
      <td>${{s.short_float!=null?(s.short_float*100).toFixed(1)+"%":"—"}}</td>
      <td>${{s.short_squeeze_bonus?badge("+"+parseFloat(s.short_squeeze_bonus).toFixed(2),"bb"):"—"}}</td>
      <td class="pos">${{s.gap_pct!=null?fpm(s.gap_pct*100,1)+"%":"—"}}</td>
      <td>
        ${{badge(pass?"PASS":"REJECT",pass?"bg":"bmu")}}
        ${{traded?badge("TRADED","bb"):""}}
      </td>
    </tr>`);
  }});
}});
document.getElementById("signalRows").innerHTML=sigRows.length
  ?sigRows.join("")
  :'<tr><td colspan="12" style="color:var(--muted);text-align:center;padding:24px">Nessun segnale L2</td></tr>';

// ── Pre-market candidates ─────────────────────────────────────────────────────
const pmRows=[];
LOGS.forEach(r=>{{
  const l2t=r.signals.map(s=>s.ticker);
  r.premarket_candidates.forEach(c=>{{
    const adv=l2t.includes(c.ticker);
    pmRows.push(`<tr>
      <td>${{r.date.slice(5)}}</td>
      <td><strong>${{c.ticker}}</strong></td>
      <td class="pos">+${{fu(c.gap_pct,2)}}%</td>
      <td>${{fu(c.adv_m,1)}}M</td>
      <td>${{c.short_float_pct!=null?c.short_float_pct.toFixed(1)+"%":"—"}}</td>
      <td class="${{(c.dist_from_3m_high_pct||0)>-5?"pos":"neg"}}">${{fpm(c.dist_from_3m_high_pct,1)}}%</td>
      <td>${{badge(adv?"Sì":"No",adv?"bg":"bmu")}}</td>
    </tr>`);
  }});
}});
document.getElementById("pmRows").innerHTML=pmRows.length
  ?pmRows.join("")
  :'<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:24px">Nessun candidato pre-market</td></tr>';
</script>
</body>
</html>"""

out = os.path.join(os.path.dirname(__file__), "dashboard.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(HTML)

print(f"✓ dashboard.html generato ({len(logs)} giorni, {len(all_trades)} trade)")
