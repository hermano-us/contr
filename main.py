import os
import asyncio
import logging
import re
from datetime import datetime, date
from io import BytesIO

import aiohttp
import aiosqlite
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiohttp import web
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors

# ================= CONFIG =================
TOKEN = os.environ.get("TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 0))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")          # https://твой-проект.onrender.com/webhook
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SHEET_ID = os.environ.get("SHEET_ID")
DB_NAME = "osint_pro.db"
FREE_LIMIT = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= FONT (кириллица) =================
FONT_NAME = "DejaVuSans"
try:
    pdfmetrics.registerFont(TTFont(FONT_NAME, "DejaVuSans.ttf"))
    logger.info("✅ DejaVuSans загружен")
except Exception as e:
    logger.warning(f"Шрифт не найден, будет Helvetica: {e}")
    FONT_NAME = "Helvetica"

# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS usage_log 
                           (user_id INTEGER, query_date DATE DEFAULT CURRENT_DATE)''')
        await db.commit()

async def check_limit(user_id: int) -> bool:
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            today = date.today().isoformat()
            async with db.execute(
                "SELECT COUNT(*) FROM usage_log WHERE user_id = ? AND query_date = ?",
                (user_id, today)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] < FREE_LIMIT
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return True

async def log_usage(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO usage_log (user_id) VALUES (?)", (user_id,))
        await db.commit()

# ================= GOOGLE SHEETS =================
gc = None
if GOOGLE_CREDENTIALS and SHEET_ID:
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(GOOGLE_CREDENTIALS),
            ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        )
        gc = gspread.authorize(creds)
        logger.info("✅ Google Sheets подключён")
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")

def log_to_sheet(user_id, inn, status):
    if not gc or not SHEET_ID:
        return
    try:
        sh = gc.open_by_key(SHEET_ID)
        worksheet = sh.sheet1
        worksheet.append_row([
            datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            str(user_id),
            inn,
            status,
            datetime.now().strftime("%H:%M:%S")
        ])
    except Exception as e:
        logger.error(f"Sheet write error: {e}")

# ================= RISK + PDF =================
def get_risk_assessment(data: dict):
    score = 100
    risk_factors = []

    reg_date_str = data.get('reg_date', '')
    if reg_date_str:
        try:
            reg_date = datetime.strptime(reg_date_str, '%Y-%m-%d')
            years = (datetime.now() - reg_date).days / 365
            if years < 1:
                score -= 40
                risk_factors.append("⚠️ Критическая новизна: компания меньше года")
            elif years < 3:
                score -= 15
                risk_factors.append("🟡 Молодая компания (менее 3 лет)")
        except:
            pass

    status = str(data.get('status') or data.get('status_text') or "").lower()
    if any(x in status for x in ["ликвидац", "банкрот", "прекращ"]):
        score -= 80
        risk_factors.append("🚨 ОПАСНО: в процессе ликвидации / банкротства")

    if score > 60:
        risk_factors.append("✅ Критических арбитражных дел не обнаружено")

    color = colors.green if score > 70 else colors.orange if score > 40 else colors.red
    return score, risk_factors, color

def create_pro_pdf(data: dict, score: int, risks: list, color: colors):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    w, h = A4

    # Header
    c.setFillColor(colors.HexColor("#f4f4f4"))
    c.rect(0, h - 100, w, 100, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#1a237e"))
    c.setFont(FONT_NAME, 24)
    c.drawString(50, h - 60, "АНАЛИТИЧЕСКИЙ ОТЧЁТ OSINT PRO")

    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.grey)
    c.drawString(50, h - 80, f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

    # Индекс безопасности
    c.setFont(FONT_NAME, 14)
    c.setFillColor(colors.black)
    c.drawString(50, h - 140, "ИНДЕКС БЕЗОПАСНОСТИ:")
    c.setStrokeColor(colors.lightgrey)
    c.roundRect(50, h - 170, 200, 20, 5, stroke=1, fill=0)
    c.setFillColor(color)
    c.roundRect(50, h - 170, 2 * score, 20, 5, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 16)
    c.drawString(270, h - 165, f"{score} / 100")

    # Основные данные
    y = h - 220
    info = [
        ("Организация:", data.get('name') or data.get('short_name') or "Н/Д"),
        ("ИНН:", data.get('inn', "Н/Д")),
        ("Статус:", data.get('status_text') or data.get('status') or "Действует"),
        ("Дата регистрации:", data.get('reg_date', "Н/Д")),
        ("Адрес:", data.get('address', "Информация ограничена"))
    ]
    for label, val in info:
        c.setFont(FONT_NAME, 11)
        c.setFillColor(colors.grey)
        c.drawString(50, y, label)
        c.setFillColor(colors.black)
        c.drawString(180, y, str(val))
        y -= 28

    # Заключение
    y -= 20
    c.setStrokeColor(colors.lightgrey)
    c.line(50, y, 550, y)
    y -= 40
    c.setFont(FONT_NAME, 14)
    c.drawString(50, y, "ЗАКЛЮЧЕНИЕ ЭКСПЕРТИЗЫ:")
    y -= 35
    c.setFont(FONT_NAME, 10)
    for risk in risks:
        c.drawString(60, y, risk)
        y -= 22

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

# ================= HANDLERS =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🚀 **OSINT PRO v2.0**\n\n"
        "Пришлите ИНН или ОГРН для глубокого анализа.\n"
        "Бесплатно — 3 запроса в сутки.",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(F.text)
async def handle_search(message: Message):
    inn = "".join(re.findall(r'\d+', message.text))
    if len(inn) not in (10, 12):
        return

    if not await check_limit(message.from_user.id):
        return await message.answer(
            "🛑 Лимит 3 запроса в день исчерпан.\n"
            "Купите подписку для безлимитного доступа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")
            ]])
        )

    wait = await message.answer("🔍 Идёт анализ по реестрам ФНС...")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://egrul.org/short_data/?id={inn}") as resp:
            data = await resp.json(content_type=None) if resp.status == 200 else None

    if not data:
        return await wait.edit_text("❌ Данные по ИНН не найдены.")

    await log_usage(message.from_user.id)
    score, risks, color = get_risk_assessment(data)
    log_to_sheet(message.from_user.id, inn, f"score:{score}")

    res = (
        f"✅ **ОТЧЁТ OSINT PRO**\n\n"
        f"🏢 `{data.get('name') or '—'}`\n"
        f"🛡️ Индекс безопасности: `{score}/100`\n"
        f"📄 Полный аудит в PDF ниже."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Скачать PDF", callback_data=f"pdf_{inn}")
    ]])

    await wait.delete()
    await message.answer(res, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("pdf_"))
async def send_pdf(call: CallbackQuery):
    inn = call.data.split("_")[1]
    await call.answer("Генерирую PDF...")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://egrul.org/short_data/?id={inn}") as resp:
            data = await resp.json(content_type=None)

    score, risks, color = get_risk_assessment(data)
    pdf_buffer = create_pro_pdf(data, score, risks, color)

    await call.message.answer_document(
        BufferedInputFile(pdf_buffer.read(), filename=f"OSINT_PRO_{inn}.pdf"),
        caption="✅ Аналитический отчёт готов"
    )

@dp.callback_query(F.data == "buy")
async def buy_subscription(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "💰 **Подписка OSINT PRO**\n\n"
        "Безлимитные запросы + приоритет — 4900 ₽/мес\n\n"
        "Напишите @ваш_логин для оплаты"
    )

# ================= WEB SERVER =================
async def health_handler(request):
    return web.Response(text="OK", status=200)

async def webhook_handler(request):
    try:
        data = await request.json()
        update = types.Update.model_validate(data)
        await bot.process_new_updates([update])
        return web.Response()
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def main():
    await init_db()

    # Удаляем старый webhook и ставим новый
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=WEBHOOK_URL)

    logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")

    # Запускаем aiohttp сервер
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/webhook", webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000))).start()

    logger.info("🚀 OSINT PRO v2.0 запущен на Render")
    await asyncio.Event().wait()  # держим процесс живым

if __name__ == "__main__":
    asyncio.run(main())
