import subprocess
import sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "python-telegram-bot==21.6"])

import os
import logging
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes,
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
DB_PATH = os.environ.get("DB_PATH", "bp_diary.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, timestamp TEXT NOT NULL, systolic INTEGER NOT NULL, diastolic INTEGER NOT NULL, pulse INTEGER, medication TEXT, notes TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ut ON entries(user_id, timestamp DESC)")
    conn.commit()
    conn.close()

def classify_bp(s, d):
    if s < 120 and d < 80: return "🟢", "Оптимальное"
    if s < 130 and d < 85: return "🟢", "Нормальное"
    if s < 140 and d < 90: return "🟡", "Повышенное"
    if s < 160 and d < 100: return "🟠", "Гипертония 1 ст."
    if s < 180 and d < 110: return "🔴", "Гипертония 2 ст."
    return "🚨", "Криз!"

WAITING_BP_FROM_PHOTO = 10
WAITING_MEDICATION = 0
WAITING_NOTES = 1

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❤️ *Дневник давления*\n\n"
        "✏️ Введите: `120/80 72`\n"
        "📸 Или отправьте фото — бот попросит ввести цифры\n\n"
        "📋 /history — записи\n"
        "📊 /stats — статистика\n"
        "🖨 /export — печать\n"
        "❓ /help — справка", parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Справка:*\n\n"
        "Напишите давление: `130/85 72`\n"
        "Или отправьте фото и введите цифры\n\n"
        "/history — 10 последних\n"
        "/history\\_all — все\n"
        "/stats — неделя\n"
        "/export — таблица\n"
        "/delete — удалить последнюю\n"
        "/cancel — отмена", parse_mode="Markdown")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Фото получено!\n\n"
        "Введите показания с экрана тонометра:\n"
        "`120/80 72`\n"
        "(верхнее/нижнее пульс)", parse_mode="Markdown")
    return WAITING_BP_FROM_PHOTO

async def bp_from_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await parse_and_save_bp(update, context)

async def handle_text_bp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await parse_and_save_bp(update, context)

async def parse_and_save_bp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "/" not in text:
        return ConversationHandler.END
    parts = text.replace(",", " ").split()
    try:
        bp = parts[0].split("/")
        s = int(bp[0].strip())
        d = int(bp[1].strip())
        p = int(parts[1].strip()) if len(parts) > 1 else None
    except (ValueError, IndexError):
        await update.message.reply_text("🤔 Формат: `120/80` или `120/80 72`", parse_mode="Markdown")
        return ConversationHandler.END
    if s < 60 or s > 300 or d < 30 or d > 200:
        await update.message.reply_text("⚠️ Проверьте давление")
        return ConversationHandler.END
    if p and (p < 30 or p > 250):
        await update.message.reply_text("⚠️ Проверьте пульс")
        return ConversationHandler.END

    emoji, label = classify_bp(s, d)
    context.user_data["systolic"] = s
    context.user_data["diastolic"] = d
    context.user_data["pulse"] = p

    pt = f"\n💓 Пульс: *{p}*" if p else ""
    kb = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_med")]]
    await update.message.reply_text(
        f"🩸 *{s}/{d}*{pt}\n{emoji} {label}\n\n💊 Лекарства? Или «Пропустить»",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_MEDICATION

async def medication_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["medication"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await update.message.reply_text("📝 Заметки? Или «Пропустить»", reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_NOTES

async def skip_medication(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_text(q.message.text, reply_markup=None)
    except Exception:
        pass
    context.user_data["medication"] = None
    kb = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await q.message.reply_text("📝 Заметки? Или «Пропустить»", reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_NOTES

async def notes_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip()
    return await save_entry(update, context, False)

async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_text(q.message.text, reply_markup=None)
    except Exception:
        pass
    context.user_data["notes"] = None
    uid = update.effective_user.id
    data = context.user_data
    conn = get_db()
    conn.execute("INSERT INTO entries (user_id,timestamp,systolic,diastolic,pulse,medication,notes) VALUES (?,?,?,?,?,?,?)",
        (uid, datetime.now().isoformat(), data["systolic"], data["diastolic"], data.get("pulse"), data.get("medication"), data.get("notes")))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) as c FROM entries WHERE user_id=?", (uid,)).fetchone()["c"]
    conn.close()
    emoji, label = classify_bp(data["systolic"], data["diastolic"])
    med = f"\n💊 {data['medication']}" if data.get("medication") else ""
    notes = f"\n📝 {data['notes']}" if data.get("notes") else ""
    pulse = f"  💓 {data['pulse']}" if data.get("pulse") else ""
    await q.message.reply_text(f"✅ *Сохранено!*\n\n🩸 *{data['systolic']}/{data['diastolic']}*{pulse}\n{emoji} {label}{med}{notes}\n\n📊 Всего: {total}", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp DESC LIMIT 10", (uid,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📋 Нет записей. Введите `120/80`", parse_mode="Markdown")
        return
    lines = ["📋 *Последние:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        e, _ = classify_bp(r["systolic"], r["diastolic"])
        p = f" 💓{r['pulse']}" if r["pulse"] else ""
        m = " 💊" if r["medication"] else ""
        lines.append(f"{e} `{dt.strftime('%d.%m %H:%M')}` *{r['systolic']}/{r['diastolic']}*{p}{m}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def history_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp DESC", (uid,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📋 Нет записей.")
        return
    lines = ["📋 *Все:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        e, _ = classify_bp(r["systolic"], r["diastolic"])
        p = f" 💓{r['pulse']}" if r["pulse"] else ""
        lines.append(f"{e} `{dt.strftime('%d.%m.%y %H:%M')}` *{r['systolic']}/{r['diastolic']}*{p}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000], parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    wa = (datetime.now() - timedelta(days=7)).isoformat()
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? AND timestamp>=?", (uid, wa)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📊 Нет данных за неделю.")
        return
    sv = [r["systolic"] for r in rows]
    dv = [r["diastolic"] for r in rows]
    pv = [r["pulse"] for r in rows if r["pulse"]]
    a_s, a_d = sum(sv)/len(sv), sum(dv)/len(dv)
    a_p = sum(pv)/len(pv) if pv else None
    emoji, label = classify_bp(int(a_s), int(a_d))
    pl = f"\n💓 Пульс: *{a_p:.0f}*" if a_p else ""
    tr = ""
    if len(sv) >= 3:
        h = len(sv)//2
        f_, s_ = sum(sv[:h])/h, sum(sv[h:])/(len(sv)-h)
        tr = "\n📉 *снижается*" if s_<f_-3 else ("\n📈 *растёт*" if s_>f_+3 else "\n➡️ *стабильно*")
    await update.message.reply_text(
        f"📊 *7 дней* ({len(rows)})\n\n🩸 *{a_s:.0f}/{a_d:.0f}*\n{emoji} {label}{pl}\n⬆️ *{max(sv)}/{max(dv)}*  ⬇️ *{min(sv)}/{min(dv)}*{tr}",
        parse_mode="Markdown")

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp", (uid,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📋 Нет записей.")
        return
    hr = ""
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        _, l = classify_bp(r["systolic"], r["diastolic"])
        hr += f"<tr><td>{dt.strftime('%d.%m.%Y')}</td><td>{dt.strftime('%H:%M')}</td><td><b>{r['systolic']}/{r['diastolic']}</b></td><td>{r['pulse'] or '-'}</td><td>{l}</td><td>{r['medication'] or '-'}</td><td>{r['notes'] or '-'}</td></tr>"
    d1 = datetime.fromisoformat(rows[0]["timestamp"]).strftime("%d.%m.%Y")
    d2 = datetime.fromisoformat(rows[-1]["timestamp"]).strftime("%d.%m.%Y")
    html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>body{{font-family:Arial;padding:20px;font-size:13px}}h1{{text-align:center;font-size:20px}}table{{width:100%;border-collapse:collapse}}th{{background:#2c3e50;color:#fff;padding:8px;text-align:left}}td{{padding:7px;border-bottom:1px solid #ddd}}tr:nth-child(even) td{{background:#f5f6fa}}</style></head><body><h1>Дневник давления</h1><p style='text-align:center;color:#666'>{d1} - {d2} | {len(rows)} записей</p><table><tr><th>Дата</th><th>Время</th><th>АД</th><th>Пульс</th><th>Статус</th><th>Лекарства</th><th>Заметки</th></tr>{hr}</table></body></html>"
    buf = BytesIO(html.encode())
    buf.name = f"bp_{datetime.now().strftime('%Y%m%d')}.html"
    buf.seek(0)
    await update.message.reply_document(document=buf, filename=buf.name, caption="🖨 Браузер → Ctrl+P")

async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp DESC LIMIT 1", (uid,)).fetchone()
    if not row:
        await update.message.reply_text("📋 Нет записей.")
        conn.close()
        return
    conn.execute("DELETE FROM entries WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑 Удалено: {row['systolic']}/{row['diastolic']}")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, handle_photo),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_bp),
        ],
        states={
            WAITING_BP_FROM_PHOTO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bp_from_photo),
            ],
            WAITING_MEDICATION: [
                CallbackQueryHandler(skip_medication, pattern="^skip_med$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, medication_text),
            ],
            WAITING_NOTES: [
                CallbackQueryHandler(skip_notes, pattern="^skip_notes$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, notes_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(skip_medication, pattern="^skip_med$"),
            CallbackQueryHandler(skip_notes, pattern="^skip_notes$"),
        ],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("history_all", history_all))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export_data))
    app.add_handler(CommandHandler("delete", delete_last))
    app.add_handler(conv)
    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
