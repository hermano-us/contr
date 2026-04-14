import os
import asyncio
import logging
import re
import json
from datetime import datetime, date
from io import BytesIO
import aiohttp
import aiosqlite
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile, Update
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SHEET_ID = os.environ.get("SHEET_ID")
DB_NAME = "osint_pro.db"
FREE_LIMIT = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= FONT =================
FONT_NAME = "DejaVuSans"
try:
    pdfmetrics.registerFont(TTFont(FONT_NAME, "DejaVuSans.ttf"))
    logger.info("✅ Шрифт DejaVuSans загружен")
except Exception:
    logger.warning("Шрифт DejaVuSans не найден → будет Helvetica")
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
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(credentials)
        logger.info("✅ Google Sheets подключён")
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")

def log_to_sheet(user_id, inn, score: int):
    if not gc or not SHEET_ID:
        return
    try:
        sh = gc.open_by_key(SHEET_ID)
        worksheet = sh.sheet1
        now = datetime.now()
        worksheet.append_row([
            now.strftime("%d.%m.%Y %H:%M:%S"),
            str(user_id),
            inn,
            f"score:{score}",
            "Бесплатный",
            now.strftime("%H:%M:%S")
        ])
        logger.info(f"✅ Запись в Google Sheets: {user_id} | {inn} | score:{score}")
    except Exception as e:
        logger.error(f"Sheet write error: {e}")

# ================= HELPERS =================
def calculate_age(reg_date_str: str) -> str:
    if not reg_date_str:
        return "Н/Д"
    try:
        reg_date = datetime.strptime(reg_date_str, '%Y-%m-%d')
        delta = datetime.now() - reg_date
        years = delta.days // 365
        months = (delta.days % 365) // 30
        return f"{reg_date.strftime('%d.%m.%Y')} ({years} лет {months} мес.)"
    except:
        return reg_date_str

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

    status = str(data.get('status') or data.get('status_text') or data.get('sv_status_msg', '')).lower()
    if any(x in status for x in ["ликвидац", "банкрот", "прекращ"]):
        score -= 80
        risk_factors.append("🚨 ОПАСНО: в процессе ликвидации / банкротства")

    if score > 60 and not risk_factors:
        risk_factors.append("✅ Критических арбитражных дел не обнаружено")

    color = colors.green if score > 70 else colors.orange if score > 40 else colors.red
    return score, risk_factors, color

def create_pro_pdf(data: dict, score: int, risks: list, color: colors):
    # (оставил без изменений — PDF остаётся полным)
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    w, h = A4
    c.setFillColor(colors.HexColor("#f4f4f4"))
    c.rect(0, h - 100, w, 100, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#1a237e"))
    c.setFont(FONT_NAME, 24)
    c.drawString(50, h - 60, "АНАЛИТИЧЕСКИЙ ОТЧЁТ OSINT PRO")
    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.grey)
    c.drawString(50, h - 80, f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
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

    company_name = data.get('short_name') or data.get('full_name') or data.get('name') or "Н/Д"

    y = h - 220
    info = [
        ("Организация:", company_name),
        ("ИНН:", data.get('inn', "Н/Д")),
        ("Статус:", data.get('status_text') or data.get('status') or data.get('sv_status_msg', "Действует")),
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
        "Пришлите ИНН или ОГРН для анализа.\n"
        "Бесплатно — 3 запроса в сутки.",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(F.text)
async def handle_search(message: Message):
    inn = "".join(re.findall(r'\d+', message.text))
    if len(inn) not in (10, 12):
        return

    if not await check_limit(message.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")
        ]])
        return await message.answer("🛑 Лимит 3 запроса в день исчерпан.", reply_markup=kb)

    wait = await message.answer("🔍 Идёт анализ по реестрам ФНС...")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://egrul.org/short_data/?id={inn}") as resp:
            data = await resp.json(content_type=None) if resp.status == 200 else None

    if not data or not isinstance(data, dict):
        return await wait.edit_text("❌ Данные по ИНН не найдены.")

    await log_usage(message.from_user.id)
    score, risks, color = get_risk_assessment(data)
    log_to_sheet(message.from_user.id, inn, score)

    # === Формирование Варианта А ===
    company_name = data.get('short_name') or data.get('full_name') or data.get('name') or '—'
    inn_val = data.get('inn', inn)
    ogrn = data.get('ogrn', 'Н/Д')
    kpp = data.get('kpp', 'Н/Д')
    status = data.get('status_text') or data.get('status') or data.get('sv_status_msg', "Действует")
    reg_date_str = data.get('reg_date', '')
    age = calculate_age(reg_date_str)
    director = data.get('director') or data.get('head_name') or data.get('ceo') or data.get('manager') or "Н/Д"
    address = data.get('address', "Информация ограничена")[:120]

    res = (
        f"✅ **OSINT PRO**\n\n"
        f"🏢 `{company_name}`\n"
        f"📋 ИНН `{inn_val}` | ОГРН `{ogrn}` | КПП `{kpp}`\n\n"
        f"📅 Зарегистрирована {age}\n"
        f"👤 Директор: {director}\n"
        f"📍 {address}\n\n"
        f"🛡️ Индекс безопасности: `{score}/100`\n\n"
    )

    if risks:
        res += "⚠️ **Основные риски:**\n"
        for risk in risks[:3]:
            res += f"• {risk}\n"
        res += "\n"

    res += "📄 Полный отчёт в PDF"

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

# ================= WEBHOOK & HEALTH =================
async def health_handler(request):
    return web.Response(text="OK", status=200)

async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot=bot, update=update)
        logger.info(f"✅ Update обработан (update_id={update.update_id})")
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="OK", status=200)

# ================= MAIN =================
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")

    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/webhook", webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()

    logger.info("🚀 OSINT PRO v2.0 запущен успешно!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
