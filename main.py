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

async def check_limit(user_id: int) -> tuple[bool, int]:
    """Возвращает (можно_использовать, осталось_запросов)"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            today = date.today().isoformat()
            async with db.execute(
                "SELECT COUNT(*) FROM usage_log WHERE user_id = ? AND query_date = ?",
                (user_id, today)
            ) as cursor:
                row = await cursor.fetchone()
                used = row[0]
                remaining = max(0, FREE_LIMIT - used)
                return used < FREE_LIMIT, remaining
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return True, FREE_LIMIT

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
        days = (delta.days % 365) % 30
        if years == 0 and months == 0:
            return f"{reg_date.strftime('%d.%m.%Y')} (новая компания)"
        return f"{reg_date.strftime('%d.%m.%Y')} ({years} лет {months} мес. {days} дн.)"
    except:
        return reg_date_str

def get_risk_assessment(data: dict):
    score = 100
    risk_factors = []
    warnings = []  # для блока "Критические предупреждения"

    # 1. Возраст компании
    reg_date_str = data.get('reg_date', '')
    if reg_date_str:
        try:
            reg_date = datetime.strptime(reg_date_str, '%Y-%m-%d')
            years = (datetime.now() - reg_date).days / 365.25
            if years < 0.5:
                score -= 50
                risk_factors.append("🚨 Крайне молодая компания (менее 6 месяцев)")
            elif years < 1:
                score -= 35
                risk_factors.append("⚠️ Критическая новизна: компания меньше года")
            elif years < 3:
                score -= 15
                risk_factors.append("🟡 Молодая компания (менее 3 лет)")
        except:
            pass

    # 2. Флаги недостоверности (самые важные!)
    if data.get('invalid_address') == 1:
        score -= 40
        risk_factors.append("🚨 Недостоверный адрес регистрации")
        warnings.append(data.get('invalid_address_msg', 'Недостоверный адрес'))
    if data.get('invalid_founder') == 1:
        score -= 35
        risk_factors.append("🚨 Недостоверные сведения об учредителях")
        warnings.append(data.get('invalid_founder_msg', 'Недостоверные учредители'))
    if data.get('invalid_chief') == 1:
        score -= 40
        risk_factors.append("🚨 Недостоверный руководитель")
        warnings.append(data.get('invalid_chief_msg', 'Недостоверный руководитель'))

    # 3. Статус
    status = str(data.get('status') or data.get('status_text') or data.get('sv_status_msg', '')).lower()
    if any(x in status for x in ["ликвидац", "банкрот", "прекращ", "недейств"]):
        score -= 80
        risk_factors.append("🚨 ОПАСНО: ликвидация / банкротство / недействующий статус")

    # 4. Дополнительные предупреждения
    sv_msg = data.get('sv_status_msg', '')
    if sv_msg and "следует обратить внимание" in sv_msg:
        score -= 20
        risk_factors.append("🟠 Есть особые сведения в ЕГРЮЛ")

    # Финализация
    if score > 85 and not risk_factors:
        risk_factors.append("✅ Критических рисков не обнаружено")
    
    color = colors.green if score > 75 else colors.orange if score > 45 else colors.red
    
    recommendation = {
        score > 80: "✅ Рекомендуется к работе",
        60 <= score <= 80: "🟡 Требует дополнительной проверки",
        score < 60: "🚫 Высокий риск! Не рекомендуется"
    }[True]

    return score, risk_factors, warnings, color, recommendation

def create_pro_pdf(data: dict, score: int, risks: list, warnings: list, color: colors):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    w, h = A4

    # Header
    c.setFillColor(colors.HexColor("#f4f4f4"))
    c.rect(0, h - 100, w, 100, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#1a237e"))
    c.setFont(FONT_NAME, 26)
    c.drawString(50, h - 60, "АНАЛИТИЧЕСКИЙ ОТЧЁТ OSINT PRO v2.1")
    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.grey)
    c.drawString(50, h - 82, f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")

    # Индекс безопасности
    c.setFont(FONT_NAME, 14)
    c.setFillColor(colors.black)
    c.drawString(50, h - 140, "ИНДЕКС БЕЗОПАСНОСТИ:")
    c.setStrokeColor(colors.lightgrey)
    c.roundRect(50, h - 170, 220, 22, 6, stroke=1, fill=0)
    c.setFillColor(color)
    c.roundRect(50, h - 170, 2.2 * score, 22, 6, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 18)
    c.drawString(290, h - 165, f"{score} / 100")

    # Основная информация
    company_name = data.get('short_name') or data.get('full_name') or data.get('name') or "Н/Д"
    y = h - 230

    info = [
        ("Организация:", company_name),
        ("ИНН:", data.get('inn', "Н/Д")),
        ("ОГРН:", data.get('ogrn', "Н/Д")),
        ("КПП:", data.get('kpp', "Н/Д")),
        ("Статус:", data.get('status_text') or data.get('status') or data.get('sv_status_msg', "Действует")),
        ("Дата регистрации:", data.get('reg_date', "Н/Д")),
        ("Руководитель:", f"{data.get('chief_position', '')} {data.get('chief', 'Н/Д')}".strip()),
        ("Адрес:", data.get('address', "Информация ограничена")[:130])
    ]

    for label, val in info:
        c.setFont(FONT_NAME, 11)
        c.setFillColor(colors.grey)
        c.drawString(50, y, label)
        c.setFillColor(colors.black)
        c.drawString(190, y, str(val))
        y -= 26

    # Критические предупреждения
    if warnings:
        y -= 20
        c.setStrokeColor(colors.red)
        c.line(50, y, 550, y)
        y -= 30
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.red)
        c.drawString(50, y, "🚨 КРИТИЧЕСКИЕ ПРЕДУПРЕЖДЕНИЯ ЕГРЮЛ:")
        y -= 25
        c.setFont(FONT_NAME, 10)
        c.setFillColor(colors.black)
        for w in warnings:
            c.drawString(60, y, f"• {w}")
            y -= 22

    # Заключение
    y -= 20
    c.setFont(FONT_NAME, 14)
    c.setFillColor(colors.black)
    c.drawString(50, y, "ЗАКЛЮЧЕНИЕ ЭКСПЕРТИЗЫ:")
    y -= 30
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
        "🚀 **OSINT PRO v2.1**\n\n"
        "Пришлите ИНН или ОГРН (10 или 12 цифр) для глубокого анализа компании.\n"
        "Бесплатно — 3 запроса в сутки.",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(F.text)
async def handle_search(message: Message):
    inn = "".join(re.findall(r'\d+', message.text))
    if len(inn) not in (10, 12):
        return

    can_use, remaining = await check_limit(message.from_user.id)
    if not can_use:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")
        ]])
        return await message.answer("🛑 Лимит 3 запроса в день исчерпан.", reply_markup=kb)

    wait = await message.answer("🔍 Идёт глубокий анализ по реестрам ЕГРЮЛ...")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://egrul.org/short_data/?id={inn}") as resp:
            data = await resp.json(content_type=None) if resp.status == 200 else None

    if not data or not isinstance(data, dict):
        return await wait.edit_text("❌ Данные по ИНН не найдены в ЕГРЮЛ.")

    await log_usage(message.from_user.id)
    score, risks, warnings, color, recommendation = get_risk_assessment(data)

    log_to_sheet(message.from_user.id, inn, score)

    # Формирование улучшенного отчёта
    company_name = data.get('short_name') or data.get('full_name') or data.get('name') or '—'
    director_info = f"{data.get('chief_position', '')} {data.get('chief', 'Н/Д')}".strip() or "Н/Д"

    res = (
        f"✅ **OSINT PRO v2.1**\n\n"
        f"🏢 `{company_name}`\n"
        f"📋 ИНН `{data.get('inn', inn)}` | ОГРН `{data.get('ogrn', 'Н/Д')}` | КПП `{data.get('kpp', 'Н/Д')}`\n\n"
        f"📅 Зарегистрирована {calculate_age(data.get('reg_date', ''))}\n"
        f"👤 Руководитель: {director_info}\n"
        f"📍 {data.get('address', 'Информация ограничена')[:140]}\n\n"
        f"🛡️ **Индекс безопасности:** `{score}/100`\n"
        f"📌 **Рекомендация:** {recommendation}\n\n"
        f"Осталось бесплатных запросов сегодня: **{remaining}/3**\n\n"
    )

    if risks:
        res += "⚠️ **Основные риски:**\n"
        for risk in risks[:5]:
            res += f"• {risk}\n"
        res += "\n"

    res += "📄 Полный профессиональный отчёт в PDF"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Скачать PDF", callback_data=f"pdf_{inn}")
    ]])

    await wait.delete()
    await message.answer(res, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("pdf_"))
async def send_pdf(call: CallbackQuery):
    inn = call.data.split("_")[1]
    await call.answer("Генерирую подробный PDF...")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://egrul.org/short_data/?id={inn}") as resp:
            data = await resp.json(content_type=None)

    score, risks, warnings, color, _ = get_risk_assessment(data)
    pdf_buffer = create_pro_pdf(data, score, risks, warnings, color)

    await call.message.answer_document(
        BufferedInputFile(pdf_buffer.read(), filename=f"OSINT_PRO_{inn}.pdf"),
        caption="✅ Полный аналитический отчёт OSINT PRO v2.1"
    )

@dp.callback_query(F.data == "buy")
async def buy_subscription(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "💰 **Подписка OSINT PRO**\n\n"
        "Безлимит + приоритетные отчёты — 4900 ₽/мес\n\n"
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

    logger.info("🚀 OSINT PRO v2.1 запущен успешно! (усиленная аналитика)")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
