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
CHECKO_API_KEY = os.environ.get("CHECKO_API_KEY")
DB_NAME = "osint_pro.db"
FREE_LIMIT = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

if CHECKO_API_KEY:
    logger.info("✅ Checko API подключён")
else:
    logger.warning("⚠️ CHECKO_API_KEY не задан — арбитраж отключён")

# ================= FONT =================
FONT_NAME = "DejaVuSans"
FONT_PATH = "DejaVuSans.ttf"

if os.path.exists(FONT_PATH):
    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
        logger.info("✅ Шрифт DejaVuSans загружен успешно")
    except Exception as e:
        logger.error(f"❌ Ошибка регистрации шрифта DejaVuSans: {e}")
        FONT_NAME = "Helvetica"
else:
    logger.warning("⚠️ Файл DejaVuSans.ttf НЕ НАЙДЕН в корне проекта! "
                   "PDF с русским текстом может падать. Загрузи шрифт на Render.com")
    FONT_NAME = "Helvetica"

# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS usage_log
                           (user_id INTEGER, query_date DATE DEFAULT CURRENT_DATE)''')
        await db.commit()

async def check_limit(user_id: int) -> tuple[bool, int]:
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
        return f"{reg_date.strftime('%d.%m.%Y')} ({years} лет {months} мес.)"
    except:
        return reg_date_str

def get_company_status(data: dict) -> tuple[str, str]:
    status = str(data.get('status_text') or data.get('status') or data.get('sv_status_msg') or "Действует").lower()
    if any(word in status for word in ["ликвидац", "прекращ", "ликвидирован"]):
        return "В процессе ликвидации / ликвидирована", "🚨"
    if any(word in status for word in ["банкрот", "банкротство"]):
        return "В процедуре банкротства", "❌"
    if any(word in status for word in ["недейств", "исключен"]):
        return "Недействующий статус", "⚠️"
    return "Действует", "✅"

async def get_arbitration_data(inn: str) -> dict | None:
    if not CHECKO_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.checko.ru/v2/legal-cases"
            params = {"key": CHECKO_API_KEY, "inn": inn}
            async with session.get(url, params=params, timeout=8) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception as e:
        logger.error(f"Arbitration error: {e}")
        return None

def get_risk_assessment(data: dict, arbitration_data: dict | None = None):
    score = 100
    risk_factors = []
    warnings = []
    mass_flags = []

    # Возраст
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

    # Массовость и недостоверность
    if data.get('invalid_address') == 1:
        score -= 40
        risk_factors.append("🚨 Недостоверный / массовый адрес")
        warnings.append(data.get('invalid_address_msg', 'Массовый адрес'))
        mass_flags.append("Адрес")
    if data.get('invalid_founder') == 1:
        score -= 35
        risk_factors.append("🚨 Недостоверные / массовые учредители")
        warnings.append(data.get('invalid_founder_msg', 'Массовые учредители'))
        mass_flags.append("Учредители")
    if data.get('invalid_chief') == 1:
        score -= 40
        risk_factors.append("🚨 Недостоверный / массовый руководитель")
        warnings.append(data.get('invalid_chief_msg', 'Массовый руководитель'))
        mass_flags.append("Руководитель")

    # Статус
    status_lower = str(data.get('status') or data.get('status_text') or data.get('sv_status_msg', '')).lower()
    if any(x in status_lower for x in ["ликвидац", "банкрот", "прекращ", "недейств"]):
        score -= 80
        risk_factors.append("🚨 ОПАСНО: ликвидация / банкротство / недействующий статус")

    # Арбитраж
    arb_count = 0
    if arbitration_data and isinstance(arbitration_data, dict):
        arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
        if arb_count > 0:
            score -= min(45, arb_count * 8)
            risk_factors.append(f"⚖️ Арбитражные дела: {arb_count} шт.")

    if score > 85 and not risk_factors:
        risk_factors.append("✅ Критических рисков не обнаружено")

    color = colors.green if score > 75 else colors.orange if score > 45 else colors.red
    recommendation = "✅ Рекомендуется к работе" if score > 80 else "🟡 Требует дополнительной проверки" if score >= 60 else "🚫 Высокий риск! Не рекомендуется"

    return score, risk_factors, warnings, color, recommendation, arbitration_data, mass_flags

# ================= УЛУЧШЕННЫЙ PDF =================
def draw_multiline(c, x, y, text, font_size=10, max_width=480, line_height=14):
    if not text:
        return y
    words = str(text).split()
    line = ""
    for word in words:
        if c.stringWidth(line + word + " ", FONT_NAME, font_size) > max_width:
            c.drawString(x, y, line)
            y -= line_height
            line = word + " "
        else:
            line += word + " "
    if line:
        c.drawString(x, y, line)
        y -= line_height
    return y

def create_pro_pdf(data: dict, score: int, risks: list, warnings: list, color: colors,
                   arbitration_data: dict | None, mass_flags: list):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    w, h = A4
    y = h - 70

    # HEADER
    c.setFillColor(colors.HexColor("#1a237e"))
    c.setFont(FONT_NAME, 26)
    c.drawString(50, y, "OSINT PRO v2.4")
    c.setFont(FONT_NAME, 11)
    c.setFillColor(colors.grey)
    c.drawString(50, y - 22, f"Аналитический отчёт • {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    y -= 70

    # ИНДЕКС БЕЗОПАСНОСТИ
    c.setFont(FONT_NAME, 14)
    c.setFillColor(colors.black)
    c.drawString(50, y, "ИНДЕКС БЕЗОПАСНОСТИ")
    c.setStrokeColor(colors.lightgrey)
    c.roundRect(50, y - 32, 240, 28, 8, stroke=1, fill=0)
    c.setFillColor(color)
    c.roundRect(50, y - 32, 2.4 * score, 28, 8, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 22)
    c.drawString(310, y - 25, f"{score} / 100")
    y -= 80

    # СТАТУС
    status_text, status_emoji = get_company_status(data)
    c.setFont(FONT_NAME, 15)
    c.setFillColor(colors.black)
    c.drawString(50, y, "Статус компании")
    c.setFillColor(colors.red if "ликвидац" in status_text.lower() or "банкрот" in status_text.lower() else colors.green)
    c.drawString(220, y, f"{status_emoji} {status_text}")
    y -= 45

    # РЕКВИЗИТЫ
    c.setFont(FONT_NAME, 13)
    c.setFillColor(colors.black)
    c.drawString(50, y, "Основные реквизиты")
    y -= 25
    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.grey)
    company_name = data.get('short_name') or data.get('full_name') or data.get('name') or "Н/Д"
    director = f"{data.get('chief_position', '')} {data.get('chief', 'Н/Д')}".strip() or "Н/Д"
    fields = [
        ("Полное наименование", company_name),
        ("ИНН", data.get('inn', "Н/Д")),
        ("ОГРН", data.get('ogrn', "Н/Д")),
        ("КПП", data.get('kpp', "Н/Д")),
        ("Руководитель", director),
        ("Дата регистрации", data.get('reg_date', "Н/Д")),
        ("Адрес", data.get('address', "Информация ограничена"))
    ]
    for label, value in fields:
        c.drawString(50, y, label + ":")
        y = draw_multiline(c, 210, y, value, font_size=10, max_width=340)
        y -= 8
    y -= 20

    # МАССОВОСТЬ
    if mass_flags:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.orange)
        c.drawString(50, y, "⚠️ Массовость сведений ЕГРЮЛ")
        y -= 22
        c.setFont(FONT_NAME, 10)
        c.setFillColor(colors.black)
        c.drawString(60, y, f"Обнаружена массовость по: {', '.join(mass_flags)}")
        y -= 30

    # ПРЕДУПРЕЖДЕНИЯ
    if warnings:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.red)
        c.drawString(50, y, "🚨 Критические предупреждения ЕГРЮЛ")
        y -= 22
        c.setFont(FONT_NAME, 10)
        c.setFillColor(colors.black)
        for w in warnings:
            y = draw_multiline(c, 60, y, f"• {w}", max_width=480)
        y -= 15

    # АРБИТРАЖ
    if arbitration_data and isinstance(arbitration_data, dict):
        arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
        if arb_count > 0:
            c.setFont(FONT_NAME, 13)
            c.setFillColor(colors.red)
            c.drawString(50, y, f"⚖️ Арбитражные дела — {arb_count} шт.")
            y -= 30

    # ЗАКЛЮЧЕНИЕ И РИСКИ
    c.setFont(FONT_NAME, 13)
    c.setFillColor(colors.black)
    c.drawString(50, y, "Заключение экспертизы и риски")
    y -= 25
    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.black)
    for risk in risks:
        y = draw_multiline(c, 60, y, f"• {risk}", max_width=480)
        y -= 4

    # РЕКОМЕНДАЦИЯ
    y -= 20
    c.setFont(FONT_NAME, 14)
    c.setFillColor(color)
    c.drawString(50, y, "РЕКОМЕНДАЦИЯ OSINT PRO")
    y -= 22
    c.setFont(FONT_NAME, 11)
    c.setFillColor(colors.black)
    c.drawString(60, y, recommendation)
    y -= 40

    # FOOTER
    c.setFont(FONT_NAME, 8)
    c.setFillColor(colors.grey)
    c.drawString(50, 40, f"OSINT PRO v2.4 • Источники: ЕГРЮЛ, Checko.ru • {datetime.now().strftime('%d.%m.%Y')}")
    c.drawString(400, 40, "Конфиденциально")
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

# ================= HANDLERS =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🚀 **OSINT PRO v2.4**\n\n"
        "Пришлите ИНН или ОГРН.\n"
        "Полный профессиональный отчёт с массовостью, арбитражем и расширенным PDF.",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(F.text)
async def handle_search(message: Message):
    inn = "".join(re.findall(r'\d+', message.text))
    if len(inn) not in (10, 12):
        return

    can_use, remaining = await check_limit(message.from_user.id)
    if not can_use:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")]])
        return await message.answer("🛑 Лимит 3 запроса в день исчерпан.", reply_markup=kb)

    wait = await message.answer("🔍 Глубокий анализ ЕГРЮЛ + арбитраж...")

    async with aiohttp.ClientSession() as session:
        egrul_task = session.get(f"https://egrul.org/short_data/?id={inn}")
        arb_task = get_arbitration_data(inn) if CHECKO_API_KEY else asyncio.sleep(0)
        egrul_resp = await egrul_task
        data = await egrul_resp.json(content_type=None) if egrul_resp.status == 200 else None
        arbitration_data = await arb_task if CHECKO_API_KEY else None

    if not data or not isinstance(data, dict):
        return await wait.edit_text("❌ Данные по ИНН не найдены.")

    await log_usage(message.from_user.id)
    score, risks, warnings, color, recommendation, arbitration_data, mass_flags = get_risk_assessment(data, arbitration_data)
    log_to_sheet(message.from_user.id, inn, score)

    status_text, status_emoji = get_company_status(data)
    company_name = data.get('short_name') or data.get('full_name') or data.get('name') or '—'
    director_info = f"{data.get('chief_position', '')} {data.get('chief', 'Н/Д')}".strip() or "Н/Д"

    res = (
        f"✅ **OSINT PRO v2.4**\n\n"
        f"🏢 `{company_name}`\n"
        f"📋 ИНН `{data.get('inn', inn)}` | ОГРН `{data.get('ogrn', 'Н/Д')}` | КПП `{data.get('kpp', 'Н/Д')}`\n\n"
        f"📌 **Статус:** {status_emoji} {status_text}\n"
        f"📅 Зарегистрирована {calculate_age(data.get('reg_date', ''))}\n"
        f"👤 Руководитель: {director_info}\n"
        f"📍 {data.get('address', 'Информация ограничена')[:140]}\n\n"
        f"🛡️ **Индекс безопасности:** `{score}/100`\n"
        f"📌 **Рекомендация:** {recommendation}\n\n"
        f"Осталось бесплатных запросов сегодня: **{remaining}/3**\n\n"
    )
    if mass_flags:
        res += f"⚠️ **Массовость:** по {', '.join(mass_flags)}\n"
    if arbitration_data:
        arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
        if arb_count > 0:
            res += f"⚖️ **Арбитраж:** {arb_count} дел\n"
    res += "\n📄 **Полный профессиональный отчёт в PDF**"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Скачать PDF", callback_data=f"pdf_{inn}")]])
    await wait.delete()
    await message.answer(res, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("pdf_"))
async def send_pdf(call: CallbackQuery):
    inn = call.data.split("_", 1)[1]
    await call.answer("Генерирую подробный PDF-отчёт...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://egrul.org/short_data/?id={inn}") as resp:
                if resp.status != 200:
                    raise ValueError(f"egrul.org вернул статус {resp.status}")
                data = await resp.json(content_type=None)

        if not isinstance(data, dict) or not data:
            raise ValueError("Получены некорректные данные от egrul.org")

        arbitration_data = await get_arbitration_data(inn) if CHECKO_API_KEY else None

        score, risks, warnings, color, _, arbitration_data, mass_flags = get_risk_assessment(
            data, arbitration_data
        )

        pdf_buffer = create_pro_pdf(data, score, risks, warnings, color, arbitration_data, mass_flags)

        await call.message.answer_document(
            BufferedInputFile(pdf_buffer.read(), filename=f"OSINT_PRO_{inn}.pdf"),
            caption="✅ Подробный аналитический отчёт OSINT PRO v2.4"
        )

    except Exception as e:
        logger.error(f"PDF generation error for INN {inn} | User {call.from_user.id}", exc_info=True)
        
        await call.message.answer(
            "❌ Не удалось сгенерировать PDF-отчёт.\n"
            "Попробуйте позже или напишите администратору."
        )
        
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"❌ Ошибка PDF!\n"
                    f"ИНН: {inn}\n"
                    f"Пользователь: {call.from_user.id} (@{call.from_user.username or '—'})\n"
                    f"Ошибка: {type(e).__name__}: {e}"
                )
            except:
                pass

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
    logger.info("🚀 OSINT PRO v2.4 запущен! (профессиональный PDF)")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
