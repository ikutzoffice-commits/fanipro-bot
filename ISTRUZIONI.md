# Istruzioni Setup Bot Presenze Fanipro

## File nella cartella
- bot.py            — codice completo del bot
- requirements.txt  — librerie necessarie
- Procfile          — configurazione Railway
- runtime.txt       — versione Python
- ISTRUZIONI.md     — questo file

─────────────────────────────────────────────
## STEP 1 — Google Cloud Console (credenziali)
─────────────────────────────────────────────

1. Vai su https://console.cloud.google.com
2. Crea un nuovo progetto → chiamalo "Presenze Fanipro"
3. Menu laterale → "API e servizi" → "Libreria"
4. Cerca "Google Sheets API" → Abilita
5. Cerca "Google Drive API" → Abilita
6. Menu laterale → "Credenziali" → "Crea credenziali" → "Account di servizio"
7. Nome account: "bot-presenze" → Crea
8. Clicca sull'account appena creato → tab "Chiavi"
9. "Aggiungi chiave" → "JSON" → scarica il file
10. Rinomina il file scaricato in: credentials.json
11. Copia l'email dell'account (es: bot-presenze@presenze-fanipro.iam.gserviceaccount.com)
12. Apri il Google Sheet "Presenze 2026" → Condividi → incolla quella email come Editor

─────────────────────────────────────────────
## STEP 2 — Carica su Railway
─────────────────────────────────────────────

1. Vai su https://railway.app → registrati gratis
2. "New Project" → "Empty project"
3. Carica tutti i file della cartella (incluso credentials.json)
4. Vai su "Variables" e aggiungi:
   BOT_TOKEN  = il token del tuo bot Telegram
   SHEET_ID   = 1T7LlssReP0hz57zG1Xc_uygG2aDlB-_YWl7-Fvimt8I
   ADMIN_ID   = 441187647
5. Railway avvia il bot automaticamente

─────────────────────────────────────────────
## STEP 3 — Tag NFC (quando arrivano)
─────────────────────────────────────────────

App da usare: NFC Tools (gratis iOS/Android)
Procedura: Scrivi → Aggiungi record → URL → incolla il link → avvicina tag

LINK DA SCRIVERE SUI TAG:
(sostituisci NomeDelBot con lo username del tuo bot)

ENTRATA:
  Triglio    → https://t.me/NomeDelBot?start=triglio
  Locri      → https://t.me/NomeDelBot?start=locri
  Crotone    → https://t.me/NomeDelBot?start=crotone
  Ufficio    → https://t.me/NomeDelBot?start=ufficio
  Nuova Sede → https://t.me/NomeDelBot?start=nuova_sede

─────────────────────────────────────────────
## COMANDI DISPONIBILI (solo admin)
─────────────────────────────────────────────

/oggi                    → tutte le timbrature di oggi per luogo
/presenti                → chi è attualmente in cantiere/ufficio
/settimana               → riepilogo ultimi 7 giorni
/mese                    → riepilogo mese corrente per dipendente e luogo
/dipendente Nome Cognome → presenze di una persona (ultimi 30 giorni)
/luogo NomeLuogo         → presenze su un luogo (ultimi 30 giorni)
/dipendenti              → lista tutti i dipendenti registrati

─────────────────────────────────────────────
## NOTIFICHE AUTOMATICHE (ogni giorno alle 20:00)
─────────────────────────────────────────────

1. ⚠️ Alert uscite mancanti (chi ha entrata ma non ha timbrato l'uscita)
2. 📊 Riepilogo serale con tutte le timbrature del giorno

─────────────────────────────────────────────
## COLORI TIMBRATURE
─────────────────────────────────────────────

🟢 Verde  = ENTRATA
🔵 Blu    = USCITA
⚠️ Giallo = anomalia / fuori orario

─────────────────────────────────────────────
## ORARIO CONSENTITO PER LE TIMBRATURE
─────────────────────────────────────────────

06:00 — 19:30
Fuori da questa fascia il bot blocca la timbratura e ti avvisa.

