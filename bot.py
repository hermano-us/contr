import telebot
import requests
from datetime import datetime
import time
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ================= НАСТРОЙКИ =================
TOKEN = os.environ.get("TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SHEET_ID = os.environ.get("SHEET_ID")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")   # ← добавим ниже

if not TOKEN:
    raise ValueError("TOKEN не задан!")

if ADMIN_CHAT_ID:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)

bot = telebot.TeleBot(TOKEN, threaded=False)   # важно для Render

# ================= HEALTH + WEBHOOK СЕРВЕР =================
class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK - Contragent OSINT webhook mode')

    def do_POST(self):
        if self.path == "/webhook":
            self.send_response(200)
            self.end_headers()
            content_length = int(self.headers['Content-Length'])
            update_str = self.rfile.read(content_length).decode('utf-8')
            try:
                update = telebot.types.Update.de_json(update_str)
                bot.process_new_updates([update])
            except Exception as e:
                print(f"Webhook error: {e}")
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), WebhookHandler)
    print(f"🚀 Webhook-сервер запущен на порту {port}")
    server.serve_forever()

# ================= GOOGLE SHEETS =================
gc = None
if GOOGLE_CREDENTIALS and SHEET_ID:
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(credentials)
        print("✅ Google Sheets подключён")
    except Exception as e:
        print(f"❌ Ошибка Sheets: {e}")

def log_to_sheet(user_id, query, status, report_type):
    if not gc or not SHEET_ID:
        return
    try:
        sh = gc.open_by_key(SHEET_ID)
        worksheet = sh.sheet1
        row = [
            datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            str(user_id),
            query,
            status,
            report_type,
            datetime.now().strftime("%H:%M:%S")
        ]
        worksheet.append_row(row)
        print(f"✅ Записано в Sheets: {query}")
    except Exception as e:
        print(f"❌ Ошибка записи Sheets: {e}")

def send_log_to_admin(user_id, query, status, details=""):
    if not ADMIN_CHAT_ID:
        return
    try:
        log_text = f"📌 ЛОГ\nПользователь: {user_id}\nЗапрос: {query}\nСтатус: {status}\n{details}\nВремя: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
        bot.send_message(ADMIN_CHAT_ID, log_text)
    except:
        pass

# ================= ОСНОВНЫЕ ФУНКЦИИ =================
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
        return "❌ Данные не найдены или превышен лимит."
    text = "✅ **Отчёт Контрагент OSINT v1.3-webhook**\n\n"
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
        if data.get("okved"):
            text += f"**Основной ОКВЭД:** {data['okved']}\n"
        history = data.get("history", [])
        text += f"**Изменений в реестре:** {len(history)} записей\n"
    text += f"\n📅 Отчёт от {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
    text += "Источник: открытые данные ФНС"
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
        "👋 Привет! Я — Контрагент OSINT (webhook)\n\nОтправь ИНН или ОГРН.",
        parse_mode="Markdown")

@bot.message_handler(content_types=['text'])
def handle_query(message):
    text = message.text.strip()
    user_id = message.chat.id

    if not text.isdigit() or len(text) < 9 or len(text) > 15:
        bot.reply_to(message, "❗️ Отправь только цифры ИНН/ОГРН")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    data, report_type = get_egrul_data(text)
    report = format_report(data, report_type)

    bot.reply_to(message, report, parse_mode="Markdown", reply_markup=get_inline_keyboard(text))

    status = "Успех" if data else "Ошибка"
    send_log_to_admin(user_id, text, status, f"Тип: {report_type}")
    log_to_sheet(user_id, text, status, report_type)

# ================= ЗАПУСК =================
if __name__ == "__main__":
    # Запускаем сервер
    threading.Thread(target=run_server, daemon=True).start()

    # Устанавливаем webhook
    if WEBHOOK_URL:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        print(f"✅ Webhook установлен на {WEBHOOK_URL}")
    else:
        print("⚠️ WEBHOOK_URL не задан в Environment!")

    print("🚀 Бот v1.3-webhook запущен успешно!")
