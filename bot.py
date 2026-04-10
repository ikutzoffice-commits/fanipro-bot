import logging
import os
from datetime import datetime, timedelta
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
from google.oauth2.service_account import Credentials
import pytz

# ─── CONFIGURAZIONE ───────────────────────────────────────────────────────────

TOKEN = os.environ.get("BOT_TOKEN", "8674327131:AAH2NAggoFBdqkn333cX71V54NqUMVsGA8g")
SHEET_ID = os.environ.get("SHEET_ID", "1T7LlssReP0hz57zG1Xc_uygG2aDlB-_YWl7-Fvimt8I")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "441187647"))
TIMEZONE = pytz.timezone("Europe/Rome")

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

# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def get_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    return gspread.authorize(creds)


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
            data_r = datetime.strptime(r.get("Data", ""), "%d/%m/%Y").replace(tzinfo=TIMEZONE)
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


async def cmd_presenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando riservato all'amministratore.")
        return
    mancanti = chi_manca_uscita()
    if not mancanti:
        await update.message.reply_text("📋 Nessuno attualmente presente.")
        return
    oggi = datetime.now(TIMEZONE).strftime("%d/%m/%Y")
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
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "ℹ️ Uso corretto: `/dipendente Nome Cognome`\nEsempio: `/dipendente Mario Rossi`",
            parse_mode="Markdown",
        )
        return
    nome = context.args[0].title()
    cognome = context.args[1].title()
    presenze = get_presenze_periodo(30)
    presenze_dip = [
        r for r in presenze
        if r.get("Nome") == nome and r.get("Cognome") == cognome
    ]
    if not presenze_dip:
        await update.message.reply_text(f"📋 Nessuna presenza trovata per *{nome} {cognome}* negli ultimi 30 giorni.", parse_mode="Markdown")
        return

    giorni = set(r.get("Data") for r in presenze_dip)
    luoghi = {}
    for r in presenze_dip:
        luogo = r.get("Luogo", "—")
        luoghi[luogo] = luoghi.get(luogo, set())
        luoghi[luogo].add(r.get("Data"))

    testo = f"👷 *{nome} {cognome}* — ultimi 30 giorni\n\n"
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
            "ℹ️ Uso corretto: `/luogo NomeLuogo`\nEsempio: `/luogo Triglio`",
            parse_mode="Markdown",
        )
        return
    luogo = " ".join(context.args).title()
    presenze = get_presenze_periodo(30)
    presenze_luogo = [r for r in presenze if r.get("Luogo", "").lower() == luogo.lower()]
    if not presenze_luogo:
        await update.message.reply_text(f"📋 Nessuna presenza trovata per *{luogo}* negli ultimi 30 giorni.", parse_mode="Markdown")
        return

    per_dipendente = {}
    for r in presenze_luogo:
        chiave = f"{r.get('Nome')} {r.get('Cognome')}"
        per_dipendente.setdefault(chiave, set()).add(r.get("Data"))

    testo = f"📍 *{luogo}* — ultimi 30 giorni\n\n"
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
    await update.message.reply_text(
        "Non ho capito. Scansiona il tag NFC per timbrare.\n\n"
        "Comandi disponibili:\n"
        "/oggi\n/presenti\n/settimana\n/mese\n"
        "/dipendente Nome Cognome\n/luogo NomeLuogo\n/dipendenti"
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
    app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ATTENDI_NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_nome)],
            ATTENDI_COGNOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ricevi_cognome)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("oggi", cmd_oggi))
    app.add_handler(CommandHandler("presenti", cmd_presenti))
    app.add_handler(CommandHandler("settimana", cmd_settimana))
    app.add_handler(CommandHandler("mese", cmd_mese))
    app.add_handler(CommandHandler("dipendente", cmd_dipendente))
    app.add_handler(CommandHandler("luogo", cmd_luogo))
    app.add_handler(CommandHandler("dipendenti", cmd_dipendenti))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messaggio_sconosciuto))

    # Job serale alle 20:00 ora italiana
    job_queue = app.job_queue
    ora_invio = datetime.now(TIMEZONE).replace(hour=20, minute=0, second=0, microsecond=0)
    if ora_invio < datetime.now(TIMEZONE):
        ora_invio += timedelta(days=1)
    job_queue.run_daily(
        job_serale,
        time=ora_invio.timetz(),
        name="riepilogo_serale",
    )

    logger.info("Bot avviato!")
    app.run_polling()


if __name__ == "__main__":
    main()
