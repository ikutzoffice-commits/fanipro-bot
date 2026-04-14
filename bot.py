import asyncio
import logging
import os
import json
import threading
from datetime import datetime, timedelta
from flask import Flask, request as flask_request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
import gspread
import pytz

# ─── CONFIGURAZIONE ───────────────────────────────────────────────────────────

TOKEN = os.environ["BOT_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
TIMEZONE = pytz.timezone("Europe/Rome")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", 8443))

ORA_INIZIO = 6    # 06:00
ORA_FINE = 19     # 19:30
MINUTI_FINE = 30

LUOGHI = {
    "triglio": "Triglio",
    "locri": "Locri",
    "crotone": "Crotone",
    "ufficio": "Ufficio",
    "nuova_sede": "Nuova Sede",
}

# ─── STATI CONVERSAZIONE ──────────────────────────────────────────────────────

ATTENDI_NOME = 1
ATTENDI_COGNOME = 2

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── FLASK (webhook + health check) ──────────────────────────────────────────

flask_app = Flask(__name__)
_ptb_app = None
_ptb_loop = None


@flask_app.get("/")
def _health():
    return "OK", 200


@flask_app.post(f"/{TOKEN}")
def _telegram_webhook():
    data = flask_request.get_json(force=True)
    update = Update.de_json(data, _ptb_app.bot)
    try:
        asyncio.run_coroutine_threadsafe(
            _ptb_app.process_update(update), _ptb_loop
        ).result(timeout=30)
    except Exception as e:
        logger.error(f"Errore processing update: {e}")
    return "OK", 200


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def get_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    return gspread.service_account_from_dict(creds_dict, scopes=scopes)


def get_sheet():
    return get_client().open_by_key(SHEET_ID).sheet1


def get_dipendenti_sheet():
    spreadsheet = get_client().open_by_key(SHEET_ID)
    try:
        return spreadsheet.worksheet("Dipendenti")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="Dipendenti", rows=200, cols=3)
        ws.append_row(["Telegram ID", "Nome", "Cognome"])
        return ws


def trova_dipendente(telegram_id: int):
    ws = get_dipendenti_sheet()
    records = ws.get_all_records()
    for r in records:
        if str(r.get("Telegram ID")) == str(telegram_id):
            return r
    return None


def registra_dipendente(telegram_id: int, nome: str, cognome: str):
    ws = get_dipendenti_sheet()
    ws.append_row([str(telegram_id), nome, cognome])


def scrivi_presenza(nome: str, cognome: str, luogo: str, tipo: str):
    sheet = get_sheet()
    now = datetime.now(TIMEZONE)
    sheet.append_row([
        now.strftime("%d/%m/%Y"),
        now.strftime("%H:%M"),
        nome,
        cognome,
        luogo,
        tipo,
    ])


def get_presenze_oggi():
    sheet = get_sheet()
    records = sheet.get_all_records()
    oggi = datetime.now(TIMEZONE).strftime("%d/%m/%Y")
    return [r for r in records if r.get("Data") == oggi]


def get_presenze_periodo(giorni: int):
    sheet = get_sheet()
    records = sheet.get_all_records()
    oggi = datetime.now(TIMEZONE)
    da = oggi - timedelta(days=giorni)
    risultati = []
    for r in records:
        try:
            data_r = TIMEZONE.localize(datetime.strptime(r.get("Data", ""), "%d/%m/%Y"))
            if da <= data_r <= oggi:
                risultati.append(r)
        except ValueError:
            continue
    return risultati


def get_presenze_mese(mese: int, anno: int):
    sheet = get_sheet()
    records = sheet.get_all_records()
    risultati = []
    for r in records:
        try:
            data_r = datetime.strptime(r.get("Data", ""), "%d/%m/%Y")
            if data_r.month == mese and data_r.year == anno:
                risultati.append(r)
        except ValueError:
            continue
    return risultati


def determina_tipo(nome: str, cognome: str, luogo: str) -> str:
    """Prima scansione del giorno su quel luogo = ENTRATA, seconda = USCITA, e così via."""
    presenze_oggi = get_presenze_oggi()
    entrate = sum(
        1 for r in presenze_oggi
        if r.get("Nome") == nome
        and r.get("Cognome") == cognome
        and r.get("Luogo") == luogo
        and r.get("Tipo") == "ENTRATA"
    )
    uscite = sum(
        1 for r in presenze_oggi
        if r.get("Nome") == nome
        and r.get("Cognome") == cognome
        and r.get("Luogo") == luogo
        and r.get("Tipo") == "USCITA"
    )
    if entrate > uscite:
        return "USCITA"
    return "ENTRATA"


def fuori_orario(now: datetime) -> bool:
    if now.hour < ORA_INIZIO:
        return True
    if now.hour > ORA_FINE:
        return True
    if now.hour == ORA_FINE and now.minute > MINUTI_FINE:
        return True
    return False


def chi_manca_uscita():
    """Restituisce lista di (nome, cognome, luogo, ora_entrata) senza uscita oggi."""
    presenze = get_presenze_oggi()
    stato = {}  # chiave: (nome, cognome, luogo) → contatori
    for r in presenze:
        chiave = (r.get("Nome"), r.get("Cognome"), r.get("Luogo"))
        if chiave not in stato:
            stato[chiave] = {"entrate": 0, "uscite": 0, "ultima_entrata": ""}
        if r.get("Tipo") == "ENTRATA":
            stato[chiave]["entrate"] += 1
            stato[chiave]["ultima_entrata"] = r.get("Ora", "")
        elif r.get("Tipo") == "USCITA":
            stato[chiave]["uscite"] += 1
    mancanti = []
    for (nome, cognome, luogo), s in stato.items():
        if s["entrate"] > s["uscite"]:
            mancanti.append((nome, cognome, luogo, s["ultima_entrata"]))
    return mancanti


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    args = context.args
    luogo_code = args[0].lower() if args else None
    luogo_nome = LUOGHI.get(luogo_code)
    dipendente = trova_dipendente(telegram_id)

    if luogo_nome:
        # Timbratura NFC
        now = datetime.now(TIMEZONE)
        if fuori_orario(now):
            await update.message.reply_text(
                f"⛔ Timbratura fuori orario!\n"
                f"L'orario consentito è dalle 06:00 alle 19:30.\n"
                f"Ora: {now.strftime('%H:%M')}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ *TIMBRATURA FUORI ORARIO*\n"
                     f"👤 {dipendente['Nome'] + ' ' + dipendente['Cognome'] if dipendente else 'Utente non registrato'}\n"
                     f"📍 {luogo_nome}\n"
                     f"🕐 {now.strftime('%H:%M')}",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        if not dipendente:
            context.user_data["luogo_pendente"] = luogo_nome
            await update.message.reply_text(
                "👋 Benvenuto! Prima di timbrare devo registrarti.\n\n"
                "Scrivi il tuo *Nome*:",
                parse_mode="Markdown",
            )
            return ATTENDI_NOME
        else:
            await timbra(update, context, dipendente["Nome"], dipendente["Cognome"], luogo_nome)
            return ConversationHandler.END
    else:
        # Apertura normale
        if not dipendente:
            await update.message.reply_text(
                "👋 Benvenuto! Sono il bot presenze di *Fanipro S.r.l.*\n\n"
                "Per registrarti scrivi il tuo *Nome*:",
                parse_mode="Markdown",
            )
            return ATTENDI_NOME
        else:
            await update.message.reply_text(
                f"👋 Ciao *{dipendente['Nome']}*!\n\n"
                "Scansiona il tag NFC per timbrare.\n\n"
                "Comandi disponibili:\n"
                "/oggi — presenze di oggi\n"
                "/presenti — chi è in cantiere adesso\n"
                "/settimana — ultimi 7 giorni\n"
                "/mese — riepilogo mese corrente\n"
                "/dipendente Nome Cognome — presenze di una persona\n"
                "/luogo NomeLuogo — presenze su un luogo\n"
                "/dipendenti — lista dipendenti registrati",
                parse_mode="Markdown",
            )
            return ConversationHandler.END


async def ricevi_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nome = update.message.text.strip().title()
    context.user_data["nome"] = nome
    await update.message.reply_text(
        f"Perfetto! Ora scrivi il tuo *Cognome*:",
        parse_mode="Markdown",
    )
    return ATTENDI_COGNOME


async def ricevi_cognome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cognome = update.message.text.strip().title()
    nome = context.user_data.get("nome")
    telegram_id = update.effective_user.id
    registra_dipendente(telegram_id, nome, cognome)
    await update.message.reply_text(
        f"✅ Registrato come *{nome} {cognome}*!\n\n"
        "Da ora in poi scansiona il tag NFC per timbrare. È tutto automatico!",
        parse_mode="Markdown",
    )
    luogo_pendente = context.user_data.get("luogo_pendente")
    if luogo_pendente:
        await timbra(update, context, nome, cognome, luogo_pendente)
    return ConversationHandler.END


async def timbra(update: Update, context: ContextTypes.DEFAULT_TYPE, nome: str, cognome: str, luogo: str):
    tipo = determina_tipo(nome, cognome, luogo)
    scrivi_presenza(nome, cognome, luogo, tipo)
    emoji_tipo = "🟢" if tipo == "ENTRATA" else "🔵"
    now = datetime.now(TIMEZONE)

    await update.message.reply_text(
        f"{emoji_tipo} *Presenza registrata!*\n\n"
        f"👷 {nome} {cognome}\n"
        f"📍 {luogo}\n"
        f"🕐 {now.strftime('%H:%M')} del {now.strftime('%d/%m/%Y')}\n"
        f"📋 {tipo}",
        parse_mode="Markdown",
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"{emoji_tipo} *{tipo}*\n"
             f"👷 {nome} {cognome}\n"
             f"📍 {luogo}\n"
             f"🕐 {now.strftime('%H:%M')}",
        parse_mode="Markdown",
    )


async def cmd_oggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    presenze = get_presenze_oggi()
    if not presenze:
        await update.message.reply_text("📋 Nessuna presenza registrata oggi.")
        return
    oggi = datetime.now(TIMEZONE).strftime("%d/%m/%Y")
    testo = f"📋 *Presenze del {oggi}*\n\n"
    per_luogo = {}
    for r in presenze:
        per_luogo.setdefault(r.get("Luogo", "—"), []).append(r)
    for luogo, righe in per_luogo.items():
        testo += f"📍 *{luogo}*\n"
        for r in righe:
            emoji = "🟢" if r.get("Tipo") == "ENTRATA" else "🔵"
            testo += f"  {emoji} {r.get('Nome')} {r.get('Cognome')} — {r.get('Ora')} ({r.get('Tipo')})\n"
        testo += "\n"
    testo += f"_Totale timbrature: {len(presenze)}_"
    await update.message.reply_text(testo, parse_mode="Markdown")


PREPOSIZIONI_LUOGO = {
    "triglio": "al Triglio",
    "locri": "a Locri",
    "crotone": "a Crotone",
    "ufficio": "in Ufficio",
    "nuova sede": "alla Nuova Sede",
}


async def cmd_presenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    mancanti = chi_manca_uscita()
    filtro_luogo = " ".join(context.args).title() if context.args else None
    if filtro_luogo:
        mancanti = [(n, c, l, o) for n, c, l, o in mancanti if l.lower() == filtro_luogo.lower()]
    if not mancanti:
        if filtro_luogo:
            prep = PREPOSIZIONI_LUOGO.get(filtro_luogo.lower(), f"a {filtro_luogo}")
            await update.message.reply_text(f"📋 Nessuno attualmente presente {prep}.", parse_mode="Markdown")
        else:
            await update.message.reply_text("📋 Nessuno attualmente presente.")
        return
    oggi = datetime.now(TIMEZONE).strftime("%d/%m/%Y")
    if filtro_luogo:
        prep = PREPOSIZIONI_LUOGO.get(filtro_luogo.lower(), f"a {filtro_luogo}")
        testo = f"👷 *Presenti ora {prep} — {oggi}*\n\n"
    else:
        testo = f"👷 *Presenti ora — {oggi}*\n\n"
    per_luogo = {}
    for nome, cognome, luogo, ora in mancanti:
        per_luogo.setdefault(luogo, []).append(f"{nome} {cognome} (dalle {ora})")
    for luogo, persone in per_luogo.items():
        testo += f"📍 *{luogo}*\n"
        for p in persone:
            testo += f"  🟢 {p}\n"
        testo += "\n"
    await update.message.reply_text(testo, parse_mode="Markdown")


async def cmd_settimana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    presenze = get_presenze_periodo(7)
    if not presenze:
        await update.message.reply_text("📋 Nessuna presenza negli ultimi 7 giorni.")
        return
    per_dipendente = {}
    for r in presenze:
        chiave = f"{r.get('Nome')} {r.get('Cognome')}"
        per_dipendente.setdefault(chiave, set()).add(r.get("Data"))
    testo = "📋 *Presenze ultimi 7 giorni*\n\n"
    for dipendente, giorni in sorted(per_dipendente.items()):
        testo += f"👷 *{dipendente}* — {len(giorni)} giorni\n"
    testo += f"\n_Totale timbrature: {len(presenze)}_"
    await update.message.reply_text(testo, parse_mode="Markdown")


async def cmd_mese(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    now = datetime.now(TIMEZONE)
    presenze = get_presenze_mese(now.month, now.year)
    if not presenze:
        await update.message.reply_text("📋 Nessuna presenza questo mese.")
        return

    # Raggruppa per dipendente
    per_dipendente = {}
    for r in presenze:
        chiave = f"{r.get('Nome')} {r.get('Cognome')}"
        if chiave not in per_dipendente:
            per_dipendente[chiave] = {"giorni": set(), "luoghi": {}}
        per_dipendente[chiave]["giorni"].add(r.get("Data"))
        luogo = r.get("Luogo", "—")
        per_dipendente[chiave]["luoghi"][luogo] = per_dipendente[chiave]["luoghi"].get(luogo, set())
        per_dipendente[chiave]["luoghi"][luogo].add(r.get("Data"))

    mesi = ["Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
            "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
    testo = f"📅 *Riepilogo {mesi[now.month-1]} {now.year}*\n\n"
    for dipendente, dati in sorted(per_dipendente.items()):
        testo += f"👷 *{dipendente}* — {len(dati['giorni'])} giorni\n"
        for luogo, giorni_luogo in sorted(dati["luoghi"].items()):
            testo += f"   📍 {luogo}: {len(giorni_luogo)}gg\n"
        testo += "\n"
    testo += f"_Totale timbrature: {len(presenze)}_"
    await update.message.reply_text(testo, parse_mode="Markdown")


async def cmd_dipendente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    testo_aiuto = (
        "ℹ️ Uso corretto: `/dipendente Nome.Cognome [giorni]`\n"
        "Esempi:\n"
        "`/dipendente Mario.Rossi`\n"
        "`/dipendente Francesco Simone.De Lio`\n"
        "`/dipendente Mario.Rossi 7`"
    )
    if not context.args:
        await update.message.reply_text(testo_aiuto, parse_mode="Markdown")
        return
    testo_input = " ".join(context.args)
    giorni_param = 30
    parti = testo_input.rsplit(" ", 1)
    if len(parti) == 2 and parti[1].isdigit():
        giorni_param = int(parti[1])
        testo_input = parti[0]
    if "." not in testo_input:
        await update.message.reply_text(testo_aiuto, parse_mode="Markdown")
        return
    nome_raw, cognome_raw = testo_input.split(".", 1)
    nome = nome_raw.strip().title()
    cognome = cognome_raw.strip().title()
    if not nome or not cognome:
        await update.message.reply_text(testo_aiuto, parse_mode="Markdown")
        return
    presenze = get_presenze_periodo(giorni_param)
    presenze_dip = [
        r for r in presenze
        if r.get("Nome") == nome and r.get("Cognome") == cognome
    ]
    if not presenze_dip:
        await update.message.reply_text(f"📋 Nessuna presenza trovata per *{nome} {cognome}* negli ultimi {giorni_param} giorni.", parse_mode="Markdown")
        return

    giorni = set(r.get("Data") for r in presenze_dip)
    luoghi = {}
    for r in presenze_dip:
        luogo = r.get("Luogo", "—")
        luoghi[luogo] = luoghi.get(luogo, set())
        luoghi[luogo].add(r.get("Data"))

    testo = f"👷 *{nome} {cognome}* — ultimi {giorni_param} giorni\n\n"
    testo += f"📆 Giorni presenti: *{len(giorni)}*\n\n"
    testo += "📍 *Per luogo:*\n"
    for luogo, gg in sorted(luoghi.items()):
        testo += f"   {luogo}: {len(gg)} giorni\n"
    testo += f"\n_Totale timbrature: {len(presenze_dip)}_"
    await update.message.reply_text(testo, parse_mode="Markdown")


async def cmd_luogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    if not context.args:
        await update.message.reply_text(
            "ℹ️ Uso corretto: `/luogo NomeLuogo [giorni]`\nEsempio: `/luogo Triglio` oppure `/luogo Triglio 7`",
            parse_mode="Markdown",
        )
        return
    giorni_param = 30
    args = list(context.args)
    if args and args[-1].isdigit():
        giorni_param = int(args.pop())
    luogo = " ".join(args).title()
    if not luogo:
        await update.message.reply_text(
            "ℹ️ Uso corretto: `/luogo NomeLuogo [giorni]`\nEsempio: `/luogo Triglio` oppure `/luogo Triglio 7`",
            parse_mode="Markdown",
        )
        return
    presenze = get_presenze_periodo(giorni_param)
    presenze_luogo = [r for r in presenze if r.get("Luogo", "").lower() == luogo.lower()]
    if not presenze_luogo:
        await update.message.reply_text(f"📋 Nessuna presenza trovata per *{luogo}* negli ultimi {giorni_param} giorni.", parse_mode="Markdown")
        return

    per_dipendente = {}
    for r in presenze_luogo:
        chiave = f"{r.get('Nome')} {r.get('Cognome')}"
        per_dipendente.setdefault(chiave, set()).add(r.get("Data"))

    testo = f"📍 *{luogo}* — ultimi {giorni_param} giorni\n\n"
    for dipendente, giorni in sorted(per_dipendente.items()):
        testo += f"👷 {dipendente}: *{len(giorni)} giorni*\n"
    testo += f"\n_Totale timbrature: {len(presenze_luogo)}_"
    await update.message.reply_text(testo, parse_mode="Markdown")


async def cmd_dipendenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    ws = get_dipendenti_sheet()
    records = ws.get_all_records()
    if not records:
        await update.message.reply_text("📋 Nessun dipendente registrato.")
        return
    testo = f"👥 *Dipendenti registrati ({len(records)})*\n\n"
    for r in sorted(records, key=lambda x: x.get("Cognome", "")):
        testo += f"👷 {r.get('Nome')} {r.get('Cognome')}\n"
    await update.message.reply_text(testo, parse_mode="Markdown")


async def messaggio_sconosciuto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text(
            "Non ho capito. Ecco i comandi disponibili:\n\n"
            "📋 *Presenze generali*\n"
            "/oggi — tutte le timbrature di oggi per luogo\n"
            "/presenti — chi è presente su tutti i luoghi adesso\n"
            "/presenti Triglio — chi è presente solo al Triglio\n"
            "/presenti Locri — chi è presente solo a Locri\n"
            "/presenti Crotone — chi è presente solo a Crotone\n"
            "/presenti Ufficio — chi è presente solo in Ufficio\n"
            "/presenti Nuova Sede — chi è presente solo in Nuova Sede\n"
            "/settimana — riepilogo ultimi 7 giorni per dipendente\n"
            "/mese — riepilogo mese corrente per dipendente e luogo\n\n"
            "👷 *Per persona*\n"
            "/dipendente Mario\\.Rossi — presenze ultimi 30 giorni\n"
            "/dipendente Mario\\.Rossi 7 — presenze ultimi 7 giorni\n"
            "/dipendente Francesco Simone\\.De Lio — nomi composti supportati\n\n"
            "📍 *Per luogo*\n"
            "/luogo Triglio — presenze ultimi 30 giorni\n"
            "/luogo Triglio 7 — presenze ultimi 7 giorni\n"
            "/luogo Nuova Sede — luoghi a due parole supportati\n\n"
            "👥 *Dipendenti*\n"
            "/dipendenti — lista tutti i dipendenti registrati\n\n"
            "⚙️ *Automatico ogni giorno alle 20:00*\n"
            "⚠️ Alert uscite mancanti\n"
            "📊 Riepilogo serale completo",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Non ho capito.\nScansiona il tag NFC sul cantiere per timbrare."
        )


# ─── JOB: RIEPILOGO E ALERT SERALE ───────────────────────────────────────────

async def job_serale(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)

    # 1 — Alert uscite mancanti
    mancanti = chi_manca_uscita()
    if mancanti:
        testo_alert = "⚠️ *USCITE MANCANTI*\n\n"
        for nome, cognome, luogo, ora in mancanti:
            testo_alert += f"👷 {nome} {cognome}\n📍 {luogo} — entrata alle {ora}\n\n"
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=testo_alert,
            parse_mode="Markdown",
        )

    # 2 — Riepilogo serale
    presenze = get_presenze_oggi()
    if not presenze:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📊 *Riepilogo serale {now.strftime('%d/%m/%Y')}*\n\nNessuna presenza registrata oggi.",
            parse_mode="Markdown",
        )
        return

    per_luogo = {}
    for r in presenze:
        per_luogo.setdefault(r.get("Luogo", "—"), []).append(r)

    testo = f"📊 *Riepilogo serale {now.strftime('%d/%m/%Y')}*\n\n"
    for luogo, righe in per_luogo.items():
        testo += f"📍 *{luogo}*\n"
        for r in righe:
            emoji = "🟢" if r.get("Tipo") == "ENTRATA" else "🔵"
            testo += f"  {emoji} {r.get('Nome')} {r.get('Cognome')} — {r.get('Ora')} ({r.get('Tipo')})\n"
        testo += "\n"
    testo += f"_Totale timbrature: {len(presenze)}_"

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=testo,
        parse_mode="Markdown",
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    global _ptb_app, _ptb_loop

    _ptb_app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ATTENDI_NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_nome)],
            ATTENDI_COGNOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_cognome)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    _ptb_app.add_handler(conv_handler)
    _ptb_app.add_handler(CommandHandler("oggi", cmd_oggi))
    _ptb_app.add_handler(CommandHandler("presenti", cmd_presenti))
    _ptb_app.add_handler(CommandHandler("settimana", cmd_settimana))
    _ptb_app.add_handler(CommandHandler("mese", cmd_mese))
    _ptb_app.add_handler(CommandHandler("dipendente", cmd_dipendente))
    _ptb_app.add_handler(CommandHandler("luogo", cmd_luogo))
    _ptb_app.add_handler(CommandHandler("dipendenti", cmd_dipendenti))
    _ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messaggio_sconosciuto))

    job_queue = _ptb_app.job_queue
    ora_invio = datetime.now(TIMEZONE).replace(hour=20, minute=0, second=0, microsecond=0)
    if ora_invio < datetime.now(TIMEZONE):
        ora_invio += timedelta(days=1)
    job_queue.run_daily(job_serale, time=ora_invio.timetz(), name="riepilogo_serale")

    if WEBHOOK_URL:
        # Avvia event loop asyncio in un thread separato
        _ptb_loop = asyncio.new_event_loop()
        threading.Thread(target=_ptb_loop.run_forever, daemon=True).start()

        # Inizializza e avvia il bot
        asyncio.run_coroutine_threadsafe(_ptb_app.initialize(), _ptb_loop).result()
        asyncio.run_coroutine_threadsafe(_ptb_app.start(), _ptb_loop).result()

        # Registra il webhook con Telegram
        asyncio.run_coroutine_threadsafe(
            _ptb_app.bot.set_webhook(
                url=f"{WEBHOOK_URL}/{TOKEN}",
                drop_pending_updates=True,
            ),
            _ptb_loop,
        ).result()

        logger.info(f"Bot avviato in modalità webhook su porta {PORT}")
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        logger.info("Bot avviato in modalità polling")
        _ptb_app.run_polling()


if __name__ == "__main__":
    main()
