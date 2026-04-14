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
import pandas as pd
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
from reportlab.platypus import Table, TableStyle
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
    logger.info("✅ Checko API подключён — полный профиль + поиск + арбитраж")
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
        await db.execute('''CREATE TABLE IF NOT EXISTS usage_log
                            (user_id INTEGER, query_date DATE DEFAULT CURRENT_DATE)''')
        try:
            await db.execute("ALTER TABLE usage_log ADD COLUMN inn TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE usage_log ADD COLUMN score INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                            (user_id INTEGER PRIMARY KEY, until_date DATE)''')
        # ================= НОВАЯ ТАБЛИЦА ДЛЯ МОНИТОРИНГА (ЭТАП 2) =================
        await db.execute('''CREATE TABLE IF NOT EXISTS monitored (
                            user_id INTEGER,
                            inn TEXT,
                            last_checked TEXT,
                            last_full_name TEXT,
                            last_status TEXT,
                            last_score INTEGER DEFAULT 0,
                            last_arb_count INTEGER DEFAULT 0,
                            last_director TEXT,
                            PRIMARY KEY (user_id, inn))''')
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
# ================= ADMIN FUNCTIONS =================
async def grant_subscription(user_id: int, days: int):
    until = (date.today() + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO subscriptions (user_id, until_date) VALUES (?, ?)", (user_id, until))
        await db.commit()
async def revoke_subscription(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        await db.commit()
async def get_stats() -> str:
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(DISTINCT user_id) FROM usage_log") as cur:
                total_users = (await cur.fetchone())[0] or 0
            async with db.execute("SELECT COUNT(*) FROM usage_log WHERE query_date = ?", (date.today().isoformat(),)) as cur:
                today_queries = (await cur.fetchone())[0] or 0
            async with db.execute("SELECT COUNT(*) FROM subscriptions WHERE until_date >= ?", (date.today().isoformat(),)) as cur:
                active_subs = (await cur.fetchone())[0] or 0
        return (f"📊 **Статистика OSINT PRO**\n\n"
                f"👥 Всего уникальных пользователей: {total_users}\n"
                f"🔥 Запросов сегодня: {today_queries}\n"
                f"💎 Активных подписок: {active_subs}\n"
                f"🕒 Сейчас: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return "❌ Ошибка получения статистики"
# ================= CACHE =================
async def get_from_cache(inn: str) -> tuple[dict | None, dict | None, str | None]:
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
    if not force_refresh:
        cached_data, cached_arb, cached_at = await get_from_cache(inn)
        if cached_data:
            return cached_data, cached_arb, cached_at
    checko_data = await get_checko_company(inn)
    data = checko_data if checko_data else await get_egrul_data(inn)
    arbitration_data = await get_arbitration_data(inn) if CHECKO_API_KEY else None
    if data:
        await save_to_cache(inn, data, arbitration_data)
        cached_at = datetime.now().isoformat()
    else:
        cached_at = None
    return data, arbitration_data, cached_at
# ================= MONITORING SYSTEM (ЭТАП 2) =================
async def is_monitored(user_id: int, inn: str) -> bool:
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT 1 FROM monitored WHERE user_id = ? AND inn = ?", (user_id, inn)) as cursor:
                return await cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Monitor check error: {e}")
        return False

async def get_user_monitored(user_id: int):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT inn, last_full_name FROM monitored WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                return await cursor.fetchall()
    except Exception as e:
        logger.error(f"Get monitored error: {e}")
        return []

async def add_to_monitoring(user_id: int, inn: str) -> bool:
    try:
        data, arbitration_data, _ = await get_company_data(inn, force_refresh=True)
        if not data:
            return False
        score, _, _, _, _, _, _ = get_risk_assessment(data, arbitration_data)
        status_text, _ = get_company_status(data)
        arb_count = 0
        if arbitration_data and isinstance(arbitration_data, dict):
            arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
        director = safe_get_director(data)
        full_name = data.get('НаимПолн') or data.get('full_name') or "Н/Д"
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """INSERT OR REPLACE INTO monitored 
                   (user_id, inn, last_checked, last_full_name, last_status, last_score, last_arb_count, last_director)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, inn, datetime.now().isoformat(), full_name, status_text, score, arb_count, director)
            )
            await db.commit()
        logger.info(f"✅ Компания {inn} добавлена в мониторинг для пользователя {user_id}")
        return True
    except Exception as e:
        logger.error(f"Add to monitoring error: {e}")
        return False

async def remove_from_monitoring(user_id: int, inn: str):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM monitored WHERE user_id = ? AND inn = ?", (user_id, inn))
            await db.commit()
        logger.info(f"✅ Компания {inn} удалена из мониторинга для пользователя {user_id}")
    except Exception as e:
        logger.error(f"Remove from monitoring error: {e}")

async def check_monitored_companies():
    """Фоновая проверка всех компаний в мониторинге (каждые 4 часа)"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id, inn FROM monitored") as cursor:
                all_monitored = await cursor.fetchall()
    except Exception as e:
        logger.error(f"Monitoring DB error: {e}")
        return

    logger.info(f"🔄 Проверка {len(all_monitored)} компаний в мониторинге...")

    for user_id, inn in all_monitored:
        try:
            data, arbitration_data, _ = await get_company_data(inn, force_refresh=True)
            if not data:
                continue

            score, _, _, _, _, _, _ = get_risk_assessment(data, arbitration_data)
            status_text, _ = get_company_status(data)
            arb_count = 0
            if arbitration_data and isinstance(arbitration_data, dict):
                arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
            director = safe_get_director(data)
            full_name = data.get('НаимПолн') or data.get('full_name') or "Н/Д"

            # Получаем предыдущее состояние
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
                    """SELECT last_status, last_arb_count, last_director 
                       FROM monitored WHERE user_id = ? AND inn = ?""",
                    (user_id, inn)
                ) as cursor:
                    last = await cursor.fetchone()

            changes = []
            if last:
                last_status, last_arb, last_dir = last
                if status_text != last_status:
                    changes.append(f"📌 Статус изменился → {status_text}")
                if arb_count > last_arb:
                    changes.append(f"⚖️ Новые арбитражные дела (+{arb_count - last_arb})")
                if director != last_dir and director != "Н/Д":
                    changes.append(f"👤 Сменился руководитель → {director}")

            # Обновляем состояние в БД
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO monitored 
                       (user_id, inn, last_checked, last_full_name, last_status, last_score, last_arb_count, last_director)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, inn, datetime.now().isoformat(), full_name, status_text, score, arb_count, director)
                )
                await db.commit()

            if changes:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📥 Открыть отчёт", callback_data=f"pdf_{inn}")],
                    [InlineKeyboardButton(text="🔄 Обновить сейчас", callback_data=f"refresh_{inn}")]
                ])
                await bot.send_message(
                    user_id,
                    f"🔔 **OSINT PRO • Обновление мониторинга**\n\n"
                    f"🏢 {full_name}\n"
                    f"📋 ИНН `{inn}`\n\n"
                    f"⚠️ **Обнаружены изменения:**\n" + "\n".join(changes),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )
                logger.info(f"📨 Уведомление отправлено пользователю {user_id} по {inn}")
        except Exception as e:
            logger.error(f"Monitoring check error for {inn} (user {user_id}): {e}")
            continue

async def monitoring_scheduler():
    """Фоновая задача — проверка мониторинга каждые 4 часа"""
    while True:
        await check_monitored_companies()
        await asyncio.sleep(14400)  # 4 часа
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
async def search_by_name(query: str) -> list[dict] | None:
    if not CHECKO_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.checko.ru/v2/search"
            params = {"key": CHECKO_API_KEY, "query": query.strip(), "limit": 10}
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("data", []) if isinstance(data.get("data"), list) else None
    except Exception as e:
        logger.error(f"Search by name error: {e}")
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
def safe_get_okved(data: dict) -> str:
    okved = data.get("ОКВЭД") or data.get("okved") or {}
    if isinstance(okved, dict):
        code = okved.get("Код") or okved.get("code") or "—"
        name = okved.get("Наим") or okved.get("name") or ""
        return f"{code} — {name}"
    return str(okved) or "Н/Д"
def get_arbitration_cases_table(arbitration_data: dict | None) -> list:
    """Возвращает данные для таблицы арбитражных дел (максимум 6 записей)"""
    if not arbitration_data or not isinstance(arbitration_data, dict):
        return []
    cases = arbitration_data.get("cases") or arbitration_data.get("data", {}).get("cases", [])
    if not isinstance(cases, list):
        return []
    table = [["Дата", "Сумма", "Истец", "Ответчик", "Статус"]]
    for case in cases[:6]:
        date_str = case.get("Дата") or case.get("date") or "—"
        amount = case.get("СуммаИск") or case.get("Сумма") or case.get("sum") or "—"
        plaintiff = (case.get("Истец") or case.get("plaintiff") or "—")[:35]
        defendant = (case.get("Ответчик") or case.get("defendant") or "—")[:35]
        status = case.get("Статус") or case.get("status") or "—"
        table.append([date_str, amount, plaintiff, defendant, status])
    return table
# ================= PDF v2.8 (ТАБЛИЦЫ + АРБИТРАЖ) =================
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
    # КЛЮЧЕВЫЕ ФАКТЫ — ТАБЛИЦА (с ОКВЭД)
    c.setFont(FONT_NAME, 13)
    c.setFillColor(colors.black)
    c.drawString(50, y, "Ключевые факты")
    y -= 30
    is_ip_flag = is_individual_entrepreneur(data)
    director = safe_get_director(data)
    branches = safe_get_branches(data)
    okved = safe_get_okved(data)
    if is_ip_flag:
        table_data = [
            ["Параметр", "Значение"],
            ["ИНН", data.get('ИНН', '—')],
            ["ОГРНИП", data.get('ОГРНИП', data.get('ОГРН', '—'))],
            ["ФИО предпринимателя", director],
            ["Дата регистрации", calculate_age(data.get('ДатаРег', ''))],
            ["Адрес", get_formatted_address(data)],
            ["Основной ОКВЭД", okved],
        ]
    else:
        table_data = [
            ["Параметр", "Значение"],
            ["ИНН", data.get('ИНН', '—')],
            ["ОГРН", data.get('ОГРН', '—')],
            ["КПП", data.get('КПП', '—')],
            ["Руководитель", director],
            ["Дата регистрации", calculate_age(data.get('ДатаРег', ''))],
            ["Адрес", get_formatted_address(data)],
            ["Уставный капитал", data.get('УставКапитал', '—')],
            ["Филиалы", f"{branches} шт." if branches else "—"],
            ["Основной ОКВЭД", okved],
        ]
    table = Table(table_data, colWidths=[150, 320])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1a237e")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), FONT_NAME),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
        ('FONTNAME', (0, 1), (-1, -1), FONT_NAME),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    table.wrapOn(c, 50, y)
    table.drawOn(c, 50, y - table._height)
    y -= table._height + 25
    # КОНТАКТЫ
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
    if uchred and uchred.get("ФЛ"):
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.black)
        c.drawString(50, y, "👥 Учредители")
        y -= 25
        founders_table = [["ФИО", "Доля %"]]
        for fl in uchred.get("ФЛ", [])[:6]:
            founders_table.append([fl.get('ФИО', '—'), f"{fl.get('Доля', '—')}%"])
        ft = Table(founders_table, colWidths=[280, 140])
        ft.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1a237e")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
        ]))
        ft.wrapOn(c, 50, y)
        ft.drawOn(c, 50, y - ft._height)
        y -= ft._height + 20
    # РИСКИ
    if mass_flags or warnings or risks:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.orange)
        c.drawString(50, y, "⚠️ Риски и предупреждения")
        y -= 25
        risk_table = [["Тип", "Описание"]]
        for item in warnings + mass_flags + risks:
            risk_table.append(["⚠️", item])
        rt = Table(risk_table, colWidths=[60, 400])
        rt.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#ff9800")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
        ]))
        rt.wrapOn(c, 50, y)
        rt.drawOn(c, 50, y - rt._height)
        y -= rt._height + 20
    # === НОВОЕ: ТАБЛИЦА АРБИТРАЖНЫХ ДЕЛ ===
    arb_table_data = get_arbitration_cases_table(arbitration_data)
    if len(arb_table_data) > 1:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.red)
        c.drawString(50, y, "⚖️ Арбитражные дела (последние)")
        y -= 28
        arb_table = Table(arb_table_data, colWidths=[70, 80, 130, 130, 90])
        arb_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#d32f2f")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        arb_table.wrapOn(c, 50, y)
        arb_table.drawOn(c, 50, y - arb_table._height)
        y -= arb_table._height + 25
    # ЗАКЛЮЧЕНИЕ
    c.setFont(FONT_NAME, 14)
    c.setFillColor(color)
    c.drawString(50, y, "РЕКОМЕНДАЦИЯ OSINT PRO")
    y -= 25
    c.setFont(FONT_NAME, 11)
    c.setFillColor(colors.black)
    c.drawString(60, y, recommendation)
    # ФУТЕР
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
# ================= GOOGLE SHEETS + EXCEL =================
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
async def export_stats_to_excel() -> BytesIO:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM usage_log ORDER BY query_date DESC") as cur:
            usage_rows = await cur.fetchall()
        usage_df = pd.DataFrame(usage_rows, columns=["user_id", "query_date", "inn", "score"])
        async with db.execute("SELECT * FROM subscriptions") as cur:
            sub_rows = await cur.fetchall()
        sub_df = pd.DataFrame(sub_rows, columns=["user_id", "until_date"])
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        usage_df.to_excel(writer, sheet_name="Запросы", index=False)
        sub_df.to_excel(writer, sheet_name="Подписки", index=False)
    buffer.seek(0)
    return buffer
# ================= MASS CHECK (ЭТАП 3) =================
async def handle_mass_check_document(message: Message):
    document = message.document
    if not document.file_name.lower().endswith(('.xlsx', '.csv')):
        return await message.answer("❌ Поддерживаются только файлы **.xlsx** и **.csv**")

    subscribed = await is_subscribed(message.from_user.id)
    if not subscribed:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")]])
        return await message.answer(
            "🛑 **Массовая проверка** доступна только по подписке Pro.\n\n"
            "Безлимит + Excel-отчёт по сотням компаний — 4900 ₽/мес",
            reply_markup=kb
        )

    wait_msg = await message.answer("📤 **Обрабатываю файл...**\nЭто может занять 30–120 секунд в зависимости от количества ИНН.")

    # Скачиваем файл
    file = await bot.get_file(document.file_id)
    file_bytes = BytesIO()
    await bot.download_file(file.file_path, file_bytes)
    file_bytes.seek(0)

    try:
        if document.file_name.lower().endswith('.csv'):
            df = pd.read_csv(file_bytes)
        else:
            df = pd.read_excel(file_bytes)
    except Exception:
        await wait_msg.edit_text("❌ Не удалось прочитать файл. Убедитесь, что формат корректный.")
        return

    # Поиск колонки с ИНН
    inn_col = None
    for col in df.columns:
        col_str = str(col).lower()
        if any(x in col_str for x in ['инн', 'inn', 'tax', 'id']):
            inn_col = col
            break

    if inn_col is None:
        # Если не нашли — берём первую колонку
        df['inn'] = df.iloc[:, 0].astype(str).str.strip()
    else:
        df['inn'] = df[inn_col].astype(str).str.strip()

    # Очищаем и валидируем ИНН
    inns = []
    for val in df['inn']:
        clean = re.sub(r'\D', '', str(val))
        if len(clean) in (10, 12):
            inns.append(clean)
    inns = list(dict.fromkeys(inns))[:100]  # максимум 100, убираем дубли

    if not inns:
        await wait_msg.edit_text("❌ В файле не найдено валидных ИНН (10 или 12 цифр).")
        return

    results = []
    for idx, inn in enumerate(inns, 1):
        data, arbitration_data, _ = await get_company_data(inn)
        if not data:
            results.append({
                "ИНН": inn,
                "Название": "Не найдено",
                "Статус": "—",
                "Индекс безопасности": 0,
                "Рекомендация": "Данные отсутствуют",
                "Арбитражных дел": 0,
                "Руководитель": "—",
                "Дата регистрации": "—"
            })
            continue

        score, _, _, _, recommendation, _, _ = get_risk_assessment(data, arbitration_data)
        name = data.get('НаимСокр') or data.get('full_name') or data.get('НаимПолн') or "—"
        status = get_company_status(data)[0]
        arb_count = len(arbitration_data.get("cases", [])) if arbitration_data and isinstance(arbitration_data, dict) else 0

        results.append({
            "ИНН": inn,
            "Название": name,
            "Статус": status,
            "Индекс безопасности": score,
            "Рекомендация": recommendation,
            "Арбитражных дел": arb_count,
            "Руководитель": safe_get_director(data),
            "Дата регистрации": calculate_age(data.get('ДатаРег', ''))
        })

    # Создаём Excel-отчёт
    result_df = pd.DataFrame(results)
    excel_buffer = BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        result_df.to_excel(writer, sheet_name="OSINT PRO — Массовый отчёт", index=False)
    excel_buffer.seek(0)

    # Статистика
    total = len(results)
    risky = len([r for r in results if r["Индекс безопасности"] < 60])
    summary = (f"✅ **Массовый отчёт OSINT PRO готов!**\n\n"
               f"📊 Проверено компаний: **{total}**\n"
               f"🔴 Высокий риск: **{risky}**\n"
               f"✅ Нормальный/низкий риск: **{total - risky}**")

    await wait_msg.delete()
    await message.answer_document(
        BufferedInputFile(excel_buffer.read(), filename=f"OSINT_PRO_mass_check_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.xlsx"),
        caption=summary
    )
# ================= HANDLERS =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    is_admin = message.from_user.id == ADMIN_CHAT_ID
    text = ("🚀 **OSINT PRO v2.8**\n\n"
            "Поиск **по ИНН** и **по названию**!\n"
            "✅ Таблицы в PDF • Арбитражные дела • ОКВЭД\n\n"
            "📌 **Новые возможности:**\n"
            "• Мониторинг изменений компаний (уведомления)\n"
            "• Массовая проверка по Excel/CSV (до 100 ИНН)\n\n"
            "Пришлите ИНН / ОГРН или название компании")
    if is_admin:
        text += "\n\n👑 **Админ-панель:** /admin"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return await message.answer("⛔️ Доступ запрещён.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📤 Экспорт в Excel", callback_data="admin_export")],
        [InlineKeyboardButton(text="💰 Цена подписки", callback_data="admin_pricing")],
        [InlineKeyboardButton(text="🔑 Выдать подписку", callback_data="admin_grant")],
        [InlineKeyboardButton(text="🚫 Отозвать подписку", callback_data="admin_revoke")],
    ])
    await message.answer("👑 **Админ-панель OSINT PRO**", reply_markup=kb)
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_CHAT_ID: return await call.answer("⛔️ Доступ запрещён.")
    await call.answer()
    stats_text = await get_stats()
    await call.message.edit_text(stats_text, parse_mode=ParseMode.MARKDOWN)
@dp.callback_query(F.data == "admin_export")
async def admin_export(call: CallbackQuery):
    if call.from_user.id != ADMIN_CHAT_ID: return await call.answer("⛔️ Доступ запрещён.")
    await call.answer("📤 Генерирую Excel...")
    try:
        excel_buffer = await export_stats_to_excel()
        await call.message.answer_document(
            BufferedInputFile(excel_buffer.read(), filename=f"OSINT_PRO_статистика_{datetime.now().strftime('%Y-%m-%d')}.xlsx"),
            caption="✅ Полная статистика запросов и подписок"
        )
    except Exception as e:
        logger.error(f"Export error: {e}")
        await call.message.answer("❌ Ошибка генерации Excel")
@dp.callback_query(F.data == "admin_pricing")
async def admin_pricing(call: CallbackQuery):
    if call.from_user.id != ADMIN_CHAT_ID: return await call.answer("⛔️ Доступ запрещён.")
    await call.answer()
    await call.message.edit_text(f"💰 **Текущая цена подписки**\n\n{SUBSCRIPTION_PRICE}\n\nБезлимит + премиум-PDF")
@dp.callback_query(F.data == "admin_grant")
async def admin_grant_start(call: CallbackQuery):
    if call.from_user.id != ADMIN_CHAT_ID: return await call.answer("⛔️ Доступ запрещён.")
    await call.answer()
    await call.message.edit_text("🔑 Отправь мне сообщение в формате:\n/grant <user_id> <дней>")
@dp.callback_query(F.data == "admin_revoke")
async def admin_revoke_start(call: CallbackQuery):
    if call.from_user.id != ADMIN_CHAT_ID: return await call.answer("⛔️ Доступ запрещён.")
    await call.answer()
    await call.message.edit_text("🚫 Отправь мне сообщение в формате:\n/revoke <user_id>")
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_search(message: Message):
    text = message.text.strip()
    inn = "".join(re.findall(r'\d+', text))
    if len(inn) in (10, 12):
        can_use, remaining, is_premium = await check_limit(message.from_user.id)
        if not can_use:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")]])
            return await message.answer("🛑 Лимит 3 запроса в день исчерпан.", reply_markup=kb)
        wait = await message.answer("🔍 Запрашиваю данные...")
        data, arbitration_data, cache_time = await get_company_data(inn)
        if not data:
            return await wait.edit_text("❌ Данные по ИНН не найдены.")
        score, risks, warnings, color, recommendation, arbitration_data, mass_flags = get_risk_assessment(data, arbitration_data)
        await log_usage(message.from_user.id, inn, score, is_premium)
        log_to_sheet(message.from_user.id, inn, score)

        # === МОНИТОРИНГ: проверяем, уже ли в списке ===
        is_mon = await is_monitored(message.from_user.id, inn)
        monitor_text = "❌ Убрать из мониторинга" if is_mon else "📌 Добавить в мониторинг"
        monitor_cb = f"monitor_remove_{inn}" if is_mon else f"monitor_add_{inn}"

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
            [InlineKeyboardButton(text="🔄 Обновить данные", callback_data=f"refresh_{inn}")],
            [InlineKeyboardButton(text=monitor_text, callback_data=monitor_cb)]
        ])
        await wait.delete()
        await message.answer(res, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # Поиск по названию
    wait = await message.answer("🔎 Ищу компании по названию...")
    results = await search_by_name(text)
    if not results:
        return await wait.edit_text("❌ Компании с таким названием не найдены.\n\nПопробуйте уточнить название или введите ИНН.")
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for item in results[:8]:
        inn_found = item.get("ИНН") or item.get("inn")
        name = item.get("НаимСокр") or item.get("НаимПолн") or item.get("name") or "Без названия"
        if inn_found:
            kb.inline_keyboard.append([InlineKeyboardButton(
                text=f"📋 {name[:45]}... (ИНН {inn_found})",
                callback_data=f"select_{inn_found}"
            )])
    await wait.edit_text(f"✅ Найдено {len(results)} совпадений.\nВыберите компанию:", reply_markup=kb)
@dp.callback_query(F.data.startswith("select_"))
async def handle_select(call: CallbackQuery):
    inn = call.data.split("_", 1)[1]
    await call.answer("Открываю отчёт...")
    try:
        data, arbitration_data, cache_time = await get_company_data(inn)
        if not data:
            return await call.message.answer("❌ Данные не найдены.")
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
        logger.error(f"Select error: {e}")
        await call.message.answer("❌ Ошибка открытия отчёта.")
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
    await call.message.answer("✅ **Кэш очищен!** Пришлите ИНН / ОГРН ещё раз.")
# ================= НОВЫЕ CALLBACK'И МОНИТОРИНГА =================
@dp.callback_query(F.data.startswith("monitor_add_"))
async def handle_monitor_add(call: CallbackQuery):
    inn = call.data.split("_", 2)[2]
    success = await add_to_monitoring(call.from_user.id, inn)
    if success:
        await call.answer("✅ Добавлено в мониторинг!\nИзменения будут приходить автоматически.")
    else:
        await call.answer("❌ Не удалось добавить в мониторинг")

@dp.callback_query(F.data.startswith("monitor_remove_"))
async def handle_monitor_remove(call: CallbackQuery):
    inn = call.data.split("_", 2)[2]
    await remove_from_monitoring(call.from_user.id, inn)
    await call.answer("✅ Убрано из мониторинга")
# ================= MASS CHECK DOCUMENT HANDLER =================
@dp.message(F.document)
async def handle_document(message: Message):
    await handle_mass_check_document(message)
@dp.callback_query(F.data == "buy")
async def buy_subscription(call: CallbackQuery):
    await call.answer()
    await call.message.answer(f"💰 **Подписка OSINT PRO**\n\nБезлимит + премиум-отчёты — {SUBSCRIPTION_PRICE}\n\nПосле оплаты напишите @ваш_логин с чеком.")
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
@dp.message(Command("monitor"))
async def cmd_monitor(message: Message):
    monitored = await get_user_monitored(message.from_user.id)
    if not monitored:
        return await message.answer("📭 У вас пока нет компаний в мониторинге.\n\nДобавляйте их из отчёта кнопкой «📌 Добавить в мониторинг»")
    text = "📌 **Ваши компании в мониторинге** (уведомления каждые 4 часа):\n\n"
    for inn, name in monitored:
        text += f"• `{inn}` — {name[:60]}...\n"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)
@dp.message(Command("grant"))
async def cmd_grant(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID: return await message.answer("⛔️ Доступ запрещён.")
    try:
        _, user_id_str, days_str = message.text.split()
        await grant_subscription(int(user_id_str), int(days_str))
        await message.answer(f"✅ Подписка выдана пользователю {user_id_str} на {days_str} дней")
    except:
        await message.answer("❌ Формат: /grant <user_id> <дней>")
@dp.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID: return await message.answer("⛔️ Доступ запрещён.")
    try:
        _, user_id_str = message.text.split()
        await revoke_subscription(int(user_id_str))
        await message.answer(f"✅ Подписка отозвана у пользователя {user_id_str}")
    except:
        await message.answer("❌ Формат: /revoke <user_id>")
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID: return await message.answer("⛔️ Доступ запрещён.")
    stats_text = await get_stats()
    await message.answer(stats_text, parse_mode=ParseMode.MARKDOWN)
@dp.message(Command("pricing"))
async def cmd_pricing(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID: return await message.answer("⛔️ Доступ запрещён.")
    await message.answer(f"💰 **Текущая цена подписки**\n\n{SUBSCRIPTION_PRICE}\n\nБезлимит + премиум-PDF")
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
    # Запускаем мониторинг в фоне (ЭТАП 2)
    asyncio.create_task(monitoring_scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=WEBHOOK_URL)
    logger.info("🚀 OSINT PRO v2.8-fix запущен с МОНИТОРИНГОМ компаний + МАССОВОЙ проверкой по Excel!")
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()
    logger.info("✅ Webhook установлен + мониторинг запущен")
    await asyncio.Event().wait()
if __name__ == "__main__":
    asyncio.run(main())
