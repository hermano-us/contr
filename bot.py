import telebot
import requests
from datetime import datetime
import time
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= НАСТРОЙКИ =================
TOKEN = os.environ.get("TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SHEET_ID = os.environ.get("SHEET_ID")

if not TOKEN:
    raise ValueError("TOKEN не задан!")

if ADMIN_CHAT_ID:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)

bot = telebot.TeleBot(TOKEN)

# Инициализация Google Sheets
gc = None
if GOOGLE_CREDENTIALS and SHEET_ID:
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(credentials)
        print("✅ Google Sheets подключён")
    except Exception as e:
        print(f"⚠️ Ошибка Google Sheets: {e}")

# Rate-limit
user_last_request = {}

def log_to_sheet(user_id, query, status, report_type):
    if not gc or not SHEET_ID:
        return
    try:
        sh = gc.open_by_key(SHEET_ID)
        worksheet = sh.sheet1
        row = [
            datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            user_id,
            query,
            status,
            report_type,
            datetime.now().strftime("%H:%M:%S")
        ]
        worksheet.append_row(row)
    except:
        pass

def send_log_to_admin(user_id, query, status, details=""):
    if not ADMIN_CHAT_ID:
        return
    try:
        log_text = f"📌 ЛОГ\nПользователь: {user_id}\nЗапрос: {query}\nСтатус: {status}\n{details}\nВремя: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
        bot.send_message(ADMIN_CHAT_ID, log_text)
    except:
        pass

def get_egrul_data(query):
    base_url = "https://egrul.org"
    try:
        r = requests.get(f"{base_url}/v2/all.php?id={query}", timeout=15)
        if r.status_code == 200:
            return r.json(), "full"
    except:
        pass
    try:
        r = requests.get(f"{base_url}/short_data/?id={query}", timeout=10)
        if r.status_code == 200:
            return r.json(), "short"
    except:
        pass
    return None, None

def format_report(data, report_type):
    if not data:
        return "❌ Данные не найдены или превышен лимит (100 запросов/сутки).\nПопробуй через 10 минут."

    text = "✅ **Отчёт Контрагент OSINT v1.2**\n\n"
    text += f"**Название:** {data.get('name') or data.get('full_name') or '—'}\n"
    text += f"**ИНН:** {data.get('inn', '—')}\n"
    text += f"**ОГРН:** {data.get('ogrn', '—')}\n"
    text += f"**Статус:** {data.get('status', '—')}\n"
    text += f"**Дата регистрации:** {data.get('reg_date', '—')}\n"

    if report_type == "full":
        if data.get("head"):
            head = data["head"]
            text += f"**Руководитель:** {head.get('name', '—')} ({head.get('position', '—')})\n"
        if data.get("address"):
            text += f"**Юр. адрес:** {data['address']}\n"
            if "mass" in str(data.get("address", "")).lower():
                text += "⚠️ **Признак массового адреса!**\n"
        if data.get("okved"):
            text += f"**Основной ОКВЭД:** {data['okved']}\n"
        history = data.get("history", [])
        text += f"**Изменений в реестре:** {len(history)} записей\n"

    text += f"\n📅 Отчёт от {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
    text += "Источник: открытые данные ФНС (egrul.org) — 100% легально"
    return text

def get_inline_keyboard(query):
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("🔄 Повторить", callback_data=f"repeat_{query}"),
        telebot.types.InlineKeyboardButton("💰 Купить подписку", callback_data="buy_subscription"),
        telebot.types.InlineKeyboardButton("📌 Мониторинг", callback_data=f"monitor_{query}")
    )
    return markup

# ================= ХЕНДЛЕРЫ =================
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
        "👋 Привет! Я — Контрагент OSINT v1.2\n\n"
        "Отправь **ИНН** или **ОГРН** — получишь подробный отчёт из ЕГРЮЛ/ЕГРИП.\n\n"
        "Пример: `7707083893`",
        parse_mode="Markdown")

@bot.message_handler(content_types=['text'])
def handle_query(message):
    text = message.text.strip()
    user_id = message.chat.id

    now = time.time()
    if user_id in user_last_request and now - user_last_request[user_id] < 8:
        bot.reply_to(message, "⏳ Подожди 8 секунд между запросами.")
        return
    user_last_request[user_id] = now

    if not text.isdigit() or len(text) < 9 or len(text) > 15:
        bot.reply_to(message, "❗️ Отправь только цифры ИНН или ОГРН")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    data, report_type = get_egrul_data(text)
    report = format_report(data, report_type)

    bot.reply_to(message, report, parse_mode="Markdown", reply_markup=get_inline_keyboard(text))

    status = "Успех" if data else "Ошибка"
    send_log_to_admin(user_id, text, status, f"Тип: {report_type}")
    log_to_sheet(user_id, text, status, report_type)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data.startswith("repeat_"):
        query = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "Повторяем запрос...")
        data, report_type = get_egrul_data(query)
        report = format_report(data, report_type)
        bot.edit_message_text(report, call.message.chat.id, call.message.message_id,
                              parse_mode="Markdown", reply_markup=get_inline_keyboard(query))

    elif call.data == "buy_subscription":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
            "💰 **Подписка Контрагент OSINT**\n\n"
            "Неограниченные запросы + мониторинг изменений — 4 900 ₽/месяц\n\n"
            "Напиши @твой_логин для оплаты (СБП / ЮKassa)",
            parse_mode="Markdown")

    elif call.data.startswith("monitor_"):
        bot.answer_callback_query(call.id, "Мониторинг в разработке (v1.3)")

print("🚀 Бот v1.2 запущен успешно!")
bot.infinity_polling()
