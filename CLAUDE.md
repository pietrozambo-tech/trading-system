# Claude Code — regole per questo repository

## CRITICO: mai pushare durante le ore di trading

**NON pushare su `main` tra le 09:25 e le 16:00 ET nei giorni feriali (lunedì–venerdì).**

Railway fa auto-deploy ad ogni push su `main`. Un deploy durante la sessione invia SIGTERM
al container in esecuzione, interrompendo il monitoring loop e lasciando posizioni aperte senza
chiusura né notifica Telegram.

**Episodio reale (1 giugno 2026):** commit `5ef1b8e` pushato alle 14:14 UTC (10:14 ET) →
Railway deploy → SIGTERM 14:16 UTC → posizione NVDA lasciata aperta → perdita non gestita.

Regola operativa:
- Modifiche urgenti durante la sessione → prepara il commit, ma pusha solo dopo le 16:00 ET.
- Se l'utente chiede esplicitamente di pushare durante le ore di trading, avvisare del rischio
  prima di procedere.

## Sicurezza

- Il file `.env` contiene API key reali (Alpaca, Anthropic, Telegram). È in `.gitignore`.
  **Non pushare mai `.env` su GitHub**, nemmeno accidentalmente.
- Railway usa variabili d'ambiente proprie — le chiavi non vanno nel codice né nei commit.

## Branch policy

- Sviluppa su `main` salvo diversa indicazione esplicita dell'utente.
- Non creare branch di feature senza discuterlo prima.

## Generazione PDF (automatic-trading-vN.pdf)

Ogni volta che l'utente chiede un PDF aggiornato, seguire **esattamente** queste istruzioni.

### Strumenti
- `python-markdown` con `extensions=["tables", "fenced_code"]`
- `weasyprint` per la conversione HTML → PDF
- Input: `README.md` nella root del repo
- Output: `/home/user/trading-system/automatic-trading-vN.pdf` (incrementa N ad ogni nuova versione)
- Commit con `git add -f automatic-trading-vN.pdf` (il `.gitignore` esclude `*.pdf`)

### Versione corrente
La versione più recente è **v6**. La prossima sarà v7.

### CSS esatto (NON modificare senza istruzione esplicita)

```css
@page { size: A4; margin: 63.5pt 62.7pt 62pt 62.7pt; }
body { font-family: Liberation Sans; font-size: 7.9pt; line-height: 1.5; color: #24292e; }
h1   { font-size: 12.6pt; font-weight: bold; border-bottom: 1.5pt solid #e1e4e8; padding-bottom: 4pt; margin-top: 14pt; margin-bottom: 6pt; }
h2   { font-size: 9.8pt;  font-weight: bold; border-bottom: 0.75pt solid #e1e4e8; padding-bottom: 3pt; margin-top: 12pt; margin-bottom: 4pt; }
h3   { font-size: 8.3pt;  font-weight: bold; margin-top: 10pt; margin-bottom: 3pt; }
h4   { font-size: 7.9pt;  font-weight: bold; margin-top: 8pt;  margin-bottom: 2pt; }
p    { margin: 0 0 5pt 0; }
a    { color: #0366d6; text-decoration: none; }
code { font-family: Liberation Mono; font-size: 6.9pt; color: #24292e; background: #f6f8fc; padding: 1pt 2.5pt; border-radius: 2pt; }
pre  { font-family: Liberation Mono; font-size: 6.1pt; color: #24292e; background: #f6f8fc; border: 0.75pt solid #eaecee; border-radius: 3pt; padding: 7pt 9pt; line-height: 1.45; white-space: pre-wrap; }
pre code { background: none; padding: 0; font-size: 6.1pt; border-radius: 0; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0; font-size: 7.1pt; }
th  { background: #f6f8fc; border: 0.75pt solid #eaecee; padding: 4pt 7pt; font-weight: bold; text-align: left; }
td  { border: 0.75pt solid #eaecee; padding: 4pt 7pt; text-align: left; }
tr:nth-child(even) td { background: #fafaf7; }
hr  { border: none; border-top: 0.75pt solid #e1e4e8; margin: 10pt 0; }
ul,ol { margin: 3pt 0 5pt 0; padding-left: 14pt; }
li  { margin-bottom: 2pt; }
blockquote { border-left: 3pt solid #dfe2e5; color: #6a737d; margin: 4pt 0; padding: 0 8pt; }
```

### Script di generazione

```python
import markdown, subprocess

CSS = "...css sopra..."

with open("/home/user/trading-system/README.md") as f:
    md_content = f.read()

html_body = markdown.markdown(md_content, extensions=["tables", "fenced_code"])
html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html_body}</body></html>"

with open("/tmp/trading_vN.html", "w") as f:
    f.write(html)

subprocess.run(["weasyprint", "/tmp/trading_vN.html", "/home/user/trading-system/automatic-trading-vN.pdf"])
```

### Verifica (opzionale)
Per verificare le dimensioni del font nel PDF generato, usare PyMuPDF (`import fitz`) e confrontare con il PDF v5 di riferimento.
