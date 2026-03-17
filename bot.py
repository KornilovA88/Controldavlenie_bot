import sys

subprocess.check_call([sys.executable, "-m", "pip", "install", "python-telegram-bot==21.6"])

import os
import logging
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "bp_diary.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            systolic INTEGER NOT NULL,
            diastolic INTEGER NOT NULL,
            pulse INTEGER,
            medication TEXT,
            notes TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_time
        ON entries(user_id, timestamp DESC)
    """)
    conn.commit()
    conn.close()


def classify_bp(sys_val, dia_val):
    if sys_val < 120 and dia_val < 80:
        return "🟢", "Оптимальное"
    elif sys_val < 130 and dia_val < 85:
        return "🟢", "Нормальное"
    elif sys_val < 140 and dia_val < 90:
        return "🟡", "Повышенное"
    elif sys_val < 160 and dia_val < 100:
        return "🟠", "Гипертония 1 ст."
    elif sys_val < 180 and dia_val < 110:
        return "🔴", "Гипертония 2 ст."
    else:
        return "🚨", "Криз!"


WAITING_MEDICATION = 0
WAITING_NOTES = 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❤️ *Дневник давления*\n\n"
        "Я помогу вести учёт артериального давления.\n\n"
        "✏️ *Введите давление:* `120/80 72`\n"
        "   (верхнее/нижнее и пульс через пробел)\n"
        "   Пульс можно не указывать: `120/80`\n\n"
        "📋 /history — последние 10 записей\n"
        "📊 /stats — статистика за неделю\n"
        "🖨 /export — таблица для печати\n"
        "❓ /help — справка\n",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "Просто напишите давление в формате:\n"
        "`130/85` или `130/85 72`\n\n"
        "Бот спросит про лекарства и заметки —\n"
        "можно пропустить кнопкой.\n\n"
        "*Команды:*\n"
        "/history — последние 10 записей\n"
        "/history\\_all — все записи\n"
        "/stats — средние за 7 дней\n"
        "/export — таблица для печати\n"
        "/delete — удалить последнюю запись\n"
        "/cancel — отменить ввод\n",
        parse_mode="Markdown",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Ввод отменён.")
    return ConversationHandler.END


async def handle_text_bp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "/" not in text:
        return ConversationHandler.END

    parts = text.replace(",", " ").split()
    bp_part = parts[0]

    try:
        bp_parts = bp_part.split("/")
        sys_val = int(bp_parts[0].strip())
        dia_val = int(bp_parts[1].strip())
        pulse_val = int(parts[1].strip()) if len(parts) > 1 else None
    except (ValueError, IndexError):
        await update.message.reply_text(
            "🤔 Не могу разобрать. Формат:\n`120/80` или `120/80 72`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if sys_val < 60 or sys_val > 300 or dia_val < 30 or dia_val > 200:
        await update.message.reply_text("⚠️ Проверьте давление (60-300 / 30-200)")
        return ConversationHandler.END
    if pulse_val and (pulse_val < 30 or pulse_val > 250):
        await update.message.reply_text("⚠️ Проверьте пульс (30-250)")
        return ConversationHandler.END

    emoji, label = classify_bp(sys_val, dia_val)
    context.user_data["systolic"] = sys_val
    context.user_data["diastolic"] = dia_val
    context.user_data["pulse"] = pulse_val

    pulse_text = f"\n💓 Пульс: *{pulse_val}* уд/мин" if pulse_val else ""
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_med")]]

    await update.message.reply_text(
        f"🩸 Давление: *{sys_val}/{dia_val}* мм рт.ст.{pulse_text}\n"
        f"{emoji} Статус: *{label}*\n\n"
        f"💊 Принимали лекарства? Напишите или нажмите «Пропустить»",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MEDICATION


async def medication_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["medication"] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await update.message.reply_text(
        "📝 Заметки? (самочувствие)\nИли нажмите «Пропустить»",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_NOTES


async def skip_medication(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["medication"] = None
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await query.edit_text(
        query.message.text + "\n\n📝 Заметки? (самочувствие)\nИли нажмите «Пропустить»",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_NOTES


async def notes_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip()
    return await save_entry(update, context, False)


async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["notes"] = None
    return await save_entry(update, context, True)


async def save_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback):
    data = context.user_data
    now = datetime.now().isoformat()
    user_id = update.effective_user.id

    conn = get_db()
    conn.execute(
        "INSERT INTO entries (user_id,timestamp,systolic,diastolic,pulse,medication,notes) VALUES (?,?,?,?,?,?,?)",
        (user_id, now, data["systolic"], data["diastolic"],
         data.get("pulse"), data.get("medication"), data.get("notes")),
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) as c FROM entries WHERE user_id=?", (user_id,)).fetchone()["c"]
    conn.close()

    emoji, label = classify_bp(data["systolic"], data["diastolic"])
    med = f"\n💊 {data['medication']}" if data.get("medication") else ""
    notes = f"\n📝 {data['notes']}" if data.get("notes") else ""
    pulse = f"  💓 {data['pulse']}" if data.get("pulse") else ""

    text = f"✅ *Запись сохранена!*\n\n🩸 *{data['systolic']}/{data['diastolic']}*{pulse}\n{emoji} {label}{med}{notes}\n\n📊 Всего записей: {total}"

    if is_callback:
        await update.callback_query.edit_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

    context.user_data.clear()
    return ConversationHandler.END


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp DESC LIMIT 10", (user_id,)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📋 Пока нет записей. Введите `120/80`", parse_mode="Markdown")
        return

    lines = ["📋 *Последние записи:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        emoji, _ = classify_bp(r["systolic"], r["diastolic"])
        pulse = f" 💓{r['pulse']}" if r["pulse"] else ""
        med = " 💊" if r["medication"] else ""
        lines.append(f"{emoji} `{dt.strftime('%d.%m %H:%M')}` *{r['systolic']}/{r['diastolic']}*{pulse}{med}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def history_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp DESC", (user_id,)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📋 Пока нет записей.")
        return

    lines = ["📋 *Все записи:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        emoji, _ = classify_bp(r["systolic"], r["diastolic"])
        pulse = f" 💓{r['pulse']}" if r["pulse"] else ""
        med = " 💊" if r["medication"] else ""
        lines.append(f"{emoji} `{dt.strftime('%d.%m.%y %H:%M')}` *{r['systolic']}/{r['diastolic']}*{pulse}{med}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000], parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? AND timestamp>=? ORDER BY timestamp", (user_id, week_ago)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📊 Нет данных за последнюю неделю.")
        return

    sys_vals = [r["systolic"] for r in rows]
    dia_vals = [r["diastolic"] for r in rows]
    pulse_vals = [r["pulse"] for r in rows if r["pulse"]]

    avg_sys = sum(sys_vals) / len(sys_vals)
    avg_dia = sum(dia_vals) / len(dia_vals)
    avg_pulse = sum(pulse_vals) / len(pulse_vals) if pulse_vals else None

    emoji, label = classify_bp(int(avg_sys), int(avg_dia))
    pulse_line = f"\n💓 Средний пульс: *{avg_pulse:.0f}*" if avg_pulse else ""

    trend = ""
    if len(sys_vals) >= 3:
        half = len(sys_vals) // 2
        first = sum(sys_vals[:half]) / half
        second = sum(sys_vals[half:]) / (len(sys_vals) - half)
        if second < first - 3:
            trend = "\n📉 Тренд: *снижается*"
        elif second > first + 3:
            trend = "\n📈 Тренд: *растёт*"
        else:
            trend = "\n➡️ Тренд: *стабильно*"

    await update.message.reply_text(
        f"📊 *Статистика за 7 дней*\nЗаписей: {len(rows)}\n\n"
        f"🩸 Среднее: *{avg_sys:.0f}/{avg_dia:.0f}*\n{emoji} {label}{pulse_line}\n\n"
        f"⬆️ Макс: *{max(sys_vals)}/{max(dia_vals)}*\n⬇️ Мин: *{min(sys_vals)}/{min(dia_vals)}*{trend}",
        parse_mode="Markdown",
    )


async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp", (user_id,)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📋 Нет записей.")
        return

    html_rows = ""
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        _, label = classify_bp(r["systolic"], r["diastolic"])
        html_rows += (
            f"<tr><td>{dt.strftime('%d.%m.%Y')}</td><td>{dt.strftime('%H:%M')}</td>"
            f"<td><b>{r['systolic']}/{r['diastolic']}</b></td><td>{r['pulse'] or '-'}</td>"
            f"<td>{label}</td><td>{r['medication'] or '-'}</td><td>{r['notes'] or '-'}</td></tr>"
        )

    d1 = datetime.fromisoformat(rows[0]["timestamp"]).strftime("%d.%m.%Y")
    d2 = datetime.fromisoformat(rows[-1]["timestamp"]).strftime("%d.%m.%Y")

    html = (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Дневник давления</title>"
        f"<style>body{{font-family:Arial;padding:20px;font-size:13px}}"
        f"h1{{text-align:center;font-size:20px}}table{{width:100%;border-collapse:collapse}}"
        f"th{{background:#2c3e50;color:#fff;padding:8px;text-align:left}}"
        f"td{{padding:7px;border-bottom:1px solid #ddd}}"
        f"tr:nth-child(even) td{{background:#f5f6fa}}</style></head><body>"
        f"<h1>Дневник давления</h1><p style='text-align:center;color:#666'>{d1} - {d2} | Записей: {len(rows)}</p>"
        f"<table><tr><th>Дата</th><th>Время</th><th>Давление</th><th>Пульс</th>"
        f"<th>Статус</th><th>Лекарства</th><th>Заметки</th></tr>{html_rows}</table></body></html>"
    )

    buf = BytesIO(html.encode("utf-8"))
    buf.name = f"bp_diary_{datetime.now().strftime('%Y%m%d')}.html"
    buf.seek(0)
    await update.message.reply_document(document=buf, filename=buf.name,
        caption="🖨 Откройте в браузере → Ctrl+P → Печать")


async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp DESC LIMIT 1", (user_id,)).fetchone()
    if not row:
        await update.message.reply_text("📋 Нет записей.")
        conn.close()
        return
    conn.execute("DELETE FROM entries WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    dt = datetime.fromisoformat(row["timestamp"])
    await update.message.reply_text(f"🗑 Удалено: {dt.strftime('%d.%m.%Y %H:%M')} — {row['systolic']}/{row['diastolic']}")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_bp)],
        states={
            WAITING_MEDICATION: [
                CallbackQueryHandler(skip_medication, pattern="^skip_med$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, medication_text),
            ],
            WAITING_NOTES: [
                CallbackQueryHandler(skip_notes, pattern="^skip_notes$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, notes_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
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
