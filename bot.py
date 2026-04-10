import telebot
import requests
import time
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# ================= НАСТРОЙКИ =================
TOKEN = "8772850572:AAHeQH6355pZyHilEbIljyrmJlgjrmwhH7s"  # ← вставь свой
bot = telebot.TeleBot(TOKEN)

# Google Sheets для логов (опционально, но рекомендую)
# Создай таблицу, поделись с сервис-аккаунтом (ниже как сделать)
SHEET_KEY = None  # если не хочешь — оставь None


# ================= ОСНОВНЫЕ ФУНКЦИИ =================
def get_egrul_data(query):
    """Основной запрос к бесплатному API egrul.org"""
    base_url = "https://egrul.org"
    # Пробуем сначала полный отчёт
    url = f"{base_url}/v2/all.php?id={query}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data, "full"
    except:
        pass

    # Фолбэк на короткий
    url_short = f"{base_url}/short_data/?id={query}"
    try:
        r = requests.get(url_short, timeout=10)
        if r.status_code == 200:
            return r.json(), "short"
    except:
        pass
    return None, None


def format_report(data, report_type):
    """Красивый отчёт"""
    if not data:
        return "❌ Данные не найдены или лимит API превышен (100 запросов/сутки)."

    text = f"✅ **Отчёт по контрагенту**\n\n"
    text += f"**ИНН:** {data.get('inn', '—')}\n"
    text += f"**ОГРН:** {data.get('ogrn', '—')}\n"
    text += f"**Название:** {data.get('name', data.get('full_name', '—'))}\n"
    text += f"**Статус:** {data.get('status', '—')}\n"
    text += f"**Дата регистрации:** {data.get('reg_date', '—')}\n\n"

    if report_type == "full":
        if "head" in data:
            head = data["head"]
            text += f"**Руководитель:** {head.get('name', '—')} ({head.get('position', '—')})\n"
        if "address" in data:
            text += f"**Адрес:** {data['address']}\n"
        text += f"\n**История выписок:** {len(data.get('history', []))} записей\n"

    text += f"\n📅 Отчёт сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
    text += "Данные из открытых реестров ФНС (egrul.org)"
    return text


# ================= ОБРАБОТЧИКИ =================
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
                     "👋 Привет! Я — Контрагент OSINT.\n\n"
                     "Отправь **ИНН** или **ОГРН** компании/ИП — и я выдам полный отчёт из ЕГРЮЛ/ЕГРИП за секунды.\n\n"
                     "Пример: `7707083893` или `1027739551234`",
                     parse_mode="Markdown")


@bot.message_handler(content_types=['text'])
def handle_inn(message):
    text = message.text.strip()
    if len(text) < 9 or not text.isdigit():
        bot.reply_to(message, "❗️ Отправь только ИНН (10 или 12 цифр) или ОГРН (13 или 15 цифр)")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    data, report_type = get_egrul_data(text)
    report = format_report(data, report_type)

    # Логируем (если Sheets подключён)
    if SHEET_KEY:
        try:
            scope = ['https://spreadsheets.google.com/feeds']
            creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads("ВАШ_JSON_КЛЮЧ"))  # позже добавим
            gc = gspread.authorize(creds)
            sh = gc.open_by_key(SHEET_KEY)
            worksheet = sh.sheet1
            worksheet.append_row(
                [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message.chat.id, text, "OK" if data else "ERROR"])
        except:
            pass

    bot.reply_to(message, report, parse_mode="Markdown")


print("🚀 Бот запущен...")
bot.infinity_polling()