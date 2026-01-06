# bot_student_control_full.py
"""
Бот для контроля ежемесячных платежей учеников с полным функционалом
- Ежемесячная оплата с гибкими сроками
- Система промокодов
- Полная админ-панель
- Разные способы оплаты
- Автоматическое управление доступом
"""
import os
import sqlite3
import time
import threading
import math
import logging
import re
import random
import string
from datetime import datetime, timedelta
import calendar
import pytz
import requests
import telebot
from telebot import types
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN")
ADMIN_IDS = [
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
]
CURRENCY = os.environ.get("CURRENCY", "BYN")
REFERRAL_PERCENT = int(os.environ.get("REFERRAL_PERCENT", "10"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
DB_PATH = os.environ.get("DB_PATH", "student_bot.db")

# Проверяем обязательные переменные
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в переменных окружения")
if not PROVIDER_TOKEN:
    raise ValueError("PROVIDER_TOKEN не установлен в переменных окружения")
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не установлены в переменных окружения")

LOCAL_TZ = pytz.timezone("Europe/Minsk")  # для GMT+3 подходит


def now_local():
    return datetime.now(LOCAL_TZ)


# ----------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

try:
    ME = bot.get_me()
    BOT_ID = ME.id
    logging.info(f"Bot started: @{ME.username} ({BOT_ID})")
except Exception as e:
    logging.exception("Can't get bot info - check BOT_TOKEN")
    raise


# ----------------- DB init + migrations -----------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()


def init_db_and_migrate():
    # Таблица групп (чатов)
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS managed_groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        is_default INTEGER DEFAULT 0,
        type TEXT DEFAULT 'group',
        added_date INTEGER
    )
    """
    )

    # Таблица тарифов (планов)
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        price_cents INTEGER,
        duration_days INTEGER DEFAULT 30,
        description TEXT,
        media_file_id TEXT,
        media_type TEXT,
        group_id INTEGER,
        created_ts INTEGER,
        media_file_ids TEXT,
        is_active INTEGER DEFAULT 1
    )
    """
    )

    # Таблица пользователей
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        referred_by INTEGER,
        cashback_cents INTEGER DEFAULT 0,
        username TEXT,
        join_date INTEGER
    )
    """
    )

    # Таблица подписок (переработанная для ежемесячных платежей)
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan_id INTEGER,
        start_ts INTEGER,
        end_ts INTEGER,
        active INTEGER DEFAULT 1,
        invite_link TEXT,
        removed INTEGER DEFAULT 0,
        group_id INTEGER,
        payment_type TEXT DEFAULT 'full',
        current_period_month INTEGER,
        current_period_year INTEGER,
        part_paid TEXT DEFAULT 'none',
        next_payment_date INTEGER,
        last_notification_ts INTEGER
    )
    """
    )

    # Таблица счетов
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS invoices (
        payload TEXT PRIMARY KEY,
        user_id INTEGER,
        plan_id INTEGER,
        amount_cents INTEGER,
        created_ts INTEGER,
        payment_type TEXT DEFAULT 'full',
        period_month INTEGER,
        period_year INTEGER,
        promo_id INTEGER DEFAULT NULL
    )
    """
    )

    # Таблица медиа для тарифов
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS plan_media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER,
        file_id TEXT,
        media_type TEXT,
        ord INTEGER DEFAULT 0,
        added_ts INTEGER,
        FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
    )
    """
    )

    # Таблица методов оплаты
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS payment_methods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        type TEXT,
        is_active INTEGER DEFAULT 1,
        description TEXT,
        details TEXT
    )
    """
    )

    # Таблица ручных платежей
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS manual_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan_id INTEGER,
        amount_cents INTEGER,
        receipt_photo TEXT,
        full_name TEXT,
        status TEXT DEFAULT 'pending',
        created_ts INTEGER,
        admin_id INTEGER,
        reviewed_ts INTEGER,
        payment_type TEXT DEFAULT 'full',
        period_month INTEGER,
        period_year INTEGER,
        promo_id INTEGER DEFAULT NULL
    )
    """
    )

    # Таблица промокодов
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS promo_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        discount_percent INTEGER,
        discount_fixed_cents INTEGER,
        is_active INTEGER DEFAULT 1,
        used_count INTEGER DEFAULT 0,
        max_uses INTEGER DEFAULT NULL,
        created_ts INTEGER,
        expires_ts INTEGER DEFAULT NULL
    )
    """
    )

    # Таблица использования промокодов
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS promo_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        promo_id INTEGER,
        user_id INTEGER,
        used_ts INTEGER,
        FOREIGN KEY(promo_id) REFERENCES promo_codes(id)
    )
    """
    )

    # Таблица категорий (предметов)
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        description TEXT,
        created_ts INTEGER,
        is_active INTEGER DEFAULT 1
    )
    """
    )

    # Добавляем поле category_id в таблицу планов
    try:
        cursor.execute("ALTER TABLE plans ADD COLUMN category_id INTEGER")
    except sqlite3.OperationalError:
        pass  # Поле уже существует

    conn.commit()

    # Инициализация методов оплаты если их нет
    cursor.execute("SELECT COUNT(*) FROM payment_methods")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            """
        INSERT INTO payment_methods (name, type, is_active, description, details)
        VALUES 
        ('💳 Банковская карта', 'card', 1, 'Оплата банковской картой', ''),
        ('👨‍💻 Ручная оплата', 'manual', 1, 'Оплата по реквизитам с подтверждением чека', 'Реквизиты для оплаты:\\n\\nБанк: Пример Банк\\nСчет: 0000 0000 0000 0000\\nПолучатель: Иван Иванов\\nНазначение: Оплата подписки')
        """
        )
        conn.commit()


init_db_and_migrate()


# ----------------- Helpers -----------------
def price_str_from_cents(cents):
    if cents is None:
        cents = 0
    return f"{cents//100}.{cents%100:02d} {CURRENCY}"


def cents_from_str(s):
    try:
        s = s.strip()
        if "." in s:
            parts = s.split(".")
            whole = int(parts[0])
            frac = parts[1][:2].ljust(2, "0")
            return whole * 100 + int(frac)
        else:
            return int(s) * 100
    except Exception:
        return None


def safe_caption(text, limit=1024):
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def add_user_if_not_exists(user_id, referred_by=None, username=None):
    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if cursor.fetchone() is None:
        cursor.execute(
            "INSERT INTO users (user_id, referred_by, cashback_cents, username, join_date) VALUES (?, ?, 0, ?, ?)",
            (
                user_id,
                referred_by,
                f"@{username}" if username else None,
                int(time.time()),
            ),
        )
        conn.commit()
        return

    # Обновляем username (без сетевых запросов к Telegram API)
    try:
        cursor.execute(
            "UPDATE users SET username = ? WHERE user_id = ?",
            (f"@{username}" if username else None, user_id),
        )
        conn.commit()
    except Exception:
        pass


def get_default_group():
    cursor.execute("SELECT chat_id FROM managed_groups WHERE is_default=1 LIMIT 1")
    r = cursor.fetchone()
    if r:
        return r[0]
    cursor.execute("SELECT chat_id FROM managed_groups LIMIT 1")
    r = cursor.fetchone()
    if r:
        return r[0]
    return None


def set_default_group(chat_id):
    cursor.execute("UPDATE managed_groups SET is_default=0")
    cursor.execute("UPDATE managed_groups SET is_default=1 WHERE chat_id=?", (chat_id,))
    conn.commit()


def create_chat_invite_link_one_time(
    bot_token, chat_id, expire_seconds=7 * 24 * 3600, member_limit=1
):
    url = f"https://api.telegram.org/bot{bot_token}/createChatInviteLink"
    expire_date = int(time.time()) + expire_seconds
    payload = {
        "chat_id": chat_id,
        "expire_date": expire_date,
        "member_limit": member_limit,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return data["result"]["invite_link"]
    except Exception as e:
        logging.warning("createChatInviteLink failed: %s", e)
    return None


def get_bot_invite_link():
    username = bot.get_me().username
    return f"https://t.me/{username}?startgroup=true"


def is_bot_admin_in_chat(chat_id):
    """Проверяет, является ли бот администратором в чате"""
    try:
        chat = bot.get_chat(chat_id)
        if chat.type in ["private", "channel"]:
            return True  # Для каналов и приватных чатов считаем, что бот имеет доступ

        member = bot.get_chat_member(chat_id, BOT_ID)
        return member.status in ["administrator", "creator"]
    except Exception as e:
        logging.warning(f"Can't check bot admin status in chat {chat_id}: {e}")
        return False


def add_group_to_db(chat_id, title, chat_type="group"):
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO managed_groups (chat_id, title, type, added_date) VALUES (?, ?, ?, ?)",
            (chat_id, title, chat_type, int(time.time())),
        )
        cursor.execute("SELECT COUNT(*) FROM managed_groups")
        count = cursor.fetchone()[0]
        if count == 1:
            cursor.execute(
                "UPDATE managed_groups SET is_default=1 WHERE chat_id=?", (chat_id,)
            )
        conn.commit()
        return True
    except Exception as e:
        logging.exception("add_group_to_db error: %s", e)
        return False


def get_all_groups_with_bot():
    cursor.execute(
        "SELECT chat_id, title, type FROM managed_groups ORDER BY added_date DESC"
    )
    return cursor.fetchall()


def get_active_payment_methods():
    cursor.execute(
        "SELECT id, name, type, description, details FROM payment_methods WHERE is_active=1 ORDER BY id"
    )
    return cursor.fetchall()


def get_payment_method_by_id(method_id):
    cursor.execute(
        "SELECT id, name, type, description, details FROM payment_methods WHERE id=?",
        (method_id,),
    )
    return cursor.fetchone()


def get_current_period():
    """Возвращает текущий месяц и год"""
    now = now_local()

    return now.month, now.year


def get_payment_deadlines():
    """Возвращает дедлайны оплаты для текущего месяца"""
    now = now_local()

    year = now.year
    month = now.month

    # Дедлайн первой части: 5 число текущего месяца 23:59
    first_deadline = datetime(year, month, 5, 23, 59, 59)

    # Дедлайн второй части: 20 число текущего месяца 23:59
    second_deadline = datetime(year, month, 20, 23, 59, 59)

    return first_deadline, second_deadline


def is_payment_period_active():
    """Проверяет, активен ли сейчас период оплаты"""
    now = now_local()

    day = now.day
    return (1 <= day <= 5) or (15 <= day <= 20)


def get_active_payment_type():
    """Всегда возвращает полную оплату"""
    return "full"


def can_user_pay_partial(user_id, plan_id):
    """Проверяет, может ли пользователь оплатить вторую часть"""
    month, year = get_current_period()
    cursor.execute(
        """
        SELECT id FROM subscriptions 
        WHERE user_id=? AND plan_id=? AND current_period_month=? AND current_period_year=? AND part_paid='first'
    """,
        (user_id, plan_id, month, year),
    )
    return cursor.fetchone() is not None


def activate_subscription(
    user_id, plan_id, payment_type="full", group_id=None, is_renewal=False
):
    """Активирует или продлевает подписку для пользователя"""
    cursor.execute(
        "SELECT price_cents, title, group_id FROM plans WHERE id=?", (plan_id,)
    )
    plan = cursor.fetchone()
    if not plan:
        return False, "Тариф не найден"

    price_cents, plan_title, plan_group_id = plan
    current_month, current_year = get_current_period()
    now_ts = int(time.time())
    now = now_local()

    target_group_id = plan_group_id if plan_group_id else group_id
    if not target_group_id:
        return False, "Не указана группа для подписки"

    try:
        # Пробуем разбанить пользователя, если он забанен
        bot.unban_chat_member(target_group_id, user_id)
        logging.info(
            f"🔄 Попытка разбанить пользователя {user_id} в группе {target_group_id}"
        )
    except Exception as e:
        # Ошибка может быть если пользователь не забанен или бот не админ
        logging.debug(f"⚠️ Не удалось разбанить пользователя {user_id}: {e}")

    # Проверяем существующую активную подписку
    cursor.execute(
        """
        SELECT id, active, current_period_month, current_period_year, end_ts, part_paid
        FROM subscriptions 
        WHERE user_id=? AND plan_id=? AND active=1
        ORDER BY id DESC LIMIT 1
    """,
        (user_id, plan_id),
    )

    existing_sub = cursor.fetchone()

    # Расчет даты окончания - всегда до 5 числа следующего месяца
    # Определяем следующий месяц
    if now.month == 12:
        next_month = 1
        next_year = now.year + 1
    else:
        next_month = now.month + 1
        next_year = now.year

    end_dt = LOCAL_TZ.localize(datetime(next_year, next_month, 5, 23, 59, 59))
    end_ts = int(end_dt.timestamp())
    part_paid = "full"

    invite_link = create_chat_invite_link_one_time(
        BOT_TOKEN, target_group_id, expire_seconds=7 * 24 * 3600, member_limit=1
    )

    if existing_sub:
        (
            sub_id,
            active,
            existing_month,
            existing_year,
            existing_end_ts,
            existing_part_paid,
        ) = existing_sub

        # Если подписка уже оплачена на текущий месяц и не истекла
        if (
            existing_month == current_month
            and existing_year == current_year
            and existing_part_paid == "full"
            and existing_end_ts > now_ts
        ):
            # Просто обновляем ссылку
            cursor.execute(
                """
                UPDATE subscriptions 
                SET invite_link=?, last_notification_ts=NULL
                WHERE id=?
            """,
                (invite_link, sub_id),
            )
            conn.commit()
            return True, invite_link
        else:
            # Обновляем существующую подписку на новый месяц
            cursor.execute(
                """
                UPDATE subscriptions 
                SET current_period_month=?, current_period_year=?, part_paid=?, 
                    start_ts=?, end_ts=?, invite_link=?, last_notification_ts=NULL,
                    active=1, removed=0, payment_type=?
                WHERE id=?
            """,
                (
                    current_month,
                    current_year,
                    part_paid,
                    now_ts,
                    end_ts,
                    invite_link,
                    payment_type,
                    sub_id,
                ),
            )
    else:
        # Создаем новую подписку
        cursor.execute(
            """
            INSERT INTO subscriptions (user_id, plan_id, start_ts, end_ts, invite_link, active, removed, group_id, 
                                     payment_type, current_period_month, current_period_year, part_paid, next_payment_date, last_notification_ts) 
            VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, NULL)
        """,
            (
                user_id,
                plan_id,
                now_ts,
                end_ts,
                invite_link,
                target_group_id,
                payment_type,
                current_month,
                current_year,
                part_paid,
                end_ts,
            ),
        )

    conn.commit()
    return True, invite_link


@bot.callback_query_handler(func=lambda call: call.data == "check_my_subscription")
def callback_check_my_subscription(call):
    """Показывает подписки пользователя"""
    show_my_subscription(call.message)
    bot.answer_callback_query(call.id)


def generate_promo_code(length=8):
    """Генерирует уникальный промокод"""
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        cursor.execute("SELECT id FROM promo_codes WHERE code=?", (code,))
        if not cursor.fetchone():
            return code


def get_promo_code(code):
    """Получает информацию о промокоде"""
    cursor.execute(
        """
        SELECT id, code, discount_percent, discount_fixed_cents, is_active, used_count, max_uses, expires_ts 
        FROM promo_codes WHERE code=?
    """,
        (code,),
    )
    return cursor.fetchone()


def can_use_promo_code(promo_id, user_id):
    """Проверяет может ли пользователь использовать промокод"""
    cursor.execute(
        "SELECT id FROM promo_usage WHERE promo_id=? AND user_id=?", (promo_id, user_id)
    )
    if cursor.fetchone():
        return False, "Вы уже использовали этот промокод"

    cursor.execute(
        "SELECT is_active, max_uses, used_count, expires_ts FROM promo_codes WHERE id=?",
        (promo_id,),
    )
    promo = cursor.fetchone()
    if not promo:
        return False, "Промокод не найден"

    is_active, max_uses, used_count, expires_ts = promo

    if not is_active:
        return False, "Промокод неактивен"

    if max_uses and used_count >= max_uses:
        return False, "Промокод уже использован максимальное количество раз"

    if expires_ts and expires_ts < int(time.time()):
        return False, "Срок действия промокода истек"

    return True, "OK"


def apply_promo_code(price_cents, promo_data):
    """Применяет промокод к цене"""
    (
        promo_id,
        code,
        discount_percent,
        discount_fixed_cents,
        is_active,
        used_count,
        max_uses,
        expires_ts,
    ) = promo_data

    if discount_percent:
        discount = int(price_cents * discount_percent / 100)
        new_price = max(0, price_cents - discount)
        return new_price, f"Промокод {code} применен! Скидка {discount_percent}%"
    elif discount_fixed_cents:
        new_price = max(0, price_cents - discount_fixed_cents)
        return (
            new_price,
            f"Промокод {code} применен! Скидка {price_str_from_cents(discount_fixed_cents)}",
        )

    return price_cents, "Ошибка применения промокода"


def get_payment_options(user_id, plan_id):
    """Возвращает доступные варианты оплаты для пользователя - только полная оплата"""
    cursor.execute("SELECT price_cents FROM plans WHERE id=?", (plan_id,))
    plan = cursor.fetchone()
    if not plan:
        return []

    price_cents = plan[0]

    options = []

    # Всегда предлагаем только полную оплату
    options.append(
        {
            "type": "full",
            "price": price_cents,
            "text": f"💳 Оплатить полностью - {price_str_from_cents(price_cents)}",
            "description": "Доступ до 5 числа следующего месяца",
        }
    )

    return options


# admin ephemeral states
admin_states = {}

# user ephemeral states для ручной оплаты и промокодов
user_states = {}


# ----------------- Update listener (fallback) -----------------
def process_updates(updates):
    for u in updates:
        try:
            if hasattr(u, "my_chat_member") and u.my_chat_member is not None:
                cm = u.my_chat_member
                chat = cm.chat
                new = cm.new_chat_member
                if new.user and new.user.id == BOT_ID:
                    chat_id = chat.id
                    title = chat.title or chat.username or str(chat_id)
                    status = new.status
                    if status in ("administrator", "creator"):
                        add_group_to_db(
                            chat_id,
                            title,
                            chat.type if hasattr(chat, "type") else "group",
                        )
                        for aid in ADMIN_IDS:
                            try:
                                bot.send_message(
                                    aid,
                                    f"✅ Бот получил права администратора в чате: {title} (ID: {chat_id})",
                                )
                            except:
                                pass
                    elif status in ("member",):
                        add_group_to_db(
                            chat_id,
                            title,
                            chat.type if hasattr(chat, "type") else "group",
                        )
                        for aid in ADMIN_IDS:
                            try:
                                bot.send_message(
                                    aid,
                                    f"✅ Бот добавлен в чат: {title} (ID: {chat_id})",
                                )
                            except:
                                pass
                    elif status in ("left", "kicked"):
                        try:
                            cursor.execute(
                                "DELETE FROM managed_groups WHERE chat_id=?", (chat_id,)
                            )
                            conn.commit()
                        except:
                            pass
                        for aid in ADMIN_IDS:
                            try:
                                bot.send_message(
                                    aid,
                                    f"❌ Бот удалён из чата: {title} (ID: {chat_id})",
                                )
                            except:
                                pass
        except Exception:
            logging.exception("Error in process_updates")


bot.set_update_listener(process_updates)


# ----------------- my_chat_member handler -----------------
@bot.my_chat_member_handler()
def handle_my_chat_member(update):
    try:
        chat = update.chat
        new = update.new_chat_member
        old = update.old_chat_member
        chat_id = chat.id
        title = chat.title or chat.username or str(chat_id)
        new_status = new.status
        old_status = old.status if old else None

        logging.info(
            f"my_chat_member update: chat={chat_id} status {old_status} -> {new_status}"
        )

        if new_status in ("administrator", "creator", "member"):
            add_group_to_db(chat_id, title, getattr(chat, "type", "group"))
            for aid in ADMIN_IDS:
                try:
                    bot.send_message(
                        aid,
                        f"✅ Бот активирован/добавлен в чат: {title} (ID: {chat_id}). Статус: {new_status}",
                    )
                except:
                    pass
            try:
                if chat.type in ("group", "supergroup"):
                    bot.send_message(
                        chat_id,
                        "✅ Бот добавлен. Для работы функций с подписками назначьте ему права администратора и используйте /register_group внутри группы.",
                    )
            except Exception:
                pass

        if new_status in ("left", "kicked"):
            try:
                cursor.execute("DELETE FROM managed_groups WHERE chat_id=?", (chat_id,))
                conn.commit()
            except:
                pass
            for aid in ADMIN_IDS:
                try:
                    bot.send_message(
                        aid, f"❌ Бот удалён из чата: {title} (ID: {chat_id})"
                    )
                except:
                    pass

    except Exception:
        logging.exception("Error in handle_my_chat_member")


# ----------------- Main menu / user handlers -----------------
def main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_plans = types.KeyboardButton("📋 Группы обучения")
    # btn_balance = types.KeyboardButton("💰 Баланс")
    # btn_ref = types.KeyboardButton("👥 Реферальная ссылка")
    btn_sub = types.KeyboardButton("🎫 Мои подписки")
    btn_bonus = types.KeyboardButton("🎁 Бонусная программа")  # Новая кнопка
    # markup.row(btn_plans, btn_balance)
    # markup.row(btn_sub, btn_ref)
    markup.row(btn_plans)
    markup.row(btn_sub)
    markup.row(btn_bonus)
    if user_id in ADMIN_IDS:
        markup.row(types.KeyboardButton("⚙️ Админ меню"))
    return markup


@bot.message_handler(func=lambda message: message.text == "🎁 Бонусная программа")
def show_bonus_program(message):
    text = "🎁 Платим вознаграждение 40 byn за приведенного друга!"
    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["start"])
def cmd_start(message):
    args = message.text.split()
    ref = None
    if len(args) > 1:
        token = args[1]
        if token.startswith("ref"):
            try:
                ref = int(token[3:])
            except:
                ref = None
    user_id = message.from_user.id
    if ref and ref != user_id:
        add_user_if_not_exists(
            user_id, referred_by=ref, username=message.from_user.username
        )
        try:
            bot.send_message(
                ref,
                f"🎉 Новый реферал! Пользователь @{message.from_user.username or message.from_user.id} пришёл по вашей ссылке.",
            )
        except:
            pass
        welcome_text = "👋 Привет! Вы пришли по реферальной ссылке."
    else:
        add_user_if_not_exists(user_id, None, username=message.from_user.username)
        welcome_text = "👋 Привет! Добро пожаловать!"

    if message.chat.type in ("group", "supergroup", "channel"):
        bot.send_message(
            message.chat.id,
            f"{welcome_text}\n\nℹ️ Для управления подписками откройте приватный чат со мной: @{ME.username}",
        )
        return

    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu(user_id))


# All user-visible command handlers below will ignore non-private chats (so bot won't chat in groups)
def only_private(fn):
    def wrapper(message, *a, **k):
        if message.chat.type != "private":
            return
        return fn(message, *a, **k)

    return wrapper


@bot.message_handler(func=lambda message: message.text == "📋 Группы обучения")
@only_private
def show_plans(message):
    # Сначала показываем выбор категории
    categories = get_all_categories()
    if not categories:
        bot.send_message(
            message.chat.id,
            "📭 Группы обучения пока не созданы.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    # Сохраняем состояние выбора категории
    user_states[message.from_user.id] = {
        "mode": "select_category",
        "chat_id": message.chat.id,
    }

    markup = types.InlineKeyboardMarkup()
    for cat_id, name, description in categories:
        # Получаем количество групп в категории
        cursor.execute(
            "SELECT COUNT(*) FROM plans WHERE category_id=? AND is_active=1", (cat_id,)
        )
        count = cursor.fetchone()[0]

        button_text = f"{name} ({count})"
        if description:
            button_text = f"{name} - {description} ({count})"

        markup.add(
            types.InlineKeyboardButton(
                button_text, callback_data=f"user_select_category:{cat_id}"
            )
        )

    bot.send_message(
        message.chat.id,
        "📚 <b>Выберите предмет:</b>\n\n"
        "Выберите интересующий вас предмет чтобы увидеть доступные группы обучения:",
        parse_mode="HTML",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("user_select_category:")
)
def callback_user_select_category(call):
    try:
        user = call.from_user
        category_id = int(call.data.split(":")[1])

        # Получаем информацию о категории
        category = get_category_by_id(category_id)
        if not category:
            bot.answer_callback_query(call.id, "❌ Предмет не найден.")
            return

        category_name = category[1]

        # Получаем группы для этой категории
        cursor.execute(
            """
            SELECT p.id, p.title, p.price_cents, p.duration_days, p.description, 
                   p.media_file_id, p.media_type, p.media_file_ids, p.group_id, mg.title as group_title
            FROM plans p
            LEFT JOIN managed_groups mg ON p.group_id = mg.chat_id
            WHERE p.is_active=1 AND p.category_id=?
            ORDER BY p.id
        """,
            (category_id,),
        )

        rows = cursor.fetchall()

        if not rows:
            bot.answer_callback_query(
                call.id, f"📭 В предмете '{category_name}' пока нет групп."
            )

            # Предлагаем вернуться к выбору категории
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    "🔙 Назад к выбору предмета", callback_data="back_to_categories"
                )
            )

            bot.send_message(
                call.message.chat.id,
                f"📭 В предмете '{category_name}' пока нет доступных групп обучения.",
                reply_markup=markup,
            )
            return

        chat_id = call.message.chat.id

        # Если групп больше одной - показываем список групп
        if len(rows) > 1:
            markup = types.InlineKeyboardMarkup()
            for r in rows:
                (
                    pid,
                    title,
                    price_cents,
                    days,
                    desc,
                    media_file_id,
                    media_type,
                    media_file_ids,
                    group_id,
                    group_title,
                ) = r
                button_text = f"{title}"
                markup.add(
                    types.InlineKeyboardButton(
                        button_text, callback_data=f"user_select_plan:{pid}"
                    )
                )

            markup.add(
                types.InlineKeyboardButton(
                    "🔙 Назад к выбору предмета", callback_data="back_to_categories"
                )
            )

            bot.answer_callback_query(call.id, f"📚 {category_name}")
            bot.send_message(
                chat_id,
                f"📚 <b>Предмет: {category_name}</b>\n\n" f"Выберите группу обучения:",
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        # Если группа только одна - сразу показываем её информацию с кнопкой оплаты
        r = rows[0]
        (
            pid,
            title,
            price_cents,
            days,
            desc,
            media_file_id,
            media_type,
            media_file_ids,
            group_id,
            group_title,
        ) = r

        # Получаем доступные варианты оплаты
        payment_options = get_payment_options(user.id, pid)

        text = (
            f"💳 <b>Оформление подписки на группу '{title}'</b>\n\n"
            f"💰 Цена в месяц: {price_str_from_cents(price_cents)}\n"
            f"📋 Описание: {desc}\n\n"
        )

        markup = types.InlineKeyboardMarkup()

        if payment_options:
            text += "<b>Детали</b>\n"
            for option in payment_options:
                text += f"• {option['text']}\n  {option['description']}\n\n"

            for option in payment_options:
                markup.add(
                    types.InlineKeyboardButton(
                        f"💸 Оплатить {price_str_from_cents(option['price'])}",
                        callback_data=f"buy_{option['type']}:{pid}",
                    )
                )

            # Добавляем кнопку оплаты с промокодом
            markup.add(
                types.InlineKeyboardButton(
                    "🎫 Оплатить с промокодом", callback_data=f"buy_with_promo:{pid}"
                )
            )
        else:
            active_type = get_active_payment_type()
            if active_type == "second":
                text += "❌ <b>У вас нет активной первой части оплаты для этой группы.</b>\n\n"
            else:
                text += "❌ <b>Сейчас не период оплаты.</b>\n\n"

            text += (
                "💳 <b>Периоды оплаты:</b>\n"
                "• 1-5 числа: полная оплата или первая часть\n"
                "• 15-20 числа: вторая часть (только при оплаченной первой)\n"
                "• В другое время: полная оплата\n\n"
                "Возвращайтесь в указанные даты!"
            )

        markup.add(
            types.InlineKeyboardButton(
                "🔙 Назад к списку групп", callback_data="back_to_plans_list"
            )
        )

        bot.answer_callback_query(call.id)

        # Отправляем медиа если есть
        media_ids_list = []
        if media_file_ids:
            media_ids_list = [
                m.strip()
                for m in media_file_ids.split(",")
                if m.strip() and is_valid_file_id(m.strip())
            ]
        elif media_file_id and is_valid_file_id(media_file_id.strip()):
            media_ids_list = [media_file_id.strip()]

        try:
            if len(media_ids_list) > 1:
                media_group = []
                valid_media_count = 0

                for m in media_ids_list[:10]:
                    if media_type == "photo":
                        media_group.append(types.InputMediaPhoto(m))
                        valid_media_count += 1
                    elif media_type == "video":
                        media_group.append(types.InputMediaVideo(m))
                        valid_media_count += 1

                if valid_media_count > 0:
                    if valid_media_count == 1:
                        if media_type == "photo":
                            bot.send_photo(
                                chat_id,
                                media_ids_list[0],
                                caption=text,
                                parse_mode="HTML",
                                reply_markup=markup,
                            )
                        elif media_type == "video":
                            bot.send_video(
                                chat_id,
                                media_ids_list[0],
                                caption=text,
                                parse_mode="HTML",
                                reply_markup=markup,
                            )
                    else:
                        bot.send_media_group(chat_id, media_group)
                        bot.send_message(
                            chat_id, text, parse_mode="HTML", reply_markup=markup
                        )
                else:
                    bot.send_message(
                        chat_id, text, parse_mode="HTML", reply_markup=markup
                    )

            elif len(media_ids_list) == 1:
                m = media_ids_list[0]
                if media_type == "photo":
                    bot.send_photo(
                        chat_id, m, caption=text, parse_mode="HTML", reply_markup=markup
                    )
                elif media_type == "video":
                    bot.send_video(
                        chat_id, m, caption=text, parse_mode="HTML", reply_markup=markup
                    )
                else:
                    bot.send_message(
                        chat_id, text, parse_mode="HTML", reply_markup=markup
                    )
            else:
                bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

        except Exception as e:
            logging.exception("Error sending plan media with payment")
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

    except Exception as e:
        logging.exception("Error in callback_user_select_category")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе предмета")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("buy_for_existing:")
)
def callback_buy_for_existing(call):
    """Оплата для существующей подписки"""
    try:
        user = call.from_user
        plan_id = int(call.data.split(":")[1])

        # Проверяем существующую подписку
        existing_sub = check_existing_subscription(user.id, plan_id)
        if not existing_sub:
            bot.answer_callback_query(call.id, "❌ Подписка не найдена")
            return

        # ИСПРАВЛЕННАЯ ПРОВЕРКА: проверяем 'paid' вместо всего объекта
        if existing_sub["paid"]:
            bot.answer_callback_query(call.id, "✅ Подписка уже оплачена")
            return

        # Получаем информацию о тарифе
        cursor.execute(
            "SELECT title, price_cents, description, group_id FROM plans WHERE id=?",
            (plan_id,),
        )
        plan = cursor.fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "❌ Тариф не найден.")
            return

        title, price_cents, description, group_id = plan

        # Показываем выбор способа оплаты
        user_states[user.id] = {
            "plan_id": plan_id,
            "original_price": price_cents,
            "title": title,
            "description": description,
            "group_id": group_id,
            "payment_type": "full",
            "mode": "renewal",  # Режим продления
        }

        payment_methods = get_active_payment_methods()
        if not payment_methods:
            bot.answer_callback_query(call.id, "❌ Нет доступных способов оплаты")
            return

        if len(payment_methods) == 1:
            method_id, name, mtype, method_desc, details = payment_methods[0]
            if mtype == "card":
                process_card_payment(
                    call,
                    plan_id,
                    user,
                    title,
                    price_cents,
                    description,
                    group_id,
                    "full",
                )
            else:
                process_manual_payment_start(
                    call,
                    plan_id,
                    user,
                    title,
                    price_cents,
                    description,
                    details,
                    "full",
                )
        else:
            markup = types.InlineKeyboardMarkup()
            for method_id, name, mtype, method_desc, details in payment_methods:
                markup.add(
                    types.InlineKeyboardButton(
                        name, callback_data=f"paymethod:{plan_id}:{method_id}:full"
                    )
                )

            markup.add(
                types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
            )

            bot.answer_callback_query(call.id, "💳 Выберите способ оплаты")
            bot.send_message(
                call.message.chat.id,
                f"💳 <b>Продление подписки на '{title}'</b>",
                parse_mode="HTML",
                reply_markup=markup,
            )

    except Exception as e:
        logging.exception("Error in callback_buy_for_existing")
        bot.answer_callback_query(call.id, "❌ Ошибка при оформлении заказа")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("user_select_plan:")
)
def callback_user_select_plan(call):
    """Обработчик выбора конкретной группы из списка"""
    try:
        user = call.from_user
        plan_id = int(call.data.split(":")[1])

        # Проверяем существующую подписку
        existing_sub = check_existing_subscription(user.id, plan_id)

        # Получаем информацию о группе
        cursor.execute(
            """
            SELECT p.id, p.title, p.price_cents, p.duration_days, p.description, 
                   p.media_file_id, p.media_type, p.media_file_ids, p.group_id, mg.title as group_title,
                   c.name as category_name
            FROM plans p
            LEFT JOIN managed_groups mg ON p.group_id = mg.chat_id
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.id=?
        """,
            (plan_id,),
        )

        r = cursor.fetchone()
        if not r:
            bot.answer_callback_query(call.id, "❌ Группа не найдена.")
            return

        (
            pid,
            title,
            price_cents,
            days,
            desc,
            media_file_id,
            media_type,
            media_file_ids,
            group_id,
            group_title,
            category_name,
        ) = r

        # Формируем текст в зависимости от состояния
        if existing_sub and existing_sub["paid"]:
            # Если подписка уже оплачена на текущий месяц
            end_date = datetime.fromtimestamp(
                existing_sub["end_ts"], LOCAL_TZ
            ).strftime("%d.%m.%Y %H:%M")
            text = (
                f"✅ <b>У вас уже есть активная подписка на эту группу!</b>\n\n"
                f"🏷️ Группа: {title}\n"
                f"📚 Предмет: {category_name}\n"
                f"📅 Оплачено до: {end_date}\n\n"
                f"Следующая оплата потребуется <b>{datetime.fromtimestamp(existing_sub['end_ts'], LOCAL_TZ).strftime('%d.%m.%Y')}</b>."
            )

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    "🔙 Назад к списку групп", callback_data="back_to_plans_list"
                )
            )

            bot.answer_callback_query(call.id, "✅ Подписка активна")

        elif existing_sub and existing_sub["needs_renewal"]:
            # Есть подписка, но нужно продление
            old_end_date = datetime.fromtimestamp(
                existing_sub["end_ts"], LOCAL_TZ
            ).strftime("%d.%m.%Y %H:%M")

            text = (
                f"🔄 <b>Продление подписки на группу '{title}'</b>\n\n"
                f"📚 Предмет: {category_name}\n"
                f"📅 Текущая подписка действительна до: {old_end_date}\n"
                f"💰 Цена продления: {price_str_from_cents(price_cents)}\n"
                f"📋 Описание: {desc or 'Описание отсутствует'}\n\n"
                f"<i>После оплаты срок действия будет продлен на месяц.</i>"
            )

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    f"🔄 Продлить подписку", callback_data=f"renew_plan:{plan_id}"
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "🎫 Оплатить с промокодом",
                    callback_data=f"buy_with_promo:{plan_id}",
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "🔙 Назад к списку групп", callback_data="back_to_plans_list"
                )
            )

            bot.answer_callback_query(call.id, f"📋 {title}")

        else:
            # Новой подписки нет или она полностью истекла
            text = (
                f"💳 <b>Оформление подписки на группу '{title}'</b>\n\n"
                f"📚 Предмет: {category_name}\n"
                f"💰 Цена в месяц: {price_str_from_cents(price_cents)}\n"
                f"📋 Описание: {desc or 'Описание отсутствует'}\n\n"
                f"<b>Детали оплаты:</b>\n"
                f"• Полная оплата - доступ до 5 числа следующего месяца\n"
                f"• Оплата принимается с 1 по 5 число каждого месяца"
            )

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    f"💸 Оплатить {price_str_from_cents(price_cents)}",
                    callback_data=f"buy_full:{plan_id}",
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "🎫 Оплатить с промокодом",
                    callback_data=f"buy_with_promo:{plan_id}",
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "🔙 Назад к списку групп", callback_data="back_to_plans_list"
                )
            )

            bot.answer_callback_query(call.id, f"📋 {title}")

        # Отправляем медиа если есть
        media_ids_list = []
        if media_file_ids:
            media_ids_list = [
                m.strip()
                for m in media_file_ids.split(",")
                if m.strip() and is_valid_file_id(m.strip())
            ]
        elif media_file_id and is_valid_file_id(media_file_id.strip()):
            media_ids_list = [media_file_id.strip()]

        try:
            if len(media_ids_list) > 1:
                media_group = []
                valid_media_count = 0

                for m in media_ids_list[:10]:
                    if media_type == "photo":
                        media_group.append(types.InputMediaPhoto(m))
                        valid_media_count += 1
                    elif media_type == "video":
                        media_group.append(types.InputMediaVideo(m))
                        valid_media_count += 1

                if valid_media_count > 0:
                    if valid_media_count == 1:
                        if media_type == "photo":
                            bot.send_photo(
                                call.message.chat.id,
                                media_ids_list[0],
                                caption=text,
                                parse_mode="HTML",
                                reply_markup=markup,
                            )
                        elif media_type == "video":
                            bot.send_video(
                                call.message.chat.id,
                                media_ids_list[0],
                                caption=text,
                                parse_mode="HTML",
                                reply_markup=markup,
                            )
                    else:
                        bot.send_media_group(call.message.chat.id, media_group)
                        bot.send_message(
                            call.message.chat.id,
                            text,
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                else:
                    bot.send_message(
                        call.message.chat.id,
                        text,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )

            elif len(media_ids_list) == 1:
                m = media_ids_list[0]
                if media_type == "photo":
                    bot.send_photo(
                        call.message.chat.id,
                        m,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                elif media_type == "video":
                    bot.send_video(
                        call.message.chat.id,
                        m,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                else:
                    bot.send_message(
                        call.message.chat.id,
                        text,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
            else:
                bot.send_message(
                    call.message.chat.id, text, parse_mode="HTML", reply_markup=markup
                )

        except Exception as e:
            logging.exception("Error sending plan media with payment")
            bot.send_message(
                call.message.chat.id, text, parse_mode="HTML", reply_markup=markup
            )

    except Exception as e:
        logging.exception("Error in callback_user_select_plan")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе группы")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("new_link:")
)
def callback_new_link(call):
    """
    Генерирует новую одноразовую пригласительную ссылку для подписки.
    Callback data: new_link:{subscription_id}
    """
    try:
        uid = call.from_user.id
        parts = call.data.split(":")
        if len(parts) < 2:
            bot.answer_callback_query(call.id, "❌ Неверные данные.")
            return

        sub_id = int(parts[1])

        # Получаем подписку
        cursor.execute(
            "SELECT user_id, plan_id, group_id, invite_link FROM subscriptions WHERE id=?",
            (sub_id,),
        )
        row = cursor.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "❌ Подписка не найдена.")
            return

        sub_user_id, plan_id, group_id, old_invite = row

        # Разрешаем только владельцу подписки или админам получать новую ссылку
        if uid != sub_user_id and call.from_user.id not in ADMIN_IDS:
            bot.answer_callback_query(
                call.id,
                "🚫 Только владелец подписки или администратор может запросить новую ссылку.",
            )
            return

        if not group_id:
            bot.answer_callback_query(
                call.id,
                "❌ Для этой подписки не указана группа (group_id). Свяжитесь с администрацией.",
            )
            return

        # Проверяем, что бот администратор в группе (рекомендуется)
        try:
            if not is_bot_admin_in_chat(group_id):
                # Попробуем предупредить больше информативно
                bot.answer_callback_query(
                    call.id,
                    "❌ Я не администратор в целевой группе — не могу создать ссылку. Назначьте боту права администратора.",
                )
                return
        except Exception:
            # не критично — попробуем создать ссылку через API, но предупредим
            logging.warning(
                "Не удалось проверить статус бота в группе, пробуем создать ссылку напрямую."
            )

        # Создаем новую одноразовую ссылку (expire_seconds можно менять)
        invite = create_chat_invite_link_one_time(
            BOT_TOKEN, group_id, expire_seconds=7 * 24 * 3600, member_limit=1
        )
        if not invite:
            bot.answer_callback_query(
                call.id,
                "❌ Не удалось создать пригласительную ссылку. Попробуйте позже.",
            )
            logging.warning(
                f"createChatInviteLink вернул None для group_id={group_id}, sub_id={sub_id}"
            )
            return

        # Сохраняем новую ссылку в БД
        try:
            cursor.execute(
                "UPDATE subscriptions SET invite_link=?, last_notification_ts=? WHERE id=?",
                (invite, int(time.time()), sub_id),
            )
            conn.commit()
        except Exception as e:
            logging.exception("Ошибка записи новой ссылки в БД")
            bot.answer_callback_query(
                call.id,
                "❌ Не удалось сохранить ссылку в базе данных (админ уведомлён).",
            )
            return

        # Отправляем ссылку пользователю (и отвечаем на callback)
        try:
            bot.answer_callback_query(call.id, "🔗 Сгенерирована новая ссылка!")
            bot.send_message(
                sub_user_id,
                f"🔗 Ваша новая одноразовая пригласительная ссылка:\n\n{invite}\n\nСсылка одноразовая и действительна короткое время.",
            )
        except Exception as e:
            logging.exception("Ошибка отправки новой ссылки пользователю")
            # если не удалось отправить пользователю (например, мы в колбэке от админа),
            # отправим в чат где нажали кнопку
            try:
                bot.send_message(call.message.chat.id, f"🔗 Ссылка:\n\n{invite}")
            except:
                pass

    except Exception as e:
        logging.exception("Ошибка в callback_new_link")
        try:
            bot.answer_callback_query(
                call.id, "❌ Внутренняя ошибка при создании ссылки."
            )
        except:
            pass


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("buy_full:")
)
def callback_buy_full(call):
    """Обработчик покупки новой подписки"""
    try:
        user = call.from_user
        plan_id = int(call.data.split(":")[1])

        cursor.execute(
            "SELECT title, price_cents, description, group_id FROM plans WHERE id=?",
            (plan_id,),
        )
        plan = cursor.fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "❌ Группа не найдена.")
            return
        title, price_cents, description, group_id = plan

        # Сохраняем информацию о выбранном тарифе
        user_states[user.id] = {
            "plan_id": plan_id,
            "original_price": price_cents,
            "title": title,
            "description": description,
            "group_id": group_id,
            "payment_type": "full",
            "mode": "new_subscription",  # Новая подписка
        }

        # Показываем выбор способа оплаты
        payment_methods = get_active_payment_methods()
        if not payment_methods:
            bot.answer_callback_query(call.id, "❌ Нет доступных способов оплаты")
            return

        if len(payment_methods) == 1:
            method_id, name, mtype, method_desc, details = payment_methods[0]
            if mtype == "card":
                process_card_payment(
                    call,
                    plan_id,
                    user,
                    title,
                    price_cents,
                    description,
                    group_id,
                    "full",
                )
            else:
                process_manual_payment_start(
                    call,
                    plan_id,
                    user,
                    title,
                    price_cents,
                    description,
                    details,
                    "full",
                )
        else:
            markup = types.InlineKeyboardMarkup()
            for method_id, name, mtype, method_desc, details in payment_methods:
                markup.add(
                    types.InlineKeyboardButton(
                        name, callback_data=f"paymethod_new:{plan_id}:{method_id}:full"
                    )
                )

            markup.add(
                types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
            )

            bot.answer_callback_query(call.id, "💳 Выберите способ оплаты")
            bot.send_message(
                call.message.chat.id,
                f"💳 <b>Оплата новой подписки на '{title}'</b>",
                parse_mode="HTML",
                reply_markup=markup,
            )

    except Exception as e:
        logging.exception("Error in callback_buy_full")
        bot.answer_callback_query(call.id, "❌ Ошибка при оформлении заказа")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("paymethod_new:")
)
def callback_paymethod_new(call):
    """Обработка выбора способа оплаты для новой подписки"""
    try:
        parts = call.data.split(":")
        pid = int(parts[1])
        method_id = int(parts[2])
        payment_type = parts[3]

        user = call.from_user

        if (
            user.id not in user_states
            or user_states[user.id].get("mode") != "new_subscription"
        ):
            bot.answer_callback_query(call.id, "❌ Сессия устарела")
            return

        state = user_states[user.id]

        method = get_payment_method_by_id(method_id)
        if not method:
            bot.answer_callback_query(call.id, "❌ Способ оплаты не найден.")
            return

        method_id, name, mtype, method_desc, details = method

        if mtype == "card":
            process_card_payment(
                call,
                pid,
                user,
                state["title"],
                state["original_price"],
                state["description"],
                state["group_id"],
                payment_type,
            )
        else:  # manual
            process_manual_payment_start(
                call,
                pid,
                user,
                state["title"],
                state["original_price"],
                state["description"],
                details,
                payment_type,
            )

    except Exception as e:
        logging.exception("Error in callback_paymethod_new")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе способа оплаты")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("renew_plan:")
)
def callback_renew_plan(call):
    """Обработчик продления существующей подписки"""
    try:
        user = call.from_user
        plan_id = int(call.data.split(":")[1])

        # Проверяем существующую подписку
        existing_sub = check_existing_subscription(user.id, plan_id)

        if not existing_sub:
            bot.answer_callback_query(call.id, "❌ Подписка не найдена.")
            return

        if existing_sub["paid"]:
            bot.answer_callback_query(
                call.id, "✅ Подписка уже оплачена на текущий месяц."
            )
            return

        # Получаем информацию о тарифе
        cursor.execute(
            "SELECT title, price_cents, description, group_id FROM plans WHERE id=?",
            (plan_id,),
        )
        plan = cursor.fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "❌ Тариф не найден.")
            return

        title, price_cents, description, group_id = plan

        # Показываем выбор способа оплаты для продления
        user_states[user.id] = {
            "plan_id": plan_id,
            "original_price": price_cents,
            "title": title,
            "description": description,
            "group_id": group_id,
            "payment_type": "full",
            "mode": "renewal",
            "existing_sub_id": existing_sub["id"],  # Сохраняем ID существующей подписки
        }

        payment_methods = get_active_payment_methods()
        if not payment_methods:
            bot.answer_callback_query(call.id, "❌ Нет доступных способов оплаты")
            return

        if len(payment_methods) == 1:
            method_id, name, mtype, method_desc, details = payment_methods[0]
            if mtype == "card":
                process_card_payment(
                    call,
                    plan_id,
                    user,
                    title,
                    price_cents,
                    description,
                    group_id,
                    "full",
                )
            else:
                process_manual_payment_start(
                    call,
                    plan_id,
                    user,
                    title,
                    price_cents,
                    description,
                    details,
                    "full",
                )
        else:
            markup = types.InlineKeyboardMarkup()
            for method_id, name, mtype, method_desc, details in payment_methods:
                markup.add(
                    types.InlineKeyboardButton(
                        name,
                        callback_data=f"paymethod_renew:{plan_id}:{method_id}:full",
                    )
                )

            markup.add(
                types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
            )

            bot.answer_callback_query(call.id, "💳 Выберите способ оплаты")
            bot.send_message(
                call.message.chat.id,
                f"💳 <b>Продление подписки на '{title}'</b>",
                parse_mode="HTML",
                reply_markup=markup,
            )

    except Exception as e:
        logging.exception("Error in callback_renew_plan")
        bot.answer_callback_query(call.id, "❌ Ошибка при оформлении продления")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("paymethod_renew:")
)
def callback_paymethod_renew(call):
    """Обработка выбора способа оплаты для продления"""
    try:
        parts = call.data.split(":")
        pid = int(parts[1])
        method_id = int(parts[2])
        payment_type = parts[3]

        user = call.from_user

        if user.id not in user_states or user_states[user.id].get("mode") != "renewal":
            bot.answer_callback_query(call.id, "❌ Сессия устарела")
            return

        state = user_states[user.id]

        method = get_payment_method_by_id(method_id)
        if not method:
            bot.answer_callback_query(call.id, "❌ Способ оплаты не найден.")
            return

        method_id, name, mtype, method_desc, details = method

        if mtype == "card":
            process_card_payment(
                call,
                pid,
                user,
                state["title"],
                state["original_price"],
                state["description"],
                state["group_id"],
                payment_type,
            )
        else:  # manual
            process_manual_payment_start(
                call,
                pid,
                user,
                state["title"],
                state["original_price"],
                state["description"],
                details,
                payment_type,
            )

    except Exception as e:
        logging.exception("Error in callback_paymethod_renew")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе способа оплаты")


def send_plan_info(
    chat_id,
    plan_id,
    title,
    price_cents,
    description,
    media_file_id,
    media_type,
    media_file_ids,
    group_title,
):
    """Функция для отправки информации о группе с медиа и кнопкой выбора"""
    txt = f"<b>{title}</b>\n{description}\n\n💵 Цена в месяц: {price_str_from_cents(price_cents)}"
    if group_title:
        txt += f"\n🏠 Группа: {group_title}"

    media_ids_list = []
    if media_file_ids:
        # Фильтруем только валидные file_id
        media_ids_list = [
            m.strip()
            for m in media_file_ids.split(",")
            if m.strip() and is_valid_file_id(m.strip())
        ]
    elif media_file_id and is_valid_file_id(media_file_id.strip()):
        media_ids_list = [media_file_id.strip()]

    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(
                "✅ Выбрать", callback_data=f"select_plan:{plan_id}"
            )
        )
        markup.add(
            types.InlineKeyboardButton(
                "🔙 Назад к списку групп", callback_data="back_to_plans_list"
            )
        )

        if len(media_ids_list) > 1:
            media_group = []
            valid_media_count = 0

            for m in media_ids_list[:10]:  # Ограничиваем 10 медиа
                if media_type == "photo":
                    media_group.append(types.InputMediaPhoto(m))
                    valid_media_count += 1
                elif media_type == "video":
                    media_group.append(types.InputMediaVideo(m))
                    valid_media_count += 1

            # Отправляем медиагруппу только если есть валидные медиа
            if valid_media_count > 0:
                if valid_media_count == 1:
                    # Если только одно медиа, отправляем как одиночное
                    if media_type == "photo":
                        bot.send_photo(
                            chat_id,
                            media_ids_list[0],
                            caption=txt,
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                    elif media_type == "video":
                        bot.send_video(
                            chat_id,
                            media_ids_list[0],
                            caption=txt,
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                else:
                    # Если несколько медиа, отправляем группой
                    bot.send_media_group(chat_id, media_group)
                    bot.send_message(
                        chat_id, txt, parse_mode="HTML", reply_markup=markup
                    )
            else:
                # Если нет валидных медиа, отправляем только текст
                bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=markup)

        elif len(media_ids_list) == 1:
            # Одно медиа
            m = media_ids_list[0]
            if media_type == "photo":
                bot.send_photo(
                    chat_id, m, caption=txt, parse_mode="HTML", reply_markup=markup
                )
            elif media_type == "video":
                bot.send_video(
                    chat_id, m, caption=txt, parse_mode="HTML", reply_markup=markup
                )
            else:
                bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=markup)
        else:
            # Нет медиа
            bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=markup)

    except Exception as e:
        logging.exception("Error sending plan media")
        # При ошибке отправляем хотя бы текст
        try:
            bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=markup)
        except:
            pass


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("buy_with_promo:")
)
def callback_buy_with_promo(call):
    """Обработчик кнопки 'Оплатить с промокодом'"""
    try:
        user = call.from_user
        pid = int(call.data.split(":")[1])

        cursor.execute(
            "SELECT title, price_cents, description, group_id FROM plans WHERE id=?",
            (pid,),
        )
        plan = cursor.fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "❌ Группа не найдена.")
            return
        title, price_cents, description, group_id = plan

        # Сохраняем информацию о выбранном тарифе для промокода
        user_states[user.id] = {
            "plan_id": pid,
            "original_price": price_cents,
            "title": title,
            "description": description,
            "group_id": group_id,
            "payment_type": "full",  # Всегда полная оплата с промокодом
            "mode": "promo_input_direct",
        }

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_promo_input")
        )

        bot.answer_callback_query(call.id, "🎫 Введите промокод")
        bot.send_message(
            call.message.chat.id,
            f"🎫 <b>Оплата с промокодом группы '{title}'</b>\n\n"
            f"💰 Исходная цена: {price_str_from_cents(price_cents)}\n\n"
            f"Введите ваш промокод:\n"
            f"Или нажмите '❌ Отмена' для возврата",
            parse_mode="HTML",
            reply_markup=markup,
        )

    except Exception as e:
        logging.exception("Error in callback_buy_with_promo")
        bot.answer_callback_query(call.id, "❌ Ошибка при оформлении заказа")


@bot.message_handler(
    func=lambda m: m.from_user.id in user_states
    and user_states[m.from_user.id].get("mode") == "promo_input_direct"
    and m.text
    and not m.text.startswith("/")
)
def handle_promo_code_input_direct(message):
    """Обработчик ввода промокода при прямом выборе 'Оплатить с промокодом'"""
    user_id = message.from_user.id
    state = user_states[user_id]

    promo_code = message.text.strip().upper()

    # Проверяем промокод
    promo_data = get_promo_code(promo_code)
    if not promo_data:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_promo_input")
        )

        bot.send_message(
            message.chat.id,
            "❌ Промокод не найден. Попробуйте другой промокод.\n"
            "Или нажмите '❌ Отмена' для возврата",
            reply_markup=markup,
        )
        return

    can_use, reason = can_use_promo_code(promo_data[0], user_id)
    if not can_use:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_promo_input")
        )

        bot.send_message(
            message.chat.id,
            f"❌ {reason}\n" "Нажмите '❌ Отмена' для возврата",
            reply_markup=markup,
        )
        return

    # Применяем промокод
    new_price, promo_message = apply_promo_code(state["original_price"], promo_data)
    state["promo_id"] = promo_data[0]
    state["promo_code"] = promo_code
    state["final_price"] = new_price
    state["mode"] = "promo_applied_direct"

    # Показываем выбор способа оплаты с учетом скидки
    payment_methods = get_active_payment_methods()
    if not payment_methods:
        bot.send_message(message.chat.id, "❌ Нет доступных способов оплаты")
        return

    text = (
        f"💳 <b>Оплата группы '{state['title']}' с промокодом</b>\n\n"
        f"💰 Исходная цена: {price_str_from_cents(state['original_price'])}\n"
        f"🎫 {promo_message}\n"
        f"💵 Итоговая цена: {price_str_from_cents(new_price)}\n\n"
        f"Выберите способ оплаты:"
    )

    if len(payment_methods) == 1:
        method_id, name, mtype, method_desc, details = payment_methods[0]
        if mtype == "card":
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    "💳 Оплатить картой",
                    callback_data=f"pay_with_promo_direct:{state['plan_id']}",
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "❌ Отмена", callback_data="cancel_promo_input"
                )
            )
            bot.send_message(
                message.chat.id, text, parse_mode="HTML", reply_markup=markup
            )
        else:
            process_manual_payment_start_from_message(
                message,
                state["plan_id"],
                state["title"],
                new_price,
                state["description"],
                details,
                "full",
                state["promo_id"],
            )
    else:
        markup = types.InlineKeyboardMarkup()
        for method_id, name, mtype, method_desc, details in payment_methods:
            markup.add(
                types.InlineKeyboardButton(
                    name,
                    callback_data=f"paymethod_promo_direct:{state['plan_id']}:{method_id}:{state['promo_id']}",
                )
            )
        markup.add(
            types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_promo_input")
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("pay_with_promo_direct:")
)
def callback_pay_with_promo_direct(call):
    """Оплата картой с примененным промокодом (прямой путь)"""
    user_id = call.from_user.id
    if user_id not in user_states or "final_price" not in user_states[user_id]:
        bot.answer_callback_query(call.id, "❌ Сессия устарела")
        return

    state = user_states[user_id]
    plan_id = int(call.data.split(":")[1])

    process_card_payment(
        call,
        plan_id,
        call.from_user,
        state["title"],
        state["final_price"],
        state["description"],
        state["group_id"],
        "full",
        state.get("promo_id"),
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("paymethod_promo_direct:")
)
def callback_paymethod_promo_direct(call):
    """Обработка выбора способа оплаты с промокодом (прямой путь)"""
    try:
        parts = call.data.split(":")
        plan_id = int(parts[1])
        method_id = int(parts[2])
        promo_id = int(parts[3])

        user = call.from_user

        if user.id not in user_states or "final_price" not in user_states[user.id]:
            bot.answer_callback_query(call.id, "❌ Сессия устарела")
            return

        state = user_states[user.id]

        method = get_payment_method_by_id(method_id)
        if not method:
            bot.answer_callback_query(call.id, "❌ Способ оплаты не найден.")
            return

        method_id, name, mtype, method_desc, details = method

        if mtype == "card":
            process_card_payment(
                call,
                plan_id,
                user,
                state["title"],
                state["final_price"],
                state["description"],
                state["group_id"],
                "full",
                promo_id,
            )
        else:  # manual
            process_manual_payment_start(
                call,
                plan_id,
                user,
                state["title"],
                state["final_price"],
                state["description"],
                details,
                "full",
                promo_id,
            )

    except Exception as e:
        logging.exception("Error in callback_paymethod_promo_direct")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе способа оплаты")


@bot.callback_query_handler(func=lambda call: call.data == "back_to_plans_list")
def callback_back_to_plans_list(call):
    """Возврат к списку групп в выбранном предмете"""
    try:
        # Просто вызываем show_plans для возврата к выбору предмета
        show_plans(call.message)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.exception("Error in callback_back_to_plans_list")
        bot.answer_callback_query(call.id, "❌ Ошибка")


@bot.callback_query_handler(func=lambda call: call.data == "back_to_categories")
def callback_back_to_categories(call):
    """Возврат к выбору категории"""
    show_plans(call.message)
    bot.answer_callback_query(call.id)


def is_valid_file_id(file_id):
    """Проверяет валидность file_id"""
    if not file_id or not isinstance(file_id, str):
        return False
    # file_id обычно состоит из букв, цифр и некоторых символов
    # Минимальная длина file_id обычно больше 10 символов
    if len(file_id) < 10:
        return False
    # Проверяем на наличие только допустимых символов
    import re

    pattern = r"^[A-Za-z0-9_-]+$"
    return bool(re.match(pattern, file_id))


@bot.message_handler(func=lambda message: message.text == "💰 Баланс")
@only_private
def show_balance(message):
    uid = message.from_user.id
    cursor.execute("SELECT cashback_cents FROM users WHERE user_id=?", (uid,))
    r = cursor.fetchone()
    bal = r[0] if r else 0
    bot.send_message(
        message.chat.id, f"💰 Ваш баланс кэшбэка: {price_str_from_cents(bal)}"
    )


@bot.message_handler(func=lambda message: message.text == "👥 Реферальная ссылка")
@only_private
def show_ref(message):
    uid = message.from_user.id
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start=ref{uid}"
    bot.send_message(
        message.chat.id,
        f"👥 Ваша реферальная ссылка:\n\n{link}\n\n💡 Делитесь и получайте {REFERRAL_PERCENT}% кэшбэка!",
    )


@bot.message_handler(func=lambda message: message.text == "🎫 Мои подписки")
@only_private
def show_my_subscription(message):
    """Показывает подписки пользователя с кнопкой продления"""
    uid = message.from_user.id
    cursor.execute(
        """
        SELECT s.id, s.plan_id, s.start_ts, s.end_ts, s.active, s.invite_link, 
               p.title, s.payment_type, s.part_paid, s.current_period_month, 
               s.current_period_year, p.price_cents
        FROM subscriptions s
        LEFT JOIN plans p ON s.plan_id = p.id
        WHERE s.user_id=? AND s.active=1
        ORDER BY s.end_ts DESC
    """,
        (uid,),
    )
    rows = cursor.fetchall()

    if not rows:
        bot.send_message(uid, "📭 У вас нет активных подписок.")
        return

    current_month, current_year = get_current_period()
    now_ts = int(time.time())

    for row in rows:
        (
            sid,
            pid,
            start_ts,
            end_ts,
            active,
            invite_link,
            title,
            payment_type,
            part_paid,
            period_month,
            period_year,
            price_cents,
        ) = row

        # Правильная проверка статуса
        if (
            period_month == current_month
            and period_year == current_year
            and part_paid == "full"
        ):
            if end_ts > now_ts:
                status_text = "✅ Активна"
                needs_renewal = False
            else:
                status_text = "❌ Истекла"
                needs_renewal = True
        else:
            status_text = "🔄 Требуется продление"
            needs_renewal = True

        txt = (
            f"🎫 <b>Группа: {title or pid}</b>\n"
            f"💳 Тип оплаты: Полная оплата\n"
            f"📊 Статус: {status_text}\n"
            f"⏰ Действует до: {datetime.fromtimestamp(end_ts, LOCAL_TZ).strftime('%d.%m.%Y %H:%M')}"
        )

        if invite_link:
            txt += f"\n🔗 Ваша пригласительная ссылка:\n{invite_link}"

        markup = types.InlineKeyboardMarkup()

        if needs_renewal:
            markup.add(
                types.InlineKeyboardButton(
                    f"🔄 Продлить за {price_str_from_cents(price_cents)}",
                    callback_data=f"renew_plan:{pid}",
                )
            )

        markup.add(
            types.InlineKeyboardButton(
                "🔗 Получить новую ссылку", callback_data=f"new_link:{sid}"
            )
        )

        bot.send_message(uid, txt, parse_mode="HTML", reply_markup=markup)


# ----------------- Payment callbacks ----------------
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("select_plan:")
)
def callback_select_plan(call):
    try:
        user = call.from_user
        pid = int(call.data.split(":")[1])
        cursor.execute(
            "SELECT title, price_cents, description, group_id FROM plans WHERE id=?",
            (pid,),
        )
        plan = cursor.fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "❌ Группа не найдена.")
            return
        title, price_cents, description, group_id = plan

        # Получаем доступные варианты оплаты
        payment_options = get_payment_options(user.id, pid)

        text = (
            f"💳 <b>Оформление подписки на группу '{title}'</b>\n\n"
            f"💰 Цена в месяц: {price_str_from_cents(price_cents)}\n"
            f"📋 Описание: {description}\n\n"
        )

        markup = types.InlineKeyboardMarkup()

        if payment_options:
            text += "<b>Детали</b>\n"
            for option in payment_options:
                text += f"• {option['text']}\n  {option['description']}\n\n"

            for option in payment_options:
                markup.add(
                    types.InlineKeyboardButton(
                        f"💸 Оплатить {price_str_from_cents(option['price'])}",
                        callback_data=f"buy_{option['type']}:{pid}",
                    )
                )
        else:
            active_type = get_active_payment_type()
            if active_type == "second":
                text += "❌ <b>У вас нет активной первой части оплаты для этой группы.</b>\n\n"
            else:
                text += "❌ <b>Сейчас не период оплаты.</b>\n\n"

            # text += ("💳 <b>Периоды оплаты:</b>\n"
            #         "• 1-5 числа: полная оплата или первая часть\n"
            #         "• 15-20 числа: вторая часть (только при оплаченной первой)\n"
            #         "• В другое время: полная оплата\n\n"
            #         "Возвращайтесь в указанные даты!")

        markup.add(
            types.InlineKeyboardButton(
                "🔙 Назад к списку групп", callback_data="back_to_plans"
            )
        )

        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id, text, parse_mode="HTML", reply_markup=markup
        )

    except Exception as e:
        logging.exception("Error in callback_select_plan")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе группы")


@bot.callback_query_handler(func=lambda call: call.data == "back_to_plans")
def callback_back_to_plans(call):
    """Возврат к списку групп"""
    show_plans(call.message)
    bot.answer_callback_query(call.id)


# Обработчики покупки
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("buy_")
)
def callback_buy_handler(call):
    try:
        user = call.from_user
        parts = call.data.split("_")
        payment_type = parts[1].split(":")[0]
        pid = int(parts[1].split(":")[1])

        cursor.execute(
            "SELECT title, price_cents, description, group_id FROM plans WHERE id=?",
            (pid,),
        )
        plan = cursor.fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "❌ Группа не найдена.")
            return
        title, price_cents, description, group_id = plan

        # Рассчитываем цену в зависимости от типа оплаты
        if payment_type in ("partial", "second_part", "half_month"):
            amount_cents = price_cents // 2
        else:  # full или full_anytime
            amount_cents = price_cents

        # Сохраняем информацию о выбранном тарифе (без промокода)
        user_states[user.id] = {
            "plan_id": pid,
            "original_price": amount_cents,
            "title": title,
            "description": description,
            "group_id": group_id,
            "payment_type": payment_type,
            "mode": "no_promo",  # Прямой переход к оплате без промокода
        }

        # Сразу показываем выбор способа оплаты
        payment_methods = get_active_payment_methods()
        if not payment_methods:
            bot.answer_callback_query(call.id, "❌ Нет доступных способов оплаты")
            return

        if len(payment_methods) == 1:
            method_id, name, mtype, method_desc, details = payment_methods[0]
            if mtype == "card":
                process_card_payment(
                    call,
                    pid,
                    user,
                    title,
                    amount_cents,
                    description,
                    group_id,
                    payment_type,
                )
            else:
                process_manual_payment_start(
                    call,
                    pid,
                    user,
                    title,
                    amount_cents,
                    description,
                    details,
                    payment_type,
                )
        else:
            markup = types.InlineKeyboardMarkup()
            for method_id, name, mtype, method_desc, details in payment_methods:
                markup.add(
                    types.InlineKeyboardButton(
                        name,
                        callback_data=f"paymethod:{pid}:{method_id}:{payment_type}",
                    )
                )

            # Добавляем кнопку отмены
            markup.add(
                types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
            )

            bot.answer_callback_query(call.id, "💳 Выберите способ оплаты")
            bot.send_message(
                call.message.chat.id,
                f"💳 <b>Выберите способ оплаты для группы '{title}'</b>",
                parse_mode="HTML",
                reply_markup=markup,
            )

    except Exception as e:
        logging.exception("Error in callback_buy_handler")
        bot.answer_callback_query(call.id, "❌ Ошибка при оформлении заказа")


# Обработчик пропуска промокода
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("skip_promo:")
)
def callback_skip_promo(call):
    try:
        user = call.from_user
        parts = call.data.split(":")
        pid = int(parts[1])
        payment_type = parts[2]

        if user.id not in user_states:
            bot.answer_callback_query(call.id, "❌ Сессия устарела")
            return

        state = user_states[user.id]
        state["mode"] = "no_promo"

        # Показываем выбор способа оплаты
        payment_methods = get_active_payment_methods()
        if not payment_methods:
            bot.answer_callback_query(call.id, "❌ Нет доступных способов оплаты")
            return

        if len(payment_methods) == 1:
            method_id, name, mtype, method_desc, details = payment_methods[0]
            if mtype == "card":
                process_card_payment(
                    call,
                    pid,
                    user,
                    state["title"],
                    state["original_price"],
                    state["description"],
                    state["group_id"],
                    payment_type,
                )
            else:
                process_manual_payment_start(
                    call,
                    pid,
                    user,
                    state["title"],
                    state["original_price"],
                    state["description"],
                    details,
                    payment_type,
                )
        else:
            markup = types.InlineKeyboardMarkup()
            for method_id, name, mtype, method_desc, details in payment_methods:
                markup.add(
                    types.InlineKeyboardButton(
                        name,
                        callback_data=f"paymethod:{pid}:{method_id}:{payment_type}",
                    )
                )

            # Добавляем кнопку отмены
            markup.add(
                types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
            )

            bot.answer_callback_query(call.id, "💳 Выберите способ оплаты")
            bot.send_message(
                call.message.chat.id,
                f"💳 <b>Выберите способ оплаты для группы '{state['title']}'</b>",
                parse_mode="HTML",
                reply_markup=markup,
            )

    except Exception as e:
        logging.exception("Error in callback_skip_promo")
        bot.answer_callback_query(call.id, "❌ Ошибка при оформлении заказа")


# Обработчик ввода промокода
@bot.message_handler(
    func=lambda m: m.from_user.id in user_states
    and user_states[m.from_user.id].get("mode") == "promo_input"
    and m.text
    and not m.text.startswith("/")
)
def handle_promo_code_input(message):
    user_id = message.from_user.id
    state = user_states[user_id]

    promo_code = message.text.strip().upper()

    # Проверяем промокод
    promo_data = get_promo_code(promo_code)
    if not promo_data:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
        )

        bot.send_message(
            message.chat.id,
            "❌ Промокод не найден. Попробуйте другой или нажмите '❌ Отмена' для возврата.",
            reply_markup=markup,
        )
        return

    can_use, reason = can_use_promo_code(promo_data[0], user_id)
    if not can_use:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
        )

        bot.send_message(
            message.chat.id,
            f"❌ {reason}\n" "Нажмите '❌ Отмена' для возврата",
            reply_markup=markup,
        )
        return

    # Применяем промокод
    new_price, promo_message = apply_promo_code(state["original_price"], promo_data)
    state["promo_id"] = promo_data[0]
    state["promo_code"] = promo_code
    state["final_price"] = new_price
    state["mode"] = "promo_applied"

    # Показываем выбор способа оплаты с учетом скидки
    payment_methods = get_active_payment_methods()
    if not payment_methods:
        bot.send_message(message.chat.id, "❌ Нет доступных способов оплаты")
        return

    text = (
        f"💳 <b>Оплата группы '{state['title']}'</b>\n\n"
        f"💰 Исходная цена: {price_str_from_cents(state['original_price'])}\n"
        f"🎫 {promo_message}\n"
        f"💵 Итоговая цена: {price_str_from_cents(new_price)}\n\n"
        f"Выберите способ оплаты:"
    )

    if len(payment_methods) == 1:
        method_id, name, mtype, method_desc, details = payment_methods[0]
        if mtype == "card":
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    "💳 Оплатить картой",
                    callback_data=f"pay_with_promo:{state['plan_id']}:{state['payment_type']}",
                )
            )
            markup.add(
                types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
            )
            bot.send_message(
                message.chat.id, text, parse_mode="HTML", reply_markup=markup
            )
        else:
            process_manual_payment_start_from_message(
                message,
                state["plan_id"],
                state["title"],
                new_price,
                state["description"],
                details,
                state["payment_type"],
                state["promo_id"],
            )
    else:
        markup = types.InlineKeyboardMarkup()
        for method_id, name, mtype, method_desc, details in payment_methods:
            markup.add(
                types.InlineKeyboardButton(
                    name,
                    callback_data=f"paymethod_promo:{state['plan_id']}:{method_id}:{state['payment_type']}:{state['promo_id']}",
                )
            )
        markup.add(
            types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


# Функции оплаты
def process_card_payment(
    call,
    pid,
    user,
    title,
    price_cents,
    description,
    group_id,
    payment_type,
    promo_id=None,
):
    """Обработка оплаты картой"""
    if group_id is None:
        group_id = get_default_group()
    if group_id is None:
        bot.answer_callback_query(
            call.id, "❌ Нет доступных групп. Обратитесь к администратору."
        )
        return

    prices = [types.LabeledPrice(label=title, amount=price_cents)]

    # Определяем режим оплаты
    state = user_states.get(user.id, {})
    mode = state.get("mode", "new_subscription")

    current_month, current_year = get_current_period()

    # Создаем payload с информацией о режиме
    payload = f"plan:{pid}:user:{user.id}:type:{payment_type}:month:{current_month}:year:{current_year}:promo:{promo_id or 0}:mode:{mode}:{int(time.time())}"

    cursor.execute(
        "INSERT OR REPLACE INTO invoices (payload, user_id, plan_id, amount_cents, created_ts, payment_type, period_month, period_year, promo_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            payload,
            user.id,
            pid,
            price_cents,
            int(time.time()),
            payment_type,
            current_month,
            current_year,
            promo_id,
        ),
    )
    conn.commit()

    try:
        description_text = (
            f"{description}\nТип оплаты: {get_payment_type_text(payment_type)}"
        )
        if mode == "renewal":
            description_text += "\nПродление подписки"
        elif mode == "new_subscription":
            description_text += "\nНовая подписка"

        if promo_id:
            description_text += f"\nПрименен промокод"

        bot.send_invoice(
            call.message.chat.id,
            title=title,
            description=description_text,
            invoice_payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=prices,
        )
        bot.answer_callback_query(call.id, "💳 Счёт для оплаты:")
    except Exception:
        logging.exception("send_invoice failed")
        bot.answer_callback_query(call.id, "❌ Ошибка создания счёта.")


def process_manual_payment_start(
    call,
    pid,
    user,
    title,
    price_cents,
    description,
    details,
    payment_type,
    promo_id=None,
):
    """Начало процесса ручной оплаты"""
    user_id = user.id
    user_states[user_id] = {
        "mode": "manual_payment",
        "plan_id": pid,
        "amount_cents": price_cents,
        "title": title,
        "step": "show_instructions",
        "payment_type": payment_type,
        "promo_id": promo_id,
    }

    payment_type_text = get_payment_type_text(payment_type)

    text = (
        f"💳 <b>Оплата {payment_type_text} группы '{title}'</b>\n\n"
        f"💰 Сумма к оплате: {price_str_from_cents(price_cents)}\n\n"
        f"📋 <b>Инструкция по оплате:</b>\n{details}\n\n"
        f"После оплаты нажмите кнопку '✅ Я оплатил(а)' и следуйте инструкциям."
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "✅ Я оплатил(а)", callback_data=f"confirm_paid:{pid}:{payment_type}"
        )
    )
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment"))

    bot.answer_callback_query(call.id, "📋 Инструкция по оплате отправлена")
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)


def process_manual_payment_start_from_message(
    message, pid, title, price_cents, description, details, payment_type, promo_id=None
):
    """Начало ручной оплаты из сообщения"""
    user_id = message.from_user.id
    user_states[user_id] = {
        "mode": "manual_payment",
        "plan_id": pid,
        "amount_cents": price_cents,
        "title": title,
        "step": "show_instructions",
        "payment_type": payment_type,
        "promo_id": promo_id,
    }

    payment_type_text = get_payment_type_text(payment_type)

    text = (
        f"💳 <b>Оплата {payment_type_text} группы '{title}'</b>\n\n"
        f"💰 Сумма к оплате: {price_str_from_cents(price_cents)}\n\n"
        f"📋 <b>Инструкция по оплате:</b>\n{details}\n\n"
        f"После оплаты нажмите кнопку '✅ Я оплатил(а)' и следуйте инструкциям."
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "✅ Я оплатил(а)", callback_data=f"confirm_paid:{pid}:{payment_type}"
        )
    )
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment"))

    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


def get_payment_type_text(payment_type):
    """Возвращает текстовое описание типа оплаты"""
    if payment_type == "full" or payment_type == "full_anytime":
        return "полной"
    elif payment_type == "partial":
        return "первой части"
    elif payment_type == "second_part":
        return "второй части"
    elif payment_type == "half_month":
        return "половины месяца"
    else:
        return ""


@bot.callback_query_handler(func=lambda call: call.data == "cancel_promo_input")
def callback_cancel_promo_input(call):
    """Отмена ввода промокода и возврат в главное меню"""
    user_id = call.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)

    bot.answer_callback_query(call.id, "❌ Ввод промокода отменен")

    # Показываем главное меню
    try:
        bot.edit_message_text(
            "❌ Ввод промокода отменен", call.message.chat.id, call.message.message_id
        )
    except:
        pass

    # Отправляем главное меню
    bot.send_message(
        call.message.chat.id, "📋 Главное меню:", reply_markup=main_menu(user_id)
    )


# Обработчики выбора способа оплаты
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("paymethod:")
)
def callback_paymethod(call):
    """Обработка выбора способа оплаты"""
    try:
        parts = call.data.split(":")
        pid = int(parts[1])
        method_id = int(parts[2])
        payment_type = parts[3]

        user = call.from_user

        if user.id not in user_states:
            bot.answer_callback_query(call.id, "❌ Сессия устарела")
            return

        state = user_states[user.id]

        # Получаем информацию о тарифе
        cursor.execute(
            "SELECT title, price_cents, description, group_id FROM plans WHERE id=?",
            (pid,),
        )
        plan = cursor.fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "❌ Тариф не найден.")
            return

        title, price_cents, description, group_id = plan

        method = get_payment_method_by_id(method_id)
        if not method:
            bot.answer_callback_query(call.id, "❌ Способ оплаты не найден.")
            return

        method_id, name, mtype, method_desc, details = method

        if mtype == "card":
            process_card_payment(
                call,
                pid,
                user,
                title,
                state["original_price"],
                description,
                group_id,
                payment_type,
            )
        else:  # manual
            process_manual_payment_start(
                call,
                pid,
                user,
                title,
                state["original_price"],
                description,
                details,
                payment_type,
            )

    except Exception as e:
        logging.exception("Error in callback_paymethod")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе способа оплаты")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("paymethod_promo:")
)
def callback_paymethod_promo(call):
    """Обработка выбора способа оплаты с промокодом"""
    try:
        parts = call.data.split(":")
        pid = int(parts[1])
        method_id = int(parts[2])
        payment_type = parts[3]
        promo_id = int(parts[4])

        user = call.from_user

        if user.id not in user_states or "final_price" not in user_states[user.id]:
            bot.answer_callback_query(call.id, "❌ Сессия устарела")
            return

        state = user_states[user.id]

        method = get_payment_method_by_id(method_id)
        if not method:
            bot.answer_callback_query(call.id, "❌ Способ оплаты не найден.")
            return

        method_id, name, mtype, method_desc, details = method

        if mtype == "card":
            process_card_payment(
                call,
                pid,
                user,
                state["title"],
                state["final_price"],
                state["description"],
                state["group_id"],
                payment_type,
                promo_id,
            )
        else:  # manual
            process_manual_payment_start(
                call,
                pid,
                user,
                state["title"],
                state["final_price"],
                state["description"],
                details,
                payment_type,
                promo_id,
            )

    except Exception as e:
        logging.exception("Error in callback_paymethod_promo")
        bot.answer_callback_query(call.id, "❌ Ошибка при выборе способа оплаты")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("pay_with_promo:")
)
def callback_pay_with_promo(call):
    """Оплата картой с примененным промокодом"""
    user_id = call.from_user.id
    if user_id not in user_states or "final_price" not in user_states[user_id]:
        bot.answer_callback_query(call.id, "❌ Сессия устарела")
        return

    state = user_states[user_id]
    parts = call.data.split(":")
    pid = int(parts[1])
    payment_type = parts[2]

    process_card_payment(
        call,
        pid,
        call.from_user,
        state["title"],
        state["final_price"],
        state["description"],
        state["group_id"],
        payment_type,
        state.get("promo_id"),
    )


# Обработчик подтверждения ручной оплаты
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("confirm_paid:")
)
def callback_confirm_paid(call):
    """Подтверждение оплаты для ручного метода"""
    try:
        parts = call.data.split(":")
        pid = int(parts[1])
        payment_type = parts[2] if len(parts) > 2 else "full"

        user_id = call.from_user.id

        # Сохраняем текущее состояние
        current_state = user_states.get(user_id, {})

        user_states[user_id] = {
            "mode": "manual_payment",
            "plan_id": pid,
            "step": "waiting_receipt",
            "amount_cents": current_state.get("amount_cents", 0),
            "payment_type": payment_type,
            "promo_id": current_state.get("promo_id"),
        }

        bot.answer_callback_query(call.id, "📎 Отправьте фото чека об оплате")
        bot.send_message(
            call.message.chat.id,
            "📎 Пожалуйста, отправьте фото или скриншот чека об оплате:",
        )

    except Exception as e:
        logging.exception("Error in callback_confirm_paid")
        bot.answer_callback_query(call.id, "❌ Ошибка")


@bot.callback_query_handler(func=lambda call: call.data == "cancel_payment")
def callback_cancel_payment(call):
    """Отмена оплаты и возврат в главное меню"""
    user_id = call.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)

    bot.answer_callback_query(call.id, "❌ Оплата отменена")

    # Показываем главное меню
    try:
        bot.edit_message_text(
            "❌ Оплата отменена", call.message.chat.id, call.message.message_id
        )
    except:
        pass

    # Отправляем главное меню
    bot.send_message(
        call.message.chat.id, "📋 Главное меню:", reply_markup=main_menu(user_id)
    )


# Обработчик фото чека для ручной оплаты
@bot.message_handler(
    content_types=["photo"],
    func=lambda m: m.from_user.id in user_states
    and user_states[m.from_user.id].get("mode") == "manual_payment"
    and user_states[m.from_user.id].get("step") == "waiting_receipt",
)
def handle_receipt_photo(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if not state or state.get("step") != "waiting_receipt":
        return

    receipt_photo = message.photo[-1].file_id
    state["receipt_photo"] = receipt_photo
    state["step"] = "waiting_name"

    bot.send_message(
        message.chat.id, "✅ Чек принят! Теперь введите ваши Фамилию и Имя:"
    )


# Обработчик ФИО для ручной оплаты
@bot.message_handler(
    func=lambda m: m.from_user.id in user_states
    and user_states[m.from_user.id].get("mode") == "manual_payment"
    and user_states[m.from_user.id].get("step") == "waiting_name"
    and m.text
)
def handle_full_name(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if not state or state.get("step") != "waiting_name":
        return

    full_name = message.text.strip()
    if len(full_name) < 2:
        bot.send_message(
            message.chat.id, "❌ Пожалуйста, введите полные Фамилию и Имя:"
        )
        return

    # Сохраняем заявку на ручную оплату
    cursor.execute(
        """
        INSERT INTO manual_payments (user_id, plan_id, amount_cents, receipt_photo, full_name, created_ts, payment_type, period_month, period_year, promo_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            user_id,
            state["plan_id"],
            state["amount_cents"],
            state["receipt_photo"],
            full_name,
            int(time.time()),
            state["payment_type"],
            *get_current_period(),
            state.get("promo_id"),
        ),
    )
    payment_id = cursor.lastrowid
    conn.commit()

    # Уведомляем админов
    cursor.execute("SELECT title FROM plans WHERE id=?", (state["plan_id"],))
    plan_title = cursor.fetchone()[0]

    payment_type_text = get_payment_type_text(state["payment_type"])

    for admin_id in ADMIN_IDS:
        try:
            text = (
                f"📋 <b>Новая заявка на ручную оплату</b>\n\n"
                f"👤 Пользователь: @{message.from_user.username or 'N/A'} (ID: {user_id})\n"
                f"🏷️ Группа: {plan_title}\n"
                f"💵 Сумма: {price_str_from_cents(state['amount_cents'])}\n"
                f"💳 Тип оплаты: {payment_type_text}\n"
                f"👤 ФИО: {full_name}"
            )

            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton(
                    "✅ Одобрить", callback_data=f"approve_payment:{payment_id}"
                ),
                types.InlineKeyboardButton(
                    "❌ Отклонить", callback_data=f"reject_payment:{payment_id}"
                ),
            )

            bot.send_photo(
                admin_id,
                state["receipt_photo"],
                caption=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            logging.error(f"Error notifying admin {admin_id}: {e}")

    # Очищаем состояние пользователя
    user_states.pop(user_id, None)

    bot.send_message(
        message.chat.id,
        "✅ Заявка отправлена на проверку! Ожидайте подтверждения администратора.",
    )


# Обработчик успешной оплаты картой
@bot.pre_checkout_query_handler(func=lambda q: True)
def handle_precheckout(q):
    bot.answer_pre_checkout_query(q.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def got_payment(message):
    sp = message.successful_payment
    payload = sp.invoice_payload
    user_id = message.from_user.id

    # Парсим payload
    parts = payload.split(":")
    plan_id = int(parts[1])
    payment_type = parts[5]
    promo_id = int(parts[11]) if len(parts) > 11 and parts[11] != "0" else None
    mode = parts[13] if len(parts) > 13 else "new_subscription"  # Получаем режим

    # Проверяем состояние пользователя
    state = user_states.get(user_id, {})

    # Всегда используем activate_subscription с правильными параметрами
    success, result = activate_subscription(
        user_id, plan_id, payment_type, state.get("group_id")
    )
    if not success:
        bot.send_message(user_id, f"❌ Ошибка активации подписки: {result}")
        return

    cursor.execute("SELECT title FROM plans WHERE id=?", (plan_id,))
    plan_title = cursor.fetchone()[0]

    # Определяем текст сообщения в зависимости от режима
    if mode == "renewal":
        txt = (
            f"✅ <b>Подписка успешно продлена!</b>\n\n"
            f"🏷️ Группа: {plan_title}\n"
            f"🔗 Ваша новая пригласительная ссылка (одноразовая):\n{result}\n\n"
            f"Спасибо, что остаетесь с нами!"
        )
    else:
        txt = (
            f"✅ <b>Подписка успешно оформлена!</b>\n\n"
            f"🏷️ Группа: {plan_title}\n"
            f"🔗 Ваша пригласительная ссылка для входа в чат (одноразовая):\n{result}\n\n"
            f"Добро пожаловать в наше сообщество!"
        )

    bot.send_message(user_id, txt, parse_mode="HTML")

    # Если был применен промокод, отмечаем его использование
    if promo_id and promo_id > 0:
        cursor.execute(
            "INSERT INTO promo_usage (promo_id, user_id, used_ts) VALUES (?, ?, ?)",
            (promo_id, user_id, int(time.time())),
        )
        cursor.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 WHERE id=?", (promo_id,)
        )
        conn.commit()

    # cashback для реферера
    cursor.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
    urow = cursor.fetchone()
    referred_by = urow[0] if urow else None

    if referred_by:
        amount_cents = sp.total_amount
        cashback = int(math.floor(amount_cents * REFERRAL_PERCENT / 100.0))
        cursor.execute(
            "UPDATE users SET cashback_cents = cashback_cents + ? WHERE user_id=?",
            (cashback, referred_by),
        )
        conn.commit()
        try:
            bot.send_message(
                referred_by,
                f"💰 Реферальный кэшбэк! Пользователь @{message.from_user.username or message.from_user.id} оплатил подписку. "
                f"Вам начислен кэшбэк: {price_str_from_cents(cashback)}",
            )
        except:
            pass

    # Очищаем состояние пользователя
    if user_id in user_states:
        user_states.pop(user_id)


def get_all_categories():
    """Получает все активные категории"""
    cursor.execute(
        "SELECT id, name, description FROM categories WHERE is_active=1 ORDER BY name"
    )
    return cursor.fetchall()


def get_category_by_id(category_id):
    """Получает категорию по ID"""
    cursor.execute(
        "SELECT id, name, description FROM categories WHERE id=?", (category_id,)
    )
    return cursor.fetchone()


def create_category(name, description=""):
    """Создает новую категорию"""
    cursor.execute(
        "INSERT INTO categories (name, description, created_ts) VALUES (?, ?, ?)",
        (name, description, int(time.time())),
    )
    conn.commit()
    return cursor.lastrowid


def update_category(category_id, name, description):
    """Обновляет категорию"""
    cursor.execute(
        "UPDATE categories SET name=?, description=? WHERE id=?",
        (name, description, category_id),
    )
    conn.commit()


def delete_category(category_id):
    """Удаляет категорию (мягкое удаление)"""
    cursor.execute("UPDATE categories SET is_active=0 WHERE id=?", (category_id,))
    conn.commit()


# ----------------- Админ-панель -----------------
@bot.message_handler(func=lambda message: message.text == "⚙️ Админ меню")
@only_private
def admin_menu(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(
            message.chat.id,
            "🚫 Доступ запрещен.",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton("➕ Новая группа"),
        types.KeyboardButton("📝 Редактировать группу"),
    )
    markup.row(
        types.KeyboardButton("👥 Управление группами"),
        types.KeyboardButton("🔄 Авто-добавление групп"),
    )
    markup.row(
        types.KeyboardButton("📊 Подписки"), types.KeyboardButton("👤 Пользователи")
    )
    markup.row(
        types.KeyboardButton("💳 Управление оплатой"),
        types.KeyboardButton("📋 Заявки на оплату"),
    )
    markup.row(
        types.KeyboardButton("🎫 Промокоды"),
        types.KeyboardButton("📚 Управление предметами"),
    )  # Новая кнопка
    markup.row(types.KeyboardButton("🔙 Главное меню"))
    bot.send_message(message.chat.id, "⚙️ Админ меню:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "edit_category_list")
def callback_edit_category_list(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    categories = get_all_categories()
    if not categories:
        bot.answer_callback_query(call.id, "📭 Нет предметов для редактирования.")
        return

    markup = types.InlineKeyboardMarkup()
    for cat_id, name, description in categories:
        button_text = name
        if description:
            button_text += f" - {description}"
        markup.add(
            types.InlineKeyboardButton(
                button_text, callback_data=f"edit_category:{cat_id}"
            )
        )

    bot.answer_callback_query(call.id, "Выберите предмет для редактирования")
    bot.send_message(
        call.message.chat.id,
        "✏️ Выберите предмет для редактирования:",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("edit_category:")
)
def callback_edit_category(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    category_id = int(call.data.split(":")[1])

    category = get_category_by_id(category_id)
    if not category:
        bot.answer_callback_query(call.id, "❌ Предмет не найден.")
        return

    cat_id, name, description = category

    admin_states[call.from_user.id] = {
        "mode": "edit_category",
        "category_id": category_id,
        "step": "name",
        "current_name": name,
        "current_description": description,
        "chat_id": call.message.chat.id,
    }

    bot.answer_callback_query(call.id, f"Редактирование: {name}")
    bot.send_message(
        call.message.chat.id,
        f"✏️ Редактирование предмета: {name}\n\n"
        f"Введите новое название (текущее: {name}):",
    )


@bot.callback_query_handler(func=lambda call: call.data == "delete_category_list")
def callback_delete_category_list(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    categories = get_all_categories()
    if not categories:
        bot.answer_callback_query(call.id, "📭 Нет предметов для удаления.")
        return

    markup = types.InlineKeyboardMarkup()
    for cat_id, name, description in categories:
        button_text = name
        if description:
            button_text += f" - {description}"
        markup.add(
            types.InlineKeyboardButton(
                button_text, callback_data=f"delete_category:{cat_id}"
            )
        )

    bot.answer_callback_query(call.id, "Выберите предмет для удаления")
    bot.send_message(
        call.message.chat.id, "🗑️ Выберите предмет для удаления:", reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("delete_category:")
)
def callback_delete_category(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    category_id = int(call.data.split(":")[1])

    category = get_category_by_id(category_id)
    if not category:
        bot.answer_callback_query(call.id, "❌ Предмет не найден.")
        return

    cat_id, name, description = category

    # Проверяем, есть ли группы в этой категории
    cursor.execute(
        "SELECT COUNT(*) FROM plans WHERE category_id=? AND is_active=1", (category_id,)
    )
    groups_count = cursor.fetchone()[0]

    if groups_count > 0:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "✅ Да, удалить вместе с группами",
                callback_data=f"confirm_delete_category_with_groups:{category_id}",
            ),
            types.InlineKeyboardButton(
                "🔄 Перенести группы в другой предмет",
                callback_data=f"transfer_category_groups:{category_id}",
            ),
        )
        markup.add(
            types.InlineKeyboardButton(
                "❌ Отмена", callback_data="cancel_delete_category"
            )
        )

        bot.answer_callback_query(call.id, "⚠️ В категории есть группы")
        bot.send_message(
            call.message.chat.id,
            f"⚠️ <b>Внимание!</b>\n\n"
            f"В предмете '{name}' есть {groups_count} активных групп.\n\n"
            f"Выберите действие:",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    # Если групп нет, сразу подтверждаем удаление
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "✅ Да, удалить", callback_data=f"confirm_delete_category:{category_id}"
        ),
        types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete_category"),
    )

    bot.answer_callback_query(call.id, "Подтвердите удаление")
    bot.send_message(
        call.message.chat.id,
        f"🗑️ <b>Подтвердите удаление предмета</b>\n\n"
        f"Предмет: {name}\n"
        f"Описание: {description or 'нет'}\n\n"
        f"Вы уверены, что хотите удалить этот предмет?",
        parse_mode="HTML",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("confirm_delete_category:")
)
def callback_confirm_delete_category(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    category_id = int(call.data.split(":")[1])

    category = get_category_by_id(category_id)
    if not category:
        bot.answer_callback_query(call.id, "❌ Предмет не найден.")
        return

    cat_id, name, description = category

    # Удаляем категорию
    delete_category(category_id)

    bot.answer_callback_query(call.id, f"✅ Предмет '{name}' удален")
    bot.send_message(call.message.chat.id, f"✅ Предмет '{name}' успешно удален.")


@bot.callback_query_handler(
    func=lambda call: call.data
    and call.data.startswith("confirm_delete_category_with_groups:")
)
def callback_confirm_delete_category_with_groups(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    category_id = int(call.data.split(":")[1])

    category = get_category_by_id(category_id)
    if not category:
        bot.answer_callback_query(call.id, "❌ Предмет не найден.")
        return

    cat_id, name, description = category

    # Удаляем категорию и деактивируем все группы в ней
    cursor.execute("UPDATE plans SET is_active=0 WHERE category_id=?", (category_id,))
    delete_category(category_id)
    conn.commit()

    bot.answer_callback_query(call.id, f"✅ Предмет и группы удалены")
    bot.send_message(
        call.message.chat.id,
        f"✅ Предмет '{name}' и все связанные группы успешно удалены.",
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("transfer_category_groups:")
)
def callback_transfer_category_groups(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    category_id = int(call.data.split(":")[1])

    category = get_category_by_id(category_id)
    if not category:
        bot.answer_callback_query(call.id, "❌ Предмет не найден.")
        return

    # Получаем все категории кроме текущей
    cursor.execute(
        "SELECT id, name, description FROM categories WHERE id != ? AND is_active=1",
        (category_id,),
    )
    other_categories = cursor.fetchall()

    if not other_categories:
        bot.answer_callback_query(call.id, "❌ Нет других предметов для переноса")
        bot.send_message(
            call.message.chat.id, "❌ Нет других предметов для переноса групп."
        )
        return

    admin_states[call.from_user.id] = {
        "mode": "transfer_category",
        "source_category_id": category_id,
        "step": "select_target",
        "chat_id": call.message.chat.id,
    }

    markup = types.InlineKeyboardMarkup()
    for cat_id, name, description in other_categories:
        button_text = name
        if description:
            button_text += f" - {description}"
        markup.add(
            types.InlineKeyboardButton(
                button_text,
                callback_data=f"select_target_category:{cat_id}:{category_id}",
            )
        )

    bot.answer_callback_query(call.id, "Выберите целевой предмет")
    bot.send_message(
        call.message.chat.id,
        f"🔄 <b>Перенос групп</b>\n\n"
        f"Выберите предмет, в который перенести группы из '{category[1]}':",
        parse_mode="HTML",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("select_target_category:")
)
def callback_select_target_category(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    parts = call.data.split(":")
    target_category_id = int(parts[1])
    source_category_id = int(parts[2])

    # Переносим группы
    cursor.execute(
        "UPDATE plans SET category_id=? WHERE category_id=?",
        (target_category_id, source_category_id),
    )
    # Удаляем исходную категорию
    delete_category(source_category_id)
    conn.commit()

    # Получаем названия категорий для сообщения
    source_category = get_category_by_id(source_category_id)
    target_category = get_category_by_id(target_category_id)

    source_name = source_category[1] if source_category else "Неизвестно"
    target_name = target_category[1] if target_category else "Неизвестно"

    bot.answer_callback_query(call.id, "✅ Группы перенесены")
    bot.send_message(
        call.message.chat.id,
        f"✅ Группы из предмета '{source_name}' успешно перенесены в предмет '{target_name}'.",
    )


@bot.callback_query_handler(func=lambda call: call.data == "cancel_delete_category")
def callback_cancel_delete_category(call):
    bot.answer_callback_query(call.id, "❌ Удаление отменено")
    bot.send_message(call.message.chat.id, "❌ Удаление предмета отменено.")


# Обработчики ввода текста для редактирования категорий
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "edit_category"
    and admin_states.get(m.from_user.id, {}).get("step") == "name"
    and m.chat.type == "private"
)
def handle_edit_category_name(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if not message.text:
        bot.send_message(message.chat.id, "❌ Отправьте название текстом.")
        return

    new_name = message.text.strip()
    state["new_name"] = new_name
    state["step"] = "description"

    bot.send_message(
        message.chat.id,
        f"✏️ Новое название: {new_name}\n\n"
        f"Введите новое описание (текущее: {state['current_description'] or 'нет'}):",
    )


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "edit_category"
    and admin_states.get(m.from_user.id, {}).get("step") == "description"
    and m.chat.type == "private"
)
def handle_edit_category_description(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    new_description = message.text.strip()

    # Обновляем категорию в базе
    update_category(state["category_id"], state["new_name"], new_description)

    # Очищаем состояние
    admin_states.pop(uid, None)

    bot.send_message(
        message.chat.id,
        f"✅ Предмет успешно обновлен!\n\n"
        f"🏷️ Название: {state['new_name']}\n"
        f"📝 Описание: {new_description or 'нет'}",
        reply_markup=main_menu(uid),
    )


@bot.message_handler(func=lambda message: message.text == "📚 Управление предметами")
@only_private
def manage_categories(message):
    if message.from_user.id not in ADMIN_IDS:
        return

    categories = get_all_categories()

    text = "📚 <b>Управление предметами</b>\n\n"
    if categories:
        text += "<b>Существующие предметы:</b>\n"
        for cat_id, name, description in categories:
            text += f"• {name}"
            if description:
                text += f" - {description}"
            text += f" (ID: {cat_id})\n"
    else:
        text += "📭 Пока нет созданных предметов.\n\n"

    text += "\nВыберите действие:"

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("➕ Добавить предмет", callback_data="add_category"),
        types.InlineKeyboardButton(
            "✏️ Редактировать предмет", callback_data="edit_category_list"
        ),
    )
    if categories:
        markup.row(
            types.InlineKeyboardButton(
                "🗑️ Удалить предмет", callback_data="delete_category_list"
            )
        )

    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "add_category")
def callback_add_category(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    admin_states[call.from_user.id] = {
        "mode": "create_category",
        "step": "name",
        "chat_id": call.message.chat.id,
    }

    bot.answer_callback_query(call.id, "Создание нового предмета...")
    bot.send_message(
        call.message.chat.id,
        "📚 Создание нового предмета\n\nВведите название предмета (например: 'Химия'):",
    )


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create_category"
    and admin_states.get(m.from_user.id, {}).get("step") == "name"
    and m.chat.type == "private"
)
def handle_category_name(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if not message.text:
        bot.send_message(message.chat.id, "❌ Отправьте название текстом.")
        return

    state["name"] = message.text.strip()
    state["step"] = "description"

    bot.send_message(
        message.chat.id,
        "📝 Введите описание предмета (или отправьте '-' чтобы пропустить):",
    )


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create_category"
    and admin_states.get(m.from_user.id, {}).get("step") == "description"
    and m.chat.type == "private"
)
def handle_category_description(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    description = message.text.strip()
    if description == "-":
        description = ""

    # Создаем категорию
    category_id = create_category(state["name"], description)

    admin_states.pop(uid, None)

    bot.send_message(
        message.chat.id,
        f"✅ Предмет '{state['name']}' успешно создан!\nID: {category_id}",
        reply_markup=main_menu(uid),
    )


@bot.message_handler(func=lambda message: message.text == "🔙 Главное меню")
@only_private
def back_to_main(message):
    bot.send_message(
        message.chat.id,
        "📋 Главное меню:",
        reply_markup=main_menu(message.from_user.id),
    )


# Создание новой группы
@bot.message_handler(func=lambda message: message.text == "➕ Новая группа")
@only_private
def cmd_newplan(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    uid = message.from_user.id

    # Проверяем есть ли категории
    categories = get_all_categories()
    if not categories:
        bot.send_message(
            message.chat.id,
            "❌ Сначала создайте хотя бы один предмет в разделе '📚 Управление предметами'",
        )
        return

    admin_states[uid] = {
        "mode": "create",
        "step": "category",
        "media_files": [],
        "media_type": None,
        "chat_id": message.chat.id,
    }

    # Показываем выбор категории
    markup = types.InlineKeyboardMarkup()
    for cat_id, name, description in categories:
        button_text = name
        if description:
            button_text += f" - {description}"
        markup.add(
            types.InlineKeyboardButton(
                button_text, callback_data=f"select_category:{cat_id}"
            )
        )

    bot.send_message(
        message.chat.id,
        "➕ Добавление новой группы обучения.\n\nШаг 1/7: Выберите предмет для этой группы:",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("select_category:")
)
def callback_admin_select_category(call):
    """Обработчик выбора категории в админ-панели"""
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    category_id = int(call.data.split(":")[1])
    uid = call.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("mode") != "create" or state.get("step") != "category":
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    state["category_id"] = category_id
    state["step"] = "title"

    # Получаем название категории для информации
    category = get_category_by_id(category_id)
    category_name = category[1] if category else "Неизвестно"

    bot.answer_callback_query(call.id, f"✅ Выбран предмет: {category_name}")

    # Обновляем сообщение или отправляем новое
    try:
        bot.edit_message_text(
            f"➕ <b>Добавление новой группы обучения</b>\n\n"
            f"📚 Предмет: {category_name}\n"
            f"Шаг 2/7: Отправьте название группы:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
        )
    except:
        # Если не удалось редактировать, отправляем новое сообщение
        bot.send_message(
            call.message.chat.id,
            f"➕ <b>Добавление новой группы обучения</b>\n\n"
            f"📚 Предмет: {category_name}\n"
            f"Шаг 2/7: Отправьте название группы:",
            parse_mode="HTML",
        )


# Обработчик ввода названия группы
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create"
    and admin_states.get(m.from_user.id, {}).get("step") == "title"
    and m.chat.type == "private"
)
def handle_plan_title(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if not message.text:
        bot.send_message(message.chat.id, "❌ Отправьте название текстом.")
        return

    state["title"] = message.text.strip()
    state["step"] = "price"

    bot.send_message(
        message.chat.id,
        f"✅ Название: {state['title']}\n\n"
        f"Шаг 3/7: Введите цену в месяц (например: 14.99):",
    )


# Обработчик ввода цены
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create"
    and admin_states.get(m.from_user.id, {}).get("step") == "price"
    and m.chat.type == "private"
)
def handle_plan_price(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    cents = cents_from_str(message.text)
    if cents is None:
        bot.send_message(message.chat.id, "❌ Неправильный формат цены. Пример: 14.99")
        return

    state["price_cents"] = cents
    state["step"] = "description"

    bot.send_message(
        message.chat.id,
        f"✅ Цена: {price_str_from_cents(cents)}\n\n"
        f"Шаг 4/7: Введите описание группы:",
    )


# Обработчик ввода описания
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create"
    and admin_states.get(m.from_user.id, {}).get("step") == "description"
    and m.chat.type == "private"
)
def handle_plan_description(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    state["description"] = message.text.strip()
    state["step"] = "group"

    # Показываем выбор группы
    groups = get_all_groups_with_bot()
    markup = types.InlineKeyboardMarkup()

    # Добавляем кнопку для группы по умолчанию
    default_group_id = get_default_group()
    if default_group_id:
        cursor.execute(
            "SELECT title FROM managed_groups WHERE chat_id=?", (default_group_id,)
        )
        default_title = cursor.fetchone()[0]
        markup.add(
            types.InlineKeyboardButton(
                f"🏠 По умолчанию: {default_title}",
                callback_data=f"select_group:default",
            )
        )

    # Добавляем остальные группы
    for chat_id, title, chat_type in groups:
        if chat_id != default_group_id:
            emoji = "📢" if chat_type == "channel" else "👥"
            markup.add(
                types.InlineKeyboardButton(
                    f"{emoji} {title}", callback_data=f"select_group:{chat_id}"
                )
            )

    bot.send_message(
        message.chat.id,
        f"✅ Описание: {state['description']}\n\n"
        f"Шаг 5/7: Выберите группу/канал для подписки:",
        reply_markup=markup,
    )


# Обработчик медиа при создании
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create"
    and admin_states.get(m.from_user.id, {}).get("step") == "media"
    and m.chat.type == "private",
    content_types=["text", "photo", "video"],
)
def handle_plan_media(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "photo"
        bot.send_message(
            message.chat.id, f"✅ Фото добавлено! Всего: {len(state['media_files'])}"
        )
        return

    if message.video:
        file_id = message.video.file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "video"
        bot.send_message(
            message.chat.id, f"✅ Видео добавлено! Всего: {len(state['media_files'])}"
        )
        return

    if message.text:
        txt = message.text.strip()
        if txt == "⏩ Пропустить медиа":
            state["step"] = "finish"
            bot.send_message(
                message.chat.id,
                "✅ Медиа пропущены.",
                reply_markup=types.ReplyKeyboardRemove(),
            )
            save_plan_to_db(state, uid)
            return

        if txt == "✅ Завершить добавление медиа":
            state["step"] = "finish"
            media_files = state.get("media_files", [])
            media_type = state.get("media_type")

            if media_files:
                cnt = len(media_files)
                if cnt == 1:
                    bot.send_message(
                        message.chat.id,
                        f"✅ Медиа добавлены! Использовано 1 превью.",
                        reply_markup=types.ReplyKeyboardRemove(),
                    )
                else:
                    bot.send_message(
                        message.chat.id,
                        f"✅ Медиа добавлены! Использовано первое из {cnt} медиа как превью.",
                        reply_markup=types.ReplyKeyboardRemove(),
                    )
            else:
                bot.send_message(
                    message.chat.id,
                    "✅ Медиа не добавлены.",
                    reply_markup=types.ReplyKeyboardRemove(),
                )

            save_plan_to_db(state, uid)
            return

        bot.send_message(
            message.chat.id,
            "❌ Отправляйте фото/видео или используйте кнопки '⏩ Пропустить медиа' / '✅ Завершить добавление медиа'.",
        )


def save_plan_to_db(state, uid):
    """Сохраняет план в базу данных"""
    try:
        # Сохраняем основную информацию о плане
        cursor.execute(
            """
            INSERT INTO plans (title, price_cents, description, group_id, category_id, created_ts, media_file_id, media_file_ids, media_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                state["title"],
                state["price_cents"],
                state["description"],
                state["group_id"],
                state["category_id"],
                int(time.time()),
                state["media_files"][0] if state.get("media_files") else None,
                ",".join(state["media_files"]) if state.get("media_files") else None,
                state.get("media_type"),
            ),
        )

        plan_id = cursor.lastrowid

        # Сохраняем медиа если есть
        if state.get("media_files"):
            for idx, file_id in enumerate(state["media_files"]):
                cursor.execute(
                    """
                    INSERT INTO plan_media (plan_id, file_id, media_type, ord, added_ts)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (plan_id, file_id, state["media_type"], idx, int(time.time())),
                )

        conn.commit()

        # Получаем название категории для сообщения
        category = get_category_by_id(state["category_id"])
        category_name = category[1] if category else "Неизвестно"

        # Получаем название группы для сообщения
        cursor.execute(
            "SELECT title FROM managed_groups WHERE chat_id=?", (state["group_id"],)
        )
        group_title = cursor.fetchone()[0]

        bot.send_message(
            state["chat_id"],
            f"✅ <b>Группа обучения создана!</b>\n\n"
            f"🏷️ Название: {state['title']}\n"
            f"💰 Цена: {price_str_from_cents(state['price_cents'])}\n"
            f"📚 Предмет: {category_name}\n"
            f"👥 Группа: {group_title}\n"
            f"📋 Описание: {state['description']}\n"
            f"🖼️ Медиа: {len(state.get('media_files', []))} шт.\n\n"
            f"ID группы: {plan_id}",
            parse_mode="HTML",
            reply_markup=main_menu(uid),
        )

        # Очищаем состояние
        admin_states.pop(uid, None)

    except Exception as e:
        logging.exception("Error saving plan to database")
        bot.send_message(state["chat_id"], f"❌ Ошибка при создании группы: {str(e)}")


# Редактирование групп
@bot.message_handler(func=lambda message: message.text == "📝 Редактировать группу")
@only_private
def admin_list_plans(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    cursor.execute(
        """
        SELECT p.id, p.title, p.price_cents, p.duration_days, p.group_id, mg.title
        FROM plans p
        LEFT JOIN managed_groups mg ON p.group_id = mg.chat_id
        WHERE p.is_active=1
        ORDER BY p.id
    """
    )
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "📭 Групп обучения нет.")
        return
    for pid, title, price_cents, days, group_id, group_title in rows:
        group_text = f"Группа: {group_title}" if group_title else "Группа: по умолчанию"
        text = f"<b>{title}</b>\nЦена в месяц: {price_str_from_cents(price_cents)}\n{group_text}"
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(
                "✏️ Редактировать", callback_data=f"editplan:{pid}"
            )
        )
        markup.add(
            types.InlineKeyboardButton("🗑 Удалить", callback_data=f"delplan:{pid}")
        )
        markup.add(
            types.InlineKeyboardButton(
                "🔍 Просмотреть медиа", callback_data=f"viewmedia:{pid}"
            )
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


# Управление группами
@bot.message_handler(func=lambda message: message.text == "👥 Управление группами")
@only_private
def cmd_groups(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    groups = get_all_groups_with_bot()
    if not groups:
        invite_link = get_bot_invite_link()
        text = (
            "📭 Нет зарегистрированных групп/каналов.\n\n"
            "💡 <b>Как добавить группу:</b>\n"
            "1. Нажмите кнопку ниже чтобы добавить бота в группу\n"
            "2. Назначьте боту права администратора\n"
            "3. Используйте команду /register_group в группе\n\n"
            "Или добавьте бота по ссылке:"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🔗 Добавить бота в группу", url=invite_link)
        )
        markup.add(
            types.InlineKeyboardButton(
                "🔄 Авто-добавление групп", callback_data="auto_add_groups"
            )
        )

        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        return

    text = "🏷️ Зарегистрированные группы/каналы:\n\n"
    for chat_id, title, chat_type in groups:
        bot_status = "✅ Админ" if is_bot_admin_in_chat(chat_id) else "❌ Не админ"
        cursor.execute(
            "SELECT is_default FROM managed_groups WHERE chat_id=?", (chat_id,)
        )
        r = cursor.fetchone()
        is_default = r[0] if r else 0
        default_text = "✅ По умолчанию" if is_default else "❌ Не по умолчанию"
        emoji = "📢" if chat_type == "channel" else "👥"
        text += f"{emoji} <b>{title}</b>\nID: <code>{chat_id}</code>\nТип: {chat_type}\n{default_text}\nСтатус: {bot_status}\n\n"

    markup = types.InlineKeyboardMarkup()
    for chat_id, title, chat_type in groups:
        cursor.execute(
            "SELECT is_default FROM managed_groups WHERE chat_id=?", (chat_id,)
        )
        r = cursor.fetchone()
        is_default = r[0] if r else 0
        if not is_default:
            markup.add(
                types.InlineKeyboardButton(
                    f"⚡ Default: {title[:15]}", callback_data=f"set_default:{chat_id}"
                )
            )

    invite_link = get_bot_invite_link()
    markup.add(types.InlineKeyboardButton("🔗 Добавить новую группу", url=invite_link))

    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


# Авто-добавление групп
@bot.message_handler(func=lambda message: message.text == "🔄 Авто-добавление групп")
@only_private
def auto_add_groups(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    invite_link = get_bot_invite_link()
    text = (
        "🔄 <b>Автоматическое добавление групп/каналов</b>\n\n"
        "1) Добавьте бота в группу по ссылке ниже\n"
        "2) Назначьте права администратора\n"
        "3) Используйте команду /register_group в группе\n\n"
        f"🔗 Ссылка: {invite_link}"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔗 Добавить бота в группу", url=invite_link))
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


# Просмотр подписок
@bot.message_handler(func=lambda message: message.text == "📊 Подписки")
@only_private
def cmd_sublist(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    cursor.execute(
        """
        SELECT s.id, s.user_id, s.plan_id, s.start_ts, s.end_ts, s.active, s.group_id, p.title, s.payment_type, s.part_paid, s.current_period_month, s.current_period_year
        FROM subscriptions s
        LEFT JOIN plans p ON s.plan_id = p.id
        ORDER BY s.id DESC LIMIT 50
    """
    )
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "📭 Подписок нет.")
        return
    text = "📊 Последние подписки:\n\n"
    current_month, current_year = get_current_period()

    for (
        sid,
        uid,
        pid,
        st,
        et,
        active,
        gid,
        ptitle,
        payment_type,
        part_paid,
        period_month,
        period_year,
    ) in rows:
        status = "✅ Активна" if active else "❌ Неактивна"

        if period_month == current_month and period_year == current_year:
            if part_paid == "full":
                payment_status = "💰 Оплачено полностью"
            elif part_paid == "first":
                payment_status = "⏳ Ожидает вторую часть"
            else:
                payment_status = "❌ Не оплачено"
        else:
            payment_status = "📅 Требуется оплата за новый месяц"

        time_left = et - int(time.time())
        days_left = max(0, time_left // (24 * 3600))
        text += f"🎫 #{sid} | 👤 {uid} | 🏷️ {ptitle or pid}\n💳 {payment_type} | {payment_status}\n📊 {status} | ⏰ Осталось: {days_left}д\n🏠 Группа: {gid}\n\n"
    bot.send_message(message.chat.id, text)


# Просмотр пользователей
@bot.message_handler(func=lambda message: message.text == "👤 Пользователи")
@only_private
def cmd_users(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    cursor.execute(
        "SELECT user_id, referred_by, cashback_cents, username, join_date FROM users ORDER BY user_id DESC LIMIT 50"
    )
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "📭 Нет пользователей.")
        return
    text = "👤 Последние пользователи:\n\n"
    for user_id, referred_by, cashback_cents, username, join_date in rows:
        ref_text = f"👥 Реферер: {referred_by}" if referred_by else "🚫 Без реферера"
        join_date_str = (
            datetime.fromtimestamp(join_date, LOCAL_TZ).strftime("%Y-%m-%d")
            if join_date
            else "N/A"
        )
        text += f"🆔 ID: {user_id}\n👤 Username: {username or 'N/A'}\n{ref_text}\n💰 Баланс: {price_str_from_cents(cashback_cents)}\n📅 Регистрация: {join_date_str}\n\n"
    bot.send_message(message.chat.id, text)


# Управление оплатой
@bot.message_handler(func=lambda message: message.text == "💳 Управление оплатой")
@only_private
def cmd_payment_management(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    methods = get_active_payment_methods()
    text = "💳 <b>Управление способами оплаты</b>\n\n"
    for method_id, name, mtype, description, details in methods:
        status = "✅ Включен"
        text += f"<b>{name}</b> ({mtype})\n{description}\nСтатус: {status}\nID: {method_id}\n\n"

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "🔧 Настроить карту", callback_data="config_payment:card"
        ),
        types.InlineKeyboardButton(
            "🔧 Настроить ручную", callback_data="config_payment:manual"
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            "🔄 Переключить карту", callback_data="toggle_payment:card"
        ),
        types.InlineKeyboardButton(
            "🔄 Переключить ручную", callback_data="toggle_payment:manual"
        ),
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


# Заявки на оплату
@bot.message_handler(func=lambda message: message.text == "📋 Заявки на оплату")
@only_private
def cmd_pending_payments(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    cursor.execute(
        """
        SELECT mp.id, mp.user_id, mp.plan_id, mp.amount_cents, mp.receipt_photo, mp.full_name, mp.created_ts, p.title, u.username, mp.payment_type
        FROM manual_payments mp
        LEFT JOIN plans p ON mp.plan_id = p.id
        LEFT JOIN users u ON mp.user_id = u.user_id
        WHERE mp.status = 'pending'
        ORDER BY mp.created_ts
    """
    )
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "📭 Нет ожидающих заявок на оплату.")
        return

    for row in rows:
        (
            payment_id,
            user_id,
            plan_id,
            amount_cents,
            receipt_photo,
            full_name,
            created_ts,
            plan_title,
            username,
            payment_type,
        ) = row
        payment_type_text = get_payment_type_text(payment_type)

        text = (
            f"📋 <b>Заявка на оплату #{payment_id}</b>\n\n"
            f"👤 Пользователь: {username or 'N/A'} (ID: {user_id})\n"
            f"🏷️ Группа: {plan_title}\n"
            f"💵 Сумма: {price_str_from_cents(amount_cents)}\n"
            f"💳 Тип оплаты: {payment_type_text}\n"
            f"👤 ФИО: {full_name}\n"
            f"⏰ Время заявки: {datetime.fromtimestamp(created_ts, LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
        )

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "✅ Одобрить", callback_data=f"approve_payment:{payment_id}"
            ),
            types.InlineKeyboardButton(
                "❌ Отклонить", callback_data=f"reject_payment:{payment_id}"
            ),
        )

        if receipt_photo:
            try:
                bot.send_photo(
                    message.chat.id,
                    receipt_photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except:
                bot.send_message(
                    message.chat.id,
                    text + f"\n\n📎 Чек: {receipt_photo}",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
        else:
            bot.send_message(
                message.chat.id, text, parse_mode="HTML", reply_markup=markup
            )


# Управление промокодами
@bot.message_handler(func=lambda message: message.text == "🎫 Промокоды")
@only_private
def cmd_promo_codes(message):
    if message.from_user.id not in ADMIN_IDS:
        return

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("➕ Создать промокод", callback_data="create_promo"),
        types.InlineKeyboardButton("📋 Список промокодов", callback_data="list_promos"),
    )
    bot.send_message(message.chat.id, "🎫 Управление промокодами:", reply_markup=markup)


# ----------------- Admin creation flow -----------------


# Обработчики callback для админ-панели
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("select_group:")
)
def callback_select_group(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    group_data = call.data.split(":")[1]
    uid = call.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("step") != "group":
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    if group_data == "default":
        group_id = get_default_group()
        if not group_id:
            bot.answer_callback_query(call.id, "❌ Группа по умолчанию не установлена.")
            return
        state["group_id"] = group_id
        cursor.execute("SELECT title FROM managed_groups WHERE chat_id=?", (group_id,))
        group_title = cursor.fetchone()[0]
        bot.answer_callback_query(
            call.id, f"✅ Выбрана группа по умолчанию: {group_title}"
        )
    else:
        group_id = int(group_data)
        state["group_id"] = group_id
        cursor.execute("SELECT title FROM managed_groups WHERE chat_id=?", (group_id,))
        group_title = cursor.fetchone()[0]
        bot.answer_callback_query(call.id, f"✅ Выбрана группа: {group_title}")

    state["step"] = "media"
    if "media_files" not in state:
        state["media_files"] = []
    state["media_type"] = None

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton("⏩ Пропустить медиа"),
        types.KeyboardButton("✅ Завершить добавление медиа"),
    )

    bot.edit_message_text(
        f"Шаг 5/6: Прикрепите фото/видео превью для группы '{state['title']}' (можно несколько).\nГруппа: {group_title}\n\nКогда закончите - нажмите '✅ Завершить добавление медиа'.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=None,
    )
    bot.send_message(
        call.message.chat.id,
        "Отправляйте медиа или используйте кнопки ниже:",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("set_default:")
)
def callback_set_default(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return
    chat_id = int(call.data.split(":")[1])
    set_default_group(chat_id)
    cursor.execute("SELECT title FROM managed_groups WHERE chat_id=?", (chat_id,))
    title = cursor.fetchone()[0]
    bot.answer_callback_query(call.id, f"✅ Группа '{title}' установлена по умолчанию!")
    try:
        bot.edit_message_text(
            f"✅ Группа '{title}' установлена по умолчанию!",
            call.message.chat.id,
            call.message.message_id,
        )
    except:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "auto_add_groups")
def callback_auto_add_groups(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    invite_link = get_bot_invite_link()
    text = (
        "🔄 <b>Автоматическое добавление групп/каналов</b>\n\n"
        "1) Добавьте бота в группу по ссылке ниже\n"
        "2) Назначьте права администратора\n"
        "3) Используйте команду /register_group в группе\n\n"
        "🔗 Ссылка для добавления бота:"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔗 Добавить бота в группу", url=invite_link))

    bot.answer_callback_query(call.id, "ℹ️ Информация об авто-добавлении")
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("viewmedia:")
)
def callback_viewmedia(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return
    pid = int(call.data.split(":")[1])
    cursor.execute(
        "SELECT file_id, media_type FROM plan_media WHERE plan_id=? ORDER BY ord",
        (pid,),
    )
    rows = cursor.fetchall()
    if not rows:
        bot.answer_callback_query(call.id, "📭 Медиа у группы не найдены.")
        return
    try:
        for fid, mtype in rows:
            if mtype == "photo":
                bot.send_photo(call.message.chat.id, fid)
            else:
                bot.send_video(call.message.chat.id, fid)
    except:
        pass
    bot.answer_callback_query(call.id, "📦 Все медиа отправлены (если были).")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("delplan:")
)
def callback_delplan(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return
    pid = int(call.data.split(":")[1])
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_del:{pid}")
    )
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    bot.answer_callback_query(call.id, "⚠️ Подтвердите удаление группы.")
    bot.send_message(
        call.message.chat.id,
        f"Вы уверены, что хотите удалить группу обучения #{pid}?",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("confirm_del:")
)
def callback_confirm_del(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return
    pid = int(call.data.split(":")[1])
    try:
        cursor.execute("DELETE FROM plan_media WHERE plan_id=?", (pid,))
        cursor.execute("UPDATE plans SET is_active=0 WHERE id=?", (pid,))
        conn.commit()
        bot.answer_callback_query(call.id, "✅ Группа обучения удалена.")
        try:
            bot.edit_message_text(
                "Группа обучения удалена.",
                call.message.chat.id,
                call.message.message_id,
            )
        except:
            pass
    except Exception:
        logging.exception("Error deleting plan")
        bot.answer_callback_query(call.id, "❌ Ошибка при удалении группы.")


# Обработка заявок на оплату
@bot.callback_query_handler(
    func=lambda call: call.data
    and (
        call.data.startswith("approve_payment:")
        or call.data.startswith("reject_payment:")
    )
)
def handle_payment_review(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    is_approve = call.data.startswith("approve_payment:")
    payment_id = int(call.data.split(":")[1])

    cursor.execute(
        """
        SELECT mp.user_id, mp.plan_id, mp.amount_cents, p.title, u.username, mp.payment_type
        FROM manual_payments mp
        LEFT JOIN plans p ON mp.plan_id = p.id
        LEFT JOIN users u ON mp.user_id = u.user_id
        WHERE mp.id = ? AND mp.status = 'pending'
    """,
        (payment_id,),
    )

    payment = cursor.fetchone()
    if not payment:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена или уже обработана.")
        return

    user_id, plan_id, amount_cents, plan_title, username, payment_type = payment

    if is_approve:
        # Одобряем заявку
        success, result = activate_subscription(user_id, plan_id, payment_type)
        if success:
            cursor.execute(
                "UPDATE manual_payments SET status='approved', admin_id=?, reviewed_ts=? WHERE id=?",
                (call.from_user.id, int(time.time()), payment_id),
            )
            conn.commit()

            # Уведомляем пользователя
            try:
                bot.send_message(
                    user_id,
                    f"✅ Ваша заявка на группу '{plan_title}' одобрена!\n\n🔗 Ваша пригласительная ссылка (одноразовая):\n{result}",
                )
            except:
                pass

            bot.answer_callback_query(call.id, "✅ Заявка одобрена!")
            try:
                bot.edit_message_caption(
                    f"✅ ЗАЯВКА ОДОБРЕНА\n\nПользователь: {username or user_id}\nГруппа: {plan_title}",
                    call.message.chat.id,
                    call.message.message_id,
                )
            except:
                pass
        else:
            bot.answer_callback_query(call.id, f"❌ Ошибка: {result}")
    else:
        # Отклоняем заявку
        cursor.execute(
            "UPDATE manual_payments SET status='rejected', admin_id=?, reviewed_ts=? WHERE id=?",
            (call.from_user.id, int(time.time()), payment_id),
        )
        conn.commit()

        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                f"❌ Ваша заявка на группу '{plan_title}' отклонена. Если вы считаете это ошибкой, свяжитесь с администратором.",
            )
        except:
            pass

        bot.answer_callback_query(call.id, "❌ Заявка отклонена!")
        try:
            bot.edit_message_caption(
                f"❌ ЗАЯВКА ОТКЛОНЕНA\n\nПользователь: {username or user_id}\nГруппа: {plan_title}",
                call.message.chat.id,
                call.message.message_id,
            )
        except:
            pass


# Управление способами оплаты
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("config_payment:")
)
def callback_config_payment(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    payment_type = call.data.split(":")[1]

    cursor.execute(
        "SELECT id, name, description, details FROM payment_methods WHERE type=?",
        (payment_type,),
    )
    method = cursor.fetchone()

    if not method:
        bot.answer_callback_query(call.id, "❌ Способ оплаты не найден.")
        return

    method_id, name, description, details = method

    text = (
        f"🔧 <b>Настройка способа оплаты: {name}</b>\n\n"
        f"📝 Текущее описание: {description}\n"
        f"💳 Текущие реквизиты: {details or 'Не указаны'}\n\n"
        f"Отправьте новое описание и реквизиты в формате:\n"
        f"<code>Описание|Реквизиты</code>\n\n"
        f"Пример:\n<code>Оплата картой|Реквизиты: 0000 0000 0000 0000</code>"
    )

    admin_states[call.from_user.id] = {
        "mode": "config_payment",
        "method_id": method_id,
        "chat_id": call.message.chat.id,
    }

    bot.answer_callback_query(call.id, "✏️ Введите новые настройки")
    bot.send_message(call.message.chat.id, text, parse_mode="HTML")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("toggle_payment:")
)
def callback_toggle_payment(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    payment_type = call.data.split(":")[1]

    cursor.execute(
        "SELECT id, is_active FROM payment_methods WHERE type=?", (payment_type,)
    )
    method = cursor.fetchone()

    if not method:
        bot.answer_callback_query(call.id, "❌ Способ оплаты не найден.")
        return

    method_id, is_active = method
    new_status = 0 if is_active else 1

    cursor.execute(
        "UPDATE payment_methods SET is_active=? WHERE id=?", (new_status, method_id)
    )
    conn.commit()

    status_text = "включен" if new_status else "выключен"
    bot.answer_callback_query(call.id, f"✅ Способ оплаты {status_text}!")

    # Обновляем сообщение
    methods = get_active_payment_methods()
    text = "💳 <b>Управление способами оплаты</b>\n\n"
    for method_id, name, mtype, description, details in methods:
        status = (
            "✅ Включен"
            if cursor.execute(
                "SELECT is_active FROM payment_methods WHERE id=?", (method_id,)
            ).fetchone()[0]
            else "❌ Выключен"
        )
        text += f"<b>{name}</b> ({mtype})\n{description}\nСтатус: {status}\nID: {method_id}\n\n"

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "🔧 Настроить карту", callback_data="config_payment:card"
        ),
        types.InlineKeyboardButton(
            "🔧 Настроить ручную", callback_data="config_payment:manual"
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            "🔄 Переключить карту", callback_data="toggle_payment:card"
        ),
        types.InlineKeyboardButton(
            "🔄 Переключить ручную", callback_data="toggle_payment:manual"
        ),
    )

    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except:
        pass


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "config_payment"
    and m.chat.type == "private"
)
def handle_payment_config(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if not message.text or "|" not in message.text:
        bot.send_message(
            message.chat.id, "❌ Неправильный формат. Используйте: Описание|Реквизиты"
        )
        return

    parts = message.text.split("|", 1)
    description = parts[0].strip()
    details = parts[1].strip()

    cursor.execute(
        "UPDATE payment_methods SET description=?, details=? WHERE id=?",
        (description, details, state["method_id"]),
    )
    conn.commit()

    admin_states.pop(uid, None)
    bot.send_message(message.chat.id, "✅ Настройки способа оплаты обновлены!")


# Управление промокодами
@bot.callback_query_handler(func=lambda call: call.data == "create_promo")
def callback_create_promo(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    admin_states[call.from_user.id] = {
        "mode": "create_promo",
        "step": "type",
        "chat_id": call.message.chat.id,
    }

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "📊 Процентная скидка", callback_data="promo_type:percent"
        ),
        types.InlineKeyboardButton(
            "💵 Фиксированная скидка", callback_data="promo_type:fixed"
        ),
    )

    bot.answer_callback_query(call.id, "Создание промокода...")
    bot.send_message(
        call.message.chat.id,
        "🎫 Создание промокода\n\nВыберите тип скидки:",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("promo_type:")
)
def callback_promo_type(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    promo_type = call.data.split(":")[1]
    uid = call.from_user.id

    if uid not in admin_states or admin_states[uid].get("mode") != "create_promo":
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    admin_states[uid]["promo_type"] = promo_type
    admin_states[uid]["step"] = "value"

    if promo_type == "percent":
        text = "Введите размер скидки в процентах (например: 10 для 10%):"
    else:
        text = "Введите размер фиксированной скидки (например: 5.00 для 5 рублей):"

    bot.answer_callback_query(call.id, "Введите значение скидки")
    bot.send_message(call.message.chat.id, text)


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create_promo"
    and admin_states.get(m.from_user.id, {}).get("step") == "value"
    and m.chat.type == "private"
)
def handle_promo_value(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    promo_type = state.get("promo_type")
    value_text = message.text.strip()

    try:
        if promo_type == "percent":
            discount_percent = int(value_text)
            if discount_percent <= 0 or discount_percent > 100:
                raise ValueError
            state["discount_percent"] = discount_percent
            state["discount_fixed_cents"] = 0
        else:
            discount_cents = cents_from_str(value_text)
            if discount_cents <= 0:
                raise ValueError
            state["discount_percent"] = 0
            state["discount_fixed_cents"] = discount_cents

        state["step"] = "max_uses"
        bot.send_message(
            message.chat.id,
            "Введите максимальное количество использований (или 0 для безлимита):",
        )

    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверное значение. Попробуйте снова:")


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create_promo"
    and admin_states.get(m.from_user.id, {}).get("step") == "max_uses"
    and m.chat.type == "private"
)
def handle_promo_max_uses(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    try:
        max_uses = int(message.text.strip())
        if max_uses < 0:
            raise ValueError

        state["max_uses"] = max_uses if max_uses > 0 else None
        state["step"] = "expires"

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.row(types.KeyboardButton("⏩ Без срока"), types.KeyboardButton("7 дней"))
        markup.row(types.KeyboardButton("30 дней"), types.KeyboardButton("90 дней"))

        bot.send_message(
            message.chat.id, "Выберите срок действия промокода:", reply_markup=markup
        )

    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверное значение. Введите число:")


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "create_promo"
    and admin_states.get(m.from_user.id, {}).get("step") == "expires"
    and m.chat.type == "private"
)
def handle_promo_expires(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    text = message.text.strip()
    expires_ts = None

    if text == "⏩ Без срока":
        expires_ts = None
    elif text == "7 дней":
        expires_ts = int(time.time()) + 7 * 24 * 3600
    elif text == "30 дней":
        expires_ts = int(time.time()) + 30 * 24 * 3600
    elif text == "90 дней":
        expires_ts = int(time.time()) + 90 * 24 * 3600
    else:
        bot.send_message(message.chat.id, "❌ Выберите вариант из кнопок:")
        return

    # Генерируем промокод
    code = generate_promo_code()

    # Сохраняем в базу
    cursor.execute(
        """
        INSERT INTO promo_codes (code, discount_percent, discount_fixed_cents, max_uses, created_ts, expires_ts)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (
            code,
            state["discount_percent"],
            state["discount_fixed_cents"],
            state["max_uses"],
            int(time.time()),
            expires_ts,
        ),
    )
    conn.commit()

    # Формируем информацию о промокоде
    promo_info = f"🎫 Промокод: <code>{code}</code>\n"
    if state["discount_percent"]:
        promo_info += f"📊 Скидка: {state['discount_percent']}%\n"
    else:
        promo_info += (
            f"💵 Скидка: {price_str_from_cents(state['discount_fixed_cents'])}\n"
        )

    promo_info += f"🔄 Макс. использований: {state['max_uses'] or 'безлимит'}\n"

    if expires_ts:
        expires_str = datetime.fromtimestamp(expires_ts, LOCAL_TZ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        promo_info += f"⏰ Действует до: {expires_str}\n"
    else:
        promo_info += "⏰ Срок действия: бессрочно\n"

    admin_states.pop(uid, None)

    # Возвращаем обычную клавиатуру
    bot.send_message(
        message.chat.id,
        f"✅ Промокод создан!\n\n{promo_info}",
        parse_mode="HTML",
        reply_markup=main_menu(uid),
    )


@bot.callback_query_handler(func=lambda call: call.data == "list_promos")
def callback_list_promos(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    cursor.execute(
        "SELECT code, discount_percent, discount_fixed_cents, is_active, used_count, max_uses, expires_ts FROM promo_codes ORDER BY created_ts DESC"
    )
    promos = cursor.fetchall()

    if not promos:
        bot.answer_callback_query(call.id, "📭 Нет промокодов.")
        return

    text = "📋 Список промокодов:\n\n"

    for promo in promos:
        (
            code,
            discount_percent,
            discount_fixed_cents,
            is_active,
            used_count,
            max_uses,
            expires_ts,
        ) = promo

        text += f"🎫 <code>{code}</code>\n"
        if discount_percent:
            text += f"📊 Скидка: {discount_percent}%\n"
        else:
            text += f"💵 Скидка: {price_str_from_cents(discount_fixed_cents)}\n"

        status = "✅ Активен" if is_active else "❌ Неактивен"
        text += f"📊 Статус: {status}\n"
        text += f"🔄 Использован: {used_count} раз"
        if max_uses:
            text += f" из {max_uses}\n"
        else:
            text += " (безлимит)\n"

        if expires_ts:
            expires_str = datetime.fromtimestamp(expires_ts, LOCAL_TZ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            text += f"⏰ Действует до: {expires_str}\n"
        else:
            text += "⏰ Срок: бессрочно\n"

        text += "\n"

    bot.answer_callback_query(call.id, "📋 Список промокодов")
    bot.send_message(call.message.chat.id, text, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def callback_cancel(call):
    bot.answer_callback_query(call.id, "Отменено.")


# ----------------- Notification system -----------------


@bot.callback_query_handler(func=lambda call: call.data == "show_plans_notification")
def callback_show_plans_notification(call):
    """Показывает группы обучения при нажатии на уведомление"""
    show_plans(call.message)
    bot.answer_callback_query(call.id)


# ----------------- Expiration and cleanup system -----------------
def check_expirations_loop():
    """Проверяет истечение сроков оплаты и удаляет неуплативших - только полная оплата"""
    while True:
        try:
            now = now_local()
            current_day = now.day
            current_hour = now.hour
            current_minute = now.minute
            current_month, current_year = get_current_period()
            now_ts = int(time.time())

            # 6-го числа в 00:01 - удаление тех, кто не оплатил
            if current_day == 6 and current_hour == 0 and current_minute == 1:
                logging.info("🗑️ Удаление неплательщиков (6-е число)")
                remove_unpaid_users()
                time.sleep(60)

            # 1-го числа в 10:00 - уведомление о необходимости оплаты
            elif current_day == 1 and current_hour == 10 and current_minute == 0:
                logging.info("📅 Отправка уведомлений об оплате (1-е число)")
                send_payment_notifications()
                time.sleep(60)

            # 4-го числа в 18:00 - Напоминание о скором дедлайне
            elif current_day == 4 and current_hour == 18 and current_minute == 0:
                logging.info("⏰ Отправка напоминаний о дедлайне (4-е число)")
                send_deadline_notifications()
                time.sleep(60)

            time.sleep(60)  # Проверяем каждую минуту

        except Exception as e:
            logging.exception("❌ Критическая ошибка в check_expirations_loop")
            time.sleep(60)


def remove_unpaid_users():
    """Удаляет пользователей с истекшими подписками из групп"""
    try:
        current_month, current_year = get_current_period()
        now_ts = int(time.time())

        # Находим пользователей, чьи подписки истекли И не оплачены на текущий месяц
        cursor.execute(
            """
            SELECT DISTINCT s.id, s.user_id, s.group_id, s.plan_id, p.title, u.username
            FROM subscriptions s
            JOIN plans p ON s.plan_id = p.id
            JOIN users u ON s.user_id = u.user_id
            WHERE s.active = 1 
            AND s.end_ts < ?
            AND (
                s.current_period_month != ? 
                OR s.current_period_year != ? 
                OR s.part_paid != 'full'
            )
        """,
            (now_ts, current_month, current_year),
        )

        expired_subs = cursor.fetchall()

        if expired_subs:
            logging.info(f"📊 Найдено {len(expired_subs)} подписок для удаления")

            for (
                sub_id,
                user_id,
                group_id,
                plan_id,
                plan_title,
                username,
            ) in expired_subs:
                try:
                    # Пытаемся удалить из группы
                    if group_id:
                        try:
                            # Используем ban_chat_member с коротким баном (30 секунд)
                            bot.ban_chat_member(
                                group_id, user_id, until_date=now_ts + 30
                            )
                            logging.info(
                                f"👤 Удален пользователь {username or user_id} из группы {group_id}"
                            )
                            time.sleep(0.5)  # Задержка для API
                        except Exception as e:
                            logging.warning(
                                f"❌ Не удалось удалить пользователя {user_id} из группы {group_id}: {e}"
                            )
                            # Не останавливаем выполнение, продолжаем с остальными

                    # Деактивируем подписку
                    cursor.execute(
                        "UPDATE subscriptions SET active = 0, removed = 1 WHERE id = ?",
                        (sub_id,),
                    )
                    conn.commit()

                    # Уведомляем пользователя
                    try:
                        bot.send_message(
                            user_id,
                            f"❌ Доступ к группе '{plan_title}' приостановлен.\n\n"
                            "Вы не оплатили подписку за текущий месяц. "
                            "Для восстановления доступа оплатите подписку в разделе '📋 Группы обучения'.",
                        )
                        logging.info(
                            f"📢 Отправлено уведомление пользователю {username or user_id}"
                        )
                    except Exception as e:
                        logging.warning(
                            f"❌ Не удалось отправить уведомление пользователю {user_id}: {e}"
                        )

                except Exception as e:
                    logging.error(f"❌ Ошибка обработки подписки {sub_id}: {e}")
                    continue  # Продолжаем обработку остальных

    except Exception as e:
        logging.error(f"❌ Ошибка в remove_unpaid_users: {e}")


def safe_remove_from_chat(chat_id, user_id):
    """Безопасное удаление пользователя из чата"""
    try:
        # Проверяем, есть ли пользователь в чате
        try:
            member = bot.get_chat_member(chat_id, user_id)
            if member.status in ["left", "kicked"]:
                return True  # Уже не в чате
        except:
            return True  # Пользователь не найден в чате

        # Пытаемся удалить с коротким баном
        bot.ban_chat_member(chat_id, user_id, until_date=int(time.time()) + 30)
        time.sleep(0.3)  # Задержка для API
        return True
    except Exception as e:
        logging.error(f"Ошибка удаления пользователя {user_id} из чата {chat_id}: {e}")
        return False


# Запускаем фоновые процессы
threading.Thread(target=check_expirations_loop, daemon=True).start()


def send_deadline_notifications():
    """Отправляет уведомления о скором дедлайне оплаты с кнопкой продления"""
    try:
        current_month, current_year = get_current_period()
        now_ts = int(time.time())

        # Находим подписки, которые истекают в ближайшие 5 дней
        cursor.execute(
            """
            SELECT s.user_id, u.username, s.plan_id, p.title, s.end_ts, p.price_cents, s.id as sub_id
            FROM subscriptions s
            JOIN users u ON s.user_id = u.user_id
            JOIN plans p ON s.plan_id = p.id
            WHERE s.active = 1 
            AND s.end_ts BETWEEN ? AND ?
            AND (s.current_period_month = ? AND s.current_period_year = ? AND s.part_paid = 'full')
            ORDER BY s.end_ts
        """,
            (now_ts, now_ts + 5 * 24 * 3600, current_month, current_year),
        )

        users = cursor.fetchall()

        notification_count = 0
        for (
            user_id,
            username,
            plan_id,
            plan_title,
            end_ts,
            price_cents,
            sub_id,
        ) in users:
            try:
                days_left = (end_ts - now_ts) // (24 * 3600)

                text = (
                    f"⏰ <b>Напоминание о дедлайне!</b>\n\n"
                    f"Группа: {plan_title}\n"
                    f"📅 Срок действия подписки заканчивается через {days_left} дней ({datetime.fromtimestamp(end_ts, LOCAL_TZ).strftime('%d.%m.%Y')})\n\n"
                    f"💳 <b>Успейте продлить подписку!</b>\n"
                    f"• Полная оплата - доступ до 5 числа следующего месяца\n\n"
                    f"После истечения срока доступ к группе будет приостановлен."
                )

                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton(
                        f"🔄 Продлить за {price_str_from_cents(price_cents)}",
                        callback_data=f"renew_plan:{plan_id}",
                    )
                )

                bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
                notification_count += 1

                logging.info(
                    f"📨 Отправлено уведомление о дедлайне пользователю {user_id}"
                )

            except Exception as e:
                logging.error(
                    f"Error sending deadline notification to user {user_id}: {e}"
                )

        logging.info(f"📊 Отправлено {notification_count} уведомлений о дедлайне")
        return notification_count

    except Exception as e:
        logging.error(f"Error in send_deadline_notifications: {e}")
        return 0


def send_payment_notifications():
    """Отправляет уведомления о необходимости оплаты - только тем, кто не оплатил"""
    try:
        current_month, current_year = get_current_period()
        now_ts = int(time.time())
        now = now_local()
        cooldown_seconds = 20 * 3600  # защита от повторных отправок при перезапусках

        # Находим пользователей с активными подписками, но не оплаченными на текущий месяц.
        # Важно: НЕ фильтруем по end_ts < now_ts, т.к. у нас есть льготный период до 5-го числа,
        # но напоминание нужно отправлять 1-го.
        cursor.execute(
            """
            SELECT DISTINCT s.user_id, u.username, s.plan_id, p.title, p.price_cents, s.id as sub_id
            FROM subscriptions s
            JOIN users u ON s.user_id = u.user_id
            JOIN plans p ON s.plan_id = p.id
            WHERE s.active = 1 
            AND NOT (
                s.current_period_month = ? 
                AND s.current_period_year = ? 
                AND s.part_paid = 'full'
            )
            AND (
                s.last_notification_ts IS NULL
                OR s.last_notification_ts < ?
            )
            ORDER BY s.user_id
        """,
            (current_month, current_year, now_ts - cooldown_seconds),
        )

        users = cursor.fetchall()

        notification_count = 0
        for user_id, username, plan_id, plan_title, price_cents, sub_id in users:
            try:
                text = (
                    f"📅 <b>Напоминание об оплате за {now.strftime('%B %Y')}</b>\n\n"
                    f"Группа: {plan_title}\n"
                    f"Наступил новый месяц! Для продолжения доступа к группе обучения необходимо оплатить подписку.\n\n"
                    f"💰 Сумма к оплате: {price_str_from_cents(price_cents)}\n"
                    f"⏰ <b>Оплатите до 5 числа следующего месяца</b>\n\n"
                    f"После истечения срока доступ к группе будет приостановлен."
                )

                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton(
                        f"💳 Оплатить {price_str_from_cents(price_cents)}",
                        callback_data=f"renew_plan:{plan_id}",
                    )
                )

                bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
                notification_count += 1

                # Обновляем время последнего уведомления
                cursor.execute(
                    """
                    UPDATE subscriptions 
                    SET last_notification_ts = ? 
                    WHERE id = ?
                """,
                    (now_ts, sub_id),
                )
                conn.commit()

                logging.info(
                    f"📨 Отправлено уведомление об оплате пользователю {user_id} ({username or 'нет username'})"
                )

            except Exception as e:
                logging.error(f"Error sending notification to user {user_id}: {e}")

        logging.info(f"📊 Отправлено {notification_count} уведомлений об оплате")
        return notification_count

    except Exception as e:
        logging.error(f"Error in send_payment_notifications: {e}")
        return 0


@bot.message_handler(commands=["run_payment_notifications"])
@only_private
def cmd_run_payment_notifications(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    cnt = send_payment_notifications()
    bot.send_message(
        message.chat.id, f"✅ Готово. Отправлено уведомлений об оплате: {cnt}"
    )


@bot.message_handler(commands=["run_deadline_notifications"])
@only_private
def cmd_run_deadline_notifications(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    cnt = send_deadline_notifications()
    bot.send_message(
        message.chat.id, f"✅ Готово. Отправлено уведомлений о дедлайне: {cnt}"
    )


@bot.message_handler(commands=["run_remove_unpaid"])
@only_private
def cmd_run_remove_unpaid(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    remove_unpaid_users()
    bot.send_message(
        message.chat.id,
        "✅ Готово. Процедура удаления неплательщиков выполнена (подробности в логах).",
    )


def check_existing_subscription(user_id, plan_id):
    """Проверяет, есть ли у пользователя активная подписка на план"""
    current_month, current_year = get_current_period()
    now_ts = int(time.time())

    cursor.execute(
        """
        SELECT s.id, s.active, s.part_paid, s.end_ts, p.title, 
               s.current_period_month, s.current_period_year,
               s.user_id, s.plan_id, s.group_id
        FROM subscriptions s
        JOIN plans p ON s.plan_id = p.id
        WHERE s.user_id = ? AND s.plan_id = ? 
        AND s.active = 1
        ORDER BY s.end_ts DESC
        LIMIT 1
    """,
        (user_id, plan_id),
    )

    existing = cursor.fetchone()
    if not existing:
        return None

    (
        sub_id,
        active,
        part_paid,
        end_ts,
        plan_title,
        sub_month,
        sub_year,
        user_id,
        plan_id,
        group_id,
    ) = existing

    # Определяем статус оплаты
    paid_for_current = (
        sub_month == current_month
        and sub_year == current_year
        and part_paid == "full"
        and end_ts > now_ts
    )

    return {
        "id": sub_id,
        "paid": paid_for_current,  # Булево значение: True если оплачена на текущий месяц
        "active": bool(active),
        "part_paid": part_paid,
        "end_ts": end_ts,
        "plan_title": plan_title,
        "current_month": sub_month,
        "current_year": sub_year,
        "user_id": user_id,
        "plan_id": plan_id,
        "group_id": group_id,
        "needs_renewal": not paid_for_current or end_ts <= now_ts,
        "status": (
            "paid"
            if paid_for_current
            else "expired" if end_ts < now_ts else "needs_payment"
        ),
    }


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("edit_field:category:")
)
def callback_edit_category_field(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    plan_id = int(call.data.split(":")[2])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    state["step"] = "editing_category"

    # Показываем выбор категории
    categories = get_all_categories()
    markup = types.InlineKeyboardMarkup()
    for cat_id, name, description in categories:
        button_text = name
        if description:
            button_text += f" - {description}"
        markup.add(
            types.InlineKeyboardButton(
                button_text, callback_data=f"select_edit_category:{cat_id}:{plan_id}"
            )
        )

    # Получаем текущую категорию
    cursor.execute("SELECT category_id FROM plans WHERE id=?", (plan_id,))
    current_category_id = cursor.fetchone()[0]

    current_category = (
        get_category_by_id(current_category_id) if current_category_id else None
    )
    current_category_name = current_category[1] if current_category else "Не указан"

    bot.send_message(
        call.message.chat.id,
        f"📚 <b>Изменение предмета</b>\n\n"
        f"Текущий предмет: {current_category_name}\n"
        f"Выберите новый предмет:",
        parse_mode="HTML",
        reply_markup=markup,
    )
    bot.answer_callback_query(call.id, "Изменение предмета")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("select_edit_category:")
)
def callback_select_edit_category(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    parts = call.data.split(":")
    category_id = int(parts[1])
    plan_id = int(parts[2])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    # Обновляем категорию в базе
    cursor.execute("UPDATE plans SET category_id=? WHERE id=?", (category_id, plan_id))
    conn.commit()

    # Обновляем состояние
    state["current_category_id"] = category_id

    category = get_category_by_id(category_id)
    category_name = category[1] if category else "Неизвестно"

    bot.answer_callback_query(call.id, f"✅ Предмет изменен: {category_name}")

    # Возвращаемся к меню редактирования
    state["step"] = "edit_choice"
    show_edit_menu(call.message.chat.id, state)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("editplan:")
)
def callback_edit_plan(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    pid = int(call.data.split(":")[1])

    # Получаем информацию о группе
    cursor.execute(
        """
        SELECT p.id, p.title, p.price_cents, p.description, p.group_id, p.media_file_ids, p.media_type
        FROM plans p
        WHERE p.id=?
    """,
        (pid,),
    )

    plan = cursor.fetchone()
    if not plan:
        bot.answer_callback_query(call.id, "❌ Группа не найдена.")
        return

    plan_id, title, price_cents, description, group_id, media_file_ids, media_type = (
        plan
    )

    # Инициализируем состояние редактирования
    uid = call.from_user.id
    admin_states[uid] = {
        "mode": "edit",
        "step": "edit_choice",
        "plan_id": plan_id,
        "current_title": title,
        "current_price": price_cents,
        "current_description": description,
        "current_group_id": group_id,
        "media_files": media_file_ids.split(",") if media_file_ids else [],
        "media_type": media_type,
        "chat_id": call.message.chat.id,
    }

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "📚 Изменить предмет", callback_data=f"edit_field:category:{plan_id}"
        )
    )
    markup.row(
        types.InlineKeyboardButton(
            "📝 Ред. название", callback_data=f"edit_field:title:{plan_id}"
        ),
        types.InlineKeyboardButton(
            "💰 Ред. цену", callback_data=f"edit_field:price:{plan_id}"
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            "📋 Ред. описание", callback_data=f"edit_field:description:{plan_id}"
        ),
        types.InlineKeyboardButton(
            "👥 Изменить группу", callback_data=f"edit_field:group:{plan_id}"
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            "✏️🖼️ медиа", callback_data=f"edit_field:media:{plan_id}"
        ),
        types.InlineKeyboardButton(
            "✅ Завершить редактирование", callback_data=f"edit_finish:{plan_id}"
        ),
    )

    text = f"✏️ <b>Редактирование группы:</b> {title}\n\nВыберите что хотите изменить:"

    bot.answer_callback_query(call.id, "✏️ Режим редактирования")
    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("edit_field:")
)
def callback_edit_field(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    parts = call.data.split(":")
    field = parts[1]
    plan_id = int(parts[2])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    state["step"] = f"editing_{field}"

    if field == "title":
        bot.send_message(
            call.message.chat.id,
            f"✏️ Текущее название: {state['current_title']}\nВведите новое название:",
        )
    elif field == "price":
        bot.send_message(
            call.message.chat.id,
            f"✏️ Текущая цена: {price_str_from_cents(state['current_price'])}\nВведите новую цену (например: 14.99):",
        )
    elif field == "description":
        bot.send_message(
            call.message.chat.id,
            f"✏️ Текущее описание: {state['current_description']}\nВведите новое описание:",
        )
    elif field == "group":
        groups = get_all_groups_with_bot()
        markup = types.InlineKeyboardMarkup()
        for chat_id, title, chat_type in groups:
            markup.add(
                types.InlineKeyboardButton(
                    f"{title} ({chat_type})",
                    callback_data=f"select_edit_group:{chat_id}:{plan_id}",
                )
            )

        cursor.execute(
            "SELECT title FROM managed_groups WHERE chat_id=?",
            (state["current_group_id"],),
        )
        current_group = cursor.fetchone()
        current_group_title = current_group[0] if current_group else "Неизвестно"

        bot.send_message(
            call.message.chat.id,
            f"👥 Текущая группа: {current_group_title}\nВыберите новую группу:",
            reply_markup=markup,
        )
    elif field == "media":
        # Показываем меню управления медиа вместо прямого перехода к добавлению
        show_media_management_menu(call.message.chat.id, state)

    bot.answer_callback_query(call.id, f"Редактирование {field}")


def show_media_management_menu(chat_id, state):
    """Показывает меню управления медиа"""
    plan_id = state["plan_id"]
    media_count = len(state.get("media_files", []))

    text = f"🖼️ <b>Управление медиа для группы '{state['current_title']}'</b>\n\n"
    text += f"📊 Текущее количество медиа: {media_count}\n\n"

    if media_count > 0:
        text += "✅ Медиа загружены. Вы можете:\n• Добавить новые медиа\n• Удалить все текущие медиа\n• Просмотреть текущие медиа"
    else:
        text += "📭 Медиа отсутствуют. Вы можете добавить новые медиа."

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "➕ Добавить медиа", callback_data=f"add_media:{plan_id}"
        ),
        types.InlineKeyboardButton(
            "🗑️ Удалить все медиа", callback_data=f"clear_media:{plan_id}"
        ),
    )

    if media_count > 0:
        markup.row(
            types.InlineKeyboardButton(
                "👀 Просмотреть текущие медиа",
                callback_data=f"view_current_media:{plan_id}",
            )
        )

    markup.row(
        types.InlineKeyboardButton(
            "🔙 Назад к редактированию", callback_data=f"back_to_edit:{plan_id}"
        )
    )

    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("add_media:")
)
def callback_add_media(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    plan_id = int(call.data.split(":")[1])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    state["step"] = "adding_media"

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("✅ Завершить добавление медиа"))
    markup.row(types.KeyboardButton("🔙 Назад к управлению медиа"))

    bot.send_message(
        call.message.chat.id,
        "📎 Отправляйте фото или видео для добавления.\n\n"
        "💡 <b>Примечание:</b> Новые медиа заменят существующие.\n"
        "Когда закончите - нажмите '✅ Завершить добавление медиа'.",
        parse_mode="HTML",
        reply_markup=markup,
    )
    bot.answer_callback_query(call.id, "Добавление медиа...")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("clear_media:")
)
def callback_clear_media(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    plan_id = int(call.data.split(":")[1])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    # Удаляем все медиа из базы
    cursor.execute("DELETE FROM plan_media WHERE plan_id=?", (plan_id,))
    cursor.execute(
        "UPDATE plans SET media_file_id=NULL, media_file_ids=NULL, media_type=NULL WHERE id=?",
        (plan_id,),
    )
    conn.commit()

    # Обновляем состояние
    state["media_files"] = []
    state["media_type"] = None

    bot.answer_callback_query(call.id, "✅ Все медиа удалены!")

    # Показываем меню управления медиа снова
    show_media_management_menu(call.message.chat.id, state)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("view_current_media:")
)
def callback_view_current_media(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    plan_id = int(call.data.split(":")[1])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    # Отправляем текущие медиа
    media_files = state.get("media_files", [])
    media_type = state.get("media_type")

    if not media_files:
        bot.answer_callback_query(call.id, "📭 Нет медиа для просмотра")
        return

    bot.answer_callback_query(call.id, "📦 Отправляем текущие медиа...")

    try:
        # Отправляем первое медиа с описанием
        if media_type == "photo":
            bot.send_photo(
                call.message.chat.id,
                media_files[0],
                caption=f"🖼️ Текущие медиа ({len(media_files)} шт.)\nПервый элемент из {len(media_files)}",
            )
        elif media_type == "video":
            bot.send_video(
                call.message.chat.id,
                media_files[0],
                caption=f"🎥 Текущие медиа ({len(media_files)} шт.)\nПервый элемент из {len(media_files)}",
            )

        # Если есть еще медиа, отправляем остальные (ограничим 5)
        if len(media_files) > 1:
            remaining_media = media_files[1:5]  # Ограничиваем 5 медиа
            media_group = []

            for file_id in remaining_media:
                if media_type == "photo":
                    media_group.append(types.InputMediaPhoto(file_id))
                elif media_type == "video":
                    media_group.append(types.InputMediaVideo(file_id))

            if media_group:
                bot.send_media_group(call.message.chat.id, media_group)

            if len(media_files) > 5:
                bot.send_message(
                    call.message.chat.id, f"📁 ... и еще {len(media_files) - 5} медиа"
                )

    except Exception as e:
        logging.error(f"Error sending media: {e}")
        bot.send_message(call.message.chat.id, "❌ Ошибка при отправке медиа")


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("back_to_edit:")
)
def callback_back_to_edit(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    plan_id = int(call.data.split(":")[1])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    # Возвращаемся к меню редактирования
    show_edit_menu(call.message.chat.id, state)
    bot.answer_callback_query(call.id, "🔙 Назад к редактированию")


# Обработчик медиа в режиме добавления
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "edit"
    and admin_states.get(m.from_user.id, {}).get("step") == "adding_media"
    and m.chat.type == "private"
)
def handle_adding_media(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "photo"
        bot.send_message(
            message.chat.id, f"✅ Фото добавлено! Всего: {len(state['media_files'])}"
        )
        return

    if message.video:
        file_id = message.video.file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "video"
        bot.send_message(
            message.chat.id, f"✅ Видео добавлено! Всего: {len(state['media_files'])}"
        )
        return

    if message.text:
        txt = message.text.strip()
        if txt == "✅ Завершить добавление медиа":
            # Сохраняем новые медиа
            media_files = state.get("media_files", [])
            media_type = state.get("media_type")

            if media_files:
                first_media = media_files[0]
                media_ids_str = ",".join(media_files)

                # Обновляем медиа в базе
                cursor.execute(
                    "UPDATE plans SET media_file_id=?, media_file_ids=?, media_type=? WHERE id=?",
                    (first_media, media_ids_str, media_type, state["plan_id"]),
                )

                # Очищаем старые медиа и добавляем новые
                cursor.execute(
                    "DELETE FROM plan_media WHERE plan_id=?", (state["plan_id"],)
                )
                for idx, fid in enumerate(media_files):
                    cursor.execute(
                        "INSERT INTO plan_media (plan_id, file_id, media_type, ord, added_ts) VALUES (?, ?, ?, ?, ?)",
                        (state["plan_id"], fid, media_type, idx, int(time.time())),
                    )

                conn.commit()

                cnt = len(media_files)
                bot.send_message(
                    message.chat.id,
                    f"✅ Медиа обновлены!\n📊 Загружено {cnt} медиа",
                    reply_markup=types.ReplyKeyboardRemove(),
                )
            else:
                bot.send_message(
                    message.chat.id,
                    "✅ Медиа не изменены",
                    reply_markup=types.ReplyKeyboardRemove(),
                )

            state["step"] = "edit_choice"
            # Показываем меню управления медиа снова
            show_media_management_menu(message.chat.id, state)
            return

        elif txt == "🔙 Назад к управлению медиа":
            # Возвращаемся к управлению медиа без сохранения
            state["step"] = "edit_choice"
            show_media_management_menu(message.chat.id, state)
            return

        bot.send_message(
            message.chat.id, "❌ Отправляйте фото или видео, или используйте кнопки"
        )


@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "edit"
    and admin_states.get(m.from_user.id, {}).get("step") == "adding_media"
    and m.chat.type == "private",
    content_types=["photo", "video"],
)
def handle_edit_media_adding(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "photo"
        bot.send_message(
            message.chat.id, f"✅ Фото добавлено! Всего: {len(state['media_files'])}"
        )
        return

    if message.video:
        file_id = message.video.file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "video"
        bot.send_message(
            message.chat.id, f"✅ Видео добавлено! Всего: {len(state['media_files'])}"
        )
        return


# Обработчик медиа в режиме редактирования (используем ту же логику что и при создании)
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "edit"
    and admin_states.get(m.from_user.id, {}).get("step") == "media"
    and m.chat.type == "private",
    content_types=["text", "photo", "video"],
)
def handle_edit_media(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "photo"
        bot.send_message(
            message.chat.id, f"✅ Фото добавлено! Всего: {len(state['media_files'])}"
        )
        return

    if message.video:
        file_id = message.video.file_id
        state.setdefault("media_files", []).append(file_id)
        state["media_type"] = "video"
        bot.send_message(
            message.chat.id, f"✅ Видео добавлено! Всего: {len(state['media_files'])}"
        )
        return

    if message.text:
        txt = message.text.strip()
        if txt == "⏩ Пропустить медиа":
            # Сохраняем группу без изменений медиа
            state["step"] = "edit_choice"
            bot.send_message(
                message.chat.id,
                "✅ Медиа не изменены.",
                reply_markup=types.ReplyKeyboardRemove(),
            )
            # Показываем меню редактирования снова
            show_edit_menu(message.chat.id, state)
            return

        if txt == "✅ Завершить добавление медиа":
            # Сохраняем новые медиа
            media_files = state.get("media_files", [])
            media_type = state.get("media_type")

            if media_files:
                first_media = media_files[0]
                media_ids_str = ",".join(media_files)

                # Обновляем медиа в базе
                cursor.execute(
                    "UPDATE plans SET media_file_id=?, media_file_ids=?, media_type=? WHERE id=?",
                    (first_media, media_ids_str, media_type, state["plan_id"]),
                )

                # Очищаем старые медиа и добавляем новые
                cursor.execute(
                    "DELETE FROM plan_media WHERE plan_id=?", (state["plan_id"],)
                )
                for idx, fid in enumerate(media_files):
                    cursor.execute(
                        "INSERT INTO plan_media (plan_id, file_id, media_type, ord, added_ts) VALUES (?, ?, ?, ?, ?)",
                        (state["plan_id"], fid, media_type, idx, int(time.time())),
                    )

                conn.commit()

                cnt = len(media_files)
                if cnt == 1:
                    bot.send_message(
                        message.chat.id,
                        f"✅ Медиа обновлены! Использовано 1 превью.",
                        reply_markup=types.ReplyKeyboardRemove(),
                    )
                else:
                    bot.send_message(
                        message.chat.id,
                        f"✅ Медиа обновлены! Использовано первое из {cnt} медиа как превью.",
                        reply_markup=types.ReplyKeyboardRemove(),
                    )
            else:
                bot.send_message(
                    message.chat.id,
                    "✅ Медиа не изменены (оставлены предыдущие).",
                    reply_markup=types.ReplyKeyboardRemove(),
                )

            state["step"] = "edit_choice"
            # Показываем меню редактирования снова
            show_edit_menu(message.chat.id, state)
            return

        bot.send_message(
            message.chat.id,
            "❌ Отправляйте фото/видео или используйте кнопки '⏩ Пропустить медиа' / '✅ Завершить добавление медиа'.",
        )


def show_edit_menu(chat_id, state):
    """Показывает меню редактирования"""
    plan_id = state["plan_id"]

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "📝 Редактировать название", callback_data=f"edit_field:title:{plan_id}"
        ),
        types.InlineKeyboardButton(
            "💰 Редактировать цену", callback_data=f"edit_field:price:{plan_id}"
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            "📋 Редактировать описание",
            callback_data=f"edit_field:description:{plan_id}",
        ),
        types.InlineKeyboardButton(
            "👥 Изменить группу", callback_data=f"edit_field:group:{plan_id}"
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            "🖼️ Управление медиа", callback_data=f"edit_field:media:{plan_id}"
        ),
        types.InlineKeyboardButton(
            "✅ Завершить редактирование", callback_data=f"edit_finish:{plan_id}"
        ),
    )

    text = f"✏️ <b>Редактирование группы:</b> {state['current_title']}\n\nВыберите что хотите изменить:"

    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


# Обработчик выбора группы при редактировании
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("select_edit_group:")
)
def callback_select_edit_group(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    parts = call.data.split(":")
    group_id = int(parts[1])
    plan_id = int(parts[2])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    cursor.execute("UPDATE plans SET group_id=? WHERE id=?", (group_id, plan_id))
    state["current_group_id"] = group_id
    conn.commit()

    cursor.execute("SELECT title FROM managed_groups WHERE chat_id=?", (group_id,))
    group_title = cursor.fetchone()[0]

    bot.answer_callback_query(call.id, f"✅ Группа изменена: {group_title}")

    # Просто отправляем сообщение и показываем меню снова
    bot.send_message(call.message.chat.id, f"✅ Группа изменена на: {group_title}")
    show_edit_menu(call.message.chat.id, state)


# Обработчик завершения редактирования
@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("edit_finish:")
)
def callback_edit_finish(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "🚫 Доступ запрещен.")
        return

    plan_id = int(call.data.split(":")[1])
    uid = call.from_user.id

    state = admin_states.get(uid)
    if not state or state.get("mode") != "edit" or state.get("plan_id") != plan_id:
        bot.answer_callback_query(call.id, "❌ Сессия устарела.")
        return

    # Очищаем состояние
    admin_states.pop(uid, None)

    bot.answer_callback_query(call.id, "✅ Редактирование завершено!")
    bot.send_message(
        call.message.chat.id,
        "✅ Редактирование группы завершено!",
        reply_markup=main_menu(uid),
    )


# Обработчик ввода текстовых данных при редактировании
@bot.message_handler(
    func=lambda m: m.from_user
    and m.from_user.id in admin_states
    and admin_states.get(m.from_user.id, {}).get("mode") == "edit"
    and admin_states.get(m.from_user.id, {}).get("step", "").startswith("editing_")
    and m.chat.type == "private"
    and m.text
)
def handle_edit_text_input(message):
    uid = message.from_user.id
    state = admin_states.get(uid)

    if not state or state.get("chat_id") != message.chat.id:
        return

    step = state.get("step", "")
    field = step.replace("editing_", "")

    if field == "title":
        new_title = message.text.strip()
        cursor.execute(
            "UPDATE plans SET title=? WHERE id=?", (new_title, state["plan_id"])
        )
        state["current_title"] = new_title
        conn.commit()
        bot.send_message(message.chat.id, f"✅ Название обновлено: {new_title}")

    elif field == "price":
        cents = cents_from_str(message.text)
        if cents is None:
            bot.send_message(
                message.chat.id, "❌ Неправильный формат цены. Пример: 14.99"
            )
            return
        cursor.execute(
            "UPDATE plans SET price_cents=? WHERE id=?", (cents, state["plan_id"])
        )
        state["current_price"] = cents
        conn.commit()
        bot.send_message(
            message.chat.id, f"✅ Цена обновлена: {price_str_from_cents(cents)}"
        )

    elif field == "description":
        new_description = message.text.strip()
        cursor.execute(
            "UPDATE plans SET description=? WHERE id=?",
            (new_description, state["plan_id"]),
        )
        state["current_description"] = new_description
        conn.commit()
        bot.send_message(message.chat.id, f"✅ Описание обновлено")

    # Возвращаемся к меню редактирования
    state["step"] = "edit_choice"
    show_edit_menu(message.chat.id, state)


# ----------------- Manual registration command for groups -----------------
@bot.message_handler(commands=["register_group"])
def cmd_register_group(message):
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        bot.send_message(
            message.chat.id, "Эта команда должна быть вызвана в группе/супергруппе."
        )
        return
    try:
        member = bot.get_chat_member(chat.id, BOT_ID)
        if member.status not in ("administrator", "creator"):
            bot.send_message(
                chat.id,
                "Назначьте бота администратором, затем повторите /register_group.",
            )
            return
    except Exception:
        bot.send_message(
            chat.id, "Не могу проверить статус. Убедитесь, что бот добавлен."
        )
        return
    add_group_to_db(chat.id, chat.title or chat.username or str(chat.id), chat.type)
    bot.send_message(
        chat.id, "✅ Группа зарегистрирована — бот видит группу и сохранит её в базе."
    )
    for aid in ADMIN_IDS:
        try:
            bot.send_message(
                aid, f"✅ Группа зарегистрирована: {chat.title} (ID: {chat.id})"
            )
        except:
            pass


# ----------------- Graceful shutdown -----------------
def shutdown():
    try:
        logging.info("Stopping bot...")
        bot.stop_polling()
    except:
        pass


# ----------------- Run polling -----------------
if __name__ == "__main__":
    logging.info("Starting student control bot...")
    try:
        bot.infinity_polling(
            timeout=60,
            long_polling_timeout=60,
            allowed_updates=[
                "message",
                "edited_message",
                "callback_query",
                "my_chat_member",
                "chat_member",
                "inline_query",
                "pre_checkout_query",
                "shipping_query",
            ],
        )
    except KeyboardInterrupt:
        shutdown()
    except Exception:
        logging.exception("Bot crashed; shutting down")
