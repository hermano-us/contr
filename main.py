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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
# ================= CONFIG =================
TOKEN = os.environ.get("TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 0))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SHEET_ID = os.environ.get("SHEET_ID")
CHECKO_API_KEY = os.environ.get("CHECKO_API_KEY")
# ================= AI CONFIG (Gemini — бесплатный + OpenAI/XAI fallback) =================
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()
AI_API_KEY = os.environ.get("AI_API_KEY")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-2.5-flash")
DB_NAME = "osint_pro.db"
FREE_LIMIT = 3
SUBSCRIPTION_PRICE = "4900 ₽/мес"
MONITORING_INTERVAL_HOURS = 4
VERSION = "2.9 PREMIUM"
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)
bot = Bot(token=TOKEN)
dp = Dispatcher()
if CHECKO_API_KEY:
    logger.info("✅ Checko API подключён — полный доступ к арбитражу и данным")
else:
    logger.warning("⚠️ CHECKO_API_KEY не задан — некоторые функции ограничены")
if AI_API_KEY:
    logger.info(f"✅ AI подключён → {AI_PROVIDER.upper()} / {AI_MODEL} (кэш 24ч)")
else:
    logger.warning("⚠️ AI_API_KEY не задан — AI-анализ отключён")
# ================= FONT =================
FONT_NAME = "DejaVuSans"
FONT_PATH = "DejaVuSans.ttf"
if os.path.exists(FONT_PATH):
    pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
    logger.info("✅ Шрифт DejaVuSans загружен успешно")
else:
    logger.error("❌ DejaVuSans.ttf не найден! Используем Helvetica")
    FONT_NAME = "Helvetica"
# ================= DATABASE =================
async def init_db():
    """Инициализация всех таблиц + миграции + индексы для высокой производительности"""
    async with aiosqlite.connect(DB_NAME) as db:
        # usage_log
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
        # subscriptions
        await db.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                            (user_id INTEGER PRIMARY KEY, until_date DATE)''')
        # monitored companies
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
        # AI cache (24 часа)
        await db.execute('''CREATE TABLE IF NOT EXISTS ai_cache (
                            inn TEXT PRIMARY KEY,
                            summary TEXT,
                            created_at TEXT)''')
        # Главный кэш компаний (1 час)
        await db.execute('''CREATE TABLE IF NOT EXISTS cache (
                            inn TEXT PRIMARY KEY,
                            data TEXT,
                            arbitration_data TEXT,
                            cached_at TEXT)''')
        # НОВОЕ: История изменений компании для графиков и таймлайна в PDF (TOP-3)
        await db.execute('''CREATE TABLE IF NOT EXISTS company_history (
                            inn TEXT,
                            timestamp TEXT,
                            score INTEGER,
                            arb_count INTEGER DEFAULT 0,
                            status TEXT,
                            director TEXT,
                            PRIMARY KEY (inn, timestamp))''')
        # Индексы — критично для скорости при 1000+ пользователях
        await db.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_user_date ON usage_log(user_id, query_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_monitored_user ON monitored(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_cache_inn ON ai_cache(inn)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_inn ON cache(inn)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_history_inn ON company_history(inn)")
        await db.commit()
    logger.info("✅ База данных инициализирована (все таблицы + индексы + company_history)")
async def is_subscribed(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT until_date FROM subscriptions WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False
            return datetime.strptime(row[0], '%Y-%m-%d').date() >= date.today()
async def check_limit(user_id: int) -> tuple[bool, int, bool]:
    """Проверка лимита (премиум = ∞)"""
    subscribed = await is_subscribed(user_id)
    if subscribed:
        return True, 999, True
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            today = date.today().isoformat()
            async with db.execute(
                "SELECT COUNT(*) FROM usage_log WHERE user_id = ? AND query_date = ?",
                (user_id, today)
            ) as cursor:
                row = await cursor.fetchone()
                used = row[0] if row else 0
                remaining = max(0, FREE_LIMIT - used)
                return used < FREE_LIMIT, remaining, False
    except Exception as e:
        logger.error(f"DB Error in check_limit: {e}")
        return True, FREE_LIMIT, False
async def log_usage(user_id: int, inn: str, score: int, is_premium: bool):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO usage_log (user_id, inn, score) VALUES (?, ?, ?)",
            (user_id, inn, score)
        )
        await db.commit()
    if is_premium:
        logger.info(f"💎 Платный запрос | user={user_id} | ИНН={inn} | score={score}")
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
        return (f"📊 **Статистика OSINT PRO v{VERSION}**\n\n"
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
# ================= HISTORY FOR PDF GRAPHS (TOP-3) =================
async def save_to_history(inn: str, score: int, arb_count: int, status: str, director: str):
    """Сохраняет точку истории только если прошло ≥30 минут с последнего сохранения (чтобы не раздувать БД)"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            # Проверяем время последней записи
            async with db.execute(
                "SELECT timestamp FROM company_history WHERE inn = ? ORDER BY timestamp DESC LIMIT 1",
                (inn,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    last_ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                    if (datetime.now() - last_ts).total_seconds() < 1800: # 30 минут
                        return
            await db.execute(
                """INSERT INTO company_history
                   (inn, timestamp, score, arb_count, status, director)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (inn, datetime.now().isoformat(), score, arb_count, status, director)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"History save error for {inn}: {e}")
async def get_company_history(inn: str, limit: int = 12) -> list:
    """Возвращает историю по ИНН (новейшие сверху) для графиков и таймлайна в PDF"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                """SELECT timestamp, score, arb_count, status, director
                   FROM company_history
                   WHERE inn = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (inn, limit)
            ) as cursor:
                return await cursor.fetchall()
    except Exception as e:
        logger.error(f"History read error: {e}")
        return []
# ================= MONITORING SYSTEM =================
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
        # НОВОЕ: сохраняем историю для графиков
        await save_to_history(inn, score, arb_count, status_text, director)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """INSERT OR REPLACE INTO monitored
                   (user_id, inn, last_checked, last_full_name, last_status, last_score, last_arb_count, last_director)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, inn, datetime.now().isoformat(), full_name, status_text, score, arb_count, director)
            )
            await db.commit()
        return True
    except Exception as e:
        logger.error(f"Add to monitoring error: {e}")
        return False
async def remove_from_monitoring(user_id: int, inn: str):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM monitored WHERE user_id = ? AND inn = ?", (user_id, inn))
            await db.commit()
    except Exception as e:
        logger.error(f"Remove from monitoring error: {e}")
async def check_monitored_companies():
    """Проверка всех компаний в мониторинге + уведомления об изменениях"""
    start = datetime.now()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id, inn FROM monitored") as cursor:
                all_monitored = await cursor.fetchall()
    except Exception as e:
        logger.error(f"Monitoring DB error: {e}")
        return
    logger.info(f"🔄 Запущена проверка мониторинга: {len(all_monitored)} компаний...")
    changes_count = 0
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
            # НОВОЕ: сохраняем историю для графиков
            await save_to_history(inn, score, arb_count, status_text, director)
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
                    changes.append(f"📌 Статус → {status_text}")
                if arb_count > last_arb:
                    changes.append(f"⚖️ +{arb_count - last_arb} арбитражных дел")
                if director != last_dir and director != "Н/Д":
                    changes.append(f"👤 Новый руководитель → {director}")
            # Обновляем данные
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO monitored
                       (user_id, inn, last_checked, last_full_name, last_status, last_score, last_arb_count, last_director)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, inn, datetime.now().isoformat(), full_name, status_text, score, arb_count, director)
                )
                await db.commit()
            if changes:
                changes_count += 1
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📥 Открыть отчёт", callback_data=f"pdf_{inn}")],
                    [InlineKeyboardButton(text="🔄 Обновить сейчас", callback_data=f"refresh_{inn}")]
                ])
                await bot.send_message(
                    user_id,
                    f"🔔 **OSINT PRO • Мониторинг**\n\n"
                    f"🏢 {full_name}\n"
                    f"📋 ИНН `{inn}`\n\n"
                    f"⚠️ **Изменения обнаружены:**\n" + "\n".join(changes),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb
                )
        except Exception as e:
            logger.error(f"Monitoring error for {inn} (user {user_id}): {e}")
            continue
    duration = (datetime.now() - start).total_seconds()
    logger.info(f"✅ Мониторинг завершён за {duration:.1f} сек. Изменений: {changes_count}")
async def monitoring_scheduler():
    """Улучшенный планировщик: запускается каждые MONITORING_INTERVAL_HOURS"""
    while True:
        await check_monitored_companies()
        await asyncio.sleep(MONITORING_INTERVAL_HOURS * 3600)
# ================= AI ANALYSIS =================
async def get_ai_summary(data: dict, score: int, risks: list, recommendation: str, arbitration_data: dict | None = None) -> str:
    if not AI_API_KEY:
        return "🔹 AI-анализ временно недоступен"
    inn = data.get('ИНН', '—')
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT summary FROM ai_cache WHERE inn = ? AND datetime('now') <= datetime(created_at, '+24 hours')",
            (inn,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]
    company_name = data.get('НаимПолн') or data.get('full_name') or data.get('НаимСокр') or "Компания"
    arb_count = 0
    if arbitration_data and isinstance(arbitration_data, dict):
        arb_count = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
    prompt = f"""Ты — эксперт по проверке контрагентов в России (OSINT PRO v{VERSION}).
Дай короткое (2–4 предложения), честное и полезное резюме на русском.
Название: {company_name}
ИНН: {inn}
Индекс безопасности: {score}/100
Рекомендация: {recommendation}
Риски: {', '.join(risks) if risks else 'нет'}
Арбитраж: {arb_count} дел
Стиль: профессиональный, лаконичный, с эмодзи. Начинай строго с:
✅ Вывод OSINT PRO AI:"""
    try:
        async with aiohttp.ClientSession() as session:
            if AI_PROVIDER == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent?key={AI_API_KEY}"
                payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.65, "maxOutputTokens": 300}}
                headers = {"Content-Type": "application/json"}
            else:
                url = "https://api.openai.com/v1/chat/completions" if AI_PROVIDER == "openai" else "https://api.x.ai/v1/chat/completions"
                payload = {"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.65, "max_tokens": 300}
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {AI_API_KEY}"}
            async with session.post(url, json=payload, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"AI API error {resp.status}: {error_text}")
                    return "🔹 AI-анализ временно недоступен"
                result = await resp.json()
                if AI_PROVIDER == "gemini":
                    summary = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                else:
                    summary = result["choices"][0]["message"]["content"].strip()
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO ai_cache (inn, summary, created_at) VALUES (?, ?, ?)",
                        (inn, summary, datetime.now().isoformat())
                    )
                    await db.commit()
                return summary
    except Exception as e:
        logger.error(f"AI summary error: {e}")
        return "🔹 AI-анализ временно недоступен (техническая ошибка)"
# ================= FINANCIAL ANALYZER + ALTMAN Z-SCORE MODEL =================
class FinancialAnalyzer:
    """Профессиональный финансовый анализ + модель Альтмана Z-score (для PDF Enterprise)"""
    def __init__(self, finance_data: list):
        self.df = pd.DataFrame(finance_data) if finance_data else pd.DataFrame()

    def get_altman_z_score(self, assets, equity, retained_earnings, ebit, revenue, total_liabilities):
        if assets <= 0:
            return 0.0
        x1 = (assets - total_liabilities) / assets
        x2 = retained_earnings / assets
        x3 = ebit / assets
        x4 = equity / total_liabilities if total_liabilities > 0 else 0
        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
        return round(z, 2)

    def analyze_latest_year(self):
        if self.df.empty:
            return {"z_score": 0, "status": "Нет финансовых данных", "ros": 0, "autonomy": 0}
        latest = self.df.iloc[-1]
        revenue = latest.get('revenue', 0)
        net_profit = latest.get('net_profit', 0)
        assets = latest.get('assets', 1)
        equity = latest.get('equity', 0)
        liabilities = latest.get('total_liabilities', 1)
        ros = (net_profit / revenue * 100) if revenue > 0 else 0
        autonomy = equity / assets
        z_score = self.get_altman_z_score(assets, equity, latest.get('retained_earnings', 0),
                                          latest.get('ebit', net_profit), revenue, liabilities)
        if z_score > 2.6:
            status = "✅ Зеленая зона (Низкий риск банкротства)"
        elif 1.1 <= z_score <= 2.6:
            status = "🟡 Серая зона (Средний риск)"
        else:
            status = "🚨 Красная зона (Высокий риск банкротства)"
        return {"z_score": z_score, "status": status, "ros": round(ros, 2), "autonomy": round(autonomy, 2)}
# ================= ADVANCED RISK ASSESSMENT v3.0 =================
async def get_advanced_risk_assessment(data: dict, arbitration_data: dict, finance_data: list = None, tax_blocks: list = None):
    """Расширенная оценка рисков v3.0 с финансовым анализом и моделью Альтмана (для Enterprise PDF)"""
    if finance_data is None:
        finance_data = []
    if tax_blocks is None:
        tax_blocks = []
    score = 100
    risk_factors = []
    # Возраст компании
    reg_date_str = data.get('ДатаРег', '')
    if reg_date_str:
        try:
            years = (datetime.now() - datetime.strptime(reg_date_str[:10], '%Y-%m-%d')).days / 365.25
            if years < 1:
                score -= 35
                risk_factors.append("⚠️ Компания существует менее 1 года")
        except:
            pass
    # Блокировки ФНС
    if tax_blocks:
        score -= 40
        risk_factors.append(f"🚨 Блокировки счетов ФНС (БИР): {len(tax_blocks)} шт.")
    # Финансовый анализ
    analyzer = FinancialAnalyzer(finance_data)
    fin_analysis = analyzer.analyze_latest_year()
    # Исправлено: проверяем наличие данных перед снижением скора (чтобы не штрафовать за отсутствие данных)
    if fin_analysis['z_score'] < 1.1 and fin_analysis['status'] != "Нет финансовых данных":
        score -= 25
        risk_factors.append(f"🚨 Высокий риск банкротства (Z-Altman: {fin_analysis['z_score']})")
    # Арбитраж (ответчик)
    arb_cases = arbitration_data.get("cases", []) if isinstance(arbitration_data, dict) else []
    if arb_cases:
        defendant_cases = [c for c in arb_cases if str(data.get('ИНН')) in str(c.get('Ответчик', ''))]
        if len(defendant_cases) > 0:
            score -= min(40, len(defendant_cases) * 10)
            risk_factors.append(f"⚖️ Ответчик в {len(defendant_cases)} арбитражных делах")
    color = colors.green if score > 75 else colors.orange if score > 45 else colors.red
    rec = "✅ Рекомендуется к работе" if score > 80 else "🟡 Требует проверки" if score >= 60 else "🚫 Высокий риск!"
    return score, risk_factors, fin_analysis, color, rec
# ================= AI EXECUTIVE SUMMARY =================
async def get_ai_executive_summary(data: dict, score: int, risks: list, fin_analysis: dict) -> str:
    """AI Executive Summary для директоров (более строгий и профессиональный)"""
    if not AI_API_KEY:
        return "🔹 AI-анализ временно недоступен"
    company_name = data.get('НаимПолн', 'Компания')
    prompt = f"""Ты — Senior Compliance Officer (OSINT PRO v{VERSION}).
Составь Executive Summary для директора.
Компания: {company_name} (ИНН: {data.get('ИНН')})
Индекс надёжности: {score}/100
Риски: {', '.join(risks)}
Фин. статус (Альтман): {fin_analysis.get('status', 'Н/Д')}
Структура ответа (строго):
1. Вердикт (✅/🟡/🚨)
2. Признаки 375-П ЦБ РФ (есть/нет)
3. Рекомендация по лимиту сделки и условиям оплаты
Пиши кратко, профессионально. Начинай строго с:
✅ Вывод OSINT PRO AI:"""
    try:
        async with aiohttp.ClientSession() as session:
            if AI_PROVIDER == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent?key={AI_API_KEY}"
                payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.65, "maxOutputTokens": 400}}
                headers = {"Content-Type": "application/json"}
            else:
                url = "https://api.openai.com/v1/chat/completions" if AI_PROVIDER == "openai" else "https://api.x.ai/v1/chat/completions"
                payload = {"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.65, "max_tokens": 400}
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {AI_API_KEY}"}
            async with session.post(url, json=payload, headers=headers, timeout=18) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"AI API error {resp.status}: {error_text}")
                    return "🔹 AI Executive Summary временно недоступен"
                result = await resp.json()
                if AI_PROVIDER == "gemini":
                    summary = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                else:
                    summary = result["choices"][0]["message"]["content"].strip()
                if not summary.startswith("✅ Вывод OSINT PRO AI:"):
                    summary = f"✅ Вывод OSINT PRO AI: {summary}"
                return summary
    except Exception as e:
        logger.error(f"AI executive error: {e}")
        return "🔹 AI Executive Summary временно недоступен (техническая ошибка)"
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
    except Exception:
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
    """Улучшенная оценка рисков v2.9 — добавлены массовые руководитель и учредители"""
    score = 100
    risk_factors = []
    warnings = []
    mass_flags = []
    # Возраст компании
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
    # Массовый адрес
    if data.get("ЮрАдрес", {}).get("МассАдрес"):
        score -= 30
        risk_factors.append("🚨 Массовый юридический адрес")
        mass_flags.append("Массовый адрес")
    # НОВОЕ: Массовый руководитель
    ruk = data.get("Руковод") or data.get("director")
    if isinstance(ruk, dict) and ruk.get("МассРук"):
        score -= 25
        risk_factors.append("🚨 Массовый руководитель")
    elif isinstance(ruk, list) and any(isinstance(r, dict) and r.get("МассРук") for r in ruk):
        score -= 25
        risk_factors.append("🚨 Массовый руководитель")
    # НОВОЕ: Массовые учредители
    if data.get("МассУчред") or "массов" in str(data.get("Учред", "")).lower():
        score -= 20
        risk_factors.append("🚨 Массовые учредители")
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
    recommendation = "✅ Рекомендуется к работе" if score > 80 else "🟡 Требует дополнительной проверки" if score >= 60 else "🚫 Высокий риск!"
    return score, risk_factors, warnings, color, recommendation, arbitration_data, mass_flags
def is_individual_entrepreneur(data: dict) -> bool:
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
# ================= GRAPH HELPERS FOR PDF (TOP-3) =================
def draw_score_trend(c, x, y, history):
    """Простой бар + линия график динамики индекса (чисто reportlab, без доп. библиотек)"""
    if not history or len(history) < 2:
        c.setFont(FONT_NAME, 10)
        c.setFillColor(colors.grey)
        c.drawString(x, y - 15, "История пока недостаточна для графика")
        return y - 45
    # history приходит newest-first → разворачиваем для графика (oldest left)
    history = history[::-1][-10:] # максимум 10 точек
    n = len(history)
    max_h = 140
    bar_w = 28
    spacing = 45
    chart_w = n * spacing
    # Заголовок
    c.setFont(FONT_NAME, 12)
    c.setFillColor(colors.HexColor("#0a1f44"))
    c.drawString(x, y + 5, "📈 ДИНАМИКА ИНДЕКСА БЕЗОПАСНОСТИ")
    y -= 25
    # Оси
    c.setStrokeColor(colors.grey)
    c.setLineWidth(1)
    c.line(x, y - max_h, x, y) # Y
    c.line(x, y - max_h, x + chart_w + 20, y - max_h) # X
    # Метки Y
    c.setFont(FONT_NAME, 8)
    for val in [0, 50, 100]:
        yy = y - max_h + (val / 100 * max_h)
        c.drawString(x - 22, yy - 3, str(val))
        c.line(x - 5, yy, x, yy)
    # Бары + линия
    prev_x = None
    prev_yy = None
    for i, (ts, score, *_) in enumerate(history):
        bar_x = x + i * spacing
        bar_h = (score / 100) * max_h
        bar_color = colors.HexColor("#00c853") if score > 75 else colors.HexColor("#ffa726") if score > 45 else colors.HexColor("#ef5350")
        c.setFillColor(bar_color)
        c.rect(bar_x, y - max_h, bar_w, bar_h, fill=1, stroke=1)
        # Значение
        c.setFillColor(colors.black)
        c.setFont(FONT_NAME, 9)
        c.drawString(bar_x + 8, y - max_h + bar_h + 5, str(score))
        # Дата
        short_date = ts[5:10].replace('-', '.')
        c.setFont(FONT_NAME, 7)
        c.drawString(bar_x + 6, y - max_h - 18, short_date)
        # Соединительная линия
        curr_x = bar_x + bar_w / 2
        curr_yy = y - max_h + bar_h
        if prev_x is not None:
            c.setStrokeColor(colors.blue)
            c.setLineWidth(1.5)
            c.line(prev_x, prev_yy, curr_x, curr_yy)
        prev_x = curr_x
        prev_yy = curr_yy
    return y - max_h - 50
# ================= PREMIUM PDF (улучшенный дизайн v2.9 + TOP-3 графики) =================
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
                   is_premium: bool, cache_time: str | None = None, ai_summary: str | None = None,
                   history: list | None = None):
    """Премиум PDF v2.9 — добавлен AI-анализ + графики истории изменений (TOP-3)"""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    # Header
    c.setFillColor(colors.HexColor("#0a1f44"))
    c.rect(0, y + 10, 595, 70, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(FONT_NAME, 28)
    c.drawString(45, y + 45, "OSINT PRO")
    c.setFont(FONT_NAME, 11)
    c.drawString(45, y + 28, f"ПРОФЕССИОНАЛЬНЫЙ АНАЛИТИЧЕСКИЙ ОТЧЁТ v{VERSION}")
    if is_premium:
        c.setFillColor(colors.HexColor("#00c853"))
        c.setFont(FONT_NAME, 13)
        c.drawString(420, y + 48, "PREMIUM")
    c.setFillColor(colors.white)
    c.setFont(FONT_NAME, 10)
    c.drawString(45, y + 12, f"{datetime.now().strftime('%d.%m.%Y %H:%M')} • Checko.ru + ЕГРЮЛ + AI")
    y -= 85
    full_name = data.get('НаимПолн') or data.get('full_name') or "Н/Д"
    status_text, status_emoji = get_company_status(data)
    c.setFont(FONT_NAME, 18)
    c.setFillColor(colors.black)
    y = draw_multiline(c, 45, y, f"{status_emoji} {full_name}", font_size=18, max_width=500, line_height=22)
    y -= 12
    c.setFont(FONT_NAME, 14)
    c.setFillColor(colors.HexColor("#0a1f44"))
    c.drawString(45, y, "ИНДЕКС БЕЗОПАСНОСТИ")
    # Progress bar
    bar_width = 280
    bar_height = 28
    c.setStrokeColor(colors.lightgrey)
    c.setLineWidth(2)
    c.roundRect(45, y - 38, bar_width, bar_height, 6, stroke=1, fill=0)
    fill_width = (score / 100) * bar_width
    c.setFillColor(color)
    c.roundRect(45, y - 38, fill_width, bar_height, 6, stroke=0, fill=1)
    c.setFont(FONT_NAME, 32)
    c.setFillColor(colors.black)
    c.drawString(360, y - 33, f"{score}")
    c.setFont(FONT_NAME, 14)
    c.drawString(415, y - 33, "/100")
    risk_label = "НИЗКИЙ РИСК" if score > 75 else "СРЕДНИЙ РИСК" if score > 45 else "ВЫСОКИЙ РИСК"
    risk_color = colors.green if score > 75 else colors.orange if score > 45 else colors.red
    c.setFillColor(risk_color)
    c.setFont(FONT_NAME, 11)
    c.drawString(360, y - 55, risk_label)
    y -= 85
    # Ключевые факты
    c.setFont(FONT_NAME, 13)
    c.setFillColor(colors.HexColor("#0a1f44"))
    c.drawString(45, y, "КЛЮЧЕВЫЕ ФАКТЫ")
    y -= 28
    is_ip_flag = is_individual_entrepreneur(data)
    director = safe_get_director(data)
    branches = safe_get_branches(data)
    okved = safe_get_okved(data)
    if is_ip_flag:
        table_data = [
            ["Параметр", "Значение"],
            ["ИНН", data.get('ИНН', '—')],
            ["ОГРНИП", data.get('ОГРНИП', data.get('ОГРН', '—'))],
            ["Предприниматель", director],
            ["Регистрация", calculate_age(data.get('ДатаРег', ''))],
            ["Адрес", get_formatted_address(data)],
            ["ОКВЭД", okved],
        ]
    else:
        table_data = [
            ["Параметр", "Значение"],
            ["ИНН", data.get('ИНН', '—')],
            ["ОГРН", data.get('ОГРН', '—')],
            ["КПП", data.get('КПП', '—')],
            ["Руководитель", director],
            ["Регистрация", calculate_age(data.get('ДатаРег', ''))],
            ["Адрес", get_formatted_address(data)],
            ["Уставный капитал", data.get('УставКапитал', '—')],
            ["Филиалы", f"{branches} шт." if branches else "—"],
            ["ОКВЭД", okved],
        ]
    table = Table(table_data, colWidths=[170, 300])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0a1f44")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), FONT_NAME),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor("#e0e0e0")),
        ('FONTNAME', (0, 1), (-1, -1), FONT_NAME),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
    ]))
    table.wrapOn(c, 45, y)
    table.drawOn(c, 45, y - table._height)
    y -= table._height + 35
    # НОВОЕ: График истории + таймлайн (TOP-3)
    if history and len(history) > 0:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.HexColor("#0a1f44"))
        c.drawString(45, y, "📈 ДИНАМИКА И ИСТОРИЯ КОМПАНИИ")
        y -= 35
        y = draw_score_trend(c, 45, y, history)
        # Таймлайн (последние записи)
        timeline_data = [["Дата", "Индекс", "Арбитраж", "Статус"]]
        for ts, sc, arb, st, _ in history[:5]:
            date_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%d.%m")
            timeline_data.append([date_str, str(sc), str(arb), st[:22]])
        if len(timeline_data) > 1:
            tl_table = Table(timeline_data, colWidths=[60, 55, 70, 280])
            tl_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0a1f44")),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
                ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
            ]))
            tl_table.wrapOn(c, 45, y)
            tl_table.drawOn(c, 45, y - tl_table._height)
            y -= tl_table._height + 25
    # Контакты
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
        c.setFillColor(colors.HexColor("#0a1f44"))
        c.drawString(45, y, "📞 КОНТАКТЫ")
        y -= 25
        contact_table_data = [["Тип", "Значение"]]
        for contact in contacts[:10]:
            if ":" in contact:
                typ, val = contact.split(":", 1)
                contact_table_data.append([typ.strip(), val.strip()])
            else:
                contact_table_data.append(["Контакт", contact])
        ct = Table(contact_table_data, colWidths=[120, 350])
        ct.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1565c0")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f7ff")]),
        ]))
        ct.wrapOn(c, 45, y)
        ct.drawOn(c, 45, y - ct._height)
        y -= ct._height + 30
    # Учредители
    uchred = data.get("Учред", {})
    if uchred and uchred.get("ФЛ"):
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.HexColor("#0a1f44"))
        c.drawString(45, y, "👥 УЧРЕДИТЕЛИ")
        y -= 25
        founders_table = [["ФИО", "Доля %"]]
        for fl in uchred.get("ФЛ", [])[:8]:
            founders_table.append([fl.get('ФИО', '—'), f"{fl.get('Доля', '—')}%"])
        ft = Table(founders_table, colWidths=[300, 170])
        ft.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0a1f44")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
        ]))
        ft.wrapOn(c, 45, y)
        ft.drawOn(c, 45, y - ft._height)
        y -= ft._height + 30
    # Риски
    all_risks = warnings + mass_flags + risks
    if all_risks:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.orange)
        c.drawString(45, y, "⚠️ РИСКИ И ПРЕДУПРЕЖДЕНИЯ")
        y -= 25
        risk_table = [["", "Описание"]]
        for item in all_risks:
            risk_table.append(["⚠️", item])
        rt = Table(risk_table, colWidths=[35, 430])
        rt.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#f57c00")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff3e0")]),
        ]))
        rt.wrapOn(c, 45, y)
        rt.drawOn(c, 45, y - rt._height)
        y -= rt._height + 30
    # Арбитраж
    arb_table_data = get_arbitration_cases_table(arbitration_data)
    if len(arb_table_data) > 1:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.red)
        c.drawString(45, y, "⚖️ АРБИТРАЖНЫЕ ДЕЛА (последние)")
        y -= 25
        arb_table = Table(arb_table_data, colWidths=[72, 75, 125, 125, 85])
        arb_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#d32f2f")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        arb_table.wrapOn(c, 45, y)
        arb_table.drawOn(c, 45, y - arb_table._height)
        y -= arb_table._height + 30
    # AI-анализ в PDF
    if ai_summary:
        c.setFont(FONT_NAME, 13)
        c.setFillColor(colors.HexColor("#0a1f44"))
        c.drawString(45, y, "🤖 AI-АНАЛИЗ OSINT PRO")
        y -= 25
        y = draw_multiline(c, 45, y, ai_summary, font_size=10, max_width=480, line_height=14)
        y -= 20
    # Рекомендация
    c.setStrokeColor(color)
    c.setLineWidth(3)
    c.roundRect(45, y - 75, 500, 85, 12, stroke=1, fill=0)
    c.setFillColor(color)
    c.setFont(FONT_NAME, 15)
    c.drawString(65, y - 25, "РЕКОМЕНДАЦИЯ OSINT PRO")
    c.setFillColor(colors.black)
    c.setFont(FONT_NAME, 11)
    y = draw_multiline(c, 65, y - 45, recommendation, font_size=11, max_width=460, line_height=16)
    # Footer
    c.setFont(FONT_NAME, 8)
    c.setFillColor(colors.grey)
    footer = f"OSINT PRO v{VERSION} • Конфиденциально • Данные на {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    if cache_time:
        try:
            dt = datetime.fromisoformat(cache_time.replace("Z", "+00:00"))
            footer += f" • Кэш: {dt.strftime('%d.%m.%Y %H:%M')}"
        except:
            pass
    c.drawString(45, 35, footer)
    c.drawString(380, 35, "Для внутренних целей • Не для перепродажи")
    # Водяной знак PREMIUM
    if is_premium:
        c.saveState()
        c.setFillColor(colors.HexColor("#00c853"))
        c.setFont(FONT_NAME, 60)
        c.setFillAlpha(0.08)
        c.rotate(35)
        c.drawString(100, -150, "PREMIUM")
        c.restoreState()
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer
# ================= ENTERPRISE PDF (Platypus + Financial + Altman) =================
def create_enterprise_pdf(data: dict, score: int, risks: list, fin_analysis: dict, ai_summary: str, is_premium: bool):
    """Новый профессиональный Enterprise PDF (Platypus) с финансовым анализом и AI Executive Summary"""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=18)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', fontName=FONT_NAME, fontSize=24, textColor=colors.HexColor("#0a1f44"), spaceAfter=20)
    h2_style = ParagraphStyle('H2', fontName=FONT_NAME, fontSize=16, textColor=colors.HexColor("#1565c0"), spaceAfter=10, spaceBefore=20)
    normal_style = ParagraphStyle('Normal', fontName=FONT_NAME, fontSize=11, spaceAfter=8)
    elements = []
    elements.append(Paragraph("OSINT PRO ENTERPRISE", title_style))
    full_name = data.get('НаимПолн') or data.get('full_name') or "Н/Д"
    elements.append(Paragraph(f"Аналитический отчёт: {full_name}", h2_style))
    elements.append(Paragraph(f"ИНН: {data.get('ИНН', '—')} | ОГРН: {data.get('ОГРН', '—')}", normal_style))
    elements.append(Spacer(1, 30))
    bg_color = colors.green if score > 75 else colors.orange if score > 45 else colors.red
    score_table = Table([[f"ИНДЕКС НАДЁЖНОСТИ: {score}/100"]], colWidths=[500], rowHeights=[50])
    score_table.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), bg_color),
                                     ('TEXTCOLOR', (0,0), (-1,-1), colors.white),
                                     ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                                     ('FONTNAME', (0,0), (-1,-1), FONT_NAME),
                                     ('FONTSIZE', (0,0), (-1,-1), 18),
                                     ('CORNER_RADIUS', (0,0), (-1,-1), 8)]))
    elements.append(score_table)
    elements.append(Spacer(1, 20))
    if ai_summary:
        elements.append(Paragraph("🤖 AI Compliance Executive Summary", h2_style))
        elements.append(Paragraph(ai_summary.replace('\n', '<br/>'), normal_style))
        elements.append(Spacer(1, 15))
    elements.append(Paragraph("📊 Финансовый анализ и скоринг", h2_style))
    fin_data = [
        ["Показатель", "Значение"],
        ["Z-счёт Альтмана", str(fin_analysis.get('z_score', 'Н/Д'))],
        ["Статус риска банкротства", fin_analysis.get('status', 'Н/Д')],
        ["Рентабельность продаж (ROS)", f"{fin_analysis.get('ros', 0)}%"],
        ["Коэффициент автономии", str(fin_analysis.get('autonomy', 'Н/Д'))]
    ]
    fin_table = Table(fin_data, colWidths=[300, 200])
    fin_table.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.HexColor("#0a1f44")),
                                   ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                                   ('FONTNAME', (0,0), (-1,-1), FONT_NAME),
                                   ('GRID', (0,0), (-1,-1), 1, colors.lightgrey),
                                   ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor("#f8f9fa")])]))
    elements.append(fin_table)
    elements.append(Spacer(1, 20))
    if risks:
        elements.append(PageBreak())
        elements.append(Paragraph("⚠️ Критические риски", h2_style))
        risk_data = [["Риск"]] + [[r] for r in risks]
        rt = Table(risk_data, colWidths=[500])
        rt.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.HexColor("#d32f2f")),
                                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                                ('FONTNAME', (0,0), (-1,-1), FONT_NAME),
                                ('GRID', (0,0), (-1,-1), 1, colors.lightgrey)]))
        elements.append(rt)
    doc.build(elements)
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
        worksheet.append_row([now.strftime("%d.%m.%Y %H:%M:%S"), str(user_id), inn, f"score:{score}", "Бесплатный" if score else "Премиум", now.strftime("%H:%M:%S")])
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
# ================= MASS CHECK (с прогресс-баром) =================
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
    wait_msg = await message.answer("📤 **Обрабатываю файл...**\n0% (0/0)")
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
        await wait_msg.edit_text("❌ Не удалось прочитать файл.")
        return
    # Поиск колонки с ИНН
    inn_col = None
    for col in df.columns:
        col_str = str(col).lower()
        if any(x in col_str for x in ['инн', 'inn', 'tax', 'id']):
            inn_col = col
            break
    if inn_col is None:
        df['inn'] = df.iloc[:, 0].astype(str).str.strip()
    else:
        df['inn'] = df[inn_col].astype(str).str.strip()
    inns = []
    for val in df['inn']:
        clean = re.sub(r'\D', '', str(val))
        if len(clean) in (10, 12):
            inns.append(clean)
    inns = list(dict.fromkeys(inns))[:100]
    if not inns:
        await wait_msg.edit_text("❌ В файле не найдено валидных ИНН.")
        return
    results = []
    total = len(inns)
    for i, inn in enumerate(inns):
        if i % 10 == 0 or i == total - 1:
            percent = int(((i + 1) / total) * 100)
            await wait_msg.edit_text(f"📤 **Обрабатываю файл...** {percent}% ({i + 1}/{total})")
        data, arbitration_data, _ = await get_company_data(inn)
        if not data:
            results.append({"ИНН": inn, "Название": "Не найдено", "Статус": "—", "Индекс безопасности": 0,
                            "Рекомендация": "Данные отсутствуют", "Арбитражных дел": 0,
                            "Руководитель": "—", "Дата регистрации": "—"})
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
    result_df = pd.DataFrame(results)
    excel_buffer = BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        result_df.to_excel(writer, sheet_name="OSINT PRO — Массовый отчёт", index=False)
    excel_buffer.seek(0)
    total = len(results)
    risky = len([r for r in results if r["Индекс безопасности"] < 60])
    summary = (f"✅ **Массовый отчёт OSINT PRO v{VERSION} готов!**\n\n"
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
    text = (f"🚀 **OSINT PRO v{VERSION}**\n\n"
            "Поиск **по ИНН / ОГРН** и **по названию**!\n"
            "✅ Премиум PDF • Арбитраж • AI-анализ • Мониторинг\n\n"
            "📌 **Возможности Pro:**\n"
            "• Мониторинг изменений (уведомления)\n"
            "• Массовая проверка Excel/CSV (до 100)\n"
            "• Полный PDF с AI-саммари + графиками\n"
            "• Безлимит + поддержка\n\n"
            "Пришлите ИНН / ОГРН или название компании")
    if is_admin:
        text += "\n\n👑 **Админ-панель:** /admin"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💡 Помощь", callback_data="help")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")]
    ])
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
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
    await message.answer("👑 **Админ-панель OSINT PRO v2.9**", reply_markup=kb)
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
    await call.message.edit_text(f"💰 **Текущая цена подписки**\n\n{SUBSCRIPTION_PRICE}\n\nБезлимит + премиум-PDF + AI + мониторинг")
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
        wait = await message.answer("🔍 Запрашиваю данные + AI-анализ...")
        data, arbitration_data, cache_time = await get_company_data(inn)
        if not data:
            return await wait.edit_text("❌ Данные по ИНН не найдены.")
        score, risks, warnings, color, recommendation, arbitration_data, mass_flags = get_risk_assessment(data, arbitration_data)
        await log_usage(message.from_user.id, inn, score, is_premium)
        log_to_sheet(message.from_user.id, inn, score)
        # НОВОЕ: сохраняем историю для графиков в PDF
        arb_count_hist = 0
        if arbitration_data and isinstance(arbitration_data, dict):
            arb_count_hist = arbitration_data.get("total", 0) or len(arbitration_data.get("cases", []))
        status_text_hist, _ = get_company_status(data)
        director_hist = safe_get_director(data)
        await save_to_history(inn, score, arb_count_hist, status_text_hist, director_hist)
        ai_summary = await get_ai_summary(data, score, risks, recommendation, arbitration_data)
        is_mon = await is_monitored(message.from_user.id, inn)
        monitor_text = "❌ Убрать из мониторинга" if is_mon else "📌 Добавить в мониторинг"
        monitor_cb = f"monitor_remove_{inn}" if is_mon else f"monitor_add_{inn}"
        is_ip_flag = is_individual_entrepreneur(data)
        company_name = data.get('НаимСокр') or data.get('short_name') or data.get('full_name') or '—'
        director = safe_get_director(data)
        founders_count = safe_get_founders_count(data)
        res = f"✅ **OSINT PRO v{VERSION}**{' PREM' if is_premium else ''}\n\n"
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
        res += f"{ai_summary}\n\n"
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
        return await wait.edit_text("❌ Компании с таким названием не найдены.")
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
    can_use, remaining, is_premium = await check_limit(call.from_user.id)
    if not can_use:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy")]])
        return await call.message.answer("🛑 Лимит 3 запроса в день исчерпан.", reply_markup=kb)
    try:
        data, arbitration_data, cache_time = await get_company_data(inn)
        if not data:
            return await call.message.answer("❌ Данные не найдены.")
        # === ИНТЕГРАЦИЯ ENTERPRISE PDF ===
        score, risks, fin_analysis, color, rec = await get_advanced_risk_assessment(data, arbitration_data)
        ai_summary = await get_ai_executive_summary(data, score, risks, fin_analysis)
        await log_usage(call.from_user.id, inn, score, is_premium)
        log_to_sheet(call.from_user.id, inn, score)
        pdf_buffer = create_enterprise_pdf(data, score, risks, fin_analysis, ai_summary, is_premium)
        await call.message.answer_document(
            BufferedInputFile(pdf_buffer.read(), filename=f"OSINT_PRO_{inn}_ENTERPRISE_v{VERSION}.pdf"),
            caption="✅ Enterprise аналитический отчёт OSINT PRO v{VERSION}"
        )
    except Exception as e:
        logger.error(f"Select error: {e}")
        await call.message.answer("❌ Ошибка открытия отчёта.")
@dp.callback_query(F.data.startswith("pdf_"))
async def send_pdf(call: CallbackQuery):
    inn = call.data.split("_", 1)[1]
    await call.answer("Генерирую Enterprise PDF...")
    try:
        data, arbitration_data, cache_time = await get_company_data(inn)
        if not data:
            raise ValueError("Нет данных")
        is_premium = await is_subscribed(call.from_user.id)
        # === ИНТЕГРАЦИЯ ENTERPRISE PDF ===
        score, risks, fin_analysis, color, rec = await get_advanced_risk_assessment(data, arbitration_data)
        ai_summary = await get_ai_executive_summary(data, score, risks, fin_analysis)
        pdf_buffer = create_enterprise_pdf(data, score, risks, fin_analysis, ai_summary, is_premium)
        await call.message.answer_document(
            BufferedInputFile(pdf_buffer.read(), filename=f"OSINT_PRO_{inn}_ENTERPRISE_v{VERSION}.pdf"),
            caption="✅ Enterprise аналитический отчёт OSINT PRO v{VERSION}"
        )
    except Exception as e:
        logger.error(f"PDF error INN {inn}", exc_info=True)
        await call.message.answer("❌ Не удалось сгенерировать PDF.")
@dp.callback_query(F.data.startswith("refresh_"))
async def handle_refresh(call: CallbackQuery):
    inn = call.data.split("_", 1)[1]
    await call.answer("🔄 Очищаем кэш...")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM cache WHERE inn = ?", (inn,))
        await db.commit()
    await call.message.answer("✅ **Кэш очищен!** Пришлите ИНН / ОГРН ещё раз.")
@dp.callback_query(F.data.startswith("monitor_add_"))
async def handle_monitor_add(call: CallbackQuery):
    inn = call.data.split("_", 2)[2]
    success = await add_to_monitoring(call.from_user.id, inn)
    if success:
        await call.answer("✅ Добавлено в мониторинг!")
    else:
        await call.answer("❌ Не удалось добавить")
@dp.callback_query(F.data.startswith("monitor_remove_"))
async def handle_monitor_remove(call: CallbackQuery):
    inn = call.data.split("_", 2)[2]
    await remove_from_monitoring(call.from_user.id, inn)
    await call.answer("✅ Убрано из мониторинга")
@dp.message(F.document)
async def handle_document(message: Message):
    await handle_mass_check_document(message)
@dp.callback_query(F.data == "buy")
async def buy_subscription(call: CallbackQuery):
    await call.answer()
    await call.message.answer(f"💰 **Подписка OSINT PRO v{VERSION}**\n\nБезлимит + премиум-PDF + AI + мониторинг — {SUBSCRIPTION_PRICE}\n\nПосле оплаты напишите @ваш_логин с чеком.")
@dp.message(Command("history"))
async def cmd_history(message: Message):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT query_date, inn, score FROM usage_log WHERE user_id = ? ORDER BY rowid DESC LIMIT 10",
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
        return await message.answer("📭 У вас пока нет компаний в мониторинге.")
    text = "📌 **Ваши компании в мониторинге**:\n\n"
    for inn, name in monitored:
        text += f"• `{inn}` — {name[:60]}...\n"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)
@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    subscribed = await is_subscribed(message.from_user.id)
    can_use, remaining, is_premium = await check_limit(message.from_user.id)
    monitored_count = len(await get_user_monitored(message.from_user.id))
    until = ""
    if subscribed:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT until_date FROM subscriptions WHERE user_id = ?", (message.from_user.id,)) as cur:
                row = await cur.fetchone()
                until = f" до {row[0]}" if row else ""
    text = f"""👤 **Ваш профиль OSINT PRO v{VERSION}**
📅 Подписка: {'✅ Активна' + until if subscribed else '❌ Нет (бесплатный режим)'}
🛡️ Остаток запросов сегодня: {'∞' if is_premium else f'{remaining}/{FREE_LIMIT}'}
📌 Компаний в мониторинге: {monitored_count}
🔥 Запросов сегодня: {await get_today_queries(message.from_user.id)}
💎 Хотите безлимит и премиум-PDF? Нажмите /buy"""
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)
async def get_today_queries(user_id: int) -> int:
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM usage_log WHERE user_id = ? AND query_date = ?",
                (user_id, date.today().isoformat())
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    except:
        return 0
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
    await message.answer(f"💰 **Текущая цена подписки**\n\n{SUBSCRIPTION_PRICE}\n\nБезлимит + премиум-PDF + AI + мониторинг")
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
    asyncio.create_task(monitoring_scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"🚀 OSINT PRO v{VERSION} запущен — премиум-продукт готов к монетизации!")
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()
    logger.info("✅ Webhook + мониторинг + AI + Enterprise PDF + Financial Analyzer запущены")
    await asyncio.Event().wait()
if __name__ == "__main__":
    asyncio.run(main())
