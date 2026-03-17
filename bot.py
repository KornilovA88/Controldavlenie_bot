import subprocess
import sys

subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
    "python-telegram-bot==21.6",
    "rapidocr-onnxruntime==1.4.4",
])

import os
import re
import logging
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
import tempfile
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
from rapidocr_onnxruntime import RapidOCR

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "bp_diary.db")
ocr_engine = RapidOCR()


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


def recognize_photo(photo_bytes):
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(photo_bytes)
        tmp_path = tmp.name

    try:
        result, _ = ocr_engine(tmp_path)
        if not result:
            return {"error": "Не удалось распознать текст на фото"}

        all_text = " ".join([line[1] for line in result])
        logger.info(f"OCR: {all_text}")

        numbers = [int(n) for n in re.findall(r"\b(\d{2,3})\b", all_text)]
        logger.info(f"Numbers: {numbers}")

        if not numbers:
            return {"error": "Не нашёл чисел на фото"}

        seen = set()
        unique = []
        for n in numbers:
            if n not in seen:
                seen.add(n)
                unique.append(n)

        if len(unique) < 2:
            return {"error": "Нашёл мало чисел", "found": unique}

        sorted_nums = sorted(unique, reverse=True)
        systolic = diastolic = pulse = None

        for n in sorted_nums:
            if systolic is None and 80 <= n <= 250:
                systolic = n
            elif diastolic is None and 40 <= n <= 160 and (systolic is None or n < systolic):
                diastolic = n
            elif pulse is None and 35 <= n <= 200 and n != systolic and n != diastolic:
                pulse = n

        if not systolic or not diastolic:
            return {"error": "Не удалось определить давление", "found": unique}
        if systolic <= diastolic:
            return {"error": f"Верхнее ({systolic}) меньше нижнего ({diastolic})", "found": unique}

        return {"systolic": systolic, "diastolic": diastolic, "pulse": pulse}
    finally:
        os.unlink(tmp_path)


WAITING_MEDICATION = 0
WAITING_NOTES = 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❤️ *Дневник давления*\n\n"
        "📸 *Отправьте фото тонометра* — распознаю цифры\n"
        "✏️ *Или введите вручную:* `120/80 72`\n\n"
        "📋 /history — последние 10 записей\n"
        "📊 /stats — статистика за неделю\n"
        "🖨 /export — таблица для печати\n"
        "❓ /help — справка\n",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "📸 Отправьте фото экрана тонометра\n"
        "✏️ Или напишите: `130/85` или `130/85 72`\n\n"
        "*Команды:*\n"
        "/history — последние 10 записей\n"
        "/history\\_all — все записи\n"
        "/stats — средние за 7 дней\n"
        "/export — таблица для печати\n"
        "/delete — удалить последнюю\n"
        "/cancel — отменить ввод\n",
        parse_mode="Markdown",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Ввод отменён.")
    return ConversationHandler.END


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Распознаю показания...")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = BytesIO()
    await file.download_to_memory(buf)

    try:
        result = recognize_photo(buf.getvalue())
    except Exception as e:
        logger.error(f"OCR error: {e}")
        await msg.edit_text("😕 Ошибка. Введите вручную: `120/80 72`", parse_mode="Markdown")
        return ConversationHandler.END

    if "error" in result:
        extra = ""
        if "found" in result:
            extra = f"\nНайденные числа: {', '.join(str(n) for n in result['found'])}"
        await msg.edit_text(f"😕 {result['error']}{extra}\n\nВведите вручную: `120/80 72`", parse_mode="Markdown")
        return ConversationHandler.END

    sys_val = result["systolic"]
    dia_val = result["diastolic"]
    pulse_val = result.get("pulse")
    emoji, label = classify_bp(sys_val, dia_val)

    context.user_data["systolic"] = sys_val
    context.user_data["diastolic"] = dia_val
    context.user_data["pulse"] = pulse_val

    pulse_text = f"\n💓 Пульс: *{pulse_val}*" if pulse_val else ""
    keyboard = [[
        InlineKeyboardButton("✅ Верно", callback_data="confirm_ocr"),
        InlineKeyboardButton("❌ Неверно", callback_data="reject_ocr"),
    ]]

    await msg.edit_text(
        f"🔍 Распознано:\n\n🩸 *{sys_val}/{dia_val}*{pulse_text}\n{emoji} {label}\n\nВерно?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MEDICATION


async def confirm_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_med")]]
    await query.edit_text(
        query.message.text + "\n\n💊 Лекарства? Напишите или «Пропустить»",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MEDICATION


async def reject_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_text("Введите вручную: `120/80 72`", parse_mode="Markdown")
    return ConversationHandler.END


async def handle_text_bp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "/" not in text:
        return ConversationHandler.END

    parts = text.replace(",", " ").split()
    try:
        bp = parts[0].split("/")
        sys_val = int(bp[0].strip())
        dia_val = int(bp[1].strip())
        pulse_val = int(parts[1].strip()) if len(parts) > 1 else None
    except (ValueError, IndexError):
        await update.message.reply_text("🤔 Формат: `120/80` или `120/80 72`", parse_mode="Markdown")
        return ConversationHandler.END

    if sys_val < 60 or sys_val > 300 or dia_val < 30 or dia_val > 200:
        await update.message.reply_text("⚠️ Проверьте давление")
        return ConversationHandler.END

    emoji, label = classify_bp(sys_val, dia_val)
    context.user_data["systolic"] = sys_val
    context.user_data["diastolic"] = dia_val
    context.user_data["pulse"] = pulse_val

    pulse_text = f"\n💓 Пульс: *{pulse_val}*" if pulse_val else ""
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_med")]]

    await update.message.reply_text(
        f"🩸 *{sys_val}/{dia_val}*{pulse_text}\n{emoji} {label}\n\n💊 Лекарства? Или «Пропустить»",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MEDICATION


async def medication_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["medication"] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await update.message.reply_text("📝 Заметки?\nИли «Пропустить»", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_NOTES


async def skip_medication(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["medication"] = None
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await q.edit_text(q.message.text + "\n\n📝 Заметки?\nИли «Пропустить»", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_NOTES


async def notes_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip()
    return await save_entry(update, context, False)


async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["notes"] = None
    return await save_entry(update, context, True)


async def save_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback):
    data = context.user_data
    user_id = update.effective_user.id
    conn = get_db()
    conn.execute(
        "INSERT INTO entries (user_id,timestamp,systolic,diastolic,pulse,medication,notes) VALUES (?,?,?,?,?,?,?)",
        (user_id, datetime.now().isoformat(), data["systolic"], data["diastolic"],
         data.get("pulse"), data.get("medication"), data.get("notes")),
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) as c FROM entries WHERE user_id=?", (user_id,)).fetchone()["c"]
    conn.close()

    emoji, label = classify_bp(data["systolic"], data["diastolic"])
    med = f"\n💊 {data['medication']}" if data.get("medication") else ""
    notes = f"\n📝 {data['notes']}" if data.get("notes") else ""
    pulse = f"  💓 {data['pulse']}" if data.get("pulse") else ""

    text = f"✅ *Сохранено!*\n\n🩸 *{data['systolic']}/{data['diastolic']}*{pulse}\n{emoji} {label}{med}{notes}\n\n📊 Всего: {total}"

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
        await update.message.reply_text("📋 Нет записей. Введите `120/80`", parse_mode="Markdown")
        return
    lines = ["📋 *Последние:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        emoji, _ = classify_bp(r["systolic"], r["diastolic"])
        p = f" 💓{r['pulse']}" if r["pulse"] else ""
        m = " 💊" if r["medication"] else ""
        lines.append(f"{emoji} `{dt.strftime('%d.%m %H:%M')}` *{r['systolic']}/{r['diastolic']}*{p}{m}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def history_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp DESC", (user_id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📋 Нет записей.")
        return
    lines = ["📋 *Все:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        emoji, _ = classify_bp(r["systolic"], r["diastolic"])
        p = f" 💓{r['pulse']}" if r["pulse"] else ""
        lines.append(f"{emoji} `{dt.strftime('%d.%m.%y %H:%M')}` *{r['systolic']}/{r['diastolic']}*{p}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000], parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? AND timestamp>=?", (user_id, week_ago)).fetchall()
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
        tr = "\n📉 *снижается*" if s_ < f_-3 else ("\n📈 *растёт*" if s_ > f_+3 else "\n➡️ *стабильно*")
    await update.message.reply_text(
        f"📊 *7 дней* ({len(rows)} зап.)\n\n🩸 Среднее: *{a_s:.0f}/{a_d:.0f}*\n{emoji} {label}{pl}\n⬆️ *{max(sv)}/{max(dv)}*  ⬇️ *{min(sv)}/{min(dv)}*{tr}",
        parse_mode="Markdown")


async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries WHERE user_id=? ORDER BY timestamp", (user_id,)).fetchall()
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
    await update.message.reply_document(document=buf, filename=buf.name, caption="🖨 Браузер → Ctrl+P → Печать")


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
            WAITING_MEDICATION: [
                CallbackQueryHandler(confirm_ocr, pattern="^confirm_ocr$"),
                CallbackQueryHandler(reject_ocr, pattern="^reject_ocr$"),
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
