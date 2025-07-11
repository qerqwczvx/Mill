import telebot
from telebot import types
from math import floor
from datetime import datetime, timedelta
import time
import random
import re
import crypto_pay
import json
from requests.exceptions import RequestException
import requests
import sqlite3
import db as db_module
import threading
import schedule
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import get_db
import config
import logging
from db import add_user, get_db  
from telebot.apihelper import ApiTelegramException
import io
from telebot.handler_backends import State, StatesGroup
from threading import Lock
import uuid


bot = telebot.TeleBot(config.BOT_TOKEN)


treasury_lock = threading.Lock()
active_treasury_admins = {}



class Database:
    def get_db(self):
        return sqlite3.connect('database.db')

    def get_user_price(self, user_id):
        """Возвращает цену для пользователя (индивидуальную или глобальную)."""
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT CUSTOM_PRICE FROM users WHERE ID = ?', (user_id,))
            custom_price = cursor.fetchone()
            if custom_price and custom_price[0] is not None:
                return custom_price[0]
            cursor.execute('SELECT PRICE FROM settings')
            result = cursor.fetchone()
            return result[0] if result else 2.0

    def is_moderator(self, user_id):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
            return cursor.fetchone() is not None

    def update_balance(self, user_id, amount):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, user_id))
            conn.commit()

    def get_group_name(self, group_id):
        return db_module.get_group_name(group_id)

    def update_last_activity(self, user_id):
        with self.get_db() as conn:
            cursor = conn.cursor()
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
            result = cursor.fetchone()
            if not result:
                cursor.execute('INSERT OR IGNORE INTO users (ID, BALANCE, REG_DATE, IS_AFK, LAST_ACTIVITY) VALUES (?, ?, ?, ?, ?)',
                              (user_id, 0.0, current_time, 0, current_time))
            else:
                if result[0] == 1:
                    cursor.execute('UPDATE users SET IS_AFK = 0 WHERE ID = ?', (user_id,))
                    print(f"{user_id} выведен из режима АФК")
            cursor.execute('UPDATE users SET LAST_ACTIVITY = ? WHERE ID = ?', (current_time, user_id))
            conn.commit()
            print(f"[DEBUG] Обновлено время активности для пользователя {user_id}: {current_time}")

    def get_afk_status(self, user_id):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
            result = cursor.fetchone()
            return bool(result[0]) if result else False

db = Database()


def is_russian_number(phone_number):
    phone_number = phone_number.strip()
    phone_number = re.sub(r'[\s\-()]+', '', phone_number)
    if phone_number.startswith('7') or phone_number.startswith('8'):
        phone_number = '+7' + phone_number[1:]
    elif phone_number.startswith('9') and len(phone_number) == 10:
        phone_number = '+7' + phone_number
    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number
    pattern = r'^\+7\d{10}$'
    return phone_number if bool(re.match(pattern, phone_number)) else None

def check_balance_and_fix(user_id):
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] < 0:
            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (user_id,))
            conn.commit()

@bot.message_handler(commands=['help'])
def help_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    db.update_last_activity(user_id)  # Обновляем время активности

    # Проверяем статус пользователя
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, BLOCKED FROM requests WHERE ID = ?', (user_id,))
        request = cursor.fetchone()

        # Если пользователь заблокирован
        if request and request[1] == 1:
            bot.send_message(chat_id, "🚫 Вы заблокированы в боте. Обратитесь к поддержке: @{config.PAYOUT_MANAGER}", parse_mode='HTML')
            return

        # Если пользователь не одобрен
        if not request or request[0] != 'approved':
            bot.send_message(chat_id, "👋 Ваша заявка на вступление ещё не одобрена. Ожидайте подтверждения администратора.", parse_mode='HTML')
            return

    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    if is_admin:
        help_text = (
            "👑 <b>Справка для администратора</b>\n\n"
            "Вы имеете полный доступ к управлению ботом. Доступные команды и действия:\n\n"
            "⚙️ <b>Админ-панель</b> (кнопка в меню)\n"
            "   - Управление заявками на вступление\n"
            "   - Настройка АФК пользователей\n"
            "   - Изменение цен для пользователей\n"
            "   - Уменьшение баланса пользователей\n"
            "   - Отправка чеков (всем или отдельным пользователям)\n\n"
            "📱 <b>Работа с номерами</b>\n"
            "   - Команда в группе: <code>вц/пк1</code> (от 1 до 70) — взять номер в обработку\n"
            "   - Команда: <code>слет +79991234567</code> — пометить номер как слетевший\n\n"
            "💰 <b>Управление выплатами</b>\n"
            "   - Просмотр и обработка заявок на вывод\n"
            "   - Создание и отправка чеков через CryptoBot\n\n"
            "📊 <b>Статистика</b>\n"
            "   - Доступна в профиле: общее количество пользователей и номеров\n\n"
            "📞 <b>Поддержка</b>\n"
            f"   - Связь с менеджером: @{config.PAYOUT_MANAGER}"
        )
    elif is_moderator:
        help_text = (
            "🛡 <b>Справка для модератора</b>\n\n"
            "Вы можете обрабатывать номера в рабочих группах. Доступные команды и действия:\n\n"
            "📱 <b>Работа с номерами</b>\n"
            "   - Команда в группе: <code>пк/вц1</code> (от 1 до 70) — взять номер в обработку\n"
            "   - Команда: <code>слет +79991234567</code> — пометить номер как слетевший  \n"
            "   - Подтверждение или отклонение номеров через кнопки\n\n"
            "🔙 <b>Возврат в меню</b>\n"
            "   - Используйте кнопку ниже или команду /start\n\n"
            "📞 <b>Поддержка</b>\n"
            f"   - Связь с менеджером: @{config.PAYOUT_MANAGER}"
        )
    else:
        # Получаем текущие настройки
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
            result = cursor.fetchone()
            price, hold_time = result if result else (2.0, 5)

        help_text = (
            f"<b>📢 Справка для пользователя {config.SERVICE_NAME}</b>\n\n"
            f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
            "<b>💼 Как это работает?</b>\n"
            "• Вы сдаёте номер, мы выплачиваем вам деньги после проверки.\n"
            f"• Моментальные выплаты через CryptoBot после {hold_time} минут работы номера.\n\n"
            "<b>💰 Тарифы:</b>\n"
            f"▪️ <code>{price}$</code> за номер (холд {hold_time} минут)\n\n"
            "<b>📱 Доступные действия:</b>\n"
            "1. <b>Сдать номер</b> — через кнопку в меню\n"
            "2. <b>Удалить номер</b> — если хотите убрать свой номер\n"
            "3. <b>Изменить номер</b> — заменить один номер на другой\n"
            "4. <b>Мой профиль</b> — просмотр баланса, активных и успешных номеров\n"
            "5. <b>Вывести деньги</b> — запрос вывода средств\n"
            "6. <b>АФК-режим</b> — скрыть номера на время отсутствия\n\n"
            f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
            "✅ Прозрачные условия\n"
            "✅ Выгодные тарифы и быстрые выплаты\n"
            "✅ Поддержка 24/7\n\n"
            "<b>📞 Поддержка:</b>\n"
            f"Связь с менеджером: @{config.PAYOUT_MANAGER}\n\n"
            "<b>🔹 Начните зарабатывать прямо сейчас!</b>"
        )

    bot.send_message(chat_id, help_text, parse_mode='HTML', reply_markup=markup)
    
cooldowns = {}  # In-memory cooldown tracking

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.username
    add_user(user_id=user_id, username=username)
    print(f"[DEBUG] Username для user_id {user_id}: {username}")  # Отладочный вывод
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Проверяем текущий статус АФК
    was_afk = db_module.get_afk_status(user_id)
    db_module.update_last_activity(user_id)  # Обновляем время активности и сбрасываем АФК
    
    chat_type = bot.get_chat(message.chat.id).type
    is_group = chat_type in ["group", "supergroup"]
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BLOCKED FROM requests WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] == 1:
            bot.send_message(
                message.chat.id,
                "🚫 Вас заблокировали в боте!",
                disable_notification=True  # Отключаем уведомление
            )
            return
    
    is_moderator = db_module.is_moderator(user_id)
    is_admin = user_id in config.ADMINS_ID

    # Уведомление о выходе из АФК
    if was_afk:
        try:
            bot.send_message(
                message.chat.id,
                "🔔 Вы вышли из режима АФК. Ваши номера снова видны.",
                parse_mode='HTML',
                disable_notification=True  # Отключаем уведомление
            )
        except Exception as e:
            print(f"[ERROR] Не удалось отправить уведомление о выходе из АФК пользователю {user_id}: {e}")

    if is_group and is_moderator and not is_admin:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT GROUP_ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
            group_id = cursor.fetchone()
            group_name = db_module.get_group_name(group_id[0]) if group_id else "Неизвестная группа"
        
        moderator_text = (
            f"Здравствуйте 🤝\n"
            f"Вы назначены модератором в группе: <b>{group_name}</b>\n\n"
            "Вот что вы можете:\n\n"
            "1. Брать номера в обработку и работать с ними\n\n"
            "2. Вы можете назначить номер слетевшим, если с ним что-то не так\n"
            "Не злоупотребляйте этим в юмористических целях!\n\n"
            "<b>Доступные вам команды в чате:</b>\n"
            "1. <b>Запросить номер</b>\n"
            "Запрос номера производится вводом таких символов как «вц/пк1» и отправлением его в рабочий чат\n"
            "Вводите номер, который вам присвоили или который приписан вашему ПК\n"
            "<b>Важно!</b> Мы не рассчитываем на ПК, которым присвоен номер больше 70\n\n"
            "2. Если с номером что-то не так, вы в течение 5 минут (это время выделенное на рассмотрение аккаунта) можете отметить его «слетевшим»\n"
            "Чтобы указать номер слетевшим, вам необходимо написать такую команду: «слет и номер с которым вы работали»\n"
            "Пример: <code>слет +79991112345</code>\n"
            "После этого номер отметится слетевшим, и выйдет сообщение о том, что номер слетел"
        )
        bot.send_message(
            message.chat.id,
            moderator_text,
            parse_mode='HTML',
            disable_notification=True  # Отключаем уведомление
        )
        return
    
    if user_id in config.ADMINS_ID:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO requests (ID, LAST_REQUEST, STATUS, BLOCKED, CAN_SUBMIT_NUMBERS) VALUES (?, ?, ?, ?, ?)',
                          (user_id, current_date, 'approved', 0, 1))
            conn.commit()
        if is_group:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
                types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
            )
            markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))
            is_afk = db_module.get_afk_status(user_id)
            afk_button_text = "🟢 Включить АФК" if not is_afk else "🔴 Выключить АФК"
            markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
            bot.send_message(
                message.chat.id,
                f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                f"<b>💼 Как это работает?</b>\n"
                f"• <i>Вы сдаете номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                f"• <i>Моментальные выплаты</i> – <b>после стоп ворка.</b>\n\n"
                f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
                f"✅ <i>Прозрачные условия сотрудничества</i>\n"
                f"✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                f"✅ <i>Оперативная поддержка 24/7</i>\n\n"
                f"<b>💰 Тарифы на сдачу номеров:</b>\n"
                f"▪️ 6$ за номер (холд 1-6$, 2-12$)\n\n"
                "<b>🔹 Начните зарабатывать прямо сейчас!</b>",
                reply_markup=markup,
                parse_mode='HTML',
                disable_notification=True  # Отключаем уведомление
            )
        else:
            # Send a temporary message to get message_id
            temp_message = bot.send_message(
                chat_id,
                "Загрузка меню...",
                parse_mode='HTML',
                disable_notification=True  # Отключаем уведомление
            )
            show_main_menu(chat_id, temp_message.message_id, user_id)
        return
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT LAST_REQUEST, STATUS FROM requests WHERE ID = ?', (user_id,))
        request = cursor.fetchone()
        if request and request[1] == 'approved':
            if is_group:
                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
                    types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
                )
                is_afk = db_module.get_afk_status(user_id)
                afk_button_text = "🟢 Включить АФК" if not is_afk else "🔴 Выключить АФК"
                markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
                bot.send_message(
                    message.chat.id,
                f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                f"<b>💼 Как это работает?</b>\n"
                f"• <i>Вы сдаете номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                f"• <i>Моментальные выплаты</i> – <b>после стоп ворка.</b>\n\n"
                f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
                f"✅ <i>Прозрачные условия сотрудничества</i>\n"
                f"✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                f"✅ <i>Оперативная поддержка 24/7</i>\n\n"
                f"<b>💰 Тарифы на сдачу номеров:</b>\n"
                f"▪️ 6$ за номер (холд 1-6$, 2-12$)\n\n"
                "<b>🔹 Начните зарабатывать прямо сейчас!</b>",
                    reply_markup=markup,
                    parse_mode='HTML',
                    disable_notification=True  # Отключаем уведомление
                )
            else:
                # Send a temporary message to get message_id
                temp_message = bot.send_message(
                    chat_id,
                    "Загрузка меню...",
                    parse_mode='HTML',
                    disable_notification=True  # Отключаем уведомление
                )
                show_main_menu(chat_id, temp_message.message_id, user_id)
            return
        if request:
            last_request_time = datetime.strptime(request[0], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - last_request_time < timedelta(minutes=15):
                time_left = 15 - ((datetime.now() - last_request_time).seconds // 60)
                bot.send_message(
                    message.chat.id, 
                    f"⏳ Ожидайте подтверждения. Вы сможете отправить новый запрос через {time_left} минут.",
                    disable_notification=True  # Отключаем уведомление
                )
                return
        cursor.execute('INSERT OR REPLACE INTO requests (ID, LAST_REQUEST, STATUS, BLOCKED, CAN_SUBMIT_NUMBERS) VALUES (?, ?, ?, ?, ?)',
                      (user_id, current_date, 'pending', 0, 1))
        conn.commit()
        bot.send_message(
            message.chat.id, 
            "👋 Здравствуйте! Ожидайте, пока вас впустит администратор.",
            disable_notification=True  # Отключаем уведомление
        )
        # Notify admins with approval buttons for non-admin/moderator pending users
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Dynamically create placeholders for config.ADMINS_ID
            admin_ids = config.ADMINS_ID
            admin_placeholders = ','.join('?' for _ in admin_ids)
            query = f'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM requests WHERE ID IN ({admin_placeholders}) OR ID IN (SELECT ID FROM personal WHERE TYPE = ?)) AND ID != ?'
            params = ('pending', *admin_ids, 'moderator', user_id)
            cursor.execute(query, params)
            pending_users = cursor.fetchall()
    if pending_users:
        admin_text = "🔔 <b>Заявки на вступления</b>\n\n"
        markup = types.InlineKeyboardMarkup()
        
        for pending_user_id, reg_date in pending_users:
            try:
                # Fetch user information using bot.get_chat_member
                user = bot.get_chat_member(pending_user_id, pending_user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
                # Create clickable username link
                username_link = f"<a href=\"tg://user?id={pending_user_id}\">{username}</a>" if user.username else "Нет username"
            except Exception as e:
                print(f"[ERROR] Не удалось получить username для user_id {pending_user_id}: {e}")
                username_link = "Неизвестный пользователь"

            # Add user details to admin_text
            admin_text += (
                f"👤 Пользователь ID: <a href=\"https://t.me/@id{pending_user_id}\">{pending_user_id}</a> (Зарегистрирован: {reg_date})\n"
                f"👤 Username: {username_link}\n\n"
            )

            # Add approve/reject buttons for each user
            approve_button = types.InlineKeyboardButton(f"✅ Одобрить {pending_user_id}", callback_data=f"approve_user_{pending_user_id}")
            reject_button = types.InlineKeyboardButton(f"❌ Отклонить {pending_user_id}", callback_data=f"reject_user_{pending_user_id}")
            markup.row(approve_button, reject_button)

        try:
            for admin_id in config.ADMINS_ID:
                bot.send_message(
                    admin_id,
                    admin_text,
                    parse_mode='HTML',
                    reply_markup=markup,
                    disable_notification=True  # Отключаем уведомление
                )
        except Exception as e:
            print(f"[ERROR] Не удалось отправить уведомление админам: {e}")

def show_main_menu(chat_id, message_id, user_id):
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK, AFK_LOCKED FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        if not result:
            db_module.add_user(user_id)
            is_afk = False
            afk_locked = False
        else:
            is_afk, afk_locked = result

    # Check if the user is a moderator
    is_moderator = db_module.is_moderator(user_id)

    if is_moderator:
        # Получаем ID группы модератора
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT GROUP_ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
            group_id = cursor.fetchone()
            group_name = db_module.get_group_name(group_id[0]) if group_id else "Неизвестная группа"

        moderator_text = (
            f"Здравствуйте 🤝\n"
            f"Вы назначены модератором в группе: <b>{group_name}</b>\n\n"
            "Вот что вы можете:\n"
            "1. Брать номера в обработку и работать с ними\n"
            "2. Вы можете назначить номер слетевшим, если с ним что-то не так\n"
            "   Не злоупотребляйте этим в юмористических целях!\n\n"
            "Доступные вам команды в чате:\n"
            "1. Запросить номер\n"
            "   Запрос номера производится вводом таких символов как «вц/пк1» и отправлением его в рабочий чат\n"
            "   Вводите номер, который вам присвоили или который приписан вашему ПК\n"
            "   Важно! Мы не рассчитываем на ПК, которым присвоен номер больше 70\n\n"
            "2. Если с номером что-то не так, вы в течение 5 минут (это время выделенное на рассмотрение аккаунта) можете отметить его «слетевшим»\n"
            "   Чтобы указать номер слетевшим, вам необходимо написать такую команду: «слет и номер с которым вы работали»\n"
            "   Пример: <code>слет +79991112345</code>\n"
            "   После этого номер отметится слетевшим, и выйдет сообщение о том, что номер слетел"
        )
        try:
            bot.edit_message_text(
                moderator_text,
                chat_id,
                message_id,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                print(f"[DEBUG] Сообщение не изменено, пропускаем редактирование для chat_id={chat_id}, message_id={message_id}")
            else:
                print(f"[ERROR] Ошибка при редактировании сообщения: {e}")
                bot.send_message(
                    chat_id,
                    moderator_text,
                    parse_mode='HTML',
                    disable_notification=True  # Отключаем уведомление
                )
    else:
        price = db_module.get_user_price(user_id)
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5

        welcome_text = (
                f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                f"<b>💼 Как это работает?</b>\n"
                f"• <i>Вы сдаете номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                f"• <i>Моментальные выплаты</i> – <b>после стоп ворка.</b>\n\n"
                f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
                f"✅ <i>Прозрачные условия сотрудничества</i>\n"
                f"✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                f"✅ <i>Оперативная поддержка 24/7</i>\n\n"
                f"<b>💰 Тарифы на сдачу номеров:</b>\n"
                f"▪️ 6$ за номер (холд 1-6$, 2-12$)\n\n"
                "<b>🔹 Начните зарабатывать прямо сейчас!</b>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
            types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
        )

        is_admin = user_id in config.ADMINS_ID
        if not is_admin and not is_moderator:
            markup.add(types.InlineKeyboardButton("🗑️ Удалить номер", callback_data="delete_number"))
            markup.add(types.InlineKeyboardButton("✏️ Изменить номер", callback_data="change_number"))

        if is_admin:
            markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))

        afk_button_text = "🔴 Выключить АФК" if is_afk and not afk_locked else "🟢 Включить АФК"
        if afk_locked:
            markup.add(types.InlineKeyboardButton(f"🔒 АФК заблокирован (админ)", callback_data="afk_locked_info"))
        else:
            markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))

        try:
            bot.edit_message_text(
                welcome_text,
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                print(f"[DEBUG] Сообщение не изменено, пропускаем редактирование для chat_id={chat_id}, message_id={message_id}")
            else:
                print(f"[ERROR] Ошибка при редактировании сообщения: {e}")
                bot.send_message(
                    chat_id,
                    welcome_text,
                    parse_mode='HTML',
                    reply_markup=markup,
                    disable_notification=True  # Отключаем уведомление
                )

        if is_afk and not afk_locked:
            bot.send_message(
                chat_id,
                "🔔 Ваш АФК отключён. Ваши номера снова видны.",
                parse_mode='HTML',
                disable_notification=True  # Отключаем уведомление
            )
        elif is_afk and afk_locked:
            bot.send_message(
                chat_id,
                "🔔 Вы в режиме АФК, заблокированном администратором. Номера скрыты.",
                parse_mode='HTML',
                disable_notification=True  # Отключаем уведомление
            )

@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    user_id = call.from_user.id
    broadcast_state.pop(user_id, None)
    show_main_menu(chat_id, message_id, user_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_user_"))
def approve_user_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    user_id = int(call.data.split("_")[2])
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET STATUS = ? WHERE ID = ?', ('approved', user_id))
        conn.commit()
        
        try:
            bot.send_message(user_id, "✅ Вас впустили в бота! Напишите /start")
            text = f"✅ Пользователь {user_id} одобрен"
        except:
            text = f"✅ Пользователь {user_id} одобрен, но уведомление не доставлено"
        
        # Добавляем кнопку "Вернуться в заявки на вступление"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📝 Вернуться в заявки на вступление", callback_data="pending_requests"))
        
        bot.edit_message_text(text,
                             call.message.chat.id,
                             call.message.message_id,
                             parse_mode='HTML',
                             reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_user_"))
def reject_user_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    user_id = int(call.data.split("_")[2])
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('UPDATE requests SET STATUS = ?, LAST_REQUEST = ? WHERE ID = ?', ('rejected', current_date, user_id))
        conn.commit()
        
        try:
            bot.send_message(user_id, "❌ Вам отказано в доступе. Вы сможете отправить новый запрос через 15 минут.")
            text = f"❌ Пользователь {user_id} отклонён"
        except:
            text = f"❌ Пользователь {user_id} отклонён, но уведомление не доставлено"
        
        # Добавляем кнопку "Вернуться в заявки на вступление"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📝 Вернуться в заявки на вступление", callback_data="pending_requests"))
        
        bot.edit_message_text(text,
                             call.message.chat.id,
                             call.message.message_id,
                             parse_mode='HTML',
                             reply_markup=markup)





#ЧТО БЫ ПОЛЬЗОВАТЕЛЬ УДАЛИЛ НОМЕР САМ СВОЙ
@bot.callback_query_handler(func=lambda call: call.data == "delete_number")
def handle_delete_number(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    # Проверяем, что пользователь не администратор и не модератор
    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)
    if is_admin or is_moderator:
        bot.answer_callback_query(call.id, "❌ Эта функция доступна только обычным пользователям!")
        return

    # Запрашиваем номер(а) для удаления
    msg = bot.send_message(
        chat_id,
        "📞 Впишите номера для удаления по одному в строке, например:\n+79891234567\n79091234567\n9021234567:",
        reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")),
        disable_notification=True  # Отключаем уведомление
    )
    bot.register_next_step_handler(msg, process_delete_number, message_id)

def process_delete_number(message, original_message_id):
    chat_id = message.chat.id
    user_id = message.from_user.id
    input_text = message.text.strip()

    # Проверка на нажатие кнопки "Назад"
    if message.text == "/start" or (message.reply_markup and any(btn.callback_data == "back_to_start" for btn in message.reply_markup.inline_keyboard[0])):
        start(message)
        return

    if not input_text or input_text.startswith('/'):
        bot.send_message(
            chat_id,
            "❌ Ввод не может быть пустым или начинаться с команды. Попробуйте снова или нажмите 'Назад'.",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)
        return

    # Разбиваем ввод на список номеров
    numbers = [num.strip() for num in input_text.split('\n') if num.strip()]
    results = []
    deleted_count = 0
    invalid_count = 0
    not_found_count = 0

    try:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            for number in numbers:
                # Нормализуем номер с помощью is_russian_number
                normalized_number = is_russian_number(number)
                if not normalized_number:
                    results.append(f"❌ Номер {number}: некорректный формат. Используйте российский номер, например, +79991234567, 79091234567 или 9021234567.")
                    invalid_count += 1
                    continue

                # Проверяем, принадлежит ли номер пользователю
                cursor.execute('SELECT * FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, normalized_number))
                number_record = cursor.fetchone()

                if not number_record:
                    results.append(f"❌ Номер {normalized_number}: не найден или не принадлежит вам.")
                    not_found_count += 1
                    continue

                # Удаляем номер
                cursor.execute('DELETE FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, normalized_number))
                conn.commit()
                deleted_count += 1
                results.append(f"✅ Номер {normalized_number}: успешно удалён.")
                print(f"[DEBUG] Номер {normalized_number} удалён для пользователя {user_id}")

        # Формируем итоговое сообщение
        summary = "\n".join(results)
        summary += f"\n\n📊 Итог: удалено {deleted_count} номеров, некорректных {invalid_count}, не найдено {not_found_count}."
        bot.send_message(
            chat_id,
            summary,
            disable_notification=True  # Отключаем уведомление
        )

        # Возвращаем пользователя в главное меню
        start(message)

    except Exception as e:
        print(f"[ERROR] Ошибка при удалении номеров для пользователя {user_id}: {e}")
        bot.send_message(
            chat_id,
            "❌ Произошла ошибка при удалении номеров. Попробуйте позже.",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)

@bot.callback_query_handler(func=lambda call: call.data == "change_number")
def handle_change_number(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    # Проверяем, что пользователь не администратор и не модератор
    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)
    if is_admin or is_moderator:
        bot.answer_callback_query(call.id, "❌ Эта функция доступна только обычным пользователям!")
        return

    # Запрашиваем старый номер с кнопкой "Назад"
    msg = bot.send_message(
        chat_id,
        "📞 Впишите номер, который хотите изменить:",
        reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")),
        disable_notification=True  # Отключаем уведомление
    )
    bot.register_next_step_handler(msg, process_old_number, message_id)

def process_old_number(message, original_message_id):
    chat_id = message.chat.id
    user_id = message.from_user.id
    old_number = message.text.strip()

    # Проверка на нажатие кнопки "Назад" или команду
    if message.text == "/start" or (message.reply_markup and any(btn.callback_data == "back_to_start" for btn in message.reply_markup.inline_keyboard[0])):
        start(message)
        return

    if not old_number or old_number.startswith('/'):
        bot.send_message(
            chat_id,
            "❌ Номер не может быть пустым или начинаться с команды. Попробуйте снова или нажмите 'Назад'.",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)
        return

    # Нормализуем номер с помощью is_russian_number
    normalized_old_number = is_russian_number(old_number)
    if not normalized_old_number:
        bot.send_message(
            chat_id,
            "❌ Номер должен быть российским (например, +79991234567, 79091234567 или 9021234567).",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)
        return

    try:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Проверяем, существует ли номер у данного пользователя
            cursor.execute('SELECT * FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, normalized_old_number))
            number_record = cursor.fetchone()

            if not number_record:
                bot.send_message(
                    chat_id,
                    f"❌ Номер {normalized_old_number} не найден среди ваших номеров или он вам не принадлежит.",
                    disable_notification=True  # Отключаем уведомление
                )
                start(message)
                return

            # Запрашиваем новый номер с кнопкой "Назад"
            msg = bot.send_message(
                chat_id,
                f"📞 Введите новый номер для замены {normalized_old_number}:",
                reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")),
                disable_notification=True  # Отключаем уведомление
            )
            bot.register_next_step_handler(msg, process_new_number, original_message_id, normalized_old_number)

    except Exception as e:
        print(f"[ERROR] Ошибка при проверке номера {normalized_old_number} для пользователя {user_id}: {e}")
        bot.send_message(
            chat_id,
            "❌ Произошла ошибка. Попробуйте позже.",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)

def process_new_number(message, original_message_id, old_number):
    chat_id = message.chat.id
    user_id = message.from_user.id
    new_number = message.text.strip()

    # Проверка на нажатие кнопки "Назад" или команду
    if message.text == "/start" or (message.reply_markup and any(btn.callback_data == "back_to_start" for btn in message.reply_markup.inline_keyboard[0])):
        start(message)
        return

    if not new_number or new_number.startswith('/'):
        bot.send_message(
            chat_id,
            "❌ Новый номер не может быть пустым или начинаться с команды. Попробуйте снова или нажмите 'Назад'.",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)
        return

    # Нормализуем новый номер с помощью is_russian_number
    normalized_new_number = is_russian_number(new_number)
    if not normalized_new_number:
        bot.send_message(
            chat_id,
            "❌ Новый номер должен быть российским (например, +79991234567, 79091234567 или 9021234567).",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)
        return

    try:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Проверяем, не существует ли новый номер у другого пользователя
            cursor.execute('SELECT * FROM numbers WHERE NUMBER = ? AND ID_OWNER != ?', (normalized_new_number, user_id))
            existing_record = cursor.fetchone()
            if existing_record:
                bot.send_message(
                    chat_id,
                    f"❌ Номер {normalized_new_number} уже используется другим пользователем.",
                    disable_notification=True  # Отключаем уведомление
                )
                start(message)
                return

            # Обновляем номер в базе данных
            cursor.execute('UPDATE numbers SET NUMBER = ? WHERE ID_OWNER = ? AND NUMBER = ?', (normalized_new_number, user_id, old_number))
            conn.commit()
            print(f"[DEBUG] Номер изменён с {old_number} на {normalized_new_number} для пользователя {user_id}")

            # Отправляем сообщение об успешном изменении
            bot.send_message(
                chat_id,
                f"✅ Номер изменён с {old_number} на {normalized_new_number} успешно!",
                disable_notification=True  # Отключаем уведомление
            )

            # Возвращаем пользователя в главное меню
            start(message)

    except Exception as e:
        print(f"[ERROR] Ошибка при изменении номера {old_number} на {normalized_new_number} для пользователя {user_id}: {e}")
        bot.send_message(
            chat_id,
            "❌ Произошла ошибка при изменении номера. Попробуйте позже.",
            disable_notification=True  # Отключаем уведомление
        )
        start(message)
        
#===========================================================================
#======================ПРОФИЛЬ=====================ПРОФИЛЬ==================
#===========================================================================

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def show_profile(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    check_balance_and_fix(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)
        
        if user:
            cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND SHUTDOWN_DATE = "0"', (user_id,))
            active_numbers = cursor.fetchone()[0]
            
            # Подсчет успешных номеров по категориям
            cursor.execute('''
                SELECT 
                    SUM(CASE WHEN STATUS = "отстоял 1/2" THEN 1 ELSE 0 END) as half,
                    SUM(CASE WHEN STATUS = "отстоял 2/2" THEN 1 ELSE 0 END) as full,
                    SUM(CASE WHEN STATUS LIKE "отстоял 2/2%" AND HOLDS_COUNT > 2 THEN 1 ELSE 0 END) as plus
                FROM numbers 
                WHERE ID_OWNER = ? AND STATUS LIKE "отстоял%"
            ''', (user_id,))
            result = cursor.fetchone()
            half, full, plus = result if result else (0, 0, 0)
            
            roles = []
            if user_id in config.ADMINS_ID:
                roles.append("👑 Администратор")
            if db.is_moderator(user_id):
                roles.append("🛡 Модератор")
            if not roles:
                roles.append("👤 Пользователь")
            
            # Формирование строки успешных номеров
            successful_text = "✅ Успешных номеров:\n"
            successful_text += f"1. 1/2: {half}\n"
            successful_text += f"2. 2/2: {full}\n"
            successful_text += f"3. 2/2+: {plus}\n"
            
            # Получаем количество предупреждений (индекс 9 для WARNINGS)
            warnings = user[9] if len(user) > 9 and user[9] is not None else 0

            profile_text = (f"👤 <b>Ваш профиль:</b>\n\n"
                          f"🆔ID ссылкой: <code>https://t.me/@id{user_id}</code>\n"
                          f"🆔 ID: <code>{user[0]}</code>\n"
                          f"💰 Баланс: {user[1]} $\n"
                          f"📱 Активных номеров: {active_numbers}\n"
                          f"{successful_text}"
                          f"🎭 Роль: {' | '.join(roles)}\n"
                          f"⚠️ Предупреждений: {warnings}/6\n"
                          f"📅 Дата регистрации: {user[2]}\n"  # REG_DATE - 3-й столбец
                          f"💵 Текущая ставка: {price}$ за номер\n"
                          f"⏱ Время холда: {hold_time} минут")

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("💳 Вывести", callback_data="withdraw"),
                types.InlineKeyboardButton("📱 Мои номера", callback_data="my_numbers")
            )
            
            if user_id in config.ADMINS_ID:
                cursor.execute('SELECT COUNT(*) FROM users')
                total_users = cursor.fetchone()[0]
                cursor.execute('SELECT COUNT(*) FROM numbers WHERE SHUTDOWN_DATE = "0"')
                active_total = cursor.fetchone()[0]
                cursor.execute('SELECT COUNT(*) FROM numbers')
                total_numbers = cursor.fetchone()[0]
                
                profile_text += (f"\n\n📊 <b>Статистика бота:</b>\n"
                               f"👥 Всего пользователей: {total_users}\n"
                               f"📱 Активных номеров: {active_total}\n"
                               f"📊 Всего номеров: {total_numbers}")
            
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
            
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.send_message(call.message.chat.id, profile_text, reply_markup=markup, parse_mode='HTML')          

@bot.callback_query_handler(func=lambda call: call.data == "withdraw")
def start_withdrawal_request(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        balance = cursor.fetchone()[0]
        
        if balance > 0:
            msg = bot.edit_message_text(f"💰 Ваш баланс: {balance}$\n💳 Введите сумму для вывода или нажмите 'Да' для вывода всего баланса:",
                                      call.message.chat.id,
                                      call.message.message_id)
            bot.register_next_step_handler(msg, handle_withdrawal_request, balance)
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("👤 Связаться с менеджером", url=f"https://t.me/{config.PAYOUT_MANAGER}"))
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.edit_message_text(f"❌ На вашем балансе недостаточно средств для вывода.\n\n"
                               f"Если вы считаете, что произошла ошибка или у вас есть вопросы по выводу, "
                               f"свяжитесь с ответственным за выплаты: @{config.PAYOUT_MANAGER}",
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup)


def handle_withdrawal_request(message, amount):
    user_id = message.from_user.id
    chat_id = message.chat.id  # Используется для get_chat_member

    # Получаем информацию о пользователе для username
    try:
        user_info = bot.get_chat_member(chat_id, user_id).user
        username = f"@{user_info.username}" if user_info.username else "Нет username"
        username_link = f"<a href=\"tg://user?id={user_id}\">{username}</a>" if user_info.username else "Нет username"
    except Exception as e:
        print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
        username_link = "Неизвестный пользователь"

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()

        if not user or user[0] <= 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ У вас нет средств на балансе для вывода.", reply_markup=markup)
            return
        withdrawal_amount = user[0]
        
        try:
            if message.text != "Да" and message.text != "да":
                try:
                    requested_amount = float(message.text)
                    if requested_amount <= 0:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.send_message(message.chat.id, "❖ Введите положительное число.", reply_markup=markup)
                        return
                        
                    if requested_amount > withdrawal_amount:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.send_message(message.chat.id, 
                                      f"❌ Запрошенная сумма ({requested_amount}$) превышает ваш баланс ({withdrawal_amount}$).", 
                                      reply_markup=markup)
                        return
                        
                    withdrawal_amount = requested_amount
                except ValueError:
                    pass
            
            processing_message = bot.send_message(message.chat.id, 
                                        f"⏳ <b>Обработка запроса на вывод {withdrawal_amount}$...</b>\n\n"
                                        f"Пожалуйста, подождите, мы формируем ваш чек.",
                                        parse_mode='HTML')
            
            # Получаем актуальный баланс казны из API CryptoBot
            treasury_balance = db_module.get_treasury_balance()
            logging.info(f"[DEBUG] Treasury balance: {treasury_balance}, Withdrawal amount: {withdrawal_amount}")
            
            if withdrawal_amount > treasury_balance:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.edit_message_text(
                    f"❌ <b>В данный момент вывод недоступен</b>\n\n"
                    f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                admin_message = (
                    f"⚠️ <b>Попытка вывода при недостаточных средствах</b>\n\n"
                    f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                    f"👤 Username: {username_link}\n"
                    f"💵 Запрошенная сумма: {withdrawal_amount}$\n"
                    f"💰 Баланс казны: {treasury_balance}$\n\n"
                    f"⛔️ Вывод был заблокирован из-за нехватки средств в казне."
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, parse_mode='HTML')
                    except:
                        continue
                return
            
            auto_input_status = db_module.get_auto_input_status()
            
            if not auto_input_status:
                cursor.execute('INSERT INTO withdraws (ID, AMOUNT, DATE, STATUS) VALUES (?, ?, ?, ?)', 
                             (user_id, withdrawal_amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending"))
                conn.commit()
                new_balance = user[0] - withdrawal_amount
                cursor.execute('UPDATE users SET BALANCE = ? WHERE ID = ?', (new_balance, user_id))
                conn.commit()
                # Вычисляем новый баланс казны для других целей (например, логирования)
                treasury_new_balance = treasury_balance - withdrawal_amount
                # Обновляем базу, если требуется
                db_module.update_treasury_balance(-withdrawal_amount)
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                bot.edit_message_text(
                    f"✅ <b>Запрос на вывод средств принят!</b>\n\n"
                    f"Сумма: <code>{withdrawal_amount}$</code>\n"
                    f"Новой баланс: <code>{new_balance}$</code>\n\n"
                    f"⚠️ Авто-вывод отключен. Средства будут выведены вручную администратором.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                admin_message = (
                    f"💰 <b>Новая заявка на выплату</b>\n\n"
                    f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                    f"👤 Username: {username_link}\n"
                    f"💵 Сумма: {withdrawal_amount}$\n"
                    f"💰 Баланс казны: {treasury_balance}$"  # Используем старый баланс
                )
                admin_markup = types.InlineKeyboardMarkup()
                admin_markup.add(
                    types.InlineKeyboardButton("✅ Отправить чек", callback_data=f"send_check_{user_id}_{withdrawal_amount}"),
                    types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_withdraw_{user_id}_{withdrawal_amount}")
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, reply_markup=admin_markup, parse_mode='HTML')
                    except:
                        continue
                return
            
            try:
                crypto_api = crypto_pay.CryptoPay()
                cheque_result = crypto_api.create_check(
                    amount=withdrawal_amount,
                    asset="USDT",
                    description=f"Выплата для пользователя {user_id}"
                )
                
                if cheque_result.get("ok", False):
                    cheque = cheque_result.get("result", {})
                    cheque_link = cheque.get("bot_check_url", "")
                    
                    if cheque_link:
                        new_balance = user[0] - withdrawal_amount
                        cursor.execute('UPDATE users SET BALANCE = ? WHERE ID = ?', (new_balance, user_id))
                        conn.commit()
                        # Вычисляем новый баланс казны для сообщения
                        treasury_new_balance = treasury_balance - withdrawal_amount
                        # Обновляем базу, если требуется
                        db_module.update_treasury_balance(-withdrawal_amount)
                        db_module.log_treasury_operation("Автоматический вывод", withdrawal_amount, treasury_new_balance)
                        
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
                        markup.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"✅ <b>Ваш вывод средств обработан!</b>\n\n"
                            f"Сумма: <code>{withdrawal_amount}$</code>\n"
                            f"Новый баланс: <code>{new_balance}$</code>\n\n"
                            f"Нажмите на кнопку ниже, чтобы активировать чек:",
                            message.chat.id, 
                            processing_message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        
                        log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] | Автоматический вывод | Пользователь {user_id} | Сумма {withdrawal_amount}$"
                        with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                            log_file.write(log_entry + "\n")
                        
                        admin_message = (
                            f"💸 <b>Автоматический вывод выполнен</b>\n\n"
                            f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                            f"👤 Username: {username_link}\n"
                            f"💵 Сумма: {withdrawal_amount}$\n"
                            f"💰 Баланс казны: {treasury_new_balance}$\n\n"
                            f"🔗 Чек: {cheque_link}"
                        )
                        for admin_id in config.ADMINS_ID:
                            try:
                                bot.send_message(admin_id, admin_message, parse_mode='HTML')
                            except:
                                continue
                    else:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.edit_message_text(
                            f"❌ <b>Не удалось создать чек для вывода</b>\n\n"
                            f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                            message.chat.id, 
                            processing_message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                else:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    bot.edit_message_text(
                        f"❌ <b>Не удалось создать чек для вывода</b>\n\n"
                        f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                        message.chat.id, 
                        processing_message.message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
            except Exception as e:
                print(f"[ERROR] Ошибка при создании чека для user_id {user_id}: {e}")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.edit_message_text(
                    f"❌ <b>Произошла ошибка при обработке вывода</b>\n\n"
                    f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                admin_message = (
                    f"⚠️ <b>Ошибка при автоматическом выводе</b>\n\n"
                    f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                    f"👤 Username: {username_link}\n"
                    f"💵 Сумма: {withdrawal_amount}$\n"
                    f"❌ Ошибка: {str(e)}"
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, parse_mode='HTML')
                    except:
                        continue
        except Exception as e:
            print(f"[ERROR] Общая ошибка в handle_withdrawal_request для user_id {user_id}: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, 
                           f"❌ Произошла ошибка при обработке запроса. Пожалуйста, попробуйте позже.", 
                           reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ["check_afk_status"])
def check_and_set_afk(call):
    try:
        user_id = call.from_user.id
        logging.debug(f"[DEBUG] Проверка статуса AFK для пользователя {user_id}. Начало выполнения.")

        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT WARNINGS, AFK_LOCKED FROM users WHERE ID = ?', (user_id,))
            user_data = cursor.fetchone()
            if not user_data:
                logging.error(f"[ERROR] Пользователь {user_id} не найден в базе данных")
                bot.answer_callback_query(call.id, "❌ Ошибка: ваш профиль не найден.")
                return

            warnings, afk_locked = user_data
            logging.debug(f"[DEBUG] Пользователь {user_id}: WARNINGS={warnings}, AFK_LOCKED={afk_locked}")

            if warnings >= 6 and afk_locked == 0:  # Изменено с == 6 на >= 6
                # Устанавливаем AFK навсегда
                cursor.execute('UPDATE users SET AFK_LOCKED = 1 WHERE ID = ?', (user_id,))
                conn.commit()
                logging.info(f"[INFO] Пользователь {user_id} переведён в AFK навсегда из-за {warnings} предупреждений")

                # Уведомляем пользователя
                message = (
                    f"⚠️ У вас {warnings} предупреждений! ❌\n"
                    f"Из-за этого вам выдан режим AFK навсегда (до снятия администратором)."
                )
                markup = telebot.types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.send_message(user_id, message, reply_markup=markup, parse_mode='HTML')

            elif afk_locked == 1:
                bot.answer_callback_query(call.id, "❌ Вы уже в AFK. Обратитесь к администратору для снятия режима.")
                return

            else:
                logging.debug(f"[DEBUG] Пользователь {user_id} не достиг 6 предупреждений или уже в AFK. Текущее количество: {warnings}")

            bot.answer_callback_query(call.id, "✅ Статус проверен.")

    except Exception as e:
        logging.error(f"Ошибка при проверке статуса AFK для {user_id}: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при проверке статуса.")

def check_all_users_for_afk():
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, WARNINGS, AFK_LOCKED FROM users')
        users = cursor.fetchall()
        for user_id, warnings, afk_locked in users:
            if warnings >= 6 and afk_locked == 0:
                cursor.execute('UPDATE users SET AFK_LOCKED = 1 WHERE ID = ?', (user_id,))
                conn.commit()
                logging.info(f"[INFO] Пользователь {user_id} переведён в AFK навсегда из-за {warnings} предупреждений")
                bot.send_message(user_id, f"⚠️ У вас {warnings} предупреждений! ❌\nИз-за этого вам выдан режим AFK навсегда (до снятия администратором).", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith("send_check_"))
def send_check_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return

    try:
        parts = call.data.split("_")
        user_id = int(parts[2])
        amount = float(parts[3])

        # Получаем информацию о пользователе для username
        try:
            user_info = bot.get_chat_member(user_id, user_id).user
            username = f"@{user_info.username}" if user_info.username else "Нет username"
            username_link = f"<a href=\"tg://user?id={user_id}\">{username}</a>" if user_info.username else "Нет username"
        except Exception as e:
            print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
            username_link = "Неизвестный пользователь"

        # Проверяем баланс пользователя
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
            user = cursor.fetchone()
            if not user or user[0] < amount:
                bot.answer_callback_query(call.id, f"❌ Недостаточно средств на балансе пользователя {user_id}!")
                bot.edit_message_text(
                    f"❌ Не удалось отправить чек на {amount}$ пользователю {user_id}: недостаточно средств на балансе.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML'
                )
                return

        # Создаём чек через CryptoBot API
        crypto_api = crypto_pay.CryptoPay()
        cheque_result = crypto_api.create_check(
            amount=amount,
            asset="USDT",
            description=f"Выплата для пользователя {user_id}"
        )

        if cheque_result.get("ok", False):
            cheque = cheque_result.get("result", {})
            cheque_link = cheque.get("bot_check_url", "")

            if cheque_link:
                # Уменьшаем баланс пользователя
                with db.get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute('UPDATE users SET BALANCE = BALANCE - ? WHERE ID = ?', (amount, user_id))
                    cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
                    new_balance = cursor.fetchone()[0]
                    conn.commit()
                    print(f"[DEBUG] Баланс пользователя {user_id} уменьшен на {amount}$, новый баланс: {new_balance}")

                # Обновляем баланс казны
                db_module.update_treasury_balance(-amount)

                # Уведомляем пользователя
                markup_user = types.InlineKeyboardMarkup()
                markup_user.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                safe_send_message(
                    user_id,
                    f"✅ Вам отправлен чек на {amount}$!\n"
                    f"🔗 Ссылка на чек: {cheque_link}\n"
                    f"💰 Ваш новый баланс: {new_balance}$",
                    parse_mode='HTML',
                    reply_markup=markup_user
                )

                # Уведомляем администратора
                markup_admin = types.InlineKeyboardMarkup()
                markup_admin.add(types.InlineKeyboardButton("🔙 Назад в заявки", callback_data="pending_withdrawals"))
                bot.edit_message_text(
                    f"✅ Чек на {amount}$ успешно отправлен пользователю {user_id} ({username_link}).\n"
                    f"💰 Новый баланс пользователя: {new_balance}$",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )

                # Логируем операцию
                db_module.log_treasury_operation("Вывод (чек)", -amount, db_module.get_treasury_balance())
            else:
                bot.edit_message_text(
                    f"❌ Не удалось создать чек для пользователя {user_id}.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML'
                )
        else:
            bot.edit_message_text(
                f"❌ Ошибка при создании чека: {cheque_result.get('error', 'Неизвестная ошибка')}",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML'
            )

        bot.answer_callback_query(call.id, f"Чек на {amount}$ отправлен пользователю {user_id}.")
    except Exception as e:
        print(f"[ERROR] Ошибка в send_check_callback: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при отправке чека!")
        bot.edit_message_text(
            f"❌ Произошла ошибка при отправке чека пользователю {user_id}.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("manual_check_"))
def manual_check_request(call):
    if call.from_user.id in config.ADMINS_ID:
        _, _, user_id, amount = call.data.split("_")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        msg = bot.edit_message_text(
            f"📤 Введите ссылку на чек для пользователя {user_id} на сумму {amount}$:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_check_link, user_id, amount)

def process_check_link_success(call, user_id, amount, check_link):
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), float(amount)))
        conn.commit()
    
    markup_admin = types.InlineKeyboardMarkup()
    markup_admin.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(
        f"✅ Чек на сумму {amount}$ успешно создан и отправлен пользователю {user_id}",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup_admin
    )
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=check_link))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    try:
        bot.send_message(int(user_id),
                       f"✅ Ваша выплата {amount}$ готова!\n💳 Нажмите кнопку ниже для активации чека",
                       reply_markup=markup)
    except Exception as e:
        print(f"Error sending message to user {user_id}: {e}")

def process_check_link(message, user_id, amount):
    if message.from_user.id in config.ADMINS_ID:
        check_link = message.text.strip()
        
        if not check_link.startswith("https://") or "t.me/" not in check_link:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"manual_check_{user_id}_{amount}"))
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, 
                           "❌ Неверный формат ссылки на чек. Пожалуйста, убедитесь, что вы скопировали полную ссылку.",
                           reply_markup=markup)
            return
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), float(amount)))
            conn.commit()
        
        markup_admin = types.InlineKeyboardMarkup()
        markup_admin.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id,
                       f"✅ Чек на сумму {amount}$ успешно отправлен пользователю {user_id}",
                       reply_markup=markup_admin)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=check_link))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.send_message(int(user_id),
                           f"✅ Ваша выплата {amount}$ готова!\n💳 Нажмите кнопку ниже для активации чека",
                           reply_markup=markup)
        except Exception as e:
            print(f"Error sending message to user {user_id}: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_withdraw_"))
def reject_withdraw(call):
    if call.from_user.id in config.ADMINS_ID:
        _, _, user_id, amount = call.data.split("_")
        amount = float(amount)
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, int(user_id)))
            cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), amount))
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Попробовать снова", callback_data="withdraw"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.send_message(int(user_id),
                           f"❌ Ваша заявка на вывод {amount}$ отклонена\n💰 Средства возвращены на баланс",
                           reply_markup=markup)
        except:
            pass
        
        markup_admin = types.InlineKeyboardMarkup()
        markup_admin.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text("✅ Выплата отклонена, средства возвращены",
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=markup_admin)


#===========================================================================
#=======================КАЗНА====================КАЗНА======================
#===========================================================================

 

@bot.callback_query_handler(func=lambda call: call.data == "treasury")
def show_treasury(call):
    if call.from_user.id not in config.dostup:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    auto_input_status = db_module.get_auto_input_status()
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nВ ней лежит: <code>{crypto_balance}</code> USDT"
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nОшибка при получении баланса: <code>{str(e)}</code>"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📤 Вывести", callback_data="treasury_withdraw"))
    markup.add(types.InlineKeyboardButton("📥 Пополнить", callback_data="treasury_deposit"))
    auto_input_text = "🔴 Включить авто-ввод" if not auto_input_status else "🟢 Выключить авто-ввод"
    markup.add(types.InlineKeyboardButton(auto_input_text, callback_data="treasury_toggle_auto"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    bot.edit_message_text(
        treasury_text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "treasury_withdraw")
def treasury_withdraw_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        msg = bot.edit_message_text(
            f"📤 <b>Вывод средств из казны</b>\n\nТекущий баланс: <code>{crypto_balance}</code> USDT\n\nВведите сумму для вывода:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        
        bot.register_next_step_handler(msg, process_treasury_withdraw)
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⚠️ <b>Ошибка при получении баланса:</b> {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

def process_treasury_withdraw(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "⛔ У вас нет доступа к этой функции.", parse_mode='HTML')
        return
    
    admin_id = message.from_user.id
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        amount = float(message.text)
        
        if amount <= 0:
            bot.send_message(
                message.chat.id,
                "❌ <b>Ошибка!</b> Введите корректную сумму (больше нуля).",
                parse_mode='HTML',
                reply_markup=markup
            )
            return
        
        with treasury_lock:
            crypto_api = crypto_pay.CryptoPay()
            balance_result = crypto_api.get_balance()
            
            if not balance_result.get("ok", False):
                raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
            
            crypto_balance = 0
            for currency in balance_result.get("result", []):
                if currency.get("currency_code") == "USDT":
                    crypto_balance = float(currency.get("available", "0"))
                    break
            
            print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
            
            if amount > crypto_balance:
                bot.send_message(
                    message.chat.id,
                    f"❌ <b>Недостаточно средств на балансе CryptoBot!</b>\nТекущий баланс: <code>{crypto_balance}</code> USDT",
                    parse_mode='HTML',
                    reply_markup=markup
                )
                return
            
            amount_to_send = calculate_amount_to_send(amount)
            
            check_result = crypto_api.create_check(
                amount=amount_to_send,
                asset="USDT",
                description=f"Вывод из казны от {admin_id}"
            )
            
            if check_result.get("ok", False):
                check = check_result.get("result", {})
                check_link = check.get("bot_check_url", "")
                
                if check_link:
                    new_balance = db_module.update_treasury_balance(-amount)
                    db_module.log_treasury_operation("Автовывод через чек", amount, new_balance)
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Активировать чек", url=check_link))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.send_message(
                        message.chat.id,
                        f"✅ <b>Средства успешно выведены с помощью чека!</b>\n\n"
                        f"Сумма: <code>{amount}</code> USDT\n"
                        f"Остаток в казне: <code>{new_balance}</code> USDT\n\n"
                        f"Для получения средств активируйте чек по кнопке ниже:",
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    return
            else:
                error_details = check_result.get("error_details", "Неизвестная ошибка")
                raise Exception(f"Ошибка при создании чека: {error_details}")
    
    except ValueError:
        bot.send_message(
            message.chat.id,
            "❌ <b>Ошибка!</b> Введите числовое значение.",
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"Ошибка при выводе через CryptoBot: {e}")
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>Ошибка при автовыводе средств:</b> {str(e)}",
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data == "treasury_deposit")
def treasury_deposit_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        msg = bot.edit_message_text(
            f"📥 <b>Пополнение казны</b>\n\nТекущий баланс: <code>{crypto_balance}</code> USDT\n\nВведите сумму для пополнения:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        
        bot.register_next_step_handler(msg, process_treasury_deposit)
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⚠️ <b>Ошибка при получении баланса:</b> {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

def process_treasury_deposit(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "⛔ У вас нет доступа к этой функции.", parse_mode='HTML')
        return
    
    admin_id = message.from_user.id
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        amount = float(message.text)
        
        if amount <= 0:
            bot.send_message(
                message.chat.id,
                "❌ <b>Ошибка!</b> Введите корректную сумму (больше нуля).",
                parse_mode='HTML',
                reply_markup=markup
            )
            return
        
        markup_crypto = types.InlineKeyboardMarkup()
        markup_crypto.add(types.InlineKeyboardButton("💳 Пополнить через CryptoBot", callback_data=f"treasury_deposit_crypto_{amount}"))
        markup_crypto.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup_crypto.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.send_message(
            message.chat.id,
            f"💰 <b>Пополнение казны на {amount}$</b>\n\n"
            f"Нажмите кнопку ниже для пополнения через CryptoBot:",
            parse_mode='HTML',
            reply_markup=markup_crypto
        )
    
    except ValueError:
        bot.send_message(
            message.chat.id,
            "❌ <b>Ошибка!</b> Введите числовое значение.",
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("treasury_deposit_crypto_"))
def treasury_deposit_crypto(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    amount = float(call.data.split("_")[-1])
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        amount_with_fee = calculate_amount_to_send(amount)
        
        invoice_result = crypto_api.create_invoice(
            amount=amount_with_fee,
            asset="USDT",
            description=f"Пополнение казны от {admin_id}",
            hidden_message="Спасибо за пополнение казны!",
            paid_btn_name="callback",
            paid_btn_url=f"https://t.me/{bot.get_me().username}",
            expires_in=300
        )
        
        if invoice_result.get("ok", False):
            invoice = invoice_result.get("result", {})
            invoice_link = invoice.get("pay_url", "")
            invoice_id = invoice.get("invoice_id")
            
            if invoice_link and invoice_id:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("💸 Оплатить инвойс", url=invoice_link))
                markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                message = bot.edit_message_text(
                    f"💰 <b>Инвойс на пополнение казны создан</b>\n\n"
                    f"Сумма: <code>{amount}</code> USDT\n\n"
                    f"1. Нажмите на кнопку 'Оплатить инвойс'\n"
                    f"2. Оплатите созданный инвойс\n\n"
                    f"⚠️ <i>Инвойс действует 5 минут</i>\n\n"
                    f"⏳ <b>Ожидание оплаты...</b>",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                check_payment_thread = threading.Thread(
                    target=check_invoice_payment,
                    args=(invoice_id, amount, admin_id, call.message.chat.id, call.message.message_id)
                )
                check_payment_thread.daemon = True
                check_payment_thread.start()
                return
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        error_message = invoice_result.get("error", {}).get("message", "Неизвестная ошибка")
        bot.edit_message_text(
            f"❌ <b>Ошибка при создании инвойса</b>\n\n"
            f"Не удалось создать инвойс через CryptoBot.\n"
            f"Ошибка: {error_message}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    except Exception as e:
        print(f"Error creating invoice for treasury deposit: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"❌ <b>Ошибка при работе с CryptoBot</b>\n\n"
            f"Произошла ошибка: {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

def check_invoice_payment(invoice_id, amount, admin_id, chat_id, message_id):
    crypto_api = crypto_pay.CryptoPay()
    start_time = datetime.now()
    timeout = timedelta(minutes=5)
    check_interval = 5
    check_counter = 0
    
    try:
        while datetime.now() - start_time < timeout:
            print(f"Checking invoice {invoice_id} (attempt {check_counter + 1})...")
            invoices_result = crypto_api.get_invoices(invoice_ids=[invoice_id])
            print(f"Invoice API response: {invoices_result}")
            
            if invoices_result.get("ok", False):
                invoices = invoices_result.get("result", {}).get("items", [])
                
                if not invoices:
                    print(f"No invoices found for ID {invoice_id}")
                    time.sleep(check_interval)
                    check_counter += 1
                    continue
                
                status = invoices[0].get("status", "")
                print(f"Invoice {invoice_id} status: {status}")
                
                if status in ["paid", "completed"]:
                    print(f"Invoice {invoice_id} paid successfully!")
                    try:
                        with treasury_lock:
                            new_balance = db_module.update_treasury_balance(amount)
                            print(f"Updated treasury balance: {new_balance}")
                            db_module.log_treasury_operation("Пополнение через Crypto Pay", amount, new_balance)
                            print(f"Logged treasury operation: amount={amount}, new_balance={new_balance}")
                        
                        balance_result = crypto_api.get_balance()
                        crypto_balance = 0
                        if balance_result.get("ok", False):
                            for currency in balance_result.get("result", []):
                                if currency.get("currency_code") == "USDT":
                                    crypto_balance = float(currency.get("available", "0"))
                                    break
                        print(f"Баланс CryptoBot после оплаты: {crypto_balance} USDT")
                        
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"✅ <b>Казна успешно пополнена!</b>\n\n"
                            f"Сумма: <code>{amount}</code> USDT\n"
                            f"Текущий баланс казны: <code>{new_balance}</code> USDT\n"
                            f"Баланс CryptoBot: <code>{crypto_balance}</code> USDT",
                            chat_id,
                            message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        print(f"Payment confirmation message updated for invoice {invoice_id}")
                        return
                    
                    except Exception as db_error:
                        print(f"Error updating treasury balance or logging operation: {db_error}")
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"⚠️ <b>Ошибка при обновлении казны:</b> {str(db_error)}\n"
                            f"Пополнение на сумму <code>{amount}</code> USDT выполнено, но казна не обновлена.",
                            chat_id,
                            message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        return
                
                elif status == "expired":
                    print(f"Invoice {invoice_id} expired.")
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔄 Создать новый инвойс", callback_data=f"treasury_deposit_crypto_{amount}"))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.edit_message_text(
                        f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
                        f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
                        f"Вы можете создать новый инвойс.",
                        chat_id,
                        message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    return
                
                check_counter += 1
                if check_counter % 5 == 0:
                    elapsed = datetime.now() - start_time
                    remaining_seconds = int(timeout.total_seconds() - elapsed.total_seconds())
                    minutes = remaining_seconds // 60
                    seconds = remaining_seconds % 60
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Оплатить инвойс", url=invoices[0].get("pay_url", "")))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.edit_message_text(
                        f"💰 <b>Инвойс на пополнение казны создан</b>\n\n"
                        f"Сумма: <code>{amount}</code> USDT\n\n"
                        f"1. Нажмите на кнопку 'Оплатить инвойс'\n"
                        f"2. Оплатите созданный инвойс\n\n"
                        f"⏱ <b>Оставшееся время:</b> {minutes}:{seconds:02d}\n"
                        f"⏳ <b>Ожидание оплаты...</b>",
                        chat_id,
                        message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    print(f"Waiting message updated: {minutes}:{seconds:02d} remaining")
            
            else:
                print(f"API request failed: {invoices_result}")
            
            time.sleep(check_interval)
        
        print(f"Invoice {invoice_id} not paid after timeout.")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 Создать новый инвойс", callback_data=f"treasury_deposit_crypto_{amount}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
            f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
            f"Вы можете создать новый инвойс.",
            chat_id,
            message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    except Exception as e:
        print(f"Error in check_invoice_payment thread: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"❌ <b>Ошибка при проверке оплаты</b>\n\n"
            f"Произошла ошибка: {str(e)}\n"
            f"Пожалуйста, попробуйте снова.",
            chat_id,
            message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data == "treasury_toggle_auto")
def treasury_toggle_auto_input(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    
    new_status = db_module.toggle_auto_input()
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        status_text = "включен" if new_status else "выключен"
        operation = f"Авто-ввод {status_text}"
        db_module.log_treasury_operation(operation, 0, crypto_balance)
        
        status_emoji = "🟢" if new_status else "🔴"
        auto_message = f"{status_emoji} <b>Авто-ввод {status_text}!</b>\n"
        if new_status:
            auto_message += "Средства будут автоматически поступать в казну."
        else:
            auto_message += "Средства больше не будут автоматически поступать в казну."
        
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nВ ней лежит: <code>{crypto_balance}</code> USDT\n\n{auto_message}"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📤 Вывести", callback_data="treasury_withdraw"))
        markup.add(types.InlineKeyboardButton("📥 Пополнить", callback_data="treasury_deposit"))
        
        auto_input_text = "🔴 Включить авто-ввод" if not new_status else "🟢 Выключить авто-ввод"
        markup.add(types.InlineKeyboardButton(auto_input_text, callback_data="treasury_toggle_auto"))
        
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            treasury_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⚠️ <b>Ошибка при получении баланса:</b> {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("treasury_withdraw_all_"))
def treasury_withdraw_all(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    amount = float(call.data.split("_")[-1])
    
    if amount <= 0:
        bot.answer_callback_query(call.id, "⚠️ Баланс казны пуст. Нечего выводить.", show_alert=True)
        return
    
    with treasury_lock:
        operation_success = False
        
        try:
            crypto_api = crypto_pay.CryptoPay()
            balance_result = crypto_api.get_balance()
            
            if not balance_result.get("ok", False):
                raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
            
            crypto_balance = 0
            for currency in balance_result.get("result", []):
                if currency.get("currency_code") == "USDT":
                    crypto_balance = float(currency.get("available", "0"))
                    break
            
            print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
            
            if crypto_balance < amount:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                bot.edit_message_text(
                    f"❌ <b>Недостаточно средств на балансе CryptoBot!</b>\n"
                    f"Баланс: <code>{crypto_balance}</code> USDT, требуется: <code>{amount}</code> USDT.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                return
            
            amount_to_send = calculate_amount_to_send(amount)
            
            check_result = crypto_api.create_check(
                amount=amount_to_send,
                asset="USDT",
                description=f"Вывод всей казны от {admin_id}"
            )
            
            if check_result.get("ok", False):
                check = check_result.get("result", {})
                check_link = check.get("bot_check_url", "")
                
                if check_link:
                    new_balance = db_module.update_treasury_balance(-amount)
                    db_module.log_treasury_operation("Вывод всей казны через чек", amount, new_balance)
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Активировать чек", url=check_link))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.edit_message_text(
                        f"✅ <b>Все средства успешно выведены с помощью чека!</b>\n\n"
                        f"Сумма: <code>{amount}</code> USDT\n"
                        f"Остаток в казне: <code>{new_balance}</code> USDT\n\n"
                        f"Для получения средств активируйте чек по кнопке ниже:",
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    operation_success = True
                    return
                else:
                    error_details = check_result.get("error_details", "Неизвестная ошибка")
                    raise Exception(f"Ошибка при создании чека: {error_details}")
        
        except Exception as e:
            print(f"Ошибка при выводе через CryptoBot: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            
            bot.edit_message_text(
                f"⚠️ <b>Ошибка при работе с CryptoBot:</b> {str(e)}",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        
        if not operation_success:
            new_balance = db_module.update_treasury_balance(-amount)
            db_module.log_treasury_operation("Вывод всей казны", amount, new_balance)
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            
            bot.edit_message_text(
                f"✅ <b>Все средства успешно выведены!</b>\n\n"
                f"Сумма: <code>{amount}</code> USDT\n"
                f"Остаток в казне: <code>{new_balance}</code> USDT",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=markup
            )

def calculate_amount_to_send(target_amount):
    """
    Рассчитывает сумму для отправки с учётом комиссии CryptoBot (3%).
    Возвращает сумму, которую нужно отправить, чтобы после комиссии получить target_amount.
    """
    commission_rate = 0.03  # Комиссия 3%
    amount_with_fee = target_amount / (1 - commission_rate) 
    rounded_amount = round(amount_with_fee, 2)  
    
    received_amount = rounded_amount * (1 - commission_rate)
    if received_amount < target_amount:
        rounded_amount += 0.01  
    
    return round(rounded_amount, 2)


#=================================================================================
#===============================НАСТРОЙКИ=========================================
#=================================================================================



@bot.callback_query_handler(func=lambda call: call.data == "settings")
def show_settings(call):
    if call.from_user.id in config.ADMINS_ID:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT PRICE, HOLD_TIME, PRICE_ADM FROM settings')
            result = cursor.fetchone()
            price, hold_time, price_adm = result if result else (2.0, 30, 4.5)
        
        settings_text = (
            "<b>⚙️ Настройки оплаты</b>\n\n"
            f"Текущая ставка: <code>{price}$</code> за номер\n"
            f"Время холда: <code>{hold_time}</code> минут\n\n"
            f"Текущая ставка для владельца: <code>{price_adm}$</code>\n\n"
            "Выберите параметр для изменения:"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💰 Изменить сумму", callback_data="change_amount"))
        markup.add(types.InlineKeyboardButton("💰 Изменить сумму для админов", callback_data="change_amount_adm"))
        markup.add(types.InlineKeyboardButton("⏱ Изменить время холда", callback_data="change_hold_time"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(settings_text,
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "change_amount_adm")
def change_amount_adm_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("💰 Введите новую сумму оплаты для админов (в долларах, например: 5):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_amount_adm)

def process_change_amount_adm(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_amount = float(message.text)
            if new_amount <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET PRICE_ADM = ?', (new_amount,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Сумма оплаты для админов изменена на {new_amount}$", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное число!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "change_amount")
def change_amount_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("💰 Введите новую сумму оплаты (в долларах, например: 2):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_amount)


def process_change_amount(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_amount = float(message.text)
            if new_amount <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET PRICE = ?', (new_amount,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Сумма оплаты изменена на {new_amount}$", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное число!", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "change_hold_time")
def change_hold_time_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("⏱ Введите новое время холда (в минутах, например: 5):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_hold_time)

def process_change_hold_time(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_time = int(message.text)
            if new_time <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET HOLD_TIME = ?', (new_time,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Время холда изменено на {new_time} минут", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное целое число!", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("view_group_stats_"))
def view_group_stats(call):
    user_id = call.from_user.id
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра статистики!")
        return

    try:
        _, _, group_id, page = call.data.split("_")
        group_id = int(group_id)
        page = int(page)
        if page < 1:
            page = 1
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        # Подсчитываем участников (модераторов) группы
        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = "moder"', (group_id,))
        member_count = cursor.fetchone()[0]

        # Получаем номера с статусом "отстоял" для конкретной группы
        cursor.execute('''
            SELECT n.NUMBER, n.TAKE_DATE, n.SHUTDOWN_DATE
            FROM numbers n
            LEFT JOIN personal p ON p.ID = n.ID_OWNER
            WHERE n.STATUS = 'отстоял'
            AND (p.GROUP_ID = ? OR p.GROUP_ID IS NULL)
            ORDER BY n.SHUTDOWN_DATE DESC
        ''', (group_id,))
        numbers = cursor.fetchall()

    # Пагинация
    items_per_page = 20
    total_pages = max(1, (len(numbers) + items_per_page - 1) // items_per_page)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_numbers = numbers[start_idx:end_idx]

    # Формируем текст статистики
    text = (
        f"<b>📊 Статистика группы {group_id}:</b>\n\n"
        f"📱 Успешных номеров: {len(numbers)}\n"
        f"────────────────────\n"
        f"<b>📱 Список номеров (страница {page}/{total_pages}):</b>\n\n"
    )

    if not page_numbers:
        text += "📭 Нет успешных номеров в этой группе."
    else:
        for number, take_date, shutdown_date in page_numbers:
            text += f"Номер: {number}\n"
            text += f"🟢 Встал: {take_date}\n"
            text += f"🟢 Отстоял: {shutdown_date}\n"
            text += "───────────────────\n"

    # Проверяем лимит символов
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано, используйте пагинацию)"

    # Формируем разметку
    markup = types.InlineKeyboardMarkup()

    # Кнопки пагинации
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"view_group_stats_{group_id}_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"view_group_stats_{group_id}_{page+1}"))
        if row:
            markup.row(*row)

    markup.add(types.InlineKeyboardButton("👥 Все группы", callback_data="admin_view_groups"))
    markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )









#=======================================================================================
#=======================================================================================
#===================================АДМИНКА=====================================
#=======================================================================================
#=======================================================================================
#=======================================================================================


@bot.callback_query_handler(func=lambda call: call.data == "admin_panel")
def admin_panel(call):
    user_id = call.from_user.id
    broadcast_state.pop(user_id, None)
    with treasury_lock:
        if call.from_user.id in active_treasury_admins:
            del active_treasury_admins[call.from_user.id]
            
    
    if call.from_user.id in config.ADMINS_ID:           
        with db.get_db() as conn:
                cursor = conn.cursor()
                # Подсчёт слетевших номеров
                cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS = "слетел"')
                numbers_count = cursor.fetchone()[0]
                
                # Подсчёт всех обработанных номеров
                cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS IN ("активен", "слетел", "отстоял")')
                total_numbers = cursor.fetchone()[0]

                admin_text = (
                    "<b>⚙️ Панель администратора</b>\n\n"
                    f"📱 Слетевших номеров: <code>{numbers_count}</code>\n"
                    f"📊 Всего обработанных номеров: <code>{total_numbers}</code>"
                )

        markup = types.InlineKeyboardMarkup()

        markup.add(types.InlineKeyboardButton("👥 Модераторы", callback_data="moderators"))
        markup.add(types.InlineKeyboardButton("👤 Пользователи", callback_data="all_users_1"))

        markup.add(types.InlineKeyboardButton("📝 Заявки", callback_data="pending_requests"))
        markup.add(types.InlineKeyboardButton("👥 Группы", callback_data="groups"))
        markup.add(types.InlineKeyboardButton("📢 Рассылка", callback_data="broadcast"))

        markup.add(types.InlineKeyboardButton("📱 Все номера", callback_data="all_numbers"))
        markup.add(types.InlineKeyboardButton("📱 Номера", callback_data="user_numbers_all"))
        markup.add(types.InlineKeyboardButton("🔍 Найти номер", callback_data="search_number"))

        markup.add(types.InlineKeyboardButton("💰 Казна", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings"))
        markup.add(types.InlineKeyboardButton("⚙️ Настройки (главного меню)", callback_data="Gv"))

        markup.add(types.InlineKeyboardButton("🗄️ База данных", callback_data="db_menu"))

        markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="Gv"))
        markup.add(types.InlineKeyboardButton("↩️ Назад", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(call.message.chat.id, admin_text, parse_mode='HTML', reply_markup=markup)



#===============================================================
#==========================МОДЕРАТОРЫ===========================
#===============================================================

@bot.callback_query_handler(func=lambda call: call.data == "moderators")
def moderators(call):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    if call.from_user.id in config.ADMINS_ID:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("➕ Добавить", callback_data="add_moder"),
            types.InlineKeyboardButton("➖ Удалить", callback_data="remove_moder"))
        markup.add(
        types.InlineKeyboardButton("➖Удалить по кнопке модератора", callback_data="delete_moderator"),
        types.InlineKeyboardButton("👥 Все модераторы", callback_data="all_moderators_1"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(call.message.chat.id, "👥 Управление модераторами:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "delete_moderator")
def delete_moderator_request(call):
    if call.from_user.id in config.ADMINS_ID:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ID FROM personal WHERE TYPE = 'moder'")
            moderators = cursor.fetchall()
        
        if not moderators:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.send_message(call.message.chat.id, "❌ Нет модераторов для удаления", reply_markup=markup)
            return

        text = "👥 Выберите модератора для удаления:\n\n"
        markup = types.InlineKeyboardMarkup()
        for moder in moderators:
            text += f"ID: {moder[0]}\n"
            markup.add(types.InlineKeyboardButton(f"Удалить {moder[0]}", callback_data=f"confirm_delete_moder_{moder[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(call.message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_delete_moder_"))
def confirm_delete_moderator(call):
    if call.from_user.id in config.ADMINS_ID:
        moder_id = int(call.data.split("_")[3])
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM personal WHERE ID = ? AND TYPE = 'moder'", (moder_id,))
            affected_rows = cursor.rowcount
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        if affected_rows > 0:
            try:
                msg = bot.send_message(moder_id, "⚠️ Ваши права модератора были отозваны администратором.")
                # Планируем удаление через 30 секунд
                threading.Timer(30.0, lambda: bot.delete_message(moder_id, msg.message_id)).start()
            except:
                pass
            bot.send_message(call.message.chat.id, f"✅ Модератор с ID {moder_id} успешно удален", reply_markup=markup)
        else:
            bot.send_message(call.message.chat.id, f"❌ Модератор с ID {moder_id} не найден", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "add_moder")
def add_moder_request(call):
    if call.from_user.id in config.ADMINS_ID:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            print(f"[ERROR] Не удалось удалить сообщение: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))    
        msg = bot.send_message(
            call.message.chat.id, 
            "👤 Введите ID пользователя для назначения модератором:", 
            reply_markup=markup
        )
        # Передаём initial_message_id (msg.message_id) в process_add_moder
        bot.register_next_step_handler(msg, process_add_moder, msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "remove_moder")
def remove_moder_request(call):
    if call.from_user.id in config.ADMINS_ID:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))   
        msg = bot.send_message(call.message.chat.id, "👤 Введите ID пользователя для удаления из модераторов:", reply_markup = markup)
        bot.register_next_step_handler(msg, process_remove_moder)

def process_remove_moder(message):
    try:
        moder_id = int(message.text)
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM personal WHERE ID = ? AND TYPE = ?', (moder_id, 'moder'))
            conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            if cursor.rowcount > 0:
                try:
                    msg = bot.send_message(moder_id, "⚠️ У вас были отозваны права модератора.")
                    threading.Timer(30.0, lambda: bot.delete_message(moder_id, msg.message_id)).start()
                except:
                    pass
                bot.send_message(message.chat.id, f"✅ Пользователь {moder_id} успешно удален из модераторов!", reply_markup=markup)
            else:
                bot.send_message(message.chat.id, "⚠️ Этот пользователь не является модератором!", reply_markup=markup)
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        bot.send_message(message.chat.id, "❌ Ошибка! Введите корректный ID пользователя (только цифры)", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("all_moderators_"))
def all_moderators_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра списка модераторов!")
        return
    try:
        page = int(call.data.split("_")[2])
        if page < 1:
            page = 1
    except (IndexError, ValueError):
        page = 1
    
    with get_db() as conn:
        cursor = conn.cursor()
        # Получаем всех модераторов и их группы (без USERNAME)
        cursor.execute('''
            SELECT p.ID, g.NAME
            FROM personal p
            LEFT JOIN groups g ON p.GROUP_ID = g.ID
            WHERE p.TYPE = 'moder'
            ORDER BY p.ID
        ''')
        moderators = cursor.fetchall()
    
    if not moderators:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
        bot.edit_message_text(
            "📭 Нет модераторов.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        return
    
    items_per_page = 10
    total_pages = (len(moderators) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_moderators = moderators[start_idx:end_idx]
    
    text = f"<b>👥 Список модераторов (страница {page}/{total_pages}):</b>\n\n"
    with get_db() as conn:
        cursor = conn.cursor()
        for idx, (moder_id, group_name) in enumerate(page_moderators, start=start_idx + 1):
            cursor.execute('''
                SELECT COUNT(*) 
                FROM numbers 
                WHERE CONFIRMED_BY_MODERATOR_ID = ? AND STATUS = 'отстоял'
            ''', (moder_id,))
            accepted_numbers = cursor.fetchone()[0]
            try:
                user = bot.get_chat(moder_id)
                username = user.username if user.username else "Нет username"
            except Exception as e:
                logging.error(f"Ошибка при получении username для user_id {moder_id}: {e}")
                username = "Ошибка получения"
            
            group_display = group_name if group_name else "Без группы"
            # Форматируем UserID как ссылку
            text += f"{idx}. 🆔UserID: <a href=\"tg://user?id={moder_id}\">{moder_id}</a>\n"
            text += f"Username: @{username}\n"
            text += f"🏠 Группа: {group_display}\n"
            text += f"📱 Принято номеров: {accepted_numbers}\n"
            text += "────────────────────\n"
    
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Слишком много данных, используйте пагинацию)"
    
    markup = InlineKeyboardMarkup()
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"all_moderators_{page-1}"))
        row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_moderators_{page+1}"))
        markup.row(*row)
    
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        logging.error(f"Ошибка при обновлении сообщения all_moderators: {e}")
        bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    
    bot.answer_callback_query(call.id)

def process_add_moder(message, initial_message_id):
    try:
        new_moder_id = int(message.text)
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM personal WHERE ID = ? AND TYPE = ?', (new_moder_id, 'moder'))
            if cursor.fetchone() is not None:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                    bot.delete_message(message.chat.id, initial_message_id)
                except Exception as e:
                    print(f"Ошибка удаления сообщения: {e}")
                bot.send_message(message.chat.id, "⚠️ Этот пользователь уже является модератором!", reply_markup=markup)
                return

            cursor.execute('SELECT COUNT(*) FROM groups')
            if cursor.fetchone()[0] == 0:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("➕ Создать группу", callback_data="add_group"))
                markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                    bot.delete_message(message.chat.id, initial_message_id)
                except Exception as e:
                    print(f"Ошибка удаления сообщения: {e}")
                bot.send_message(message.chat.id, "❌ Нет созданных групп! Сначала создайте группу.", reply_markup=markup)
                return

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
            bot.delete_message(message.chat.id, initial_message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения: {e}")
        msg = bot.send_message(
            message.chat.id,
            f"👤 ID модератора: {new_moder_id}\n📝 Введите название группы для назначения:",
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_assign_group, new_moder_id, msg.message_id)  # Передаём message_id

    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
            bot.delete_message(message.chat.id, initial_message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка! Введите корректный ID пользователя (только цифры)", reply_markup=markup)

def process_assign_group(message, new_moder_id, group_message_id):
    group_name = message.text.strip()
    
    if not group_name:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения (ввод названия группы): {e}")
        bot.send_message(message.chat.id, "❌ Название группы не может быть пустым!", reply_markup=markup)
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM groups WHERE NAME = ?', (group_name,))
        group = cursor.fetchone()

        if not group:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("👥 Группы", callback_data="groups"))
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception as e:
                print(f"Ошибка удаления сообщения (ввод названия группы): {e}")
            bot.send_message(message.chat.id, f"❌ Группа '{group_name}' не найдена! Создайте её или выберите существующую.", 
                            reply_markup=markup)
            return

        group_id = group[0]

        try:
            # Удаляем сообщение с названием группы и предыдущее сообщение с запросом
            try:
                bot.delete_message(message.chat.id, message.message_id)
                bot.delete_message(message.chat.id, group_message_id)
            except Exception as e:
                print(f"Ошибка удаления сообщения (запрос названия группы): {e}")
                # Если удаление не удалось, редактируем сообщение
                bot.edit_message_text(
                    f"✅ Пользователь {new_moder_id} успешно назначен модератором в группу '{group_name}'!",
                    message.chat.id,
                    group_message_id,
                    reply_markup=None
                )
            
            # Назначаем модератора
            cursor.execute('INSERT INTO personal (ID, TYPE, GROUP_ID) VALUES (?, ?, ?)', 
                          (new_moder_id, 'moder', group_id))
            conn.commit()
            
            # Отправляем подтверждение модератору и планируем удаление
            moder_msg = bot.send_message(new_moder_id, f"🎉 Вам выданы права модератора в группе '{group_name}'! Напишите /start, чтобы начать работу.")
            threading.Timer(30.0, lambda: bot.delete_message(new_moder_id, moder_msg.message_id)).start()

            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, f"✅ Пользователь {new_moder_id} успешно назначен модератором в группу '{group_name}'!", 
                            reply_markup=markup)

        except telebot.apihelper.ApiTelegramException:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, f"❌ Ошибка: Пользователь {new_moder_id} не начал диалог с ботом!", 
                            reply_markup=markup)
        except Exception as e:
            print(f"Ошибка в process_assign_group: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, "❌ Произошла ошибка при назначении модератора!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "delete_moderator")
def delete_moderator_request(call):
    if call.from_user.id in config.ADMINS_ID:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ID FROM personal WHERE TYPE = 'moder'")
            moderators = cursor.fetchall()
        
        if not moderators:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.send_message(call.message.chat.id, "❌ Нет модераторов для удаления", reply_markup=markup)
            return

        text = "👥 Выберите модератора для удаления:\n\n"
        markup = types.InlineKeyboardMarkup()
        for moder in moderators:
            text += f"ID: {moder[0]}\n"
            markup.add(types.InlineKeyboardButton(f"Удалить {moder[0]}", callback_data=f"confirm_delete_moder_{moder[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(call.message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_delete_moder_"))
def confirm_delete_moderator(call):
    if call.from_user.id in config.ADMINS_ID:
        moder_id = int(call.data.split("_")[3])
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM personal WHERE ID = ? AND TYPE = 'moder'", (moder_id,))
            affected_rows = cursor.rowcount
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        if affected_rows > 0:
            try:
                msg = bot.send_message(moder_id, "⚠️ Ваши права модератора были отозваны администратором.")
                # Планируем удаление через 30 секунд
                threading.Timer(30.0, lambda: bot.delete_message(moder_id, msg.message_id)).start()
            except:
                pass
            bot.send_message(call.message.chat.id, f"✅ Модератор с ID {moder_id} успешно удален", reply_markup=markup)
        else:
            bot.send_message(call.message.chat.id, f"❌ Модератор с ID {moder_id} не найден", reply_markup=markup)

#=======================================================================#=======================================================================
#===============================================ВСЕ ПОЛЬЗОВАТЕЛИ ======================================================
#=======================================================================#=======================================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("all_users_"))
def show_all_users(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра пользователей!")
        return
    
    try:
        page = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        page = 1  # Если что-то пошло не так, открываем первую страницу
    
    # Получаем всех пользователей из таблицы requests
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM requests')
        all_users = cursor.fetchall()
    
    if not all_users:
        text = "📭 Нет пользователей в боте."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        # Пагинация
        users_per_page = 8
        total_pages = (len(all_users) + users_per_page - 1) // users_per_page
        page = max(1, min(page, total_pages))  # Ограничиваем страницу допустимым диапазоном
        
        start_idx = (page - 1) * users_per_page
        end_idx = start_idx + users_per_page
        page_users = all_users[start_idx:end_idx]
        
        # Формируем текст
        text = f"<b>Управляйте людьми:</b>\n({page} страница)\n\n"
        markup = types.InlineKeyboardMarkup()
        
        # Добавляем кнопки для каждого пользователя
        for user_data in page_users:
            user_id = user_data[0]
            try:
                user = bot.get_chat_member(user_id, user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
            except:
                username = "Неизвестный пользователь"
            
            markup.add(types.InlineKeyboardButton(f"{user_id} {username}", callback_data=f"user_details_{user_id}"))
        
        # Кнопки пагинации
        if total_pages > 1:
            row = []
            if page > 1:
                row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_users_{page-1}"))
            row.append(types.InlineKeyboardButton(f"{page}", callback_data=f"all_users_{page}"))
            if page < total_pages:
                row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_users_{page+1}"))
            markup.row(*row)
        
        # Кнопка "Найти по username или userid"
        markup.add(types.InlineKeyboardButton("🔍 Найти по username или userid", callback_data="find_user"))
        
        # Кнопка "Вернуться в админ-панель"
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "find_user")
def find_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для поиска пользователей!")
        return
    
    # Запрашиваем у админа username или userid
    text = "🔍 Введите @username или userid пользователя для поиска:"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="all_users_1"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    msg = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    
    # Регистрируем следующий шаг для обработки введённых данных
    bot.register_next_step_handler(msg, process_user_search, call.message.chat.id)

def process_user_search(message, original_chat_id):
    if message.chat.id != original_chat_id or message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ Ошибка: действие доступно только администратору!")
        return
    
    search_query = message.text.strip()
    
    # Удаляем сообщение с введёнными данными
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
    
    # Проверяем, что ввёл пользователь
    user_id = None
    username = None
    
    if search_query.startswith('@'):
        username = search_query[1:].lower()  # Убираем @ и приводим к нижнему регистру
    else:
        try:
            user_id = int(search_query)  # Пробуем преобразовать в число (userid)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат! Введите @username или userid (число).")
            return
    
    # Ищем пользователя в базе
    found_user_id = None
    username_display = "Неизвестный пользователь"
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        if user_id:
            cursor.execute('SELECT ID, USERNAME FROM users WHERE ID = ?', (user_id,))
            user = cursor.fetchone()
            if user:
                found_user_id = user[0]
                username_display = f"@{user[1]}" if user[1] else "Нет username"
        else:
            cursor.execute('SELECT ID, USERNAME FROM users')
            users = cursor.fetchall()
            for uid, uname in users:
                if uname and uname.lower() == username:
                    found_user_id = uid
                    username_display = f"@{uname}"
                    break
    
    # Формируем ответ
    if found_user_id:
        text = (
            f"<b>Найденный пользователь:</b>\n\n"
            f"🆔 ID: <code>{found_user_id}</code>\n"
            f"👤 Username: {username_display}\n"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"👁️ Подробнее ({found_user_id})", callback_data=f"user_details_info_{found_user_id}"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        text = "❌ Пользователь не найден!"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    # Отправляем новое сообщение
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_details_"))
def user_details(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для управления пользователями!")
        return
    
    # Разбираем callback_data
    parts = call.data.split("_")
    if len(parts) < 3 or not parts[2].isdigit():  # Проверяем, что третий элемент — число
        bot.answer_callback_query(call.id, "❌ Некорректный формат запроса. Обратитесь к администратору.")
        print(f"[DEBUG] Некорректный call.data: {call.data}")
        return
    
    user_id = int(parts[2])  # Извлекаем user_id как число
    
    # Получаем информацию о пользователе
    with db.get_db() as conn:
        cursor = conn.cursor()
        
        # Проверяем, есть ли пользователь в таблице requests
        cursor.execute('SELECT BLOCKED, CAN_SUBMIT_NUMBERS FROM requests WHERE ID = ?', (user_id,))
        user_data = cursor.fetchone()
        if not user_data:
            text = f"❌ Пользователь с ID {user_id} не найден!"
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
            markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
            return
        
        is_blocked = user_data[0]
        can_submit_numbers = user_data[1]
        
        # Получаем баланс из таблицы users
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        balance_data = cursor.fetchone()
        balance = balance_data[0] if balance_data and balance_data[0] is not None else 0.0
        print(f"[DEBUG] Баланс пользователя {user_id}: {balance:.2f}")
        
        # Статистика по номерам
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ?', (user_id,))
        total_numbers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS IN ("отстоял 1/2", "отстоял 2/2", "отстоял 2/2+ холд")', (user_id,))
        successful_numbers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS IN ("слетел", "слёт 1/2 холд", "слёт 2/2", "слёт 2/2+")', (user_id,))
        shutdown_numbers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS = "не валид"', (user_id,))
        invalid_numbers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS = "активен"', (user_id,))
        active_numbers = cursor.fetchone()[0]
        
        # Подсчёт номеров по конкретным статусам "отстоял"
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS = "отстоял 1/2"', (user_id,))
        half_hold_numbers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS = "отстоял 2/2"', (user_id,))
        full_hold_numbers = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS = "отстоял 2/2+ холд"', (user_id,))
        extra_hold_numbers = cursor.fetchone()[0]
    
    # Получаем username через Telegram API
    try:
        user = bot.get_chat_member(user_id, user_id).user
        username = f"@{user.username}" if user.username else "Нет username"
    except Exception as e:
        print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
        username = "Неизвестный пользователь"
    
    # Формируем текст
    text = (
        f"<b>Пользователь {user_id} {username}</b>\n\n"
        f"💰 Баланс: {balance:.2f} $\n"
        f"📱 Сколько всего залил: {total_numbers}\n"
        f"✅ Сколько всего успешных: {successful_numbers}\n"
        f"⏳ Сколько слетело: {shutdown_numbers}\n"
        f"❌ Сколько не валидных: {invalid_numbers}\n"
        f"🔄 Которые на данный момент работают: {active_numbers}\n"
        f"📊 Отстояло 1/2: {half_hold_numbers}\n"
        f"📊 Отстояло 2/2: {full_hold_numbers}\n"
        f"📊 Отстояло 2/2+: {extra_hold_numbers}\n"
    )
    
    # Формируем кнопки
    markup = InlineKeyboardMarkup()
    
    # Кнопка блокировки/разблокировки
    if is_blocked:
        markup.add(InlineKeyboardButton("✅ Разблокировать в боте", callback_data=f"unblock_user_{user_id}"))
    else:
        markup.add(InlineKeyboardButton("❌ Заблокировать в боте", callback_data=f"block_user_{user_id}"))
    
    # Кнопка "Выгнать из бота"
    markup.add(InlineKeyboardButton("🚪 Выгнать из бота", callback_data=f"kick_user_{user_id}"))
    
    # Кнопка запрета/разрешения сдачи номеров
    if can_submit_numbers:
        markup.add(InlineKeyboardButton("🚫 Запретить сдавание номеров", callback_data=f"disable_numbers_{user_id}"))
    else:
        markup.add(InlineKeyboardButton("✅ Разрешить сдавание номеров", callback_data=f"enable_numbers_{user_id}"))
    
    # Кнопки навигации
    markup.add(InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
    markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение: {e}")
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("block_user_"))
def block_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    user_id = int(call.data.split("_")[2])  # Убедимся, что user_id определён
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET BLOCKED = 1 WHERE ID = ?', (user_id,))
        conn.commit()
    
    try:
        bot.send_message(user_id, "🚫 Вас заблокировали в боте!")
    except:
        pass
    
    bot.answer_callback_query(call.id, f"Пользователь {user_id} заблокирован!")
    user_details(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("unblock_user_"))
def unblock_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET BLOCKED = 0 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "✅ Вас разблокировали в боте! Напишите /start")
    except:
        pass
    bot.answer_callback_query(call.id, f"Пользователь {user_id} разблокирован!")
    user_details(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("kick_user_"))
def kick_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_kick_{user_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="all_users_1")
    )
    bot.edit_message_text(
        f"⚠️ Выгнать и удалить все данные пользователя {user_id}?",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
#подтверждение кика из бота
@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_kick_"))
def confirm_kick_user(call):
    user_id = int(call.data.split("_")[2])
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM users WHERE ID = ?', (user_id,))
            cursor.execute('DELETE FROM requests WHERE ID = ?', (user_id,))
            cursor.execute('DELETE FROM numbers WHERE ID_OWNER = ?', (user_id,))
            cursor.execute('DELETE FROM withdraws WHERE ID = ?', (user_id,))
            cursor.execute('DELETE FROM checks WHERE USER_ID = ?', (user_id,))
            cursor.execute('DELETE FROM personal WHERE ID = ?', (user_id,))
            conn.commit()
            print(f"{user_id} полностью удалён.")
        try:
            bot.send_message(
                user_id,
                "🚪 Вас выгнали из бота! Вам нужно снова подать заявку на вступление. Напишите /start"
            )
        except:
            pass
        bot.answer_callback_query(call.id, f"Пользователь {user_id} выгнан из бота!")
        call.data = "all_users_1"
        show_all_users(call)
    except Exception as e:
        print(f"[ERROR] Ошибка при удалении пользователя {user_id}: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при удалении!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("disable_numbers_"))
def disable_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET CAN_SUBMIT_NUMBERS = 0 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "🚫 Вам запретили сдавать номера!")
    except:
        pass  
    bot.answer_callback_query(call.id, f"Пользователю {user_id} запрещено сдавать номера!")
    # Обновляем информацию о пользователе
    user_details(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("enable_numbers_"))
def enable_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET CAN_SUBMIT_NUMBERS = 1 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "✅ Вам разрешили сдавать номера!")
    except:
        pass
    bot.answer_callback_query(call.id, f"Пользователю {user_id} разрешено сдавать номера!")
    # Обновляем информацию о пользователе
    user_details(call)

#========================================================================================================================
#==================================================== КОД ДЛЯ ПРИНЯТИЯ ЗАЯВОК В БОТА===================================
#========================================================================================================================

@bot.callback_query_handler(func=lambda call: call.data == "pending_requests")
def pending_requests(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к заявкам!")
        return

    bot.answer_callback_query(call.id)

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        # Динамически создаём placeholders для config.ADMINS_ID
        admin_ids = config.ADMINS_ID
        if not admin_ids:
            query = 'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM personal WHERE TYPE = ?)'
            params = ('pending', 'moderator')
        else:
            admin_placeholders = ','.join('?' for _ in admin_ids)
            query = f'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM requests WHERE ID IN ({admin_placeholders}) OR ID IN (SELECT ID FROM personal WHERE TYPE = ?))'
            params = ('pending', *admin_ids, 'moderator')
        cursor.execute(query, params)
        pending_users = cursor.fetchall()

    admin_text = "🔔 <b>Заявки на вступления</b>\n\n"
    markup = types.InlineKeyboardMarkup()
    if pending_users:
        for user_id, reg_date in pending_users:
            try:
                # Используем bot.get_chat_member для получения объекта пользователя
                user = bot.get_chat_member(user_id, user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
                # Делаем username кликабельным
                username_link = f"<a href=\"tg://user?id={user_id}\">{username}</a>" if user.username else "Нет username"
            except Exception as e:
                print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
                username_link = "Неизвестный пользователь"

            admin_text += (
                f"👤 Пользователь ID: https://t.me/@id{user_id} (Зарегистрирован: {reg_date})\n"
                f"👤 Username: {username_link}\n"  # Используем кликабельную ссылку
            )
            # Добавляем кнопки "Одобрить" и "Отклонить"
            approve_button = types.InlineKeyboardButton(f"✅ Одобрить {user_id}", callback_data=f"approve_user_{user_id}")
            reject_button = types.InlineKeyboardButton(f"❌ Отклонить {user_id}", callback_data=f"reject_user_{user_id}")
            markup.row(approve_button, reject_button)
    else:
        admin_text += "📭 Нет новых заявок на вступление.\n"

    # Кнопки навигации
    markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    try:
        bot.edit_message_text(
            admin_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
        bot.send_message(call.message.chat.id, admin_text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pending_requests"))
def show_pending_requests(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра заявок!")
        return
    
    page = 1
    if "_" in call.data:
        try:
            page = int(call.data.split("_")[1])
            if page < 1:
                page = 1
        except (IndexError, ValueError):
            page = 1

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = "pending"')
        requests = cursor.fetchall()
    
    if not requests:
        text = "📭 Нет заявок на вступление."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup,
            disable_notification=True  # Отключаем уведомление
        )
        return
    
    # Пагинация
    items_per_page = 20
    total_pages = (len(requests) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_requests = requests[start_idx:end_idx]
    
    text = f"<b>📝 Заявки на вступление (страница {page}/{total_pages}):</b>\n\n"
    markup = types.InlineKeyboardMarkup()
    
    for user_id, last_request in page_requests:
        try:
            user = bot.get_chat_member(user_id, user_id).user
            username = f"@{user.username}" if user.username else "Нет username"
        except:
            username = "Неизвестный пользователь"
        
        text += (
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: {username}\n"
            f"📅 Дата заявки: {last_request}\n"
            f"────────────────────\n"
        )
        
        markup.row(
            types.InlineKeyboardButton(f"✅ Одобрить {user_id}", callback_data=f"approve_user_{user_id}"),
            types.InlineKeyboardButton(f"❌ Отклонить {user_id}", callback_data=f"reject_user_{user_id}")
        )
    
    # Проверяем лимит символов
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано, используйте пагинацию)"
    
    # Кнопки пагинации
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"pending_requests_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"pending_requests_{page+1}"))
        if row:
            markup.row(*row)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup,
            disable_notification=True  # Отключаем уведомление
        )
    except:
        bot.send_message(
            call.message.chat.id,
            text,
            parse_mode='HTML',
            reply_markup=markup,
            disable_notification=True  # Отключаем уведомление
        )

@bot.callback_query_handler(func=lambda call: call.data == "pending_requests")
def pending_requests(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к заявкам!")
        return

    bot.answer_callback_query(call.id)

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        # Динамически создаём placeholders для config.ADMINS_ID
        admin_ids = config.ADMINS_ID
        if not admin_ids:
            query = 'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM personal WHERE TYPE = ?)'
            params = ('pending', 'moderator')
        else:
            admin_placeholders = ','.join('?' for _ in admin_ids)
            query = f'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM requests WHERE ID IN ({admin_placeholders}) OR ID IN (SELECT ID FROM personal WHERE TYPE = ?))'
            params = ('pending', *admin_ids, 'moderator')
        cursor.execute(query, params)
        pending_users = cursor.fetchall()

    admin_text = "🔔 <b>Заявки на вступления</b>\n\n"
    markup = types.InlineKeyboardMarkup()
    if pending_users:
        for user_id, reg_date in pending_users:
            try:
                # Используем bot.get_chat_member для получения объекта пользователя
                user = bot.get_chat_member(user_id, user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
                # Делаем username кликабельным
                username_link = f"<a href=\"tg://user?id={user_id}\">{username}</a>" if user.username else "Нет username"
            except Exception as e:
                print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
                username_link = "Неизвестный пользователь"

            admin_text += (
                f"👤 Пользователь ID: https://t.me/@id{user_id} (Зарегистрирован: {reg_date})\n"
                f"👤 Username: {username_link}\n"  # Используем кликабельную ссылку
            )
            # Добавляем кнопки "Одобрить" и "Отклонить"
            approve_button = types.InlineKeyboardButton(f"✅ Одобрить {user_id}", callback_data=f"approve_user_{user_id}")
            reject_button = types.InlineKeyboardButton(f"❌ Отклонить {user_id}", callback_data=f"reject_user_{user_id}")
            markup.row(approve_button, reject_button)
    else:
        admin_text += "📭 Нет новых заявок на вступление.\n"

    # Кнопки навигации
    markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    try:
        bot.edit_message_text(
            admin_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
        bot.send_message(call.message.chat.id, admin_text, parse_mode='HTML', reply_markup=markup)


#=======================================================================================
#=======================================================================================
#===================================ГРУППЫ==============================================
#=======================================================================================
#=======================================================================================
#=======================================================================================

@bot.callback_query_handler(func=lambda call: call.data == "groups")
def groups_menu(call):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для управления группами!")
        return
    
    text = "<b>👥 Управление группами</b>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Добавить группу", callback_data="add_group"))
    markup.add(types.InlineKeyboardButton("➖ Удалить группу", callback_data="remove_group"))
    markup.add(types.InlineKeyboardButton("📊 Статистика", callback_data="group_statistics"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

#ДОБАВЛЕНИЕ ИД ГРУППЫ ДЛЯ ПРИНЯТИЕ НОМЕРОВ
@bot.callback_query_handler(func=lambda call: call.data == "add_group")
def add_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="groups"))
    
    msg = bot.edit_message_text(
        "📝 Введите ID группы (например, -1002453887941):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    
    bot.register_next_step_handler(msg, process_group_id_add)

def process_group_id_add(message):
    try:
        group_id = int(message.text.strip())
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM groups WHERE ID = ?', (group_id,))
            if cursor.fetchone():
                bot.reply_to(message, "❌ Эта группа уже зарегистрирована для принятия номеров!")
                return
            cursor.execute('INSERT INTO groups (ID, NAME) VALUES (?, ?)', (group_id, f"{group_id}"))
            conn.commit()
        bot.reply_to(message, f"✅ Группа с ID {group_id} успешно добавлена для принятия номеров!")
        # Возвращаем в админ-панель
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)
    except ValueError:
        bot.reply_to(message, "❌ Неверный формат ID! Введите числовое значение.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при добавлении группы: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "remove_group")
def remove_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, NAME FROM groups')
        groups = cursor.fetchall()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="groups"))
    if not groups:
        bot.edit_message_text(
            "📭 Нет зарегистрированных групп для удаления.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        return
    markup = types.InlineKeyboardMarkup()
    for group_id, group_name in groups:
        markup.add(types.InlineKeyboardButton(f"➖ {group_name} (ID: {group_id})", callback_data=f"confirm_remove_{group_id}"))
    markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
    bot.edit_message_text(
        "<b>➖ Выберите группу для удаления:</b>",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_remove_"))
def confirm_remove_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    group_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        group = cursor.fetchone()
        if not group:
            bot.answer_callback_query(call.id, "❌ Группа не найдена!")
            return
        group_name = group[0]
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Подтвердить удаление", callback_data=f"remove_confirmed_{group_id}"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="remove_group"))
        bot.edit_message_text(
            f"<b>Подтвердите удаление группы:</b>\n🏠 {group_name} (ID: {group_id})",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_confirmed_"))
def remove_confirmed_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    group_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM groups WHERE ID = ?', (group_id,))
        conn.commit()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
    bot.edit_message_text(
        f"✅ Группа с ID {group_id} успешно удалена!",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id, "Группа удалена!")

#СТАТИСТИКА ГРУПП

@bot.callback_query_handler(func=lambda call: call.data.startswith("group_statistics"))
def group_statistics(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра статистики!")
        return

    page = 1
    if "_" in call.data:
        try:
            page = int(call.data.split("_")[1])
            if page < 1:
                page = 1
        except (IndexError, ValueError):
            page = 1

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, NAME FROM groups ORDER BY NAME')
        groups = cursor.fetchall()

    if not groups:
        text = "📭 Нет доступных групп."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        return

    items_per_page = 5
    total_pages = (len(groups) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_groups = groups[start_idx:end_idx]

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT HOLD_TIME, PRICE_ADM FROM settings')
        result = cursor.fetchone()
        hold_time_result, price_adm_result = result if result else (5, 4.5)
        HOLD_TIME_MINUTES = int(hold_time_result) if hold_time_result else 5
        PRICE_ADM = float(price_adm_result) if price_adm_result else 4.5

    text = f"<b>📊 Список групп (страница {page}/{total_pages}):</b>\n\n"
    for group_id, group_name in page_groups:
        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = ?', (group_id, 'moderator'))
        moderator_count = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT COUNT(*) 
            FROM numbers n
            WHERE n.GROUP_CHAT_ID = ? AND n.STATUS IN ('отстоял 1/2', 'отстоял 2/2', 'отстоял 2/2+ холд')
        ''', (group_id,))
        total_numbers = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT n.TAKE_DATE, n.SHUTDOWN_DATE, n.STATUS
            FROM numbers n
            WHERE n.GROUP_CHAT_ID = ? AND n.STATUS IN ('отстоял 1/2', 'отстоял 2/2', 'отстоял 2/2+ холд')
            AND n.TAKE_DATE NOT IN ('0', '1') AND n.SHUTDOWN_DATE NOT IN ('0', '1')
        ''', (group_id,))
        numbers_data = cursor.fetchall()
        total_minutes = 0.0
        total_earnings = 0.0
        MAX_HOLDS = 2

        for take_date, shutdown_date, status in numbers_data:
            try:
                take_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                shutdown_time = datetime.strptime(shutdown_date, "%Y-%m-%d %H:%M:%S")
                minutes = (shutdown_time - take_time).total_seconds() / 60
                minutes = max(0, minutes)  # Исключаем отрицательное время
                if status == 'отстоял 1/2' and minutes < HOLD_TIME_MINUTES:
                    minutes = HOLD_TIME_MINUTES  # Минимальное время для 1 холда
                total_minutes += minutes
                holds_count = max(1, int(minutes / HOLD_TIME_MINUTES)) if status == 'отстоял 1/2' else int(minutes / HOLD_TIME_MINUTES)
                earnings = min(min(holds_count, MAX_HOLDS) * PRICE_ADM, 12.0) if holds_count > 0 else 0.0
                total_earnings += earnings
            except ValueError as e:
                print(f"[DEBUG] Ошибка парсинга для группы {group_id}: {e}")
                continue

        text += f"🏠 <b>{group_name}</b>\n"
        text += "────────────────────\n"
        text += f"👥 Модераторов: <code>{moderator_count}</code>\n"
        text += f"📱 Успешных номеров: <code>{total_numbers}</code>\n"
        text += f"⏳ Отстояло минут: <code>{total_minutes:.1f}</code>\n"
        text += f"💰 Должны заплатить: <code>${total_earnings:.2f}</code>\n\n"

    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано из-за лимита, используйте пагинацию)"

    markup = types.InlineKeyboardMarkup()
    for group_id, group_name in page_groups:
        markup.add(types.InlineKeyboardButton(f"📊 {group_name[:20]}", callback_data=f"group_stats_{group_id}_1"))

    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"group_statistics_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"group_statistics_{page+1}"))
        markup.row(*row)

    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception as e:
        logging.error(f"Ошибка при обновлении сообщения group_statistics: {e}")
        bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

        
@bot.callback_query_handler(func=lambda call: call.data.startswith("group_stats_"))
def show_group_stats(call):
    bot.answer_callback_query(call.id)
    
    # Извлекаем ID группы и номер страницы из callback_data
    parts = call.data.split("_")
    group_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 1
    ITEMS_PER_PAGE = 10

    with db.get_db() as conn:
        cursor = conn.cursor()
        # Получаем название группы
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        group_name = cursor.fetchone()
        if not group_name:
            bot.edit_message_text("❌ Группа не найдена.", call.message.chat.id, call.message.message_id, parse_mode='HTML')
            return
        group_name = group_name[0]

        # Подсчёт модераторов в группе
        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = ?', (group_id, 'moderator'))
        total_moderators = cursor.fetchone()[0]

        # Получаем все номера для группы с нужными статусами
        cursor.execute('''
            SELECT NUMBER, TAKE_DATE, SHUTDOWN_DATE, STATUS
            FROM numbers
            WHERE GROUP_CHAT_ID = ? AND STATUS IN ('отстоял 1/2', 'отстоял 2/2', 'отстоял 2/2+ холд')
            AND TAKE_DATE NOT IN ('0', '1') AND SHUTDOWN_DATE NOT IN ('0', '1')
            ORDER BY SHUTDOWN_DATE DESC
        ''', (group_id,))
        all_numbers = cursor.fetchall()
        total_numbers = len(all_numbers)

        # Рассчитываем пагинацию
        total_pages = (total_numbers + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE if total_numbers > 0 else 1
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        page_numbers = all_numbers[start_idx:end_idx]

        # Получаем настройки для расчёта
        cursor.execute('SELECT HOLD_TIME, PRICE_ADM FROM settings')
        hold_time_result, price_adm_result = cursor.fetchone() or (5, 4.5)
        HOLD_TIME_MINUTES = int(hold_time_result) if hold_time_result else 5
        PRICE_ADM = float(price_adm_result) if price_adm_result else 4.5

        # Формируем текст сообщения
        stats_text = (
            f"📊 <b>Статистика группы: {group_name}</b>\n\n"
            f"👥 Модераторов: <code>{total_moderators}</code>\n"
            f"📱 Успешных номеров: <code>{total_numbers}</code>\n\n"
            f"📋 Список номеров (страница {page}/{total_pages}):\n\n"
        )
        if not page_numbers:
            stats_text += "📱 В группе пока нет отстоявших номеров.\n"
        else:
            for number, take_date, shutdown_date, status in page_numbers:
                try:
                    take_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                    shutdown_time = datetime.strptime(shutdown_date, "%Y-%m-%d %H:%M:%S")
                    minutes = (shutdown_time - take_time).total_seconds() / 60  # Реальное время в минутах
                    # Если минут меньше 0 (ошибка дат), используем 0
                    minutes = max(0, minutes)
                    # Для статуса 'отстоял 1/2' время считается с момента смены статуса
                    if status == 'отстоял 1/2' and minutes < HOLD_TIME_MINUTES:
                        minutes = HOLD_TIME_MINUTES  # Минимальное время для 1 холда
                    time_str = f"{int(minutes)}"  # Реальное время без округления до 30
                    holds_count = max(1, int(minutes / HOLD_TIME_MINUTES)) if status == 'отстоял 1/2' else int(minutes / HOLD_TIME_MINUTES)
                    hold_str = f"{holds_count}"
                except ValueError as e:
                    print(f"[DEBUG] Ошибка парсинга для номера {number}: {e}")
                    time_str = "Неизвестно"
                    hold_str = "Неизвестно"
                
                stats_text += f"📱 Номер: <code>{number}</code>\n"
                stats_text += f"Простоял: {time_str} минут\n"
                stats_text += f"Холд: {hold_str}\n\n"

        TELEGRAM_MESSAGE_LIMIT = 4096
        if len(stats_text) > TELEGRAM_MESSAGE_LIMIT:
            stats_text = stats_text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано из-за лимита)"

        # Создаём кнопки для пагинации
        markup = types.InlineKeyboardMarkup()
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Пред", callback_data=f"group_stats_{group_id}_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("След ➡️", callback_data=f"group_stats_{group_id}_{page+1}"))
        if nav_buttons:
            markup.add(*nav_buttons)
        markup.add(types.InlineKeyboardButton("🔙 К списку групп", callback_data="group_statistics_1"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

        # Обновляем сообщение
        bot.edit_message_text(
            stats_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )

#================================================
#=======================РАССЫЛКА=================
#================================================

broadcast_state = {}

@bot.callback_query_handler(func=lambda call: call.data == "broadcast")
def request_broadcast_message(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для рассылки!")
        return
    # Устанавливаем состояние: рассылка активна
    broadcast_state[call.from_user.id] = {"active": True}
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    msg = bot.edit_message_text(
        "📢 Введите текст для рассылки:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_broadcast_message)

def process_broadcast_message(message):
    user_id = message.from_user.id
    # Проверяем, активна ли рассылка
    if user_id not in broadcast_state or not broadcast_state[user_id].get("active", False):
        # Если рассылка отменена, просто игнорируем сообщение
        return
    if user_id not in config.ADMINS_ID:
        bot.reply_to(message, "❌ У вас нет прав для рассылки!")
        return
    broadcast_text = message.text
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.ID
                FROM users u
                LEFT JOIN personal p ON u.ID = p.ID
                WHERE p.TYPE IS NULL OR p.TYPE NOT IN ('moder', 'ADMIN')
            ''')
            users = cursor.fetchall()
        
        success = 0
        failed = 0
        for user in users:
            try:
                bot.send_message(user[0], broadcast_text)
                success += 1
                time.sleep(0.05)  # Задержка для лимитов Telegram
            except Exception as e:
                logging.error(f"Не удалось отправить сообщение пользователю {user[0]}: {e}")
                failed += 1
        
        stats_text = (
            f"📊 <b>Статистика рассылки:</b>\n\n"
            f"✅ Успешно отправлено: {success}\n"
            f"❌ Не удалось отправить: {failed}\n"
            f"👥 Всего пользователей: {len(users)}"
        )
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Новая рассылка", callback_data="broadcast"))
        markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        markup.add(InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id, stats_text, reply_markup=markup, parse_mode='HTML')
    
    except Exception as e:
        logging.error(f"Ошибка при выполнении рассылки: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при выполнении рассылки.")
    finally:
        # Очищаем состояние после завершения
        broadcast_state.pop(user_id, None)

#================================================================================================
#================================================================================================
#===================================== ВСЕ НОМЕРА ===============================================
#================================================================================================
#================================================================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("all_numbers"))
def show_all_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра всех номеров!")
        return
    
    bot.answer_callback_query(call.id)
    page = int(call.data.split("_")[2]) if len(call.data.split("_")) > 2 else 1
    numbers_per_page = 5

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM numbers')
        total_numbers = cursor.fetchone()[0]
        
        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)
        
        offset = (page - 1) * numbers_per_page
        cursor.execute('''
            SELECT n.NUMBER, n.STATUS, n.TAKE_DATE, n.SHUTDOWN_DATE, n.ID_OWNER, 
                   n.CONFIRMED_BY_MODERATOR_ID, n.GROUP_CHAT_ID, n.TG_NUMBER, u.USERNAME, n.HOLDS_COUNT
            FROM numbers n
            LEFT JOIN users u ON n.ID_OWNER = u.ID
            ORDER BY n.TAKE_DATE DESC
            LIMIT ? OFFSET ?
        ''', (numbers_per_page, offset))
        numbers = cursor.fetchall()
    
    # Получаем PRICE_ADM и HOLD_TIME_MINUTES из настроек
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT HOLD_TIME, PRICE_ADM FROM settings')
        result = cursor.fetchone()
        hold_time_result, price_adm_result = result if result else (5, 4.5)
    HOLD_TIME_MINUTES = int(hold_time_result) if hold_time_result else 5
    PRICE_ADM = float(price_adm_result) if price_adm_result else 4.5

    numbers_text = f"📋 <b>Все номера (страница {page}/{total_pages})</b>\n\n"
    if not numbers:
        numbers_text += "📭 Номера отсутствуют."
    else:
        from datetime import datetime
        current_time = datetime.now()
        
        for number, status, take_date, shutdown_date, owner_id, confirmed_by_moderator_id, group_chat_id, tg_number, username, holds_count in numbers:
            group_name = db.get_group_name(group_chat_id) if group_chat_id else "Не указана"
            take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else str(current_time)
            
            # Расчет времени отстоя с округлением до 30 минут
            if take_date_str != "Неизвестно" and shutdown_date_str != "Неизвестно":
                take_time = datetime.strptime(take_date_str, "%Y-%m-%d %H:%M:%S")
                shutdown_time = datetime.strptime(shutdown_date_str, "%Y-%m-%d %H:%M:%S")
                minutes = (shutdown_time - take_time).total_seconds() / 60
                rounded_minutes = (minutes // 30) * 30
            else:
                rounded_minutes = 0.0
            
            # Расчет выплаты на основе PRICE_ADM
            total_earnings = 0.0
            MAX_HOLDS = 2
            if status.startswith("отстоял"):
                holds_count = int(holds_count) if holds_count.isdigit() else 0
                effective_holds = min(holds_count, MAX_HOLDS)
                if status == 'отстоял 1/2' and rounded_minutes >= HOLD_TIME_MINUTES:
                    total_earnings = min(PRICE_ADM, 6.0)  # Максимум 6$ за 1 холд
                elif status in ['отстоял 2/2', 'отстоял 2/2+ холд']:
                    total_earnings = min(effective_holds * PRICE_ADM, 12.0)  # Максимум 12$ за 2 холда
            
            moderator_info = f"Модератор: @{confirmed_by_moderator_id}" if confirmed_by_moderator_id else "Модератор: Не назначен"
            username_display = f"@{username}" if username and username != "Не указан" else "Без username"
            
            numbers_text += (
                f"📱 Номер: <code>{number}</code>\n"
                f"👤 Владелец: <a href=\"tg://user?id={owner_id}\">{owner_id}</a> ({username_display})\n"
                f"📊 Статус: {status}\n"
                f"🟢 Взято: {take_date_str}\n"
                f"🔴 Отстоял: {shutdown_date_str if shutdown_date != '0' else 'Ещё активен'}\n"
                f"⏳ Отстояло минут: <code>{rounded_minutes:.1f}</code>\n"
                f"🏷 Группа: {group_name}\n"
                f"📱 ВЦ: {tg_number or 'Не указан'}\n"
                f"{moderator_info}\n"
                f"💰 Выплата: ${total_earnings:.2f}\n\n"
            )
    
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(numbers_text) > TELEGRAM_MESSAGE_LIMIT:
        numbers_text = numbers_text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано из-за лимита)"

    markup = types.InlineKeyboardMarkup()
    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_numbers_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_numbers_{page+1}"))
        markup.add(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(
            numbers_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
        bot.send_message(
            call.message.chat.id,
            numbers_text,
            reply_markup=markup,
            parse_mode='HTML'
        )

def show_numbers_page(call, page):
    user_id = call.from_user.id
    if user_id not in numbers_data_cache:
        bot.answer_callback_query(call.id, "❌ Данные устарели, пожалуйста, запросите список заново!")
        return
    
    numbers = numbers_data_cache[user_id]
    items_per_page = 5
    total_items = len(numbers)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    
    if page < 0 or page >= total_pages:
        bot.answer_callback_query(call.id, "❌ Страница недоступна!")
        return
    
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)
    page_numbers = numbers[start_idx:end_idx]
    
    text = f"<b>📱 Список всех номеров (Страница {page + 1} из {total_pages}):</b>\n\n"
    if not page_numbers:
        text += "📭 Номера отсутствуют."
    else:
        for number, take_date, shutdown_date, owner_id, group_name, username, holds_count in page_numbers:
            group_info = f"👥 Группа: {group_name}" if group_name else "👥 Группа: Не указана"
            user_info = f"🆔 Пользователь: {owner_id}" if owner_id else "🆔 Пользователь: Не указан"
            username_display = f"@{username}" if username and username != "Не указан" else "Без username"
            hold_info = "отстоял"
            total_payout = min(holds_count, 2) * 2.0
            if holds_count == 1:
                hold_info = "отстоял 1/2"
            elif holds_count == 2:
                hold_info = "отстоял 2/2"
            elif holds_count > 2:
                hold_info = "отстоял 2/2+ холд"
            text += (
                f"📞 <code>{number}</code>\n"
                f"{user_info} ({username_display})\n"
                f"{group_info}\n"
                f"📅 Взят: {take_date}\n"
                f"📴 Отключён: {shutdown_date or 'Ещё активен'}\n"
                f"📊 Статус: {hold_info}\n"
                f"💰 Выплата: ${total_payout:.2f}\n\n"
            )
    
    markup = types.InlineKeyboardMarkup()
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"numbers_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"numbers_page_{page+1}"))
    if nav_buttons:
        markup.row(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        print(f"Удалено старое сообщение {call.message.message_id} в чате {call.message.chat.id}")
    except Exception as e:
        print(f"Ошибка при удалении старого сообщения: {e}")
    
    bot.send_message(
        call.message.chat.id,
        text,
        parse_mode='HTML',
        reply_markup=markup
    )
    print(f"Страница {page + 1} отправлена успешно")

@bot.callback_query_handler(func=lambda call: call.data.startswith("numbers_page_"))
def numbers_page_callback(call):
    page = int(call.data.split("_")[2])
    show_numbers_page(call, page)


#================================================================================================
#================================================================================================
#========================================== ИНФОРМАЦИЯ О ЛЮДЯХ ==================================
#================================================================================================
#===============================================================================================


@bot.callback_query_handler(func=lambda call: call.data.startswith("user_numbers_all"))
def show_user_numbers(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    db.update_last_activity(user_id)

    try:
        # Разбираем callback_data для пагинации
        parts = call.data.split("_")
        page = 1  # Значение по умолчанию
        if len(parts) > 2 and parts[2].isdigit():
            page = int(parts[2])

        with db.get_db() as conn:
            cursor = conn.cursor()
            # Получаем всех пользователей с номерами и их предупреждениями
            cursor.execute('''
                SELECT DISTINCT n.ID_OWNER, u.USERNAME, u.ID, u.WARNINGS
                FROM numbers n
                JOIN users u ON n.ID_OWNER = u.ID
                ORDER BY u.REG_DATE DESC
            ''')
            users = cursor.fetchall()

            if not users:
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
                bot.edit_message_text(
                    "📭 Нет пользователей с номерами.",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return

            users_per_page = 5
            total_pages = max(1, (len(users) + users_per_page - 1) // users_per_page)
            page = max(1, min(page, total_pages))  # Ограничиваем страницу
            offset = (page - 1) * users_per_page
            paginated_users = users[offset:offset + users_per_page]

            # Формируем текст
            users_text = f"📋 <b>Список пользователей (страница {page}/{total_pages})</b>\n\n"
            for owner_id, username, user_id, warnings in paginated_users:
                cursor.execute('''
                    SELECT COUNT(*), SUM(CASE WHEN n.STATUS = "отстоял" THEN 1 ELSE 0 END)
                    FROM numbers n
                    WHERE n.ID_OWNER = ?
                ''', (owner_id,))
                total_numbers, confirmed_numbers = cursor.fetchone() or (0, 0)
                price = db.get_user_price(owner_id) or 2.0
                earnings = confirmed_numbers * price if confirmed_numbers else 0.0

                users_text += (
                    f"👤 <a href='tg://user?id={user_id}'>{username or 'Не указан'}</a> (ID: {user_id})\n"
                    f"💰 Заработано: ${earnings:.2f}\n"
                    f"📊 Отстояло номеров: {confirmed_numbers or 0} (всего: {total_numbers})\n"
                    f"⚠️ Предупреждения: {warnings or 0}\n\n"
                )

            # Ограничение длины сообщения
            TELEGRAM_MESSAGE_LIMIT = 4096
            if len(users_text) > TELEGRAM_MESSAGE_LIMIT:
                users_text = users_text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано из-за лимита)"

            # Формируем разметку
            markup = InlineKeyboardMarkup()
            if total_pages > 1:
                nav_buttons = []
                if page > 1:
                    nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"user_numbers_all_{page-1}"))
                if page < total_pages:
                    nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"user_numbers_all_{page+1}"))
                if nav_buttons:
                    markup.add(*nav_buttons)

            # Кнопки для выбора пользователей
            for owner_id, username, _, _ in paginated_users:
                username_display = username or f"ID: {owner_id}"
                markup.add(InlineKeyboardButton(username_display, callback_data=f"admin_user_details_{owner_id}_{page}"))

            markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))

            try:
                bot.edit_message_text(
                    users_text,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
                safe_send_message(
                    call.message.chat.id,
                    users_text,
                    parse_mode='HTML',
                    reply_markup=markup
                )

    except Exception as e:
        print(f"[ERROR] Ошибка в show_user_numbers: {e}")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        bot.edit_message_text(
            "❌ Произошла ошибка при загрузке списка пользователей.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_user_details_"))
def admin_show_user_details(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    db.update_last_activity(user_id)

    try:
        # Разбираем callback_data для получения owner_id и страницы
        parts = call.data.split("_")
        owner_id = int(parts[3])
        page = 1  # Значение по умолчанию
        if len(parts) > 4 and parts[4].isdigit():
            page = int(parts[4])

        with db.get_db() as conn:
            cursor = conn.cursor()
            # Получаем информацию о пользователе, включая предупреждения
            cursor.execute('SELECT USERNAME, WARNINGS FROM users WHERE ID = ?', (owner_id,))
            user = cursor.fetchone()
            if not user:
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("🔙 Назад", callback_data=f"user_numbers_all_{page}"))
                bot.edit_message_text(
                    f"❌ Пользователь ID {owner_id} не найден!",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return
            username, warnings = user or ("Не указан", 0)

            # Получаем все номера пользователя
            cursor.execute('''
                SELECT NUMBER, HOLDS_COUNT, TAKE_DATE, SHUTDOWN_DATE
                FROM numbers
                WHERE ID_OWNER = ?
                ORDER BY TAKE_DATE DESC
            ''', (owner_id,))
            numbers = cursor.fetchall()

            if not numbers:
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("🔙 Назад", callback_data=f"user_numbers_all_{page}"))
                bot.edit_message_text(
                    f"📭 У пользователя @{username} (ID: {owner_id}) нет номеров.",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return

            numbers_per_page = 5
            total_pages = max(1, (len(numbers) + numbers_per_page - 1) // numbers_per_page)
            page = max(1, min(page, total_pages))
            offset = (page - 1) * numbers_per_page
            paginated_numbers = numbers[offset:offset + numbers_per_page]

            # Формируем текст
            user_text = (
                f"👤 Пользователь: <a href='tg://user?id={owner_id}'>@{username}</a> (ID: {owner_id})\n"
                f"⚠️ Предупреждения: {warnings or 0}\n\n"
                f"📋 Номера (страница {page}/{total_pages}):\n"
            )

            current_time = datetime(2025, 7, 2, 17, 16)  # Текущее время: 2 июля 2025, 17:16 +04
            for number, holds_count, take_date, shutdown_date in paginated_numbers:
                # Если HOLDS_COUNT = 0, время отстоя равно 0
                if holds_count == 0:
                    user_text += (
                        f"📱 Номер: <code>{number}</code>\n"
                        f"🔢 Какой холд: {holds_count or 0}\n"
                        f"⏳ Сколько отстоял: 0 часов 0 минут\n"
                        f"---\n"
                    )
                    continue

                # Проверяем и обрабатываем даты
                try:
                    take_date = datetime.strptime(take_date, '%Y-%m-%d %H:%M:%S') if take_date and take_date != '0' else current_time
                except ValueError:
                    print(f"[ERROR] Неверный формат TAKE_DATE для номера {number}: {take_date}")
                    take_date = current_time
                
                try:
                    end_date = datetime.strptime(shutdown_date, '%Y-%m-%d %H:%M:%S') if shutdown_date and shutdown_date != '0' else current_time
                except ValueError:
                    print(f"[ERROR] Неверный формат SHUTDOWN_DATE для номера {number}: {shutdown_date}")
                    end_date = current_time

                # Время отстоя: от TAKE_DATE (начало первого холда) до SHUTDOWN_DATE или текущего времени
                time_diff = end_date - take_date
                total_seconds = int(time_diff.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                user_text += (
                    f"📱 Номер: <code>{number}</code>\n"
                    f"🔢 Какой холд: {holds_count or 0}\n"
                    f"⏳ Сколько отстоял: {hours} часов {minutes} минут\n"
                    f"---\n"
                )

            # Ограничение длины сообщения
            TELEGRAM_MESSAGE_LIMIT = 4096
            if len(user_text) > TELEGRAM_MESSAGE_LIMIT:
                user_text = user_text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано из-за лимита)"

            # Формируем разметку
            markup = InlineKeyboardMarkup()
            if total_pages > 1:
                nav_buttons = []
                if page > 1:
                    nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_user_details_{owner_id}_{page-1}"))
                if page < total_pages:
                    nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"admin_user_details_{owner_id}_{page+1}"))
                if nav_buttons:
                    markup.add(*nav_buttons)

            markup.add(InlineKeyboardButton("🔙 Назад", callback_data=f"user_numbers_all_{page}"))

            try:
                bot.edit_message_text(
                    user_text,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
                safe_send_message(
                    call.message.chat.id,
                    user_text,
                    parse_mode='HTML',
                    reply_markup=markup
                )

    except Exception as e:
        print(f"[ERROR] Ошибка в admin_show_user_details: {e}")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 Назад", callback_data=f"user_numbers_all_{page}"))
        bot.edit_message_text(
            "❌ Произошла ошибка при загрузке информации о пользователе.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_details_"))
def show_user_details(call):
    try:
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        db.update_last_activity(user_id)

        # Разбираем callback_data
        parts = call.data.split("_")
        if len(parts) < 2 or not parts[1].isdigit():  # Проверяем, что второй элемент — число
            bot.answer_callback_query(call.id, "❌ Некорректный запрос. Обратитесь к администратору.")
            print(f"[DEBUG] Некорректный call.data: {call.data}")
            return

        target_user_id = int(parts[1])  # Извлекаем user_id как второй элемент

        if user_id not in config.ADMINS_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра деталей!")
            return

        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT USERNAME, ID, BALANCE, REG_DATE, IS_AFK, WARNINGS FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            if not user:
                bot.answer_callback_query(call.id, "❌ Пользователь не найден!")
                return

            username, user_id, balance, reg_date, is_afk, warnings = user
            cursor.execute('''
                SELECT COUNT(*), SUM(CASE WHEN STATUS = "отстоял" THEN 1 ELSE 0 END)
                FROM numbers
                WHERE ID_OWNER = ?
            ''', (target_user_id,))
            total_numbers, confirmed_numbers = cursor.fetchone() or (0, 0)
            price = db.get_user_price(target_user_id) or 2.0
            earnings = confirmed_numbers * price if confirmed_numbers else 0.0

            details_text = (
                f"👤 <b>Детали пользователя</b>\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"👤 Username: {username or 'Не указан'}\n"
                f"💰 Баланс: ${balance:.2f}\n"
                f"💸 Заработано: ${earnings:.2f}\n"
                f"📅 Регистрация: {reg_date}\n"
                f"📊 Отстояло номеров: {confirmed_numbers or 0} (всего: {total_numbers})\n"
                f"🎭 Статус: {'🟢 Активен' if not is_afk else '🔴 В АФК'}\n"
                f"⚠️ Предупреждения: {warnings or 0}\n"
            )

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Назад", callback_data=f"user_numbers_all_1"))
            markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))

            try:
                bot.edit_message_text(details_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
                bot.send_message(call.message.chat.id, details_text, reply_markup=markup, parse_mode='HTML')

    except Exception as e:
        print(f"[ERROR] Ошибка в show_user_details: {e}")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        bot.edit_message_text("❌ Произошла ошибка при загрузке деталей пользователя.", call.message.chat.id, call.message.message_id, reply_markup=markup)
#=====
# ПОИСК ИНФОРМАЦИИ О НОМЕРЕ

@bot.callback_query_handler(func=lambda call: call.data == "search_number")
def search_number_callback(call):
    user_id = call.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Отправляем сообщение с просьбой ввести номер
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_main"))
    msg = bot.edit_message_text(
        "📱 Пожалуйста, введите номер телефона в формате +79991234567 (используйте reply на это сообщение):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode='HTML'
    )
    
    # Регистрируем следующий шаг для обработки введённого номера
    bot.register_next_step_handler(msg, process_search_number, call.message.chat.id, msg.message_id)


def process_search_number(message, original_chat_id, original_message_id):
    user_id = message.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Проверяем, что сообщение является ответом (reply)
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода номера!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Нормализуем введённый номер
    number_input = message.text.strip()
    normalized_number = is_russian_number(number_input)
    if not normalized_number:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат номера! Используйте российский номер, например: +79991234567",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Удаляем сообщение с введённым номером
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с номером {normalized_number}: {e}")
    
    # Получаем HOLD_TIME и PRICE_ADM из настроек
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT HOLD_TIME, PRICE_ADM FROM settings')
        result = cursor.fetchone()
        hold_time_result, price_adm_result = result if result else (30, 4.5)
    HOLD_TIME_MINUTES = int(hold_time_result) if hold_time_result else 30
    PRICE_ADM = float(price_adm_result) if price_adm_result else 4.5
    
    # Ищем информацию о номере в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT NUMBER, ID_OWNER, STATUS, TAKE_DATE, SHUTDOWN_DATE, CONFIRMED_BY_MODERATOR_ID, TG_NUMBER, SUBMIT_DATE, GROUP_CHAT_ID, HOLDS_COUNT
            FROM numbers
            WHERE NUMBER = ?
        ''', (normalized_number,))
        number_data = cursor.fetchone()
    
    # Формируем сообщение с информацией о номере
    if number_data:
        number, owner_id, status, take_date, shutdown_date, confirmed_by_moderator_id, tg_number, submit_date, group_chat_id, holds_count = number_data
        
        # Получаем имя группы
        group_name = db.get_group_name(group_chat_id) if group_chat_id else "Не указана"
        
        # Формируем отображаемые даты
        take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
        shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
        
        # Расчёт времени работы
        from datetime import datetime
        total_earnings = 0.0
        minutes_worked_str = "0:00"
        if take_date_str != "Неизвестно" and shutdown_date_str != "Не завершён":
            take_time = datetime.strptime(take_date_str, "%Y-%m-%d %H:%M:%S")
            shutdown_time = datetime.strptime(shutdown_date_str, "%Y-%m-%d %H:%M:%S")
            minutes = (shutdown_time - take_time).total_seconds() / 60
            rounded_minutes = (minutes // 30) * 30  # Для проверки холда
            hours = int(minutes // 60)  # Округление до часа вниз
            minutes_worked_str = f"{hours}:00"
            # Расчёт выплаты
            MAX_HOLDS = 2
            holds_count = int(holds_count) if holds_count else 0
            effective_holds = min(holds_count, MAX_HOLDS)
            if status == 'слёт 1/2 холд' and rounded_minutes >= HOLD_TIME_MINUTES:
                total_earnings = min(PRICE_ADM, 6.0)
            elif status in ['отстоял 2/2', 'слёт 2/2 холд', 'отстоял 2/2+ холд']:
                total_earnings = min(effective_holds * PRICE_ADM, 12.0)
        
        # Получаем username модератора
        moderator_info = "Модератор: Не назначен"
        if confirmed_by_moderator_id:
            try:
                moderator_info_data = bot.get_chat_member(message.chat.id, confirmed_by_moderator_id).user
                moderator_username = f"@{moderator_info_data.username}" if moderator_info_data.username else f"ID {confirmed_by_moderator_id}"
                moderator_info = f"Модератор: {moderator_username}"
            except Exception as e:
                print(f"[ERROR] Не удалось получить username модератора {confirmed_by_moderator_id}: {e}")
                moderator_info = f"Модератор: ID {confirmed_by_moderator_id}"
        
        # Получаем username владельца
        owner_info = f"👤 Владелец: ID {owner_id}"
        try:
            owner_data = bot.get_chat_member(message.chat.id, owner_id).user
            owner_username = f"@{owner_data.username}" if owner_data.username else f"ID {owner_id}"
            owner_info = f"👤 Владелец: {owner_username}"
        except Exception as e:
            print(f"[ERROR] Не удалось получить username владельца {owner_id}: {e}")
        
        # Формируем текст в нужном формате
        text = (
            f"📱 Номер: <code>{number}</code>\n"
            f"{owner_info}\n"
            f"📊 Статус: {status}\n"
            f"🟢 Взято: {take_date_str.split(' ')[1][:5]}\n"
            f"⏱ Сколько простоял: {minutes_worked_str}\n"
            f"{moderator_info}\n"
            f"🏷 Группа: {group_name}\n"
            f"📱 ВЦ: {tg_number or 'Не указан'}\n"
        )
    else:
        text = f"❌ Номер <code>{normalized_number}</code> не найден в базе данных."
    
    # Обновляем исходное сообщение
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔍 Поиск другого номера", callback_data="search_number"))
    markup.add(types.InlineKeyboardButton("🔙 В админ панель", callback_data="admin_panel"))
    
    try:
        bot.edit_message_text(
            text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        bot.send_message(
            original_chat_id,
            text,
            parse_mode='HTML',
            reply_markup=markup
        )


def check_time():
    while True:
        current_time = datetime.now().strftime("%H:%M")
        if current_time == config.CLEAR_TIME:
            clear_database()
            time.sleep(61)
        time.sleep(30)




#ПОИСК НОМЕРА ИНФОРМАЦИЯ О НЁМ

def run_bot():
    time_checker = threading.Thread(target=check_time)
    time_checker.daemon = True
    time_checker.start()
    bot.polling(none_stop=True, skip_pending=True)
class AdminStates(StatesGroup):
    waiting_for_number = State()


#============================

@bot.callback_query_handler(func=lambda call: call.data == "change_hold_time")
def change_hold_time_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("⏱ Введите новое время холда (в минутах, например: 5):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_hold_time)

def process_change_hold_time(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_time = int(message.text)
            if new_time <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET HOLD_TIME = ?', (new_time,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Время холда изменено на {new_time} минут", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное целое число!", reply_markup=markup)



# ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ НОМЕРА:
@bot.callback_query_handler(func=lambda call: call.data.startswith("my_numbers"))
def show_my_numbers(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    page = int(call.data.split("_")[2]) if len(call.data.split("_")) > 2 else 1
    numbers_per_page = 5  # Количество номеров на странице
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ?', (user_id,))
        total_numbers = cursor.fetchone()[0]
        
        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)
        
        offset = (page - 1) * numbers_per_page
        cursor.execute('SELECT NUMBER, STATUS, TAKE_DATE, SHUTDOWN_DATE, HOLDS_COUNT FROM numbers WHERE ID_OWNER = ? ORDER BY TAKE_DATE DESC LIMIT ? OFFSET ?', 
                      (user_id, numbers_per_page, offset))
        numbers = cursor.fetchall()
    
    # Получаем цену пользователя из базы данных
    user_price = db_module.get_user_price(user_id)
    
    numbers_text = f"📱 <b>Мои номера (страница {page}/{total_pages})</b>\n\n"
    if not numbers:
        numbers_text += "📭 У вас пока нет номеров."
    else:
        from datetime import datetime
        current_time = datetime.now()
        
        for number, status, take_date, shutdown_date, holds_count in numbers:
            take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else str(current_time)  # Текущее время, если не завершён
            
            # Расчет времени отстоя
            take_time = datetime.strptime(take_date_str, "%Y-%m-%d %H:%M:%S") if take_date_str != "Неизвестно" else current_time
            shutdown_time = datetime.strptime(shutdown_date_str, "%Y-%m-%d %H:%M:%S") if shutdown_date != "0" else current_time
            time_held = shutdown_time - take_time
            time_held_str = f"{time_held.total_seconds() // 3600}h {(time_held.total_seconds() % 3600) // 60}m" if shutdown_date != "0" else f"{(current_time - take_time).total_seconds() // 3600}h {(current_time - take_time).total_seconds() % 3600 // 60}m"
            
            # Расчет выплаты на основе количества холдов (максимум за 2 холда)
            payout = 0
            if status.startswith("отстоял"):
                holds_count = int(holds_count) if holds_count.isdigit() else 0
                payout = min(holds_count, 2) * user_price  # Максимум 2 холда
            
            numbers_text += (
                f"📱 Номер: <code>{number}</code>\n"
                f"📊 Статус: {status}\n"
                f"🟢 Взято: {take_date_str}\n"
                f"⏱ Сколько отстоял: {time_held_str}\n"
                f"💰 Выплата: ${payout:.2f}\n\n"
            )
            
            # Обновляем баланс пользователя в базе только при завершении номера
            if payout > 0 and shutdown_date != "0":
                cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (payout, user_id))
                conn.commit()
    
    markup = types.InlineKeyboardMarkup()
    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"my_numbers_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"my_numbers_{page+1}"))
        markup.add(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="profile"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    bot.edit_message_text(numbers_text,
                          call.message.chat.id,
                          call.message.message_id,
                          reply_markup=markup,
                          parse_mode='HTML')

def safe_send_message(chat_id, text, parse_mode=None, reply_markup=None):
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except ApiTelegramException as e:
        if e.result_json.get('error_code') == 429:
            time.sleep(1)
            safe_send_message(chat_id, text, parse_mode, reply_markup)
        else:
            logging.error(f"Ошибка отправки сообщения {chat_id}: {e}")
# Глобальная переменная для хранения данных номеров (можно заменить на временное хранилище в будущем)
numbers_data_cache = {}



@bot.callback_query_handler(func=lambda call: call.data == "change_amount")
def change_amount_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("💰 Введите новую сумму оплаты (в долларах, например: 2):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_amount)


def process_change_amount(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_amount = float(message.text)
            if new_amount <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET PRICE = ?', (new_amount,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Сумма оплаты изменена на {new_amount}$", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное число!", reply_markup=markup)








# Словарь для отслеживания сообщений с кодами
code_messages = {}  # {number: {"chat_id": int, "message_id": int, "timestamp": datetime, "tg_number": int, "owner_id": int}}













































@bot.callback_query_handler(func=lambda call: call.data == "submit_number")
def submit_number(call):
    user_id = call.from_user.id 
    db.update_last_activity(user_id)
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT CAN_SUBMIT_NUMBERS FROM requests WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] == 0:
            bot.answer_callback_query(call.id, "🚫 Вам запрещено сдавать номера!")
            return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    msg = bot.send_message(
        call.message.chat.id,
        f"📱 Введите ваши номера телефона (по одному в строке):\nПример:\n+79991234567\n79001234567\n9021234567\n💵 Текущая цена: {price}$ за номер\n⏱ Холд: {hold_time} минут",
        reply_markup=markup,
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, process_numbers)

def process_numbers(message):
    if not message or not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📱 Попробовать снова", callback_data="submit_number"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, отправьте номера текстом!",
            reply_markup=markup,
            disable_notification=True
        )
        return

    numbers = message.text.strip().split('\n')
    if not numbers or all(not num.strip() for num in numbers):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📱 Попробовать снова", callback_data="submit_number"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main"))
        bot.send_message(
            message.chat.id,
            "❌ Вы не указали ни одного номера!",
            reply_markup=markup,
            disable_notification=True
        )
        return

    valid_numbers = []
    invalid_numbers = []
    used_numbers = []
    
    for number in numbers:
        number = number.strip()
        if not number:
            continue
        corrected_number = is_russian_number(number)
        if corrected_number:
            valid_numbers.append(corrected_number)
        else:
            invalid_numbers.append(number)

    if not valid_numbers:
        response_text = "❌ Все введённые номера некорректны!\nПожалуйста, вводите номера в формате +79991234567, 79001234567 или 9021234567."
        if invalid_numbers:
            response_text += "\n\n❌ Неверный формат:\n" + "\n".join(invalid_numbers)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📱 Попробовать снова", callback_data="submit_number"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main"))
        bot.send_message(
            message.chat.id,
            response_text,
            reply_markup=markup,
            parse_mode='HTML',
            disable_notification=True
        )
        return

    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            success_count = 0
            already_exists = 0
            used_count = 0
            successfully_added = []

            for number in valid_numbers:
                try:
                    # Проверяем, был ли номер ранее подтверждён
                    cursor.execute('SELECT NUMBER FROM numbers WHERE NUMBER = ? AND CONFIRMED_BY_MODERATOR_ID IS NOT NULL AND CONFIRMED_BY_MODERATOR_ID != 0', (number,))
                    used_number = cursor.fetchone()
                    if used_number:
                        used_numbers.append(number)
                        used_count += 1
                        continue

                    # Проверяем статус "Ошибка"
                    cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
                    status_result = cursor.fetchone()
                    if status_result and status_result[0] == "Ошибка":
                        used_numbers.append(number)
                        used_count += 1
                        continue

                    # Проверяем, есть ли номер в активных записях
                    cursor.execute('SELECT NUMBER, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
                    existing_number = cursor.fetchone()

                    if existing_number:
                        if existing_number[1] == "0":
                            already_exists += 1
                            continue
                        else:
                            cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))

                    # Добавляем номер в таблицу numbers
                    cursor.execute(
                        'INSERT INTO numbers (NUMBER, ID_OWNER, TAKE_DATE, SHUTDOWN_DATE, STATUS) VALUES (?, ?, ?, ?, ?)',
                        (number, message.from_user.id, '0', '0', 'ожидает')
                    )
                    success_count += 1
                    successfully_added.append(number)
                except sqlite3.IntegrityError:
                    already_exists += 1
                    continue
            conn.commit()

        response_text = "<b>📊 Результат добавления номеров:</b>\n\n"
        if success_count > 0:
            response_text += f"✅ Успешно добавлено: {success_count} номеров\n"
            response_text += "📱 Добавленные номера:\n" + "\n".join(successfully_added) + "\n"
        if already_exists > 0:
            response_text += f"⚠️ Уже существуют: {already_exists} номеров\n"
        if used_count > 0:
            response_text += f"🚫 Ранее подтверждены или помечены как 'Ошибка': {used_count} номеров\n"
            response_text += "📱 Подтверждённые/ошибочные номера:\n" + "\n".join(used_numbers) + "\n"
        if invalid_numbers:
            response_text += f"❌ Неверный формат:\n" + "\n".join(invalid_numbers) + "\n"

    except Exception as e:
        print(f"Ошибка в process_numbers: {e}")
        response_text = "❌ Произошла ошибка при добавлении номеров. Попробуйте снова."

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📱 Добавить ещё", callback_data="submit_number"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.send_message(
        message.chat.id,
        response_text,
        reply_markup=markup,
        parse_mode='HTML',
        disable_notification=True
    )


@bot.callback_query_handler(func=lambda call: call.data == "db_menu")
def db_menu_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к управлению БД!")
        return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📥 Скачать БД (НОМЕРА)", callback_data="download_numbers"))
    markup.add(InlineKeyboardButton("🗑 Очистить БД (НОМЕРА+БАЛАНС+ПРЕДЫ)", callback_data="clear_numbers"))
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    bot.edit_message_text("🗃 Управление базой данных", call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "download_numbers")
def download_numbers_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для скачивания БД!")
        return
    
    download_numbers(call.message.chat.id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "clear_numbers")
def clear_numbers_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для очистки БД!")
        return
    
    clear_database(call.message.chat.id)
    bot.answer_callback_query(call.id, "✅ Номера и балансы очищены!")

#ЧИСТКА ЛИБО В РУЧНУЮ ЛИБО АВТОМАТИЧЕСКИ БАЗЫ ДАННЫХ ( НОМЕРА )
def clear_database(chat_id=None):
    """Очищает все номера из таблицы numbers, обнуляет баланс и предупреждения пользователей."""
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            
            # Получаем пользователей, у которых есть номера, исключая админов и модераторов
            cursor.execute('''
                SELECT DISTINCT ID_OWNER 
                FROM numbers 
                WHERE ID_OWNER NOT IN (SELECT ID FROM personal WHERE TYPE IN ('ADMIN', 'moder'))
            ''')
            users_with_numbers = [row[0] for row in cursor.fetchall()]
            
            # Получаем всех пользователей для обнуления баланса и предупреждений
            cursor.execute('SELECT ID FROM users')
            all_users = [row[0] for row in cursor.fetchall()]
            
            # Удаляем все номера
            cursor.execute('DELETE FROM numbers')
            deleted_numbers = cursor.rowcount
            
            # Обнуляем баланс и предупреждения всех пользователей
            cursor.execute('UPDATE users SET BALANCE = 0, WARNINGS = 0')
            reset_balances = cursor.rowcount
            conn.commit()
            
            logging.info(f"Удалено {deleted_numbers} номеров, обнулено {reset_balances} балансов и предупреждений в {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.")
            
            # Уведомляем пользователей с номерами
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
            for user_id in users_with_numbers:
                try:
                    bot.send_message(
                        user_id,
                        "🔄 Все номера очищены, ваш баланс и предупреждения обнулёны.\n📱 Пожалуйста, поставьте свои номера снова.",
                        reply_markup=markup
                    )
                    logging.info(f"Уведомление отправлено пользователю {user_id}")
                    time.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
            
            # Уведомляем админов
            admin_message = (
                f"🔄 Все номера, балансы и предупреждения очищены.\n"
                f"🗑 Удалено {deleted_numbers} номеров.\n"
                f"💸 Обнулено {reset_balances} балансов и предупреждений."
            )
            for admin_id in config.ADMINS_ID:
                try:
                    bot.send_message(admin_id, admin_message)
                    logging.info(f"Уведомление отправлено админу {admin_id}")
                    time.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
            
            # Если очистка вызвана админом, отправляем подтверждение
            if chat_id:
                bot.send_message(
                    chat_id,
                    f"✅ Таблица номеров, балансы и предупреждения очищены.\n"
                    f"🗑 Удалено {deleted_numbers} номеров.\n"
                    f"💸 Обнулено {reset_balances} балансов и предупреждений."
                )

    except Exception as e:
        logging.error(f"Ошибка при очистке таблицы numbers или обнулении балансов/предупреждений: {e}")
        if chat_id:
            bot.send_message(chat_id, "❌ Ошибка при очистке номеров, балансов или предупреждений.")

def download_numbers(chat_id):
    """Создаёт и отправляет текстовый файл с данными из таблицы numbers."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM numbers')
            rows = cursor.fetchall()
            
            if not rows:
                bot.send_message(chat_id, "📭 Таблица номеров пуста.")
                return
            
            # Создаём текстовый файл в памяти
            output = io.StringIO()
            # Заголовки столбцов
            columns = [desc[0] for desc in cursor.description]
            output.write(','.join(columns) + '\n')
            # Данные
            for row in rows:
                output.write(','.join(str(val) if val is not None else '' for val in row) + '\n')
            
            # Подготовка файла для отправки
            output.seek(0)
            file_content = output.getvalue().encode('utf-8')
            file = io.BytesIO(file_content)
            file.name = 'numbers.txt'
            
            # Отправка файла
            bot.send_document(chat_id, file, caption="📄 Данные из таблицы номеров")
            logging.info(f"Файл numbers.txt отправлен админу {chat_id}")
    
    except Exception as e:
        logging.error(f"Ошибка при скачивании таблицы numbers: {e}")
        bot.send_message(chat_id, "❌ Ошибка при скачивании таблицы номеров.")

def schedule_clear_database():
    """Настраивает планировщик для очистки таблицы numbers и обнуления балансов в указанное время."""
    schedule.every().day.at(config.CLEAR_TIME).do(clear_database)
    logging.info(f"Планировщик настроен для очистки номеров и балансов в {config.CLEAR_TIME}")

    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(60)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logging.info("Планировщик очистки запущен.")




#=============================================================================================================





@bot.callback_query_handler(func=lambda call: call.data == "Gv")
def settingssss(data):
    # Определяем, является ли входной параметр callback (call) или сообщением (message)
    is_callback = hasattr(data, 'message')
    user_id = data.from_user.id
    chat_id = data.message.chat.id if is_callback else data.chat.id
    message_id = data.message.message_id if is_callback else data.message_id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        if is_callback:
            bot.answer_callback_query(data.id, "❌ У вас нет прав для выполнения этого действия!")
        else:
            bot.send_message(chat_id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    # Очищаем активные обработчики, чтобы избежать нежелательной реакции на ввод текста
    bot.clear_step_handler_by_chat_id(chat_id)
    
    # Формируем текст и кнопки для меню
    menu_text = "📋 <b>Меню:</b>"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить номер у пользователя", callback_data="delete_number"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить все номера у пользователя", callback_data="delete_all_numbers"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    # Редактируем или отправляем сообщение в зависимости от типа вызова
    try:
        if is_callback:
            bot.edit_message_text(
                menu_text,
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        else:
            bot.send_message(
                chat_id,
                menu_text,
                parse_mode='HTML',
                reply_markup=markup
            )
    except Exception as e:
        print(f"[ERROR] Не удалось обработать сообщение: {e}")
        bot.send_message(
            chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data == "delete_number")
def delete_number_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    msg = bot.edit_message_text(
        "📱 Введите ID пользователя и номер телефона для удаления (в формате: <ID> <номер>, например: 123456 +79991234567):",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, process_delete_number, call.message.chat.id, msg.message_id)

def process_delete_number(message, original_chat_id, original_message_id):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Проверяем формат ввода
    try:
        user_id, number = message.text.strip().split()
        user_id = int(user_id)
        number = is_russian_number(number)
        if not number:
            raise ValueError("Неверный формат номера")
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="delete_number"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат! Введите ID пользователя и номер телефона (например: 123456 +79991234567)",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Проверяем, существует ли номер
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NUMBER FROM numbers WHERE NUMBER = ? AND ID_OWNER = ?', (number, user_id))
        if not cursor.fetchone():
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="delete_number"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                f"❌ Номер <code>{number}</code> не найден у пользователя ID {user_id}!",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
    
    # Удаляем сообщение с введёнными данными
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение: {e}")
    
    # Отправляем предупреждение с подтверждением
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_delete_number_{user_id}_{number}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="Gv")
    )
    bot.edit_message_text(
        f"⚠️ Вы уверены, что хотите удалить номер <code>{number}</code> у пользователя ID {user_id}?",
        original_chat_id,
        original_message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_delete_number_"))
def confirm_delete_number(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Извлекаем user_id и number из callback_data
    try:
        _, _, user_id, number = call.data.split("_", 3)
        user_id = int(user_id)
    except ValueError:
        bot.answer_callback_query(call.id, "❌ Ошибка в данных!")
        return
    
    # Удаляем номер из базы данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM numbers WHERE NUMBER = ? AND ID_OWNER = ?', (number, user_id))
        conn.commit()
    
    # Отправляем подтверждение удаления
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔍 Удалить другой номер", callback_data="delete_number"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    bot.edit_message_text(
        f"✅ Номер <code>{number}</code> успешно удалён у пользователя ID {user_id}!",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id, "Номер удалён!")

@bot.callback_query_handler(func=lambda call: call.data == "delete_all_numbers")
def delete_all_numbers_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    msg = bot.edit_message_text(
        "👤 Введите ID пользователя для удаления всех его номеров:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, process_delete_all_numbers, call.message.chat.id, msg.message_id)

def process_delete_all_numbers(message, original_chat_id, original_message_id):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Проверяем формат ввода
    try:
        user_id = int(message.text.strip())
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="delete_all_numbers"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат! Введите ID пользователя (например: 123456)",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Проверяем, есть ли номера у пользователя
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ?', (user_id,))
        count = cursor.fetchone()[0]
        if count == 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="delete_all_numbers"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                f"❌ У пользователя ID {user_id} нет номеров!",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
    
    # Удаляем сообщение с введённым ID
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение: {e}")
    
    # Отправляем предупреждение с подтверждением
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_delete_all_numbers_{user_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="Gv")
    )
    bot.edit_message_text(
        f"⚠️ Вы уверены, что хотите удалить ВСЕ номера у пользователя ID {user_id}? (Найдено {count} номеров)",
        original_chat_id,
        original_message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_delete_all_numbers_"))
def confirm_delete_all_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Извлекаем user_id из callback_data
    try:
        _, _, user_id = call.data.split("_", 2)
        user_id = int(user_id)
    except ValueError:
        bot.answer_callback_query(call.id, "❌ Ошибка в данных!")
        return
    
    # Удаляем все номера пользователя
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM numbers WHERE ID_OWNER = ?', (user_id,))
        deleted_count = cursor.rowcount
        conn.commit()
    
    # Отправляем подтверждение удаления
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔍 Удалить номера другого пользователя", callback_data="delete_all_numbers"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    bot.edit_message_text(
        f"✅ Удалено {deleted_count} номеров у пользователя ID {user_id}!",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id, "Все номера удалены!")

#Выдать чек
@bot.callback_query_handler(func=lambda call: call.data == "send_check")
def send_check_start(call):
    user_id = call.from_user.id
    
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    text = "📝 <b>Укажите user ID или @username</b> (используйте reply на это сообщение):"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    
    try:
        msg = bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_user_id_for_check, call.message.chat.id, call.message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        msg = bot.send_message(
            call.message.chat.id,
            text,
            parse_mode='HTML',
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_user_id_for_check, call.message.chat.id, msg.message_id)

def process_user_id_for_check(message, original_chat_id, original_message_id):
    user_id = message.from_user.id
    
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода user ID или @username!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    input_text = message.text.strip()
    target_user_id = None
    username = None
    
    if input_text.startswith('@'):
        username = input_text[1:]
        print(f"[DEBUG] Processing username: {username}")
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                target_user_id = user[0]
                print(f"[DEBUG] Found user ID {target_user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
                bot.send_message(
                    message.chat.id,
                    f"❌ Пользователь с @username '{username}' не найден!",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return
    else:
        try:
            target_user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {target_user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                "❌ Неверный формат! Введите числовой ID или @username.",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
    
    # Проверяем, существует ли пользователь и получаем баланс
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, BALANCE, USERNAME FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        if not user:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                f"❌ Пользователь с ID {target_user_id} не найден!",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
        target_user_id, balance, username = user
        print(f"{target_user_id}: текущий баланс={balance}, username={username}")
    
    # Проверяем, что баланс больше 0
    if balance <= 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            f"❌ Баланс пользователя {target_user_id} ({username if username else 'Нет username'}) равен {balance:.2f} $. Чек не может быть создан!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Удаляем сообщение с user ID
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с user ID: {e}")
    
    # Списываем весь баланс до 0.0 с блокировкой
    with db_lock:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Повторно проверяем баланс перед списанием
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            print(f"[DEBUG] Повторная проверка баланса пользователя {target_user_id}: {user[0] if user else 'не найден'}")
            if not user or user[0] <= 0:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
                bot.send_message(
                    original_chat_id,
                    f"❌ Баланс пользователя {target_user_id} равен {user[0]:.2f} $. Чек не может быть создан!",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return
            
            amount = round(float(user[0]), 2)  # Округляем баланс до 2 знаков
            print(f"[DEBUG] Создание чека на сумму {amount:.2f} для пользователя {target_user_id}")
            
            # Обнуляем баланс
            print(f"[DEBUG] Выполняется UPDATE для пользователя {target_user_id}, установка BALANCE = 0")
            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (target_user_id,))
            if cursor.rowcount == 0:
                print(f"[ERROR] UPDATE не затронул строки: пользователь {target_user_id} не найден или баланс не изменён")
                bot.send_message(
                    original_chat_id,
                    f"❌ Ошибка: не удалось обнулить баланс пользователя {target_user_id}.",
                    parse_mode='HTML'
                )
                return
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            new_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Перед фиксацией транзакции: новый баланс={new_balance:.2f}")
            conn.commit()
            print(f"[DEBUG] Транзакция зафиксирована: баланс пользователя {target_user_id} обнулён, новый баланс: {new_balance:.2f}")
            # Проверка баланса после фиксации
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            verified_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Проверка после фиксации: баланс={verified_balance:.2f}")
            if verified_balance != 0.0:
                print(f"[ERROR] Несоответствие баланса после фиксации: ожидалось 0.0, получено {verified_balance:.2f}")
    
    # Создаём чек через CryptoBot API
    crypto_api = crypto_pay.CryptoPay()
    cheque_result = crypto_api.create_check(
        amount=str(amount),
        asset="USDT",
        description=f"Выплата всего баланса для пользователя {target_user_id}",
        pin_to_user_id=target_user_id
    )
    print(f"[DEBUG] Результат создания чека: {cheque_result}")
    
    if cheque_result.get("ok", False):
        cheque = cheque_result.get("result", {})
        cheque_link = cheque.get("bot_check_url", "")
        
        if cheque_link:
            # Обновляем баланс казны
            try:
                db_module.update_treasury_balance(-amount)
                print(f"[DEBUG] Баланс казны уменьшен на {amount:.2f}")
            except Exception as treasury_error:
                print(f"[ERROR] Ошибка при обновлении баланса казны: {treasury_error}")
                bot.send_message(
                    original_chat_id,
                    f"❌ Ошибка при обновлении баланса казны: {str(treasury_error)}.",
                    parse_mode='HTML'
                )
                return
            
            # Уведомляем пользователя
            markup_user = types.InlineKeyboardMarkup()
            markup_user.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            try:
                safe_send_message(
                    target_user_id,
                    f"✅ Вам отправлен чек на {amount:.2f}$ (весь ваш баланс)!\n"
                    f"🔗 Ссылка на чек: {cheque_link}\n"
                    f"💰 Ваш новый баланс: {new_balance:.2f}$",
                    parse_mode='HTML',
                    reply_markup=markup_user
                )
                print(f"[DEBUG] Уведомление отправлено пользователю {target_user_id}")
            except Exception as notify_error:
                print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {notify_error}")
                bot.send_message(
                    original_chat_id,
                    f"⚠️ Не удалось уведомить пользователя {target_user_id}: {notify_error}",
                    parse_mode='HTML'
                )
            
            # Уведомляем администратора
            username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
            markup_admin = types.InlineKeyboardMarkup()
            markup_admin.add(types.InlineKeyboardButton("🔙 Назад в заявки", callback_data="pending_withdrawals"))
            try:
                bot.edit_message_text(
                    f"✅ Чек на {amount:.2f}$ (весь баланс) успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance:.2f}$",
                    original_chat_id,
                    original_message_id,
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            except Exception as e:
                print(f"[ERROR] Не удалось обновить сообщение для администратора: {e}")
                bot.send_message(
                    original_chat_id,
                    f"✅ Чек на {amount:.2f}$ (весь баланс) успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance:.2f}$",
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            
            # Логируем операцию
            try:
                db_module.log_treasury_operation("Вывод (чек на весь баланс)", -amount, db_module.get_treasury_balance())
                print(f"[DEBUG] Операция логирована: Вывод (чек) на {amount:.2f}$")
            except Exception as log_error:
                print(f"[ERROR] Ошибка при логировании операции: {log_error}")
        else:
            print("[ERROR] Ссылка на чек отсутствует")
            bot.send_message(
                original_chat_id,
                f"❌ Не удалось создать чек для пользователя {target_user_id}: нет ссылки.",
                parse_mode='HTML'
            )
    else:
        error_msg = cheque_result.get('error', {}).get('name', 'Неизвестная ошибка')
        print(f"[ERROR] Ошибка при создании чека: {error_msg}")
        bot.send_message(
            original_chat_id,
            f"❌ Ошибка при создании чека: {error_msg}",
            parse_mode='HTML'
        )
    
    # Возвращаемся к главному меню
    menu_text = "📋 <b>Меню:</b>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
    
    try:
        bot.edit_message_text(
            menu_text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        bot.send_message(
            original_chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )

def process_check_amount(message, target_user_id, original_chat_id, original_message_id, current_balance, username_display):
    user_id = message.from_user.id
    
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода суммы!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError("Сумма должна быть положительной")
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат суммы! Введите положительное число (например, 10.5).",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Списываем сумму с баланса пользователя с блокировкой
    with db_lock:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Повторно проверяем баланс перед списанием
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            print(f"[DEBUG] Повторная проверка баланса пользователя {target_user_id}: {user[0] if user else 'не найден'}")
            if not user or user[0] < amount:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
                bot.send_message(
                    message.chat.id,
                    f"❌ Недостаточно средств на балансе пользователя {target_user_id}! Текущий баланс: {user[0] if user else 0} $",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return
            
            # Уменьшаем баланс
            print(f"[DEBUG] Выполняется UPDATE для пользователя {target_user_id}, уменьшение на {amount}")
            cursor.execute('UPDATE users SET BALANCE = BALANCE - ? WHERE ID = ?', (amount, target_user_id))
            if cursor.rowcount == 0:
                print(f"[ERROR] UPDATE не затронул строки: пользователь {target_user_id} не найден или баланс не изменён")
                bot.send_message(
                    message.chat.id,
                    f"❌ Ошибка: не удалось обновить баланс пользователя {target_user_id}.",
                    parse_mode='HTML'
                )
                return
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            new_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Перед фиксацией транзакции: новый баланс={new_balance}")
            conn.commit()
            print(f"[DEBUG] Транзакция зафиксирована: баланс пользователя {target_user_id} уменьшен на {amount}$, новый баланс: {new_balance}")
            # Проверка баланса после фиксации
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            verified_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Проверка после фиксации: баланс={verified_balance}")
            if verified_balance != new_balance:
                print(f"[ERROR] Несоответствие баланса после фиксации: ожидалось {new_balance}, получено {verified_balance}")
    
    # Удаляем сообщение с суммой
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с суммой: {e}")
    
    # Создаём чек через CryptoBot API
    crypto_api = crypto_pay.CryptoPay()
    cheque_result = crypto_api.create_check(
        amount=amount,
        asset="USDT",
        description=f"Выплата для пользователя {target_user_id}",
        pin_to_user_id=target_user_id
    )
    print(f"[DEBUG] Результат создания чека: {cheque_result}")
    
    if cheque_result.get("ok", False):
        cheque = cheque_result.get("result", {})
        cheque_link = cheque.get("bot_check_url", "")
        
        if cheque_link:
            # Обновляем баланс казны
            try:
                db_module.update_treasury_balance(-amount)
                print(f"[DEBUG] Баланс казны уменьшен на {amount}")
            except Exception as treasury_error:
                print(f"[ERROR] Ошибка при обновлении баланса казны: {treasury_error}")
                bot.send_message(
                    original_chat_id,
                    f"❌ Ошибка при обновлении баланса казны: {str(treasury_error)}.",
                    parse_mode='HTML'
                )
                return
            
            # Уведомляем пользователя
            markup_user = types.InlineKeyboardMarkup()
            markup_user.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            try:
                safe_send_message(
                    target_user_id,
                    f"✅ Вам отправлен чек на {amount}$!\n"
                    f"🔗 Ссылка на чек: {cheque_link}\n"
                    f"💰 Ваш новый баланс: {new_balance}$",
                    parse_mode='HTML',
                    reply_markup=markup_user
                )
                print(f"[DEBUG] Уведомление отправлено пользователю {target_user_id}")
            except Exception as notify_error:
                print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {notify_error}")
                bot.send_message(
                    original_chat_id,
                    f"⚠️ Не удалось уведомить пользователя {target_user_id}: {notify_error}",
                    parse_mode='HTML'
                )
            
            # Уведомляем администратора
            markup_admin = types.InlineKeyboardMarkup()
            markup_admin.add(types.InlineKeyboardButton("🔙 Назад в заявки", callback_data="pending_withdrawals"))
            try:
                bot.edit_message_text(
                    f"✅ Чек на {amount}$ успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance}$",
                    original_chat_id,
                    original_message_id,
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            except Exception as e:
                print(f"[ERROR] Не удалось обновить сообщение для администратора: {e}")
                bot.send_message(
                    original_chat_id,
                    f"✅ Чек на {amount}$ успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance}$",
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            
            # Логируем операцию
            try:
                db_module.log_treasury_operation("Вывод (чек)", -amount, db_module.get_treasury_balance())
                print(f"[DEBUG] Операция логирована: Вывод (чек) на {amount}$")
            except Exception as log_error:
                print(f"[ERROR] Ошибка при логировании операции: {log_error}")
        else:
            print("[ERROR] Ссылка на чек отсутствует")
            bot.send_message(
                original_chat_id,
                f"❌ Не удалось создать чек для пользователя {target_user_id}: нет ссылки.",
                parse_mode='HTML'
            )
    else:
        error_msg = cheque_result.get('error', {}).get('name', 'Неизвестная ошибка')
        print(f"[ERROR] Ошибка при создании чека: {error_msg}")
        bot.send_message(
            original_chat_id,
            f"❌ Ошибка при создании чека: {error_msg}",
            parse_mode='HTML'
        )
    
    # Возвращаемся к главному меню
    menu_text = "📋 <b>Меню:</b>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
    
    try:
        bot.edit_message_text(
            menu_text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        bot.send_message(
            original_chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )

#ИЗМЕНИТ ЦЕНУ:
# bot.py
@bot.callback_query_handler(func=lambda call: call.data == "change_price")
def change_price_start(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))

    msg = bot.edit_message_text(
        "📝 Введите ID пользователя или @username, для которого хотите установить индивидуальную цену (ответьте на это сообщение):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_user_id_for_price)

def process_user_id_for_price(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этого действия!")
        return
    
    input_text = message.text.strip()
    user_id = None
    username = None
    
    if input_text.startswith('@'):
        username = input_text[1:]  # Убираем @ (например, Devshop19)
        print(f"[DEBUG] Processing username: {username}")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                user_id = user[0]
                print(f"[DEBUG] Found user ID {user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
                bot.send_message(message.chat.id, f"❌ Пользователь с @username '{username}' не найден!", reply_markup=markup)
                return
    else:
        try:
            user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
            bot.send_message(message.chat.id, "❌ Неверный формат! Введите числовой ID или @username.", reply_markup=markup)
            return
    
    # Проверяем, существует ли пользователь
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM users WHERE ID = ?', (user_id,))
        if not cursor.fetchone():
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
            bot.send_message(message.chat.id, f"❌ Пользователь с ID {user_id} не найден!", reply_markup=markup)
            return
    
    msg = bot.send_message(
        message.chat.id,
        f"💵 Введите новую цену (в $) для пользователя {user_id} (ответьте на это сообщение):"
    )
    bot.register_next_step_handler(msg, process_price, user_id)

def process_price(message, user_id):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этого действия!")
        return
    
    try:
        price = float(message.text.strip())
        if price <= 0:
            raise ValueError("Цена должна быть положительной!")
        
        db_module.set_custom_price(user_id, price)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            f"✅ Индивидуальная цена для пользователя {user_id} установлена: {price}$",
            reply_markup=markup
        )
        
        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                f"💵 Ваша индивидуальная цена за номер изменена на {price}$!"
            )
        except Exception as e:
            print(f"[ERROR] Не удалось уведомить пользователя {user_id}: {e}")
            
    except ValueError as e:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)}", reply_markup=markup)


# Переменная для хранения состояния
AFK_STATE = {}

@bot.callback_query_handler(func=lambda call: call.data == "afk_settings")
def afk_settings(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к настройкам АФК!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    
    msg = bot.edit_message_text(
        "⚙️ <b>Настройки АФК</b>\n\nВведите ID пользователя или @username для управления его АФК-статусом:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    # Сохраняем состояние
    AFK_STATE[call.from_user.id] = {"step": "awaiting_user_id", "message_id": msg.message_id}
    bot.register_next_step_handler(msg, process_afk_user_id)

def process_afk_user_id(message):
    admin_id = message.from_user.id
    if admin_id not in AFK_STATE or AFK_STATE[admin_id]["step"] != "awaiting_user_id":
        print(f"[DEBUG] Invalid state for admin_id {admin_id}: {AFK_STATE.get(admin_id)}")
        return

    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return

    input_text = message.text.strip()
    print(f"[DEBUG] Input text: '{input_text}'")

    target_user_id = None
    username = None
    if input_text.startswith('@'):
        username = input_text[1:]  # Убираем @ (например, Devshop19)
        print(f"[DEBUG] Processing username: {username}")
    else:
        try:
            target_user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {target_user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
            bot.send_message(message.chat.id, "❌ Неверный формат. Введите числовой ID или @username.", reply_markup=markup)
            bot.register_next_step_handler(message, process_afk_user_id)
            return

    if username:
        with db.get_db() as conn:
            cursor = conn.cursor()
            # Отладка: выводим всех пользователей
            cursor.execute('SELECT ID, USERNAME FROM users')
            all_users = cursor.fetchall()
            print(f"[DEBUG] All users in DB: {all_users}")
            
            # Поиск без учёта регистра
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                target_user_id = user[0]
                print(f"[DEBUG] Found user ID {target_user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
                bot.send_message(message.chat.id, f"❌ Пользователь с @username '{username}' не найден.", reply_markup=markup)
                bot.register_next_step_handler(message, process_afk_user_id)
                print(f"[DEBUG] Username {username} not found in DB")
                return

    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT IS_AFK, AFK_LOCKED, USERNAME FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            if not user:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
                bot.send_message(message.chat.id, f"❌ Пользователь с ID {target_user_id} не найден.", reply_markup=markup)
                bot.register_next_step_handler(message, process_afk_user_id)
                return
            
            is_afk, afk_locked, username = user
            print(f"[DEBUG] User {target_user_id}: IS_AFK={is_afk}, AFK_LOCKED={afk_locked}, USERNAME={username}")
            afk_status_text = "Включён" if is_afk else "Выключен"
            
            username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
            username_text = f"👤 Username: <a href=\"tg://user?id={target_user_id}\">{username_display}</a>\n" if username and username != "Не указан" else "👤 Username: Нет username\n"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
                types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
            )
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
            
            bot.send_message(
                message.chat.id,
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"{username_text}"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}\n"
                f"🔒 <b>Блокировка АФК:</b> {'Да' if afk_locked else 'Нет'}",
                parse_mode='HTML',
                reply_markup=markup
            )
    except Exception as e:
        print(f"[ERROR] Ошибка при обработке AFK для пользователя {target_user_id}: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при получении данных. Попробуйте позже.")
    
    AFK_STATE.pop(admin_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_enable_afk_"))
def admin_enable_afk(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Извлекаем target_user_id из callback_data
    target_user_id = int(call.data.replace("admin_enable_afk_", ""))
    
    # Обновляем статус AFK в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET IS_AFK = ?, AFK_LOCKED = ? WHERE ID = ?', (1, 1, target_user_id))
        conn.commit()
        print(f"[DEBUG] АФК включён для пользователя {target_user_id} с блокировкой")

    # Обновляем сообщение с актуальным статусом
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        # Извлекаем IS_AFK, AFK_LOCKED и USERNAME
        cursor.execute('SELECT IS_AFK, AFK_LOCKED, USERNAME FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        is_afk, afk_locked, username = user
        afk_status_text = "Включён" if is_afk else "Выключен"
        
        # Форматируем username как кликабельную ссылку
        username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
        username_text = f"👤 Username: <a href=\"tg://user?id={target_user_id}\">{username_display}</a>\n" if username and username != "Не указан" else "👤 Username: Нет username\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
            types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
        
        try:
            bot.edit_message_text(
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"{username_text}"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"[ERROR] Не удалось обновить сообщение: {e}")
            bot.send_message(chat_id, f"👤 <b>User ID:</b> {target_user_id}\n{username_text}🔔 <b>АФК Статус:</b> {afk_status_text}", parse_mode='HTML', reply_markup=markup)

    # Отправляем уведомление пользователю
    try:
        bot.send_message(
            target_user_id,
            "🔔 <b>Ваш АФК-статус был изменён администратором</b>\n\n"
            "Теперь ваш АФК: <b>Включён</b>",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {e}")

    bot.answer_callback_query(call.id, "✅ АФК включён для пользователя!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_disable_afk_"))
def admin_disable_afk(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Извлекаем target_user_id из callback_data
    target_user_id = int(call.data.replace("admin_disable_afk_", ""))
    
    # Обновляем статус AFK в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET IS_AFK = ?, AFK_LOCKED = ? WHERE ID = ?', (0, 0, target_user_id))
        conn.commit()
        print(f"[DEBUG] АФК выключен для пользователя {target_user_id}, блокировка снята")

    # Обновляем сообщение с актуальным статусом
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        # Извлекаем IS_AFK, AFK_LOCKED и USERNAME
        cursor.execute('SELECT IS_AFK, AFK_LOCKED, USERNAME FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        is_afk, afk_locked, username = user
        afk_status_text = "Включён" if is_afk else "Выключен"
        
        # Форматируем username как кликабельную ссылку
        username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
        username_text = f"👤 Username: <a href=\"tg://user?id={target_user_id}\">{username_display}</a>\n" if username and username != "Не указан" else "👤 Username: Нет username\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
            types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
        
        try:
            bot.edit_message_text(
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"{username_text}"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"[ERROR] Не удалось обновить сообщение: {e}")
            bot.send_message(chat_id, f"👤 <b>User ID:</b> {target_user_id}\n{username_text}🔔 <b>АФК Статус:</b> {afk_status_text}", parse_mode='HTML', reply_markup=markup)

    # Отправляем уведомление пользователю
    try:
        bot.send_message(
            target_user_id,
            "🔔 <b>Ваш АФК-статус был изменён администратором</b>\n\n"
            "Теперь ваш АФК: <b>Выключен</b>",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {e}")

    bot.answer_callback_query(call.id, "✅ АФК выключен для пользователя!")



def cancel_old_checks(crypto_api):
    try:
        checks_result = crypto_api.get_checks(status="active")
        if checks_result.get("ok", False):
            for check in checks_result["result"]["items"]:
                check_id = check["check_id"]
                crypto_api.delete_check(check_id=check_id)
                print(f"[INFO] Отменён чек {check_id}, высвобождено {check['amount']} USDT")
    except Exception as e:
        print(f"[ERROR] Не удалось отменить старые чеки: {e}")



@bot.callback_query_handler(func=lambda call: call.data == "send_all_checks")
def send_all_checks(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    crypto_api = crypto_pay.CryptoPay()
    
    try:
        cancel_old_checks(crypto_api)
        balance_result = crypto_api.get_balance()
        if not balance_result.get("ok", False):
            bot.edit_message_text(
                "❌ Ошибка при проверке баланса CryptoPay. Попробуйте позже.",
                call.message.chat.id,
                call.message.message_id
            )
            return
        
        usdt_balance = next((float(item["available"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
        usdt_onhold = next((float(item["onhold"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
        
        print(f"[INFO] Баланс CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT")
        
        if usdt_balance <= 0:
            bot.edit_message_text(
                f"❌ Недостаточно средств на балансе CryptoPay.\nДоступно: {usdt_balance} USDT\nВ резерве: {usdt_onhold} USDT",
                call.message.chat.id,
                call.message.message_id
            )
            return
    except Exception as e:
        print(f"[ERROR] Не удалось проверить баланс CryptoPay: {e}")
        bot.edit_message_text(
            "❌ Ошибка при проверке баланса CryptoPay. Попробуйте позже.",
            call.message.chat.id,
            call.message.message_id
        )
        return
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM treasury WHERE ID = 1')
        treasury_result = cursor.fetchone()
        treasury_balance = treasury_result[0] if treasury_result else 0.0
        
        if treasury_balance <= 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.edit_message_text(
                "❌ Недостаточно средств в казне для выплат.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            return
        
        # Получаем пользователей с балансом > 0.2, включая USERNAME
        cursor.execute('SELECT ID, BALANCE, USERNAME FROM users WHERE BALANCE > 0.2')
        users = cursor.fetchall()
        
        if not users:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
            bot.edit_message_text(
                "❌ Нет пользователей с балансом больше 0.2$.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            return
        
        success_count = 0
        total_amount = 0
        failed_users = []
        checks_report = []  # Список для отчёта
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for user_id, balance, username in users:
            # Проверяем, достаточно ли средств перед попыткой выплаты
            if float(balance) > treasury_balance:
                failed_users.append((user_id, balance, username, "Недостаточно средств в казне"))
                continue
            if float(balance) > usdt_balance:
                failed_users.append((user_id, balance, username, "Недостаточно средств на CryptoPay"))
                continue
            
            for attempt in range(3):
                try:
                    cheque_result = crypto_api.create_check(
                        amount=str(balance),
                        asset="USDT",
                        pin_to_user_id=user_id,
                        description=f"Автоматическая выплата для пользователя {user_id}"
                    )
                    
                    # Проверяем, является ли cheque_result строкой, и парсим её как JSON
                    if isinstance(cheque_result, str):
                        try:
                            cheque_result = json.loads(cheque_result)
                        except json.JSONDecodeError as e:
                            print(f"[ERROR] Не удалось распарсить ответ от create_check: {cheque_result}, ошибка: {e}")
                            failed_users.append((user_id, balance, username, "Ошибка парсинга ответа от CryptoPay"))
                            break
                    
                    # Проверяем, если метод createCheck отключён
                    if isinstance(cheque_result, dict) and not cheque_result.get("ok", False):
                        error = cheque_result.get("error", {})
                        if isinstance(error, dict) and error.get("code") == 403 and error.get("name") == "METHOD_DISABLED":
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                            bot.edit_message_text(
                                "❌ В @CryptoBot отключена возможность создавать чеки. Включите метод createCheck в настройках приложения.",
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup
                            )
                            return
                        else:
                            error_name = error.get("name", "Неизвестная ошибка") if isinstance(error, dict) else "Неизвестная ошибка"
                            failed_users.append((user_id, balance, username, f"Ошибка CryptoPay: {error_name}"))
                            break
                    
                    if cheque_result.get("ok", False):
                        cheque = cheque_result.get("result", {})
                        cheque_link = cheque.get("bot_check_url", "")
                        
                        if cheque_link:
                            # Записываем чек в базу данных
                            cursor.execute('''
                                INSERT INTO checks (USER_ID, AMOUNT, CHECK_CODE, STATUS, CREATED_AT)
                                VALUES (?, ?, ?, ?, ?)
                            ''', (user_id, balance, cheque_link, 'pending', current_time))
                            conn.commit()
                            
                            # Обнуляем баланс пользователя
                            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (user_id,))
                            conn.commit()
                            
                            # Обновляем баланс казны
                            treasury_balance -= float(balance)
                            cursor.execute('UPDATE treasury SET BALANCE = ? WHERE ID = 1', (treasury_balance,))
                            conn.commit()
                            db_module.log_treasury_operation("Автоматический вывод (массовый)", balance, treasury_balance)
                            
                            # Формируем отчёт
                            username_display = username if username and username != "Не указан" else "Не указан"
                            checks_report.append({
                                "cheque_link": cheque_link,
                                "user_id": user_id,
                                "username": username_display,
                                "amount": balance
                            })
                            
                            # Отправляем сообщение пользователю
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
                            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                            try:
                                bot.send_message(
                                    user_id,
                                    f"✅ Ваш чек на сумму {balance}$ готов!\n"
                                    f"Нажмите на кнопку ниже, чтобы активировать его:",
                                    reply_markup=markup,
                                    parse_mode='HTML'
                                )
                            except Exception as e:
                                print(f"[ERROR] Не удалось отправить сообщение пользователю {user_id}: {e}")
                                failed_users.append((user_id, balance, username, "Ошибка отправки сообщения"))
                                break
                            
                            # Логируем успех
                            log_entry = f"[{current_time}] | Массовая выплата | Пользователь {user_id} | Сумма {balance}$ | Успех"
                            with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                                log_file.write(log_entry + "\n")
                            
                            success_count += 1
                            total_amount += balance
                            usdt_balance -= float(balance)
                            break
                    else:
                        error = cheque_result.get("error", {}).get("name", "Неизвестная ошибка") if isinstance(cheque_result, dict) else "Неизвестная ошибка"
                        failed_users.append((user_id, balance, username, f"Ошибка CryptoPay: {error}"))
                        break
                except RequestException as e:
                    print(f"[ERROR] Попытка {attempt + 1} для пользователя {user_id}: {e}")
                    if attempt == 2:
                        failed_users.append((user_id, balance, username, f"Ошибка запроса: {str(e)}"))
                    continue
        
        # Формируем отчёт для администратора
        report = (
            f"✅ Отправлено чеков: {success_count}\n"
            f"💰 Общая сумма: {total_amount}$\n"
            f"💰 Баланс CryptoPay: {usdt_balance}$\n"
            f"💰 В резерве CryptoPay: {usdt_onhold}$\n"
        )
        if checks_report:
            report += "\n📋 Успешные выплаты:\n"
            for entry in checks_report:
                report += (
                    f"ID: {entry['user_id']}, "
                    f"Username: @{entry['username']}, "
                    f"Сумма: {entry['amount']}$, "
                    f"Ссылка: {entry['cheque_link']}\n"
                    f""
                    f"————————————————————————"
                )
        if failed_users:
            report += "\n❌ Не удалось обработать для пользователей:\n"
            for user_id, balance, username, error in failed_users:
                username_display = username if username and username != "Не указан" else "Не указан"
                report += f"ID: {user_id}, Username: @{username_display}, Сумма: {balance}$, Ошибка: {error}\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.edit_message_text(
            report,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        # Сохраняем отчёт в файл
        report_filename = f"checks_report_{current_time.replace(':', '-')}.txt"
        with open(report_filename, "w", encoding="utf-8") as report_file:
            report_file.write(report)
        
        # Уведомляем администраторов
        admin_message = (
            f"💸 <b>Массовая отправка чеков завершена</b>\n\n"
            f"✅ Успешно отправлено: {success_count} чеков\n"
            f"💰 Общая сумма: {total_amount}$\n"
            f"💰 Остаток в казне: {treasury_balance}$\n"
            f"💰 Баланс CryptoPay: {usdt_balance}$\n"
            f"💰 В резерве CryptoPay: {usdt_onhold}$\n"
        )
        if checks_report:
            admin_message += "\n📋 Успешные выплаты:\n"
            for entry in checks_report:
                admin_message += (
                    f"ID: {entry['user_id']}, "
                    f"Username: @{entry['username']}, "
                    f"Сумма: {entry['amount']}$, "
                    f"Ссылка: {entry['cheque_link']}\n"
                )
        if failed_users:
            admin_message += "\n❌ Ошибки:\n"
            for user_id, balance, username, error in failed_users:
                username_display = username if username and username != "Не указан" else "Не указан"
                admin_message += f"ID: {user_id}, Username: @{username_display}, Сумма: {balance}$, Ошибка: {error}\n"
        
        for admin_id in config.ADMINS_ID:
            try:
                bot.send_message(admin_id, admin_message, parse_mode='HTML')
            except:
                continue


# bot.py
SEND_CHECK_STATE = {}
search_state = {}

@bot.callback_query_handler(func=lambda call: call.data == "send_check")
def send_check_start(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    msg = bot.edit_message_text(
        "Введите user_id или @username пользователя, которому нужно отправить чек (ответьте на это сообщение):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    
    SEND_CHECK_STATE[call.from_user.id] = {"step": "awaiting_user_id", "message_id": msg.message_id}
    bot.register_next_step_handler(msg, process_user_id_input)

def process_user_id_input(message):
    admin_id = message.from_user.id
    if admin_id not in SEND_CHECK_STATE or SEND_CHECK_STATE[admin_id]["step"] != "awaiting_user_id":
        return
    
    if not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Пожалуйста, введите user_id или @username.", reply_markup=markup)
        bot.register_next_step_handler(message, process_user_id_input)
        return
    
    input_text = message.text.strip()
    user_id = None
    username = None
    
    if input_text.startswith('@'):
        username = input_text[1:]
        print(f"[DEBUG] Processing username: {username}")
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                user_id = user[0]
                print(f"[DEBUG] Found user ID {user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Пользователь с @username '{username}' не найден.", reply_markup=markup)
                bot.register_next_step_handler(message, process_user_id_input)
                return
    else:
        try:
            user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            bot.reply_to(message, "❌ Неверный формат! Введите числовой ID или @username.", reply_markup=markup)
            bot.register_next_step_handler(message, process_user_id_input)
            return
    
    # Проверяем, существует ли пользователь
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, BALANCE, USERNAME FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if not user:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Пользователь с ID {user_id} не найден.", reply_markup=markup)
            bot.register_next_step_handler(message, process_user_id_input)
            return
        user_id, current_balance, username = user
    
    # Запрашиваем сумму
    username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    msg = bot.reply_to(
        message,
        f"Введите сумму чека в USDT для пользователя {user_id} ({username_display})\n"
        f"Текущий баланс пользователя: {current_balance} $:",
        reply_markup=markup
    )
    
    SEND_CHECK_STATE[admin_id] = {
        "step": "awaiting_amount",
        "user_id": user_id,
        "message_id": msg.message_id
    }
    bot.register_next_step_handler(msg, process_amount_input)

def process_amount_input(message):
    admin_id = message.from_user.id
    if admin_id not in SEND_CHECK_STATE or SEND_CHECK_STATE[admin_id]["step"] != "awaiting_amount":
        return
    
    if not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Пожалуйста, введите сумму в USDT.", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    amount_str = message.text.strip()
    if not re.match(r'^\d+(\.\d{1,2})?$', amount_str):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Сумма должна быть числом (например, 1.5). Попробуйте снова:", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    amount = float(amount_str)
    user_id = SEND_CHECK_STATE[admin_id]["user_id"]
    
    if amount < 0.1:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Минимальная сумма чека — 0.1 USDT.", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM treasury WHERE ID = 1')
        treasury_result = cursor.fetchone()
        treasury_balance = treasury_result[0] if treasury_result else 0.0
        
        if amount > treasury_balance:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Недостаточно средств в казне. В казене: {treasury_balance} USDT.", reply_markup=markup)
            return
        
        # Проверка баланса CryptoPay
        crypto_api = crypto_pay.CryptoPay()
        try:
            balance_result = crypto_api.get_balance()
            if not balance_result.get("ok", False):
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, "❌ Ошибка при проверке баланса CryptoPay.", reply_markup=markup)
                return
            
            usdt_balance = next((float(item["available"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
            usdt_onhold = next((float(item["onhold"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
            
            print(f"[INFO] Баланс CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT")
            
            if amount > usdt_balance:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Недостаточно средств на CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT.", reply_markup=markup)
                return
        except Exception as e:
            print(f"[ERROR] Не удалось проверить баланс CryptoPay: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, "❌ Ошибка при проверке баланса CryptoPay.", reply_markup=markup)
            return
        
        # Создаём чек
        try:
            cheque_result = crypto_api.create_check(
                amount=str(amount),
                asset="USDT",
                pin_to_user_id=user_id,
                description=f"Чек для пользователя {user_id} от администратора"
            )
            
            if not cheque_result.get("ok", False):
                error = cheque_result.get("error", {}).get("name", "Неизвестная ошибка")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Ошибка при создании чека: {error}", reply_markup=markup)
                return
            
            cheque = cheque_result.get("result", {})
            cheque_link = cheque.get("bot_check_url", "")
            
            if not cheque_link:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, "❌ Не удалось получить ссылку на чек.", reply_markup=markup)
                return
            
            # Сохраняем чек в базе
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                INSERT INTO checks (USER_ID, AMOUNT, CHECK_CODE, STATUS, CREATED_AT)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, amount, cheque_link, 'pending', current_time))
            
            # Обновляем баланс казны
            treasury_balance -= amount
            cursor.execute('UPDATE treasury SET BALANCE = ? WHERE ID = 1', (treasury_balance,))
            conn.commit()
            db_module.log_treasury_operation("Ручной чек", amount, treasury_balance)
            
            # Логируем операцию
            log_entry = f"[{current_time}] | Ручной чек | Пользователь {user_id} | Сумма {amount}$ | Успех"
            with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                log_file.write(log_entry + "\n")
            
            # Отправляем чек пользователю
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(
                    user_id,
                    f"✅ Ваш чек на сумму {amount}$ готов!\n"
                    f"Нажмите на кнопку ниже, чтобы активировать его:",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            except Exception as e:
                print(f"[ERROR] Не удалось отправить чек пользователю {user_id}: {e}")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Не удалось отправить чек пользователю {user_id}: {e}", reply_markup=markup)
                return
            
            # Уведомляем администратора
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"✅ Чек на {amount}$ успешно отправлен пользователю {user_id}.", reply_markup=markup)
            
            SEND_CHECK_STATE.pop(admin_id, None)
        
        except Exception as e:
            print(f"[ERROR] Не удалось создать чек для пользователя {user_id}: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Ошибка при создании чека: {e}", reply_markup=markup)
            return


# Обработчик текстового ввода для поиска
@bot.message_handler(func=lambda message: search_state.get(message.from_user.id, {}).get("awaiting_search", False))
def handle_search_query(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.reply_to(message, "❌ У вас нет прав для поиска!")
        return
    
    query = message.text.strip()
    search_state[message.from_user.id] = {"query": query}
    bot.reply_to(message, f"🔍 Выполняется поиск по запросу: '{query}'...")
    
    # Вызываем соответствующую функцию обработки в зависимости от контекста
    if search_state[message.from_user.id].get("context") == "send_check":
        process_user_id_input(message)
    # Добавьте другие контексты, если они есть (например, change_price, reduce_balance)






#=============================================================================================================

#НОМЕРА КОТОРЫЕ НЕ ОБРАБАТЫВАЛИ В ТЕЧЕНИЕ 10 МИНУТ +
def check_number_timeout():
    """Проверяет, истекло ли время ожидания кода (10 минут)."""
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT NUMBER, TAKE_DATE, ID_OWNER, MODERATOR_ID, STATUS FROM numbers')
                numbers = cursor.fetchall()
                
                current_time = datetime.now()
                for number, take_date, owner_id, moderator_id, status in numbers:
                    if take_date in ("0", "1") or status not in ("на проверке", "taken"):
                        continue
                    try:
                        take_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                        elapsed_time = (current_time - take_time).total_seconds() / 60
                        # Проверяем, не был ли номер автоматически подтверждён
                        cursor.execute('SELECT CONFIRMED_BY_MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
                        confirmed_by = cursor.fetchone()[0]
                        if elapsed_time >= 10 and confirmed_by is not None:
                            # Номер возвращается в очередь только если не был автоматически подтверждён
                            cursor.execute('UPDATE numbers SET MODERATOR_ID = NULL, TAKE_DATE = "0", STATUS = "ожидает" WHERE NUMBER = ?', (number,))
                            conn.commit()
                            logging.info(f"Номер {number} возвращён в очередь из-за бездействия модератора.")
                            
                            if owner_id:
                                markup_owner = types.InlineKeyboardMarkup()
                                markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
                                markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                safe_send_message(
                                    owner_id,
                                    f"📱 Ваш номер {number} возвращён в очередь из-за бездействия модератора.",
                                    parse_mode='HTML',
                                    reply_markup=markup_owner
                                )
                            
                            if moderator_id:
                                markup_mod = types.InlineKeyboardMarkup()
                                markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                                markup_mod.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                safe_send_message(
                                    moderator_id,
                                    f"📱 Номер {number} возвращён в очередь из-за бездействия.",
                                    parse_mode='HTML',
                                    reply_markup=markup_mod
                                )
                    except ValueError as e:
                        logging.error(f"Неверный формат времени для номера {number}: {e}")
            time.sleep(60)  # Проверяем каждую минуту
        except Exception as e:
            logging.error(f"Ошибка в check_number_timeout: {e}")
            time.sleep(60)

def check_number_hold_time():
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
                result = cursor.fetchone()
                price, hold_time = result if result else (2.0, 5)

                cursor.execute('''
                    SELECT NUMBER, ID_OWNER, TAKE_DATE, STATUS, CONFIRMED_BY_MODERATOR_ID, HOLDS_COUNT
                    FROM numbers 
                    WHERE STATUS IN ('активен', 'отстоял 1/2', 'отстоял 2/2') AND TAKE_DATE NOT IN ('0', '1')
                ''')
                numbers = cursor.fetchall()

                current_time = datetime.now()
                for number, owner_id, take_date, status, mod_id, holds_count in numbers:
                    try:
                        start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                        time_elapsed = (current_time - start_time).total_seconds() / 60
                        if time_elapsed < hold_time:
                            print(f"[TIMEOUT_CHECK] Номер {number}, статус: {status}, владелец: {owner_id}, модератор: {mod_id if mod_id else 'None'}, время: {time_elapsed:.2f}/{hold_time} минут")
                            continue

                        cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
                        current_status = cursor.fetchone()[0]
                        if current_status not in ['активен', 'отстоял 1/2', 'отстоял 2/2']:
                            print(f"[TIMEOUT_CHECK] Номер {number} имеет неподходящий статус: {current_status}, пропускаем")
                            continue

                        payout = 0
                        new_status = status
                        if holds_count == 0 and current_status == 'активен':
                            new_status = "отстоял 1/2"
                            payout = price
                        elif holds_count == 1 and current_status == 'отстоял 1/2':
                            new_status = "отстоял 2/2"
                            payout = price
                        elif holds_count >= 2:
                            new_status = "отстоял 2/2+ холд"
                            payout = 0

                        if payout > 0:
                            cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (payout, owner_id))
                            print(f"[DEBUG] Начислено {payout}$ пользователю {owner_id} за номер {number} (HOLD {holds_count + 1})")

                            # Формируем сообщение для пользователя
                            hold_text = "1/2 отстоял" if new_status == "отстоял 1/2" else "2/2 отстоял"
                            continues_text = "и продолжает стоять" if new_status != "отстоял 2/2+ холд" else ""
                            message_text = (
                                f"📱 <b>Номер:</b> <code>{number}</code>\n"
                                f"✅ <b>Отстоял холд:</b> {hold_text}\n"
                                f"💰 <b>Начислилось:</b> {payout}$\n"
                                f"⏳ <b>Номер отстоял:</b> {time_elapsed:.2f} минут {continues_text}"
                            )
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"))
                            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                            safe_send_message(owner_id, message_text, parse_mode='HTML', reply_markup=markup)

                        shutdown_date = current_time.strftime("%Y-%m-%d %H:%M:%S")
                        cursor.execute('''
                            UPDATE numbers 
                            SET STATUS = ?, SHUTDOWN_DATE = ?, HOLDS_COUNT = HOLDS_COUNT + 1
                            WHERE NUMBER = ?
                        ''', (new_status, shutdown_date, number))
                        conn.commit()
                        print(f"[DEBUG] Номер {number} отстоял. STATUS: {new_status}, HOLDS_COUNT: {holds_count + 1}, PAYOUT: {payout}")

                    except Exception as e:
                        print(f"Ошибка при обработке номера {number}: {e}")

        except Exception as e:
            print(f"Ошибка в check_number_hold_time: {e}")
        
        time.sleep(60)
        
#Обработчики для получеяяния номеров



def get_number_in_group(user_id, chat_id, message_id, tg_number):
    try:
        # Проверяем права модератора
        if not db_module.is_moderator(user_id) and user_id not in config.ADMINS_ID:
            bot.send_message(chat_id, "❌ У вас нет прав для выполнения этой команды!", parse_mode='HTML')
            return

        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT NUMBER, ID_OWNER, SUBMIT_DATE
                FROM numbers
                WHERE STATUS = "ожидает" AND ID_OWNER NOT IN (SELECT ID FROM users WHERE AFK_LOCKED = 1)
                ORDER BY SUBMIT_DATE ASC
                LIMIT 1
            ''')
            number_data = cursor.fetchone()

        if not number_data:
            bot.send_message(chat_id, "📭 Нет доступных номеров для обработки.", parse_mode='HTML')
            return

        number, owner_id, submit_date = number_data

        # Обновляем номер в базе
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE numbers 
                SET STATUS = "taken", 
                    MODERATOR_ID = ?, 
                    TG_NUMBER = ?, 
                    TAKE_DATE = ?,
                    GROUP_CHAT_ID = ?
                WHERE NUMBER = ?
            ''', (user_id, tg_number, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), chat_id, number))
            conn.commit()

        # Инициализируем или обновляем active_code_requests для этого user_id
        if user_id not in active_code_requests:
            active_code_requests[user_id] = {}
        active_code_requests[user_id][message_id] = {
            'number': number,
            'owner_id': owner_id,
            'tg_number': tg_number,
            'chat_id': chat_id
        }

        # Получаем username модератора
        try:
            user = bot.get_chat_member(chat_id, user_id).user
            username = f"@{user.username}" if user.username else "Unknown"
        except Exception as e:
            print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
            username = "Unknown"

        # Создаем клавиатуру с кнопкой "Не валид"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("❌Не валид", callback_data=f"invalid_{number}_{tg_number}"))

        # Отправляем сообщение в группу с запросом фото
        sent_message = bot.send_message(
            chat_id,
            f"📱  <i>(ВЦ {tg_number})</i>\n"
            f"📌 <code>{number}</code>\n"
            f"<b>Действие:</b> <b>Отправьте фотографию для верификации</b> в ответ на это сообщение.\n"
            f"<i>⚠️ Убедитесь, что фото чёткое и соответствует номеру!</i>",
            parse_mode='HTML',
            reply_markup=markup
        )

        # Сохраняем сообщение для проверки реплая
        code_messages[number] = {
            "chat_id": chat_id,
            "message_id": sent_message.message_id,
            "timestamp": datetime.now(),
            "tg_number": tg_number,
            "owner_id": owner_id
        }

        print(f"[DEBUG] Модератор {user_id} взял номер {number}, ожидает фото для верификации.")

    except Exception as e:
        print(f"[ERROR] Ошибка при получении номера в группе: {e}")
        bot.send_message(chat_id, "❌ Произошла ошибка при получении номера.", parse_mode='HTML')


@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_invalid_"))
def mark_number_invalid(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    try:
        # Разбираем callback_data
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        group_chat_id = int(parts[3])
        tg_number = int(parts[4])

        # Проверяем, существует ли номер в базе и является ли пользователь владельцем
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER, MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            owner_id, moderator_id = result

            # Проверяем, что вызывающий пользователь является владельцем номера
            if call.from_user.id != owner_id:
                bot.answer_callback_query(call.id, "❌ У вас нет прав для пометки этого номера как невалидного!")
                return

            # Удаляем номер из базы
            try:
                cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
                conn.commit()
                print(f"[DEBUG] Номер {number} удалён из базы данных")
            except Exception as e:
                print(f"[ERROR] Ошибка при удалении номера {number} из базы: {e}")
                raise e

        # Формируем confirmation_key
        confirmation_key = f"{number}_{owner_id}"
        if confirmation_key in confirmation_messages:
            try:
                bot.delete_message(
                    confirmation_messages[confirmation_key]["chat_id"],
                    confirmation_messages[confirmation_key]["message_id"]
                )
            except Exception as e:
                print(f"[ERROR] Ошибка при удалении сообщения подтверждения {confirmation_key}: {e}")
            del confirmation_messages[confirmation_key]
            print(f"[DEBUG] Удалён confirmation_key {confirmation_key} из confirmation_messages")

        # Очищаем active_code_requests и уведомляем владельца, если есть активный запрос
        if owner_id in active_code_requests and number in active_code_requests[owner_id]:
            message_id = active_code_requests[owner_id][number]
            try:
                bot.edit_message_text(
                    f"❌ Запрос кода для номера {number} отменён, так как номер помечен как невалидный.",
                    owner_id,
                    message_id,
                    parse_mode='HTML'
                )
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось обновить сообщение для owner_id {owner_id}, message_id {message_id}: {e}")
            del active_code_requests[owner_id][number]
            print(f"[DEBUG] Удалён номер {number} из active_code_requests для owner_id {owner_id}")
            if not active_code_requests[owner_id]:
                del active_code_requests[owner_id]
                print(f"[DEBUG] Удалён owner_id {owner_id} из active_code_requests")

        # Уведомляем владельца
        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(
            types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        bot.edit_message_text(
            f"❌ Вы отметили номер {number} как невалидный. Номер удалён из системы.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup_owner,
            parse_mode='HTML'
        )

        # Уведомляем модератора в группе
        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        try:
            bot.send_message(
                group_chat_id,
                f"📱 <b>ВЦ {tg_number}</b>\n"
                f"❌ Владелец номера {number} отметил его как невалидный. \n Приносим свои извинения пожалуйста возьмите новый номер",
                reply_markup=markup_mod,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            print(f"[ERROR] Не удалось отправить сообщение в группу {group_chat_id}: {e}")
            if moderator_id:
                try:
                    bot.send_message(
                        moderator_id,
                        f"📱 <b>ВЦ {tg_number}</b>\n"
                        f"❌ Владелец номера {number} отметил его как невалидный. Номер удалён из системы.\n"
                        f"⚠️ Не удалось отправить сообщение в группу (ID: {group_chat_id}).",
                        reply_markup=markup_mod,
                        parse_mode='HTML'
                    )
                except telebot.apihelper.ApiTelegramException as e:
                    print(f"[ERROR] Не удалось отправить сообщение модератору {moderator_id}: {e}")

        bot.answer_callback_query(call.id, "✅ Номер отмечен как невалидный.")
    except Exception as e:
        print(f"[ERROR] Ошибка в mark_number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_invalid_"))
def moderator_mark_number_invalid(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 5:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        tg_number = int(parts[3])
        owner_id = int(parts[4])

        # Проверяем, является ли пользователь модератором
        if not db.is_moderator(call.from_user.id) and call.from_user.id not in config.ADMINS_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
            return

        # Проверяем, существует ли номер в базе
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            if result[0] != owner_id:
                bot.answer_callback_query(call.id, "❌ Неверный ID владельца!")
                return

            # Удаляем номер из базы
            cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
            conn.commit()

        bot.edit_message_text(
            f"✅ Номер {number} успешно удален из системы",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )

        # Уведомляем владельца
        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(
            types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        try:
            bot.send_message(
                owner_id,
                f"❌ Ваш номер {number} был отклонен модератором.\n📱 Проверьте номер и сдайте заново.",
                reply_markup=markup_owner,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            print(f"Не удалось отправить сообщение владельцу {owner_id}: {e}")
            for admin_id in config.ADMINS_ID:
                try:
                    bot.send_message(
                        admin_id,
                        f"⚠️ Не удалось уведомить владельца {owner_id} об отклонении номера {number}: {e}",
                        parse_mode='HTML'
                    )
                except:
                    pass

        # Очищаем confirmation_messages
        confirmation_key = f"{number}_{owner_id}"
        if confirmation_key in confirmation_messages:
            del confirmation_messages[confirmation_key]

        # Очищаем active_code_requests
        if owner_id in active_code_requests and number in active_code_requests[owner_id]:
            del active_code_requests[owner_id][number]
            if not active_code_requests[owner_id]:
                del active_code_requests[owner_id]

        bot.answer_callback_query(call.id, "✅ Номер успешно удалён.")
    except Exception as e:
        print(f"Ошибка в moderator_mark_number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера!")
        

# Словари для хранения контекста
confirmation_messages = {}
button_contexts = {}
code_messages = {}

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_code_"))
def process_confirm_code(call):
    try:
        data_parts = call.data.split("_")
        logging.debug(f"[DEBUG] Получен callback_data: {repr(call.data)}, parts: {data_parts}, length: {len(data_parts)}")

        if len(data_parts) < 3:
            logging.error(f"[ERROR] Некорректный формат callback_data: {call.data}, parts: {data_parts}, expected at least 3 parts")
            bot.answer_callback_query(call.id, "❌ Некорректные данные, попробуйте снова.")
            return
        if data_parts[0] != "confirm" or data_parts[1] != "code":
            logging.error(f"[ERROR] Неверный префикс callback_data: {call.data}, expected 'confirm_code'")
            bot.answer_callback_query(call.id, "❌ Некорректные данные, попробуйте снова.")
            return

        number = data_parts[2]
        tg_number = data_parts[3] if len(data_parts) > 3 else None
        if not tg_number:
            logging.error(f"[ERROR] Не найден tg_number в callback_data: {call.data}")
            bot.answer_callback_query(call.id, "❌ Некорректные данные, попробуйте снова.")
            return

        user_id = call.from_user.id
        # Используем chat_id из code_messages для группы
        group_chat_id = code_messages[number]['chat_id']

        logging.debug(f"[DEBUG] Подтверждение номера: number={number}, tg_number={tg_number}, user_id={user_id}, group_chat_id={group_chat_id}")

        if 'code_messages' not in globals() or number not in code_messages:
            logging.error(f"[ERROR] code_messages не инициализирован или номер {number} не найден")
            bot.answer_callback_query(call.id, "❌ Ошибка: данные о номере не найдены.")
            return

        owner_id = code_messages[number]['owner_id']
        if user_id != owner_id:
            logging.error(f"[ERROR] Пользователь {user_id} не является владельцем номера {number}, owner_id={owner_id}")
            bot.answer_callback_query(call.id, "❌ Только владелец может подтвердить номер.")
            return

        # Проверяем текущий статус номера
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
            status = cursor.fetchone()
            if not status:
                logging.error(f"[ERROR] Номер {number} не найден в таблице numbers")
                bot.answer_callback_query(call.id, "❌ Номер не найден в базе данных.")
                return

            status = status[0]
            if 'слет' in status.lower():
                logging.debug(f"[DEBUG] Номер {number} уже помечен как слетевший (статус: {status}), кнопки не отображаются")
                bot.send_message(
                    group_chat_id,
                    f"📱 <b>ВЦ {tg_number}</b>\n"
                    f"🔢 <b>Номер:</b> <code>{number}</code>\n"
                    f"❌ Номер уже помечен как слетевший ({status})",
                    parse_mode='HTML'
                )
                return

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logging.debug(f"[DEBUG] Текущее время: {current_time}")
            cursor.execute('''
                UPDATE numbers 
                SET STATUS = 'активен', TAKE_DATE = ?, CONFIRMED_BY_OWNER_ID = ?
                WHERE NUMBER = ?
            ''', (current_time, user_id, number))
            conn.commit()
            logging.debug(f"[DEBUG] Обновление статуса номера {number} выполнено")

        # Удаляем старое сообщение в группе с обработкой исключения
        message_id = code_messages[number]['message_id']
        try:
            bot.delete_message(group_chat_id, message_id)
            logging.debug(f"[DEBUG] Старое сообщение в группе {group_chat_id}, message_id={message_id} успешно удалено")
        except telebot.apihelper.ApiTelegramException as e:
            if "message to delete not found" in str(e) or e.result.status_code == 400:
                logging.warning(f"[WARNING] Сообщение для удаления не найдено (group_chat_id={group_chat_id}, message_id={message_id}), продолжаем")
            else:
                logging.error(f"[ERROR] Ошибка при удалении сообщения: {e}, продолжаем")
        except Exception as e:
            logging.error(f"[ERROR] Неожиданная ошибка при удалении сообщения: {e}, продолжаем")

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("⚠️ Не встал", callback_data=f"not_active_{number}_{tg_number}"),
            telebot.types.InlineKeyboardButton("Ошибка", callback_data=f"error_{number}_{tg_number}")
        )
        bot.send_message(
            group_chat_id,  # Отправляем в группу
            f"📱 <b>ВЦ {tg_number}</b>\n"
            f"🔢 <b>Номер:</b> <code>{number}</code>\n"
            f"✅ Подтверждён\n"
            f"<i>Если не так, нажмите «⚠️ Не встал» или «Ошибка»</i>",
            reply_markup=markup,
            parse_mode='HTML'
        )

        # Удаляем сообщение с фото у владельца и отправляем новое
        try:
            bot.delete_message(owner_id, call.message.message_id)
            bot.send_message(owner_id, f"🎉 Поздравляем! Ваш номер <code>{number}</code> успешно подтверждён! ✅\n⏳ Отсчёт времени начат.", parse_mode='HTML')
            logging.debug(f"[DEBUG] Сообщение с фото у владельца {user_id} удалено, отправлено новое сообщение")
        except Exception as e:
            logging.error(f"[ERROR] Не удалось удалить сообщение с фото у владельца или отправить новое: {e}")

        logging.debug(f"[DEBUG] Номер {number} подтверждён и переведён в статус 'активен' владельцем")
        bot.answer_callback_query(call.id, "✅ Номер подтверждён, отсчёт начат!")

    except Exception as e:
        logging.error(f"Ошибка при подтверждении номера: {str(e)} - Контекст: number={number}, user_id={user_id}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при подтверждении.") 

@bot.callback_query_handler(func=lambda call: call.data.startswith("not_active_"))
def process_not_active(call):
    try:
        data_parts = call.data.split("_")
        if len(data_parts) < 3:
            logging.error(f"[ERROR] Некорректный формат callback_data: {call.data}, expected at least 3 parts")
            bot.answer_callback_query(call.id, "❌ Некорректные данные, попробуйте снова.")
            return

        number = data_parts[2]
        tg_number = data_parts[3] if len(data_parts) > 3 else None
        if not tg_number:
            logging.error(f"[ERROR] Не найден tg_number в callback_data: {call.data}")
            bot.answer_callback_query(call.id, "❌ Некорректные данные, попробуйте снова.")
            return

        user_id = call.from_user.id
        group_chat_id = call.message.chat.id

        logging.debug(f"[DEBUG] Пометка номера как 'Не встал': number={number}, tg_number={tg_number}, user_id={user_id}")

        # Проверяем текущий статус номера
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
            status = cursor.fetchone()
            if not status:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            status = status[0]
            forbidden_statuses = ["слёт 1/2 холд", "слёт 2/2", "слёт 2/2+", "не валид"]
            if status in forbidden_statuses:
                bot.answer_callback_query(call.id, f"❌ Номер {number} уже помечен как слетевший ({status}), действие отменено.")
                return

        # Получаем ID владельца номера
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner_id = cursor.fetchone()
            if not owner_id:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            owner_id = owner_id[0]

        # Увеличиваем количество предупреждений владельца
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET WARNINGS = WARNINGS + 1 WHERE ID = ?', (owner_id,))
            conn.commit()

        # Обновляем статус номера на "⚠️ Не встал"
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ("⚠️ Не встал", number))
            conn.commit()

        # Уведомляем владельца
        markup_owner = telebot.types.InlineKeyboardMarkup()
        markup_owner.add(types.InlineKeyboardButton("📱 Сдать новый номер", callback_data="submit_number"))
        markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(
            owner_id,
            f"⚠️ Ваш номер <code>{number}</code> был помечен как 'Не встал'. ❌\n"
            f"🔒 Это добавило вам 1 предупреждение. Пожалуйста, сдайте новый номер.",
            reply_markup=markup_owner,
            parse_mode='HTML'
        )

        # Обновляем сообщение в группе для модератора
        bot.edit_message_text(
            f"📱 <b>ВЦ {tg_number}</b>\n"
            f"🔢 <b>Номер:</b> <code>{number}</code>\n"
            f"⚠️ Вы отметили как 'Не встал'",
            group_chat_id,
            call.message.message_id,
            parse_mode='HTML'
        )

        logging.debug(f"[DEBUG] Номер {number} помечен как 'Не встал', предупреждение добавлено владельцу {owner_id}")
        bot.answer_callback_query(call.id, "✅ Номер помечен как 'Не встал'.")

    except Exception as e:
        logging.error(f"Ошибка при пометке номера как 'Не встал': {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("error_"))
def process_error(call):
    try:
        _, number, tg_number = call.data.split("_")
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        logging.debug(f"[DEBUG] Пометка номера как ошибка: number={number}, tg_number={tg_number}, user_id={user_id}")

        # Проверяем текущий статус номера
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
            status = cursor.fetchone()
            if not status:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            status = status[0]
            forbidden_statuses = ["слёт 1/2 холд", "слёт 2/2", "слёт 2/2+", "не валид"]
            if status in forbidden_statuses:
                bot.answer_callback_query(call.id, f"❌ Номер {number} уже помечен как слетевший ({status}), действие отменено.")
                return

        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Обновляем статус на "Ошибка" и помечаем номер как исключённый из повторной сдачи
            cursor.execute('UPDATE numbers SET STATUS = ?, IS_EXCLUDED = 1 WHERE NUMBER = ?', ("Ошибка", number))
            conn.commit()

        # Обновляем сообщение в группе
        bot.edit_message_text(
            f"📱 <b>ВЦ {tg_number}</b>\n"
            f"🔢 <b>Номер:</b> <code>{number}</code>\n"
            f"❌ <b>Помечен как ошибка</b>",
            chat_id,
            call.message.message_id,
            parse_mode='HTML'
        )

        # Уведомляем владельца
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner_id = cursor.fetchone()
        if owner_id:
            markup_owner = telebot.types.InlineKeyboardMarkup()
            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(
                owner_id[0],
                f"⚠️ Ваш номер <code>{number}</code> был помечен как ошибка. ❌\n"
                f"Этот номер исключён из повторной сдачи. Пожалуйста, сдайте новый номер.",
                reply_markup=markup_owner,
                parse_mode='HTML'
            )

        logging.debug(f"[DEBUG] Номер {number} помечен как 'Ошибка'")
        bot.answer_callback_query(call.id, "✅ Номер помечен как ошибка.")

    except Exception as e:
        logging.error(f"Ошибка при пометке номера как ошибка: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при пометке.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("invalid_code_"))
def invalid_code(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return

        number = parts[2]
        tg_number = int(parts[3])

        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ID_OWNER, MODERATOR_ID, GROUP_CHAT_ID
                FROM numbers
                WHERE NUMBER = ? AND STATUS = "taken"
            ''', (number,))
            number_data = cursor.fetchone()

        if not number_data:
            bot.answer_callback_query(call.id, "❌ Номер не найден или не в статусе 'taken'!")
            return

        owner_id, moderator_id, group_chat_id = number_data

        # Проверяем, что пользователь является владельцем номера
        if call.from_user.id != owner_id:
            bot.answer_callback_query(call.id, "❌ Вы не владелец этого номера!")
            return

        # Обновляем статус номера на "слетел"
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE numbers 
                SET STATUS = "слетел", 
                    SHUTDOWN_DATE = ?,
                    VERIFICATION_CODE = NULL
                WHERE NUMBER = ?
            ''', (current_time, number))
            conn.commit()

        # Уведомляем пользователя
        bot.send_message(
            owner_id,
            f"❌ Номер <code>{number}</code> помечен как невалидный.",
            parse_mode='HTML'
        )

        # Обновляем сообщение в группе
        if number in code_messages:
            bot.edit_message_text(
                f"📱 <b>ВЦ {tg_number}</b>\n"
                f"❌ Номер <code>{number}</code> помечен как невалидный в {current_time}.",
                code_messages[number]["chat_id"],
                code_messages[number]["message_id"],
                parse_mode='HTML'
            )
            del code_messages[number]

        bot.answer_callback_query(call.id, "❌ Номер помечен как невалидный.")

        print(f"[DEBUG] Номер {number} помечен как невалидный пользователем {owner_id}")

    except Exception as e:
        print(f"[ERROR] Ошибка при обработке невалидного кода: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке кода!")


def create_back_to_main_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    return markup

#Обработчики для подтверждения/отклонения номеров

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_reject_"))
def handle_number_rejection(call):
    number = call.data.split("_")[2]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
        owner = cursor.fetchone()
        cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
        conn.commit()

        if owner:
            markup_owner = types.InlineKeyboardMarkup()
            markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"))
            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(owner[0], 
                               f"❌ Ваш номер {number} был отклонен модератором.\n📱 Проверьте номер и сдайте заново.", 
                               reply_markup=markup_owner)
            except:
                pass

    markup_mod = types.InlineKeyboardMarkup()
    markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
    markup_mod.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    bot.edit_message_text(f"📱 Номер {number} отклонен и удалён из очереди.\n❌ Номер не встал.", 
                         call.message.chat.id, 
                         call.message.message_id, 
                         reply_markup=markup_mod)





@bot.callback_query_handler(func=lambda call: call.data.startswith("number_active_"))
def number_active(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return

        number = parts[2]
        tg_number = int(parts[3])

        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner = cursor.fetchone()
            if not owner:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return

            # Устанавливаем статус 'активен', ID модератора и время подтверждения
            current_time = datetime.now().strftime("%H:%M")
            cursor.execute(
                '''
                UPDATE numbers 
                SET STATUS = ?, 
                    CONFIRMED_BY_MODERATOR_ID = ?, 
                    TAKE_DATE = ? 
                WHERE NUMBER = ?
                ''',
                ('активен', call.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), number)
            )
            conn.commit()
            print(f"[DEBUG] Номер {number} подтверждён модератором {call.from_user.id}, статус: активен, TAKE_DATE: {current_time}")

        if owner:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(
                    owner[0],
                    f"✅ Ваш номер {number} подтверждён и теперь активен.\n⏳ Встал: {current_time}.",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                print(f"[DEBUG] Уведомление отправлено владельцу {owner[0]} о подтверждении номера {number}")
            except Exception as e:
                print(f"[ERROR] Не удалось отправить уведомление владельцу {owner[0]}: {e}")

        # Обновляем сообщение в группе
        bot.edit_message_text(
            f"📱 <b>ВЦ {tg_number}</b>\n"
            f"✅ Номер <code>{number}</code> подтверждён в {current_time}.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "✅ Номер успешно подтверждён!")

    except Exception as e:
        print(f"[ERROR] Ошибка в number_active: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при подтверждении номера!")


@bot.callback_query_handler(func=lambda call: call.data.startswith("invalid_"))
def handle_invalid_number(call):
    number = call.data.split("_")[1]
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER, MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            owner_id, moderator_id = result
            if call.from_user.id != moderator_id and call.from_user.id not in config.ADMINS_ID:
                bot.answer_callback_query(call.id, "❌ Вы не можете отклонить этот номер!")
                return

            cursor.execute('UPDATE numbers SET STATUS = "слетел", SHUTDOWN_DATE = ?, VERIFICATION_CODE = NULL WHERE NUMBER = ?',
                          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), number))
            conn.commit()

        markup = create_back_to_main_markup()
        safe_send_message(
            owner_id,
            f"❌ Ваш номер <code>{number}</code> был отклонён модератором.\n"
            f"📱 Проверьте номер и сдайте заново.",
            parse_mode='HTML',
            reply_markup=markup
        )

        bot.edit_message_text(
            f"✅ Номер <code>{number}</code> помечен как слетевший.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        if number in code_messages:
            del code_messages[number]
        bot.answer_callback_query(call.id, "✅ Номер помечен как слетевший.")

    except Exception as e:
        print(f"[ERROR] Ошибка в handle_invalid_number: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("number_failed_"))
def handle_number_failed(call):
    number = call.data.split("_")[2]
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT TAKE_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, STATUS FROM numbers WHERE NUMBER = ?', (number,))
            data = cursor.fetchone()
            if not data:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            
            take_date, owner_id, confirmed_by_moderator_id, status = data
            
            if status == "отстоял":
                bot.answer_callback_query(call.id, "✅ Номер уже отстоял своё время!")
                return

            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5
            
            end_time = datetime.now()
            if take_date in ("0", "1"):
                work_time = 0
                worked_enough = False
            else:
                start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                work_time = (end_time - start_time).total_seconds() / 60
                worked_enough = work_time >= hold_time
            
            shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE numbers SET SHUTDOWN_DATE = ?, STATUS = "слетел" WHERE NUMBER = ?', 
                          (shutdown_date, number))
            conn.commit()
        
        mod_message = (
            f"📱 Номер: {number}\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            mod_message += f"🟢 Встал: {take_date}\n"
        mod_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            mod_message += f"⚠️ Номер не отработал минимальное время ({hold_time} минут)!\n"
        mod_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        owner_message = (
            f"❌ Ваш номер {number} слетел.\n"
            f"📱 Номер: {number}\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            owner_message += f"🟢 Встал: {take_date}\n"
        owner_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            owner_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        bot.send_message(owner_id, owner_message, parse_mode='HTML')
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderator_numbers"))
        bot.edit_message_text(mod_message, 
                            call.message.chat.id, 
                            call.message.message_id, 
                            reply_markup=markup,
                            parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_number_failed: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")


#Просмотр номеров:




@bot.callback_query_handler(func=lambda call: call.data.startswith("view_failed_number_"))
def view_failed_number(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
        f"🟢 Встал: {take_date}\n"
        f"🔴 Слетел: {shutdown_date}\n"
    )
    
    markup = types.InlineKeyboardMarkup()
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_stood_number_"))
def view_stood_number(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
        f"🟢 Встал: {take_date}\n"
        f"🟢 Отстоял: {shutdown_date}\n"
    )
    
    markup = types.InlineKeyboardMarkup()
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_number_details_"))
def view_number_details(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE, MODERATOR_ID, CONFIRMED_BY_MODERATOR_ID 
            FROM numbers 
            WHERE NUMBER = ?
        ''', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date, moderator_id, confirmed_by_moderator_id = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
    )
    # Показываем "Встал" только для статусов "активен" или "отстоял" и если TAKE_DATE не "0" или "1"
    if status in ("активен", "отстоял") and take_date not in ("0", "1"):
        text += f"🟢 Встал: {take_date}\n"
    if shutdown_date and shutdown_date != "0":
        text += f"{'🟢 Отстоял' if status == 'отстоял' else '🔴 Слетел'}: {shutdown_date}\n"
    
    markup = types.InlineKeyboardMarkup()
    if is_moderator and shutdown_date == "0" and status == "активен" and confirmed_by_moderator_id == user_id:
        markup.add(types.InlineKeyboardButton("-", callback_data=f""))
    
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    

       

































active_code_requests = {}

@bot.message_handler(content_types=['text'], func=lambda message: message.chat.type in ['group', 'supergroup'])
def handle_group_commands(message):
    try:
        user_id = message.from_user.id
        chat_id = message.chat.id
        message_id = message.message_id
        text = message.text.strip()

        # Проверяем, является ли чат зарегистрированной группой
        if chat_id not in db_module.get_all_group_ids():
            return

        # Обработка команд вцN, пкN
        pattern = r'^(вц|пк|ВЦ|ПК)\s*(\d+)$'
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            prefix, number_str = match.groups()
            tg_number = int(number_str)
            if not 1 <= tg_number <= 70:
                bot.reply_to(message, "❌ Число должно быть в диапазоне от 1 до 70.", parse_mode='HTML')
                return
            get_number_in_group(user_id, chat_id, message_id, tg_number)

        # Обработка команды слет/слёт +номер
        valid_commands = ['слет', 'слёт', 'Слет', 'Слёт', 'СЛЕТ', 'СЛЁТ', '/слет', '/cлёт', '/СЛЕТ', '/СЛЁТ']
        parts = text.split()
        if parts and parts[0] in valid_commands:
            logging.info(f"[DEBUG] Обработчик текста вызван, сообщение: {message.text}, chat_type: {message.chat.type}, user_id: {message.from_user.id}, chat_id: {message.chat.id}")
            is_mod = db_module.is_moderator(user_id)
            logging.info(f"[DEBUG] Права: is_moderator({user_id}) = {is_mod}, ADMINS_ID = {config.ADMINS_ID}")
            if not is_mod and user_id not in config.ADMINS_ID:
                bot.reply_to(message, "❌ У вас нет прав!")
                logging.info(f"[DEBUG] Нет прав для user_id {user_id}")
                return

            if len(parts) != 2:
                bot.reply_to(message, "❌ Используйте формат: <команда> +номер (например, слёт +7900123455)")
                logging.info(f"[DEBUG] Неверный формат команды {text} от user_id {user_id}")
                return

            number = parts[1].strip()
            logging.info(f"[DEBUG] Извлечён номер: {number}")

            if not number.startswith('+') or len(number) < 10:
                bot.reply_to(message, "❌ Неверный формат номера!")
                logging.info(f"[DEBUG] Неверный формат номера {number} от user_id {user_id}")
                return

            with db_module.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT TAKE_DATE, STATUS, HOLDS_COUNT
                    FROM numbers
                    WHERE NUMBER = ?
                ''', (number,))
                data = cursor.fetchone()

                if not data:
                    bot.reply_to(message, f"❌ Номер {number} не найден в базе!")
                    logging.info(f"[DEBUG] Номер {number} не найден для user_id {user_id}")
                    return

                take_date, status, holds_count = data
                end_time = datetime.now()
                shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")

                if status == "отстоял 1/2":
                    new_status = "слёт 1/2 холд"
                elif status == "отстоял 2/2":
                    new_status = "слёт 2/2"
                elif status == "отстоял 2/2+ холд":
                    new_status = "слёт 2/2+"
                elif status == "taken":
                    new_status = "не валид"
                elif status == "активен":
                    new_status = "не валид"
                else:
                    bot.reply_to(message, f"❌ Номер {number} не в подходящем статусе для слёта ({status})!")
                    logging.info(f"[DEBUG] Неподходящий статус {status} для номера {number} от user_id {user_id}")
                    return

                cursor.execute('''
                    UPDATE numbers 
                    SET STATUS = ?, SHUTDOWN_DATE = ?, HOLDS_COUNT = HOLDS_COUNT + 1
                    WHERE NUMBER = ?
                ''', (new_status, shutdown_date, number))
                conn.commit()

                work_time = 0
                if take_date not in ("0", "1"):
                    start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                    work_time = (end_time - start_time).total_seconds() / 60

                mod_message = (
                    f"📱 Номер: <code>{number}</code>\n"
                    f"📊 Статус: {new_status}\n"
                    f"🟢 Встал: {take_date if take_date not in ('0', '1') else 'Неизвестно'}\n"
                    f"🔴 Слетел: {shutdown_date}\n"
                    f"⏳ Время работы: {work_time:.2f} минут"
                )
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.send_message(chat_id, mod_message, parse_mode='HTML', reply_markup=markup)

                with db_module.get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
                    owner_id = cursor.fetchone()
                    if owner_id:
                        owner_message = (
                            f"❌ Ваш номер {number} слетел.\n"
                            f"📱 Номер: <code>{number}</code>\n"
                            f"📊 Статус: {new_status}\n"
                            f"🟢 Встал: {take_date if take_date not in ('0', '1') else 'Неизвестно'}\n"
                            f"🔴 Слетел: {shutdown_date}\n"
                            f"⏳ Время работы: {work_time:.2f} минут"
                        )
                        markup_owner = types.InlineKeyboardMarkup()
                        markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
                        markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        safe_send_message(owner_id[0], owner_message, parse_mode='HTML', reply_markup=markup_owner)

                logging.info(f"[DEBUG] Успешная обработка {text} для номера {number} от user_id {user_id}")

    except Exception as e:
        logging.error(f"Ошибка при обработке {text}: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при обработке команды.")


@bot.message_handler(content_types=['photo'], func=lambda message: message.chat.type in ['group', 'supergroup'])
def handle_photo_commands(message):
    try:
        user_id = message.from_user.id
        chat_id = message.chat.id
        message_id = message.message_id

        logging.debug(f"[DEBUG] Получено фото: user_id={user_id}, chat_id={chat_id}, message_id={message_id}")

        if chat_id not in db_module.get_all_group_ids():
            logging.debug(f"[DEBUG] Чат {chat_id} не зарегистрирован")
            return

        if not message.reply_to_message:
            logging.debug(f"[DEBUG] Нет реплая: message.reply_to_message={message.reply_to_message}")
            bot.reply_to(message, "❌ Отправьте фото в ответ на сообщение с номером.", parse_mode='HTML')
            return
        if message.reply_to_message.from_user.id != bot.user.id:
            logging.debug(f"[DEBUG] Реплай не от бота: reply_from={message.reply_to_message.from_user.id}, bot_id={bot.user.id}")
            bot.reply_to(message, "❌ Отправьте фото в ответ на моё сообщение.", parse_mode='HTML')
            return

        photo_id = message.photo[-1].file_id
        reply_message_id = message.reply_to_message.message_id

        logging.debug(f"[DEBUG] Фото обработано: photo_id={photo_id}, reply_message_id={reply_message_id}")

        number = None
        tg_number = None
        owner_id = None
        if 'code_messages' in globals() and code_messages:
            logging.debug(f"[DEBUG] code_messages: {code_messages}")
            for num, data in code_messages.items():
                logging.debug(f"[DEBUG] Проверка: message_id={data['message_id']}, reply_message_id={reply_message_id}, chat_id={data['chat_id']}, current_chat_id={chat_id}")
                if (data['message_id'] == reply_message_id and
                        data['chat_id'] == chat_id and
                        user_id in active_code_requests and
                        any(req['number'] == num for req in active_code_requests[user_id].values())):
                    number = num
                    tg_number = data['tg_number']
                    owner_id = data['owner_id']
                    logging.debug(f"[DEBUG] Найден номер: {number}, tg_number={tg_number}, owner_id={owner_id}")
                    break

        if not number or not tg_number or not owner_id:
            logging.debug(f"[DEBUG] Номер не найден в code_messages для reply_message_id={reply_message_id}")
            bot.reply_to(message, "❌ Не удалось привязать фото к номеру.", parse_mode='HTML')
            return

        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
            status = cursor.fetchone()
            if status and status[0] != 'taken':
                logging.debug(f"[DEBUG] Неверный статус: {status[0]}")
                return

        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE numbers 
                SET PHOTO_ID = ?
                WHERE NUMBER = ?
            ''', (photo_id, number))
            conn.commit()

        # Отправляем новое сообщение в группу вместо редактирования
        sent_message = bot.send_message(
            chat_id,
            f"📲 <b><u>ВЦ {tg_number}</u></b>\n"
            f"🔥 <b>Номер:</b> <code>{number}</code>\n"
            f"🎥 <b>Фотка проверена и принята! ✅</b>\n"
            f"<i>Ожидаем подтверждение от пользователя 🚀</i>",
            parse_mode='HTML'
        )
        code_messages[number] = {'message_id': sent_message.message_id, 'chat_id': chat_id, 'owner_id': owner_id, 'tg_number': tg_number}
        logging.debug(f"[DEBUG] Новое сообщение отправлено в группу: chat_id={chat_id}, message_id={sent_message.message_id}")

        try:
            bot.delete_message(chat_id, message_id)
            logging.debug(f"[DEBUG] Сообщение с фото от модератора {user_id} удалено, message_id={message_id}")
        except Exception as e:
            logging.error(f"[ERROR] Не удалось удалить сообщение с фото: {e}")

        callback_data = f"confirm_code_{number}_{tg_number}"
        logging.debug(f"[DEBUG] Сформирован callback_data: {repr(callback_data)}")
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("✅ Подтвердить", callback_data=callback_data),
            telebot.types.InlineKeyboardButton("❌ Не валидная фотография", callback_data=f"invalid_code_{number}_{tg_number}")
        )
        try:
            bot.send_photo(
                owner_id,
                photo_id,
                caption=f"📱 <b><u>ВЦ {tg_number}</u></b>\n"
                        f"🎉 Ваш номер <code>{number}</code> взят в обработку! 🔥\n"
                        f"<i>🚀 Подтвердите валидность фотки или отметьте, если что-то не так! ❌</i>",
                reply_markup=markup,
                parse_mode='HTML'
            )
            logging.debug(f"[DEBUG] Фото отправлено владельцу {owner_id}")
        except Exception as e:
            logging.error(f"[ERROR] Не удалось отправить фото владельцу {owner_id}: {e}")
            bot.reply_to(message, "❌ Ошибка при отправке фото владельцу.", parse_mode='HTML')
            return

        print(f"[DEBUG] Модератор {user_id} отправил фото {photo_id} для номера {number}")

    except Exception as e:
        logging.error(f"Ошибка при обработке фото: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при обработке фотографии.")

def safe_send_message(chat_id, text, parse_mode=None, reply_markup=None):
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения пользователю {chat_id}: {e}")


# Глобальный словарь для отслеживания активных запросов кодов по user_id
active_code_requests = {}


@bot.callback_query_handler(func=lambda call: call.data == "toggle_afk")
def toggle_afk(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    new_afk_status = db_module.toggle_afk_status(user_id)
    
    
    # Уведомление о смене статуса АФК
    try:
        if new_afk_status:
            bot.send_message(
                call.message.chat.id,
                "🔔 Вы вошли в режим АФК. Ваши номера скрыты. Что-бы выйти из рeжима АФК, пропишите /start",
                parse_mode='HTML'
            )
        else:
            bot.send_message(
                call.message.chat.id,
                "🔔 Вы вышли из режима АФК. Ваши номера снова видны.",
                parse_mode='HTML'
            )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление о смене АФК пользователю {user_id}: {e}")
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)
    
    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)
    
    if is_moderator:
        welcome_text = "Заявки"
    else:
        welcome_text = (
                f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                f"<b>💼 Как это работает?</b>\n"
                f"• <i>Вы сдаете номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                f"• <i>Моментальные выплаты</i> – <b>после стоп ворка.</b>\n\n"
                f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
                f"✅ <i>Прозрачные условия сотрудничества</i>\n"
                f"✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                f"✅ <i>Оперативная поддержка 24/7</i>\n\n"
                f"<b>💰 Тарифы на сдачу номеров:</b>\n"
                f"▪️ 6$ за номер (холд 1-6$, 2-12$)\n\n"
                "<b>🔹 Начните зарабатывать прямо сейчас!</b>",
        )
    
    markup = types.InlineKeyboardMarkup()
    if not is_moderator or is_admin:
        markup.row(
            types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
            types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
        )
    if is_admin:
        markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))
    if is_moderator:
        markup.add(
            types.InlineKeyboardButton("📲 Получить номер", callback_data="get_number"),
            types.InlineKeyboardButton("📋 Мои номера", callback_data="moderator_numbers")
        )
    afk_button_text = "🟢 Включить АФК" if not new_afk_status else "🔴 Выключить АФК"
    markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
    
    bot.edit_message_text(
        welcome_text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML' if not is_moderator else None,
        reply_markup=markup
    )
    
    status_text = "включён" if new_afk_status else "выключен"
    bot.answer_callback_query(call.id, f"Режим АФК {status_text}. Ваши номера {'скрыты' if new_afk_status else 'видимы'}.")


def init_db():
    db_module.create_tables()
    db_module.migrate_db()
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(numbers)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'GROUP_CHAT_ID' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN GROUP_CHAT_ID INTEGER')
                print("[INFO] Столбец GROUP_CHAT_ID успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец GROUP_CHAT_ID: {e}")

        if 'TG_NUMBER' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN TG_NUMBER INTEGER DEFAULT 1')
                print("[INFO] Столбец TG_NUMBER успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец TG_NUMBER: {e}")

        if 'VERIFICATION_CODE' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN VERIFICATION_CODE TEXT')
                print("[INFO] Столбец VERIFICATION_CODE успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец VERIFICATION_CODE: {e}")

        if 'IS_EXCLUDED' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN IS_EXCLUDED INTEGER DEFAULT 0')
                print("[INFO] Столбец IS_EXCLUDED успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец IS_EXCLUDED: {e}")

        cursor.execute("PRAGMA table_info(users)")
        user_columns = [col[1] for col in cursor.fetchall()]
        
        if 'IS_AFK' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN IS_AFK INTEGER DEFAULT 0')
                print("[INFO] Столбец IS_AFK успешно добавлен в таблицу users.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец IS_AFK: {e}")

        if 'LAST_ACTIVITY' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN LAST_ACTIVITY TEXT')
                print("[INFO] Столбец LAST_ACTIVITY успешно добавлен в таблицу users.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец LAST_ACTIVITY: {e}")

        if 'WARNINGS' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN WARNINGS INTEGER DEFAULT 0')
                print("[INFO] Столбец WARNINGS успешно добавлен в таблицу users.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец WARNINGS: {e}")

        conn.commit()

@bot.callback_query_handler(func=lambda call: call.data.startswith("number_invalid_"))
def number_invalid(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return

        number = parts[2]
        tg_number = int(parts[3])

        # Проверяем права модератора
        if not db_module.is_moderator(call.from_user.id) and call.from_user.id not in config.ADMINS_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для обработки номера!")
            return

        # Обновляем статус номера на "слетел"
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE numbers 
                SET STATUS = "слетел", 
                    SHUTDOWN_DATE = ?,
                    VERIFICATION_CODE = NULL
                WHERE NUMBER = ?
            ''', (current_time, number))
            conn.commit()

        # Уведомляем владельца
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner = cursor.fetchone()

        if owner:
            try:
                bot.send_message(
                    owner[0],
                    f"❌ Ваш номер <code>{number}</code> помечен как невалидный.",
                    parse_mode='HTML'
                )
                print(f"[DEBUG] Уведомление отправлено владельцу {owner[0]} о невалидности номера {number}")
            except Exception as e:
                print(f"[ERROR] Не удалось отправить уведомление владельцу {owner[0]}: {e}")

        # Обновляем сообщение в группе
        if number in code_messages:
            bot.edit_message_text(
                f"📱 <b>ВЦ {tg_number}</b>\n"
                f"❌ Номер <code>{number}</code> помечен как невалидный в {current_time}.",
                code_messages[number]["chat_id"],
                code_messages[number]["message_id"],
                parse_mode='HTML'
            )
            del code_messages[number]

        bot.answer_callback_query(call.id, "❌ Номер помечен как невалидный.")

        print(f"[DEBUG] Номер {number} помечен как невалидный модератором {call.from_user.id}")

    except Exception as e:
        print(f"[ERROR] Ошибка в number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера!")

db_lock = Lock()

def check_inactivity():
    """Проверяет неактивность пользователей и переводит их в АФК через 10 минут."""
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT ID, LAST_ACTIVITY, IS_AFK FROM users')
                users = cursor.fetchall()
                current_time = datetime.now()
                for user_id, last_activity, is_afk in users:
                    # Пропускаем пользователей, которые уже в АФК или без активности
                    if is_afk or not last_activity:
                        continue
                    # Проверяем, является ли пользователь модератором
                    cursor.execute('SELECT ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
                    is_moder = cursor.fetchone() is not None
                    # Проверяем, является ли пользователь администратором из config.ADMINS_ID
                    is_admin = user_id in config.ADMINS_ID
                    if is_moder or is_admin:
                        continue  # Пропускаем модераторов и администраторов
                    try:
                        last_activity_time = datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
                        if current_time - last_activity_time >= timedelta(minutes=10):
                            # Переводим в АФК, только если пользователь ещё не в АФК
                            if not db_module.get_afk_status(user_id):
                                db_module.toggle_afk_status(user_id)
                                try:
                                    bot.send_message(
                                        user_id,
                                        "🔔 Вы были переведены в режим АФК из-за неактивности (10 минут). "
                                        "Ваши номера скрыты. Нажмите 'Выключить АФК' в главном меню, чтобы вернуться.",
                                        parse_mode='HTML'
                                    )
                                except Exception as e:
                                    print(f"[ERROR] Не удалось отправить уведомление об АФК пользователю {user_id}: {e}")
                    except ValueError as e:
                        print(f"[ERROR] Неверный формат времени активности для пользователя {user_id}: {e}")
            time.sleep(60)  # Проверяем каждую минуту
        except Exception as e:
            print(f"[ERROR] Ошибка в check_inactivity: {e}")
            time.sleep(60)

if __name__ == "__main__":
    init_db()
    check_all_users_for_afk()
    timeout_thread = threading.Thread(target=check_number_timeout, daemon=True)
    timeout_thread.start()
    hold_time_thread = threading.Thread(target=check_number_hold_time, daemon=True)
    hold_time_thread.start()
    inactivity_thread = threading.Thread(target=check_inactivity, daemon=True)
    inactivity_thread.start()
    run_bot()