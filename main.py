import os
import asyncio
import logging
import re
import json
from datetime import datetime, date, timedelta
from io import BytesIO
import aiohttp
import aiosqlite
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile, Update
from aiogram.filters import CommandStart, Command
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
SUBSCRIPTION_PRICE = "4900 ₽/мес"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=TOKEN)
dp = Dispatcher()
if CHECKO_API_KEY:
    logger.info("✅ Checko API подключён — полный профиль компании")
else:
    logger.warning("⚠️ CHECKO_API_KEY не задан — используется только ЕГРЮЛ")
# ================= FONT =================
FONT_NAME = "DejaVuSans"
FONT_PATH = "DejaVuSans.ttf"
if os.path.exists(FONT_PATH):
    pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
    logger.info("✅ Шрифт DejaVuSans загружен")
else:
    logger.error("❌ DejaVuSans.ttf не найден!")
    FONT_NAME = "Helvetica"
# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Основная таблица логов (с миграцией для новых колонок)
        await db.execute('''CREATE TABLE IF NOT EXISTS usage_log 
                            (user_id INTEGER, query_date DATE DEFAULT CURRENT_DATE)''')
        try:
            await db.execute("ALTER TABLE usage_log ADD COLUMN inn TEXT")
        except Exception:
            pass  # колонка уже существует
        try:
            await db.execute("ALTER TABLE usage_log ADD COLUMN score INTEGER DEFAULT 0")
        except Exception:
            pass  # колонка уже существует

        await db.execute('''CREATE TABLE IF NOT EXISTS subscriptions 
                            (user_id INTEGER PRIMARY KEY, until_date DATE)''')

        # Новый кэш (TTL 1 час)
        await db.execute('''CREATE TABLE IF NOT EXISTS cache (
                            inn TEXT PRIMARY KEY,
                            data TEXT,
                            arbitration_data TEXT,
                            cached_at TEXT)''')

        await db.commit()

async def is_subscribed(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT until_date FROM subscriptions WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False
            return datetime.strptime(row[0], '%Y-%m-%d').date() >= date.today()

async def check_limit(user_id: int) -> tuple[bool, int, bool]:
    subscribed = await is_subscribed(user_id)
    if subscribed:
        return True, 999, True
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            today = date.today().isoformat()
            async with db.execute("SELECT COUNT(*) FROM usage_log WHERE user_id = ? AND query_date = ?", (user_id, today)) as cursor:
                row = await cursor.fetchone()
                used = row[0] if row else 0
                remaining = max(0, FREE_LIMIT - used)
                return used < FREE_LIMIT, remaining, False
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return True, FREE_LIMIT, False

async def log_usage(user_id: int, inn: str, score: int, is_premium: bool):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO usage_log (user_id, inn, score) VALUES (?, ?, ?)",
            (user_id, inn, score)
        )
        await db.commit()
    if is_premium:
        logger.info(f"Платный запрос от {user_id} по ИНН {inn} (score: {score})")

async def grant_subscription(user_id: int, days: int):
    until = (date.today() + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO subscriptions (user_id, until_date) VALUES (?, ?)", (user_id, until))
        await db.commit()

async def revoke_subscription(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        await db.commit()

# ================= CACHE =================
async def get_from_cache(inn: str) -> tuple[dict | None, dict | None, str | None]:
    """Возвращает (data, arbitration_data, cached_at) если кэш свежий (TTL 1 час)"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                """SELECT data, arbitration_data, cached_at 
                   FROM cache 
                   WHERE inn = ? 
                   AND datetime('now') <= datetime(cached_at, '+1 hour')""",
                (inn,)
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    data = json.loads(row[0])
                    arb = json.loads(row[1]) if row[1] else None
                    return data, arb, row[2]
    except Exception as e:
        logger.error(f"Cache read error: {e}")
    return None, None, None

async def save_to_cache(inn: str, data: dict | None, arbitration_data: dict | None):
    if not data:
        return
    try:
        data_json = json.dumps(data)
        arb_json = json.dumps(arbitration_data) if arbitration_data else None
        cached_at = datetime.now().isoformat()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """INSERT OR REPLACE INTO cache (inn, data, arbitration_data, cached_at)
                   VALUES (?, ?, ?, ?)""",
                (inn, data_json, arb_json, cached_at)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Cache save error: {e}")

async def get_company_data(inn: str, force_refresh: bool = False) -> tuple[dict | None, dict | None, str | None]:
    """Единая точка получения данных: кэш → Checko → ЕГРЮЛ fallback + сохранение в кэш"""
    if not force_refresh:
        cached_data, cached_arb, cached_at = await get_from_cache(inn)
        if cached_data:
            return cached_data, cached_arb, cached_at

    # Свежие данные
    checko_data = await get_checko_company(inn)
    data = checko_data if checko_data else await get_egrul_data(inn)
    arbitration_data = await get_arbitration_data(inn) if CHECKO_API_KEY else None

    if data:
        await save_to_cache(inn, data, arbitration_data)
        cached_at = datetime.now().isoformat()
    else:
        cached_at = None

    return data, arbitration_data, cached_at

# ================= API =================
async def get_checko_company(inn: str) -> dict | None:
    if not CHECKO_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.checko.ru/v2/company"
            params = {"key": CHECKO_API_KEY, "inn": inn}
            async with session.get(url, params=params, timeout=12) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("data") if data.get("meta", {}).get("status") == "ok" else None
    except Exception as e:
        logger.error(f"Checko error: {e}")
        return None

async def get_egrul_data(inn: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://egrul.org/short_data/?id={inn}", timeout=8) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
    except:
        pass
    return None

async def get_arbitration_data(inn: str) -> dict | None:
    if not CHECKO_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.checko.ru/v2/legal-cases"
            params = {"key": CHECKO_API_KEY, "inn": inn}
            async with session.get(url, params=params, timeout=8) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception as e:
        logger.error(f"Arbitration error: {e}")
        return None

# ================= HELPERS =================
def get_formatted_address(data: dict) -> str:
    if isinstance(data.get("ЮрАдрес"), dict):
        return data["ЮрАдрес"].get("АдресРФ") or data.get("address", "Н/Д")
    return data.get("address", "Н/Д")

def calculate_age(reg_date_str: str) -> str:
    if not reg_date_str:
        return "Н/Д"
    try:
        reg_date = datetime.strptime(reg_date_str[:10], '%Y-%m-%d')
        delta = datetime.now() - reg_date
        years = delta.days // 365
        months = (delta.days % 365) // 30
        return f"{reg_date.strftime('%d.%m.%Y')} ({years} лет {months} мес.)"
    except:
        return reg_date_str

def get_company_status(data: dict) -> tuple[str, str]:
    status = str(data.get('Статус', {}).get('Наим') or data.get('status_text') or data.get('status') or "Действует").lower()
    if any(word in status for word in ["ликвидац", "прекращ", "ликвидирован"]):
        return "В процессе ликвидации / ликвидирована", "🚨"
    if any(word in status for word in ["банкрот", "банкротство"]):
        return "В процедуре банкротства", "❌"
    if any(word in status for word in ["недейств", "исключен"]):
        return "Недействующий статус", "⚠️"
    return "Действует", "✅"

def get_risk_assessment(data: dict, arbitration_data: dict | None = None):
    score = 100
    risk_factors = []
    warnings = []
    mass_flags = []
    reg_date_str = data.get('ДатаРег') or data.get('reg_date', '')
    if reg_date_str:
        try:
            reg_date = datetime.strptime(reg_date_str[:10], '%Y-%m-%d')
            years = (datetime.now() - reg_date).days / 365.25
            if years < 0.5:
                score -= 50
                risk_factors.append("🚨 Крайне молодая компания")
            elif years < 1:
                score -= 35
                risk_factors.append("⚠️ Компания меньше года")
            elif years < 3:
                score -= 15
                risk_factors.append("🟡 Молодая компания")
        except:
            pass
    if data.get("ЮрАдрес", {}).get("МассАдрес"):
        score -= 30
        risk_factors.append("🚨 Массовый юридический адрес")
        mass_flags.append("Адрес")
    arb_count = 0
    if arbitration_data and isinstance(arbitration_data, dict):
        arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
        if arb_count > 0:
            score -= min(45, arb_count * 8)
            risk_factors.append(f"⚖️ Арбитражные дела: {arb_count} шт.")
    if score > 85 and not risk_factors:
        risk_factors.append("✅ Критических рисков не обнаружено")
    color = colors.green if score > 75 else colors.orange if score > 45 else colors.red
    recommendation = "✅ Рекомендуется к работе" if score > 80 else "🟡 Требует дополнительной проверки" if score >= 60 else "🚫 Высокий риск!"
    return score, risk_factors, warnings, color, recommendation, arbitration_data, mass_flags

# Новые безопасные хелперы (устойчивость к изменениям API Checko)
def is_individual_entrepreneur(data: dict) -> bool:
    if not data:
        return False
    name = (data.get("НаимПолн") or data.get("full_name") or "").lower()
    ogrn = data.get("ОГРН") or data.get("ОГРНИП") or ""
    return "индивидуальный предприниматель" in name or name.startswith("ип ") or len(str(ogrn)) == 15

def safe_get_director(data: dict) -> str:
    ruk = data.get("Руковод")
    if isinstance(ruk, list) and ruk:
        return ruk[0].get("ФИО") or ruk[0].get("Наим") or "Н/Д"
    if isinstance(ruk, dict):
        return ruk.get("ФИО") or ruk.get("Наим") or "Н/Д"
    return "Н/Д"

def safe_get_branches(data: dict) -> int:
    fil = data.get("Филиалы")
    if isinstance(fil, list):
        return len(fil)
    return 0

def safe_get_founders_count(data: dict) -> int:
    uch = data.get("Учред", {}) or {}
    fl = len(uch.get("ФЛ", [])) if isinstance(uch.get("ФЛ"), list) else 0
    org = len(uch.get("РосОрг", [])) if isinstance(uch.get("РосОрг"), list) else 0
    return fl + org

# ================= PDF v2.8 (улучшено) =================
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
                   recommendation: str, arbitration_data: dict | None, mass_flags: list,
                   is_premium: bool, cache_time: str | None = None):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    y = A4[1] - 50

    c.setFillColor(colors.HexColor("#1a237e"))
    c.setFont(FONT_NAME, 26)
    c.drawString(50, y, "OSINT PRO v2.8")
    if is_premium:
        c.setFillColor(colors.HexColor("#00b300"))
        c.drawString(380, y - 5, "PREMIUM")
    c.setFont(FONT_NAME, 11)
    c.setFillColor(colors.grey)
    c.drawString(50, y - 28, f"ПРОФЕССИОНАЛЬНЫЙ АНАЛИТИЧЕСКИЙ ОТЧЁТ • {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    y -= 75

    full_name = data.get('НаимПолн') or data.get('full_name') or "Н/Д"
    c.setFont(FONT_NAME, 16)
    c.setFillColor(colors.black)
    y = draw_multiline(c, 50, y, full_name, font_size=16, max_width=480, line_height=20)
    y -= 35

    # ИНДЕКС БЕЗОПАСНОСТИ
    c.setFont(FONT_NAME, 14)
    c.setFillColor(colors.black)
    c.drawString(50, y, "ИНДЕКС БЕЗОПАСНОСТИ")
    c.setStrokeColor(colors.lightgrey)
    c.roundRect(50, y - 38, 250, 38, 8, stroke=1, fill=0)
    c.setFillColor(color)
    c.roundRect(50, y - 38, 2.4 * score, 38, 8, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 28)
    c.drawString(320, y - 32, f"{score}/100")
    y -= 75

    status_text, status_emoji = get_company_status(data)
    c.setFont(FONT_NAME, 14)
    c.setFillColor(colors.black)
    c.drawString(50, y, "Статус компании")
    c.setFillColor(colors.green if status_emoji == "✅" else colors.red)
    c.drawString(220, y, f"{status_emoji} {status_text}")
    y -= 45

    # КЛЮЧЕВЫЕ ФАКТЫ (улучшенный табличный вид + поддержка ИП)
    c.setFont(FONT_NAME, 13)
    c.setFillColor(colors.black)
    c.drawString(50, y, "Ключевые факты")
    y -= 25

    is_ip_flag = is_individual_entrepreneur(data)
    director = safe_get_director(data)
    branches = safe_get_branches(data)

    if is_ip_flag:
        fields = [
            ("ИНН", data.get('ИНН', '—')),
            ("ОГРНИП", data.get('ОГРНИП', data.get('ОГРН', '—'))),
            ("ФИО предпринимателя", director),
            ("Дата регистрации", calculate_age(data.get('ДатаРег', ''))),
            ("Адрес", get_formatted_address(data)),
        ]
    else:
        fields = [
            ("ИНН", data.get('ИНН', '—')),
            ("ОГРН", data.get('ОГРН', '—')),
            ("КПП", data.get('КПП', '—')),
            ("Руководитель", director),
            ("Дата регистрации", calculate_age(data.get('ДатаРег', ''))),
            ("Адрес", get_formatted_address(data)),
            ("Уставный капитал", data.get('УставКапитал', '—')),
            ("Филиалы", f"{branches} шт." if branches else "—"),
        ]

    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.grey)
    for label, value in fields:
        c.drawString(50, y, f"{label}:")
        y = draw_multiline(c, 210, y, str(value), max_width=340)
        y -= 8
    y -= 25

    # === КОНТАКТЫ (уже исправлено ранее) ===
    contacts = data.get("Контакты") or []
    if isinstance(contacts, dict):
        flat = []
        for k, v in contacts.items():
            if isinstance(v, list):
                flat.extend([f"{k}: {item}" for item in v if item])
            elif v:
                flat.append(f"{k}: {v}")
        contacts = flat
    if contacts:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.blue)
        c.drawString(50, y, "📞 Контакты")
        y -= 22
        c.setFont(FONT_NAME, 10)
        c.setFillColor(colors.black)
        for contact in contacts[:8]:
            y = draw_multiline(c, 60, y, f"• {contact}", max_width=480)
            y -= 5
        y -= 15

    # УЧРЕДИТЕЛИ
    uchred = data.get("Учред", {})
    if uchred:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.black)
        c.drawString(50, y, "👥 Учредители")
        y -= 22
        c.setFont(FONT_NAME, 9)
        for fl in uchred.get("ФЛ", [])[:6]:
            y = draw_multiline(c, 60, y, f"• {fl.get('ФИО', '—')} — {fl.get('Доля', '—')}%", max_width=480)
            y -= 4
        y -= 15

    # РИСКИ
    if mass_flags or warnings:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.orange)
        c.drawString(50, y, "⚠️ Сведения ЕГРЮЛ о рисках")
        y -= 22
        c.setFont(FONT_NAME, 10)
        c.setFillColor(colors.black)
        for w in warnings + mass_flags:
            y = draw_multiline(c, 60, y, f"• {w}", max_width=480)
            y -= 6
        y -= 15

    # АРБИТРАЖ
    if arbitration_data and isinstance(arbitration_data, dict):
        arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
        if arb_count > 0:
            c.setFont(FONT_NAME, 13)
            c.setFillColor(colors.red)
            c.drawString(50, y, f"⚖️ Арбитражные дела — {arb_count} шт.")
            y -= 30

    # ЗАКЛЮЧЕНИЕ
    c.setFont(FONT_NAME, 13)
    c.setFillColor(colors.black)
    c.drawString(50, y, "Экспертное заключение и риски")
    y -= 22
    c.setFont(FONT_NAME, 10)
    c.setFillColor(colors.black)
    for risk in risks:
        y = draw_multiline(c, 60, y, f"• {risk}", max_width=480)
        y -= 6
    y -= 20

    c.setFont(FONT_NAME, 14)
    c.setFillColor(color)
    c.drawString(50, y, "РЕКОМЕНДАЦИЯ OSINT PRO")
    y -= 25
    c.setFont(FONT_NAME, 11)
    c.setFillColor(colors.black)
    c.drawString(60, y, recommendation)

    # ФУТЕР + информация о кэше
    c.setFont(FONT_NAME, 8)
    c.setFillColor(colors.grey)
    footer = f"OSINT PRO v2.8 • Checko.ru + ЕГРЮЛ"
    if cache_time:
        try:
            dt = datetime.fromisoformat(cache_time.replace("Z", "+00:00"))
            footer += f" • Кэш: {dt.strftime('%d.%m.%Y %H:%M')}"
        except:
            pass
    c.drawString(50, 40, footer)
    c.drawString(380, 40, "Конфиденциально")
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

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
        worksheet.append_row([now.strftime("%d.%m.%Y %H:%M:%S"), str(user_id), inn, f"score:{score}", "Бесплатный", now.strftime("%H:%M:%S")])
    except Exception as e:
        logger.error(f"Sheet write error: {e}")

# ================= HANDLERS =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🚀 **OSINT PRO v2.8**\n\n"
        "Полный анализ компаний и **ИП** из Checko.ru + ЕГРЮЛ\n"
        "✅ Кэширование данных (1 час)\n"
        "📖 /history — ваша история запросов\n\n"
        "Пришлите ИНН или ОГРН (10 или 12 цифр)",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(Command("history"))
async def cmd_history(message: Message):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT query_date, inn, score FROM usage_log "
                "WHERE user_id = ? ORDER BY rowid DESC LIMIT 10",
                (message.from_user.id,)
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return await message.answer("📭 История запросов пуста.")
        text = "📖 **Ваша история запросов (последние 10):**\n\n"
        for qdate, inn_val, sc in rows:
            inn_val = inn_val or "—"
            text += f"• `{qdate}` | ИНН `{inn_val}` | Индекс: `{sc}/100`\n"
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"History error: {e}")
        await message.answer("❌ Не удалось загрузить историю.")

@dp.message(F.text)
async def handle_search(message: Message):
    inn = "".join(re.findall(r'\d+', message.text))
    if len(inn) not in (10, 12):
        return

    can_use, remaining, is_premium = await check_limit(message.from_user.id)
    if not can_use:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")]])
        return await message.answer("🛑 Лимит 3 запроса в день исчерпан.", reply_markup=kb)

    wait = await message.answer("🔍 Запрашиваю данные из Checko.ru...")

    data, arbitration_data, cache_time = await get_company_data(inn)
    if not data:
        return await wait.edit_text("❌ Данные по ИНН не найдены.")

    score, risks, warnings, color, recommendation, arbitration_data, mass_flags = get_risk_assessment(data, arbitration_data)

    await log_usage(message.from_user.id, inn, score, is_premium)
    log_to_sheet(message.from_user.id, inn, score)

    is_ip_flag = is_individual_entrepreneur(data)
    company_name = data.get('НаимСокр') or data.get('short_name') or data.get('full_name') or '—'
    director = safe_get_director(data)
    founders_count = safe_get_founders_count(data)

    res = f"✅ **OSINT PRO v2.8**{' PREM' if is_premium else ''}\n\n"
    res += f"🏢 {'ИП' if is_ip_flag else 'ЮЛ'} `{company_name}`\n"
    res += f"📋 ИНН `{data.get('ИНН', inn)}` | ОГРН `{data.get('ОГРН', '—')}`\n\n"
    res += f"📌 **Статус:** {get_company_status(data)[1]} {get_company_status(data)[0]}\n"
    res += f"📅 Зарегистрирована {calculate_age(data.get('ДатаРег', ''))}\n"
    res += f"👤 Руководитель: {director}\n"
    res += f"📍 {get_formatted_address(data)[:160]}...\n"
    if founders_count:
        res += f"👥 Учредителей: {founders_count} шт.\n"
    if data.get("Контакты"):
        res += f"📞 Контакты: {len(data.get('Контакты', []))} шт.\n"
    res += f"\n🛡️ **Индекс безопасности:** `{score}/100`\n"
    res += f"📌 **Рекомендация:** {recommendation}\n\n"
    if not is_premium:
        res += f"Осталось бесплатных запросов: **{remaining}/3**\n\n"
    res += "📄 **Полный профессиональный отчёт в PDF**"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать PDF", callback_data=f"pdf_{inn}")],
        [InlineKeyboardButton(text="🔄 Обновить данные", callback_data=f"refresh_{inn}")]
    ])

    await wait.delete()
    await message.answer(res, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("pdf_"))
async def send_pdf(call: CallbackQuery):
    inn = call.data.split("_", 1)[1]
    await call.answer("Генерирую PDF...")

    try:
        data, arbitration_data, cache_time = await get_company_data(inn)
        if not data:
            raise ValueError("Нет данных")

        is_premium = await is_subscribed(call.from_user.id)
        score, risks, warnings, color, recommendation, arbitration_data, mass_flags = get_risk_assessment(data, arbitration_data)

        pdf_buffer = create_pro_pdf(
            data, score, risks, warnings, color, recommendation,
            arbitration_data, mass_flags, is_premium, cache_time
        )

        await call.message.answer_document(
            BufferedInputFile(pdf_buffer.read(), filename=f"OSINT_PRO_{inn}_v2.8.pdf"),
            caption="✅ Подробный профессиональный отчёт OSINT PRO v2.8"
        )
    except Exception as e:
        logger.error(f"PDF error INN {inn}", exc_info=True)
        await call.message.answer("❌ Не удалось сгенерировать PDF. Попробуйте позже.")

@dp.callback_query(F.data.startswith("refresh_"))
async def handle_refresh(call: CallbackQuery):
    inn = call.data.split("_", 1)[1]
    await call.answer("🔄 Очищаем кэш...")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM cache WHERE inn = ?", (inn,))
        await db.commit()

    await call.message.answer(
        "✅ **Кэш очищен!**\n\n"
        "Пришлите ИНН / ОГРН ещё раз — данные будут запрошены заново из Checko.ru"
    )

@dp.callback_query(F.data == "buy")
async def buy_subscription(call: CallbackQuery):
    await call.answer()
    await call.message.answer(f"💰 **Подписка OSINT PRO**\n\nБезлимит + премиум-отчёты — {SUBSCRIPTION_PRICE}\n\nПосле оплаты напишите @ваш_логин с чеком.")

# ================= WEBHOOK =================
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

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=WEBHOOK_URL)
    logger.info("🚀 OSINT PRO v2.8-fix запущен!")
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()
    logger.info("✅ Webhook установлен")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
