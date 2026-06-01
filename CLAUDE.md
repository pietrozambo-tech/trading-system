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
