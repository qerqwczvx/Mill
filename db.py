import sqlite3
from datetime import datetime
import config
import requests  

def get_db():
    """Создаёт и возвращает подключение к базе данных."""
    return sqlite3.connect('database.db', check_same_thread=False)

def create_tables():
    """Создаёт все необходимые таблицы в базе данных."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Таблица requests
        cursor.execute('''CREATE TABLE IF NOT EXISTS requests (
            ID INTEGER PRIMARY KEY,
            LAST_REQUEST TIMESTAMP,
            STATUS TEXT DEFAULT 'pending',
            BLOCKED INTEGER DEFAULT 0,
            CAN_SUBMIT_NUMBERS INTEGER DEFAULT 1
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS groups (
            ID INTEGER PRIMARY KEY,
            NAME TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS checks (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            USER_ID INTEGER NOT NULL,
            AMOUNT REAL NOT NULL,
            CHECK_CODE TEXT,
            STATUS TEXT DEFAULT 'pending',
            CREATED_AT TEXT NOT NULL,
            ACTIVATED_AT TEXT,
            FOREIGN KEY (USER_ID) REFERENCES users (ID)
        )''')
        # Таблица withdraws
        cursor.execute('''CREATE TABLE IF NOT EXISTS withdraws (
            ID INTEGER,
            AMOUNT REAL,
            DATE TEXT,
            STATUS TEXT DEFAULT 'pending'
        )''')
        # Таблица users (добавляем STATUS)
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            ID INTEGER PRIMARY KEY,
            BALANCE REAL DEFAULT 0,
            REG_DATE TEXT,
            IS_AFK INTEGER DEFAULT 0,
            LAST_ACTIVITY TEXT,
            AFK_LOCKED INTEGER DEFAULT 0,
            STATUS TEXT DEFAULT 'pending',
            USERNAME TEXT
        )''')
        # Таблица numbers
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS numbers (
            NUMBER TEXT PRIMARY KEY,
            ID_OWNER INTEGER,
            STATUS TEXT,
            TAKE_DATE TEXT,
            CONFIRMED_BY_OWNER_ID NULL,
            SHUTDOWN_DATE TEXT,
            CONFIRMED_BY_MODERATOR_ID INTEGER,
            TG_NUMBER INTEGER,
            SUBMIT_DATE TEXT,
            VERIFICATION_CODE TEXT,
            HOLDS_COUNT INTEGER DEFAULT 0
        )
        ''')
        # Таблица settings с PRICE_ADM
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            PRICE REAL DEFAULT 2.0,
            HOLD_TIME INTEGER DEFAULT 5,
            PRICE_ADM REAL DEFAULT 4.5
        )''')
        # Инициализация settings
        cursor.execute('SELECT COUNT(*) FROM settings')
        if cursor.fetchone()[0] == 0:
            cursor.execute('INSERT INTO settings (PRICE, HOLD_TIME, PRICE_ADM) VALUES (?, ?, ?)', (2.0, 5, 4.5))
        # Таблица personal
        cursor.execute('''CREATE TABLE IF NOT EXISTS personal (
            ID INTEGER PRIMARY KEY,
            TYPE TEXT NOT NULL,
            GROUP_ID INTEGER
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS treasury (
            ID INTEGER PRIMARY KEY CHECK (ID = 1),
            BALANCE REAL DEFAULT 0,
            AUTO_INPUT INTEGER DEFAULT 0,
            CURRENCY TEXT DEFAULT 'USDT'
        )''')
        # Инициализация treasury
        cursor.execute('SELECT COUNT(*) FROM treasury')
        if cursor.fetchone()[0] == 0:
            cursor.execute('INSERT INTO treasury (ID, BALANCE, AUTO_INPUT, CURRENCY) VALUES (?, ?, ?, ?)',
                          (1, 0, 0, 'USDT'))
        conn.commit()

def get_all_group_ids():
    """Возвращает список всех ID групп из таблицы groups."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM groups')
        return [row[0] for row in cursor.fetchall()]

def set_custom_price(user_id, price):
    """Устанавливает индивидуальную цену для пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET CUSTOM_PRICE = ? WHERE ID = ?', (price, user_id))
        if cursor.rowcount == 0:
            add_user(user_id)  # Создаём пользователя, если его нет
            cursor.execute('UPDATE users SET CUSTOM_PRICE = ? WHERE ID = ?', (price, user_id))
        conn.commit()
        print(f"[DEBUG] Установлена индивидуальная цена {price}$ для пользователя {user_id}")

def migrate_db():
    """Миграция базы данных: добавляет новые столбцы, если их нет."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('PRAGMA table_info(users)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'USERNAME' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN USERNAME TEXT')
            print("[INFO] Столбец USERNAME добавлен в таблицу users.")
        if 'CUSTOM_PRICE' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN CUSTOM_PRICE REAL')
            print("[INFO] Столбец CUSTOM_PRICE добавлен в таблицу users.")
        if 'IS_AFK' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN IS_AFK INTEGER DEFAULT 0')
            print("[INFO] Столбец IS_AFK добавлен в таблицу users.")
        if 'LAST_ACTIVITY' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN LAST_ACTIVITY TEXT')
            print("[INFO] Столбец LAST_ACTIVITY добавлен в таблицу users.")
        if 'AFK_LOCKED' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN AFK_LOCKED INTEGER DEFAULT 0')
            print("[INFO] Столбец AFK_LOCKED добавлен в таблицу users.")
        if 'STATUS' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN STATUS TEXT DEFAULT "pending"')
            print("[INFO] Столбец STATUS добавлен в таблицу users.")
            # Optionally set existing users to 'approved' to avoid re-approval
            cursor.execute('UPDATE users SET STATUS = "approved" WHERE REG_DATE IS NOT NULL')
        
        # Проверка и добавление PRICE_ADM в settings
        cursor.execute('PRAGMA table_info(settings)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'PRICE_ADM' not in columns:
            cursor.execute('ALTER TABLE settings ADD COLUMN PRICE_ADM REAL DEFAULT 4.5')
            print("[INFO] Столбец PRICE_ADM добавлен в таблицу settings с значением 4.5 по умолчанию.")
        
        # Проверка и создание таблицы requests
        cursor.execute('PRAGMA table_info(requests)')
        columns = [col[1] for col in cursor.fetchall()]
        if not columns:
            cursor.execute('''CREATE TABLE requests (
                ID INTEGER PRIMARY KEY,
                LAST_REQUEST TIMESTAMP,
                STATUS TEXT DEFAULT 'pending',
                BLOCKED INTEGER DEFAULT 0,
                CAN_SUBMIT_NUMBERS INTEGER DEFAULT 1
            )''')
        else:
            if 'BLOCKED' not in columns:
                cursor.execute('ALTER TABLE requests ADD COLUMN BLOCKED INTEGER DEFAULT 0')
            if 'CAN_SUBMIT_NUMBERS' not in columns:
                cursor.execute('ALTER TABLE requests ADD COLUMN CAN_SUBMIT_NUMBERS INTEGER DEFAULT 1')
        
        # Проверка и миграция таблицы numbers
        cursor.execute('PRAGMA table_info(numbers)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'PHOTO_ID' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN PHOTO_ID TEXT')
        if 'MODERATOR_ID' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN MODERATOR_ID INTEGER')
        if 'CONFIRMED_BY_MODERATOR_ID' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN CONFIRMED_BY_MODERATOR_ID INTEGER')
        if 'STATUS' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN STATUS TEXT DEFAULT "ожидает"')
        if 'GROUP_CHAT_ID' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN GROUP_CHAT_ID INTEGER')
        if 'TG_GROUP' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN TG_GROUP TEXT')
        if 'SUBMIT_DATE' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN SUBMIT_DATE TEXT')
        if 'TG_NUMBER' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN TG_NUMBER INTEGER DEFAULT 1')
        if 'HOLDS_COUNT' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN HOLDS_COUNT INTEGER DEFAULT 0')
            print("[INFO] Столбец HOLDS_COUNT добавлен в таблицу numbers.")
        if 'CONFIRMED_BY_OWNER_ID' not in columns:  # Проверка перед добавлением
            cursor.execute('ALTER TABLE numbers ADD COLUMN CONFIRMED_BY_OWNER_ID INTEGER')
            print("[INFO] Столбец CONFIRMED_BY_OWNER_ID добавлен в таблицу numbers.")
        if 'STATUS' in columns:
            cursor.execute('UPDATE numbers SET STATUS = "ожидает" WHERE STATUS = "активен" AND TAKE_DATE = "0"')
        
        # Проверка и миграция таблицы personal
        cursor.execute('PRAGMA table_info(personal)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'GROUP_ID' not in columns:
            cursor.execute('ALTER TABLE personal ADD COLUMN GROUP_ID INTEGER')
        
        # Создание таблицы groups, если её нет
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                NAME TEXT UNIQUE NOT NULL
            )
        ''')
        
        # Проверка и миграция таблицы treasury
        cursor.execute('PRAGMA table_info(treasury)')
        columns = [col[1] for col in cursor.fetchall()]
        if not columns:
            cursor.execute('''CREATE TABLE treasury (
                ID INTEGER PRIMARY KEY CHECK (ID = 1),
                BALANCE REAL DEFAULT 0,
                AUTO_INPUT INTEGER DEFAULT 0,
                CURRENCY TEXT DEFAULT 'USDT'
            )''')
            cursor.execute('INSERT INTO treasury (ID, BALANCE, AUTO_INPUT, CURRENCY) VALUES (?, ?, ?, ?)',
                          (1, 0, 0, 'USDT'))
        conn.commit()

def add_user(user_id, balance=0.0, reg_date=None, username=None):
    """Добавляет нового пользователя в таблицу users или обновляет username, если пользователь уже существует."""
    if reg_date is None:
        reg_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Обрабатываем username: убираем @ и проверяем None
    username_to_save = username.lstrip('@') if username else "Не указан"
    
    with get_db() as conn:
        cursor = conn.cursor()
        # Проверяем, существует ли пользователь
        cursor.execute('SELECT ID, USERNAME FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        
        if user:
            # Если пользователь существует, обновляем username
            cursor.execute('UPDATE users SET USERNAME = ? WHERE ID = ?', (username_to_save, user_id))
            print(f"[DEBUG] Обновлён username для user_id {user_id}: {username_to_save} (было: {user[1]})")
        else:
            # Если пользователя нет, добавляем нового
            cursor.execute('''
                INSERT INTO users (ID, BALANCE, REG_DATE, IS_AFK, LAST_ACTIVITY, AFK_LOCKED, STATUS, USERNAME)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, balance, reg_date, 0, reg_date, 0, 'pending', username_to_save))
            print(f"[DEBUG] Добавлен новый пользователь {user_id} с username {username_to_save}")
        
        conn.commit()

def update_existing_usernames(bot):
    """Обновляет username для всех пользователей в базе, у которых USERNAME = None или 'Не указан'."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM users WHERE USERNAME IS NULL OR USERNAME = ?', ('Не указан',))
        users = cursor.fetchall()
        
        for user_id in users:
            user_id = user_id[0]
            try:
                chat_member = bot.get_chat_member(user_id, user_id)
                username = chat_member.user.username  # @Devshop19 или None
                username_to_save = username.lstrip('@') if username else "Не указан"
                cursor.execute('UPDATE users SET USERNAME = ? WHERE ID = ?', (username_to_save, user_id))
                conn.commit()
                print(f"[DEBUG] Обновлён username для ID {user_id}: {username_to_save}")
            except Exception as e:
                print(f"[ERROR] Не удалось обновить username для ID {user_id}: {e}")

def update_balance(user_id, amount):
    """Обновляет баланс пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, user_id))
        conn.commit()

def add_number(number, user_id, tg_group="1"):
    """Добавляет номер, связанный с пользователем."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM users WHERE ID = ?', (user_id,))
        if not cursor.fetchone():
            add_user(user_id)
        cursor.execute('''
            INSERT INTO numbers (NUMBER, ID_OWNER, TAKE_DATE, SHUTDOWN_DATE, STATUS, TG_GROUP, SUBMIT_DATE, HOLDS_COUNT) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (number, user_id, '0', '0', 'ожидает', tg_group, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 0))
        conn.commit()
        print(f"[DEBUG] Добавлен номер: {number}, ID_OWNER: {user_id}, STATUS: ожидает, TG_GROUP: {tg_group}, SUBMIT_DATE: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

def get_user_price(user_id):
    """Возвращает цену для пользователя (индивидуальную или глобальную)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT CUSTOM_PRICE FROM users WHERE ID = ?', (user_id,))
        custom_price = cursor.fetchone()
        if custom_price and custom_price[0] is not None:
            return custom_price[0]
        cursor.execute('SELECT PRICE FROM settings')
        result = cursor.fetchone()
        return result[0] if result else 2.0

def update_number_status(number, status, moderator_id=None):
    with get_db() as conn:
        cursor = conn.cursor()
        shutdown_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status != 'ожидает' else '0'
        cursor.execute('SELECT HOLDS_COUNT, ID_OWNER, STATUS FROM numbers WHERE NUMBER = ?', (number,))
        row = cursor.fetchone()
        if row:
            holds_count, user_id, current_status = row
            payout = 0
            if status == "отстоял":
                price = get_user_price(user_id)
                if current_status == 'активен' and holds_count == 0:
                    status = "отстоял 1/2"
                    payout = price
                elif current_status == 'отстоял 1/2' and holds_count == 1:
                    status = "отстоял 2/2"
                    payout = price
                elif holds_count >= 2:
                    status = "отстоял 2/2+ холд"
                    payout = 0
                if payout > 0:
                    cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (payout, user_id))
                    print(f"[DEBUG] Начислено {payout}$ пользователю {user_id} за номер {number}")
        cursor.execute('''
            UPDATE numbers 
            SET STATUS = ?, MODERATOR_ID = ?, SHUTDOWN_DATE = ?, HOLDS_COUNT = HOLDS_COUNT + 1
            WHERE NUMBER = ?
        ''', (status, moderator_id, shutdown_date, number))
        conn.commit()

def get_available_number(moderator_id):
    with get_db() as conn:
        cursor = conn.cursor()
        # Получаем список уникальных владельцев номеров, у которых есть доступные номера
        cursor.execute('''
            SELECT DISTINCT n.ID_OWNER
            FROM numbers n
            LEFT JOIN users u ON n.ID_OWNER = u.ID
            WHERE n.TAKE_DATE = "0" 
            AND n.STATUS = "ожидает"
            AND u.IS_AFK = 0
        ''')
        users = [row[0] for row in cursor.fetchall()]
        
        if not users:
            print(f"[DEBUG] Нет доступных номеров для модератора {moderator_id}")
            return None
        
        # Находим последнего пользователя, чей номер был взят
        cursor.execute('''
            SELECT ID_OWNER 
            FROM numbers 
            WHERE MODERATOR_ID IS NOT NULL 
            AND TAKE_DATE != "0"
            ORDER BY TAKE_DATE DESC 
            LIMIT 1
        ''')
        last_user = cursor.fetchone()
        last_user_id = last_user[0] if last_user else None
        
        # Выбираем следующего пользователя в списке
        if last_user_id and last_user_id in users:
            current_index = users.index(last_user_id)
            next_index = (current_index + 1) % len(users)
        else:
            next_index = 0
        
        next_user_id = users[next_index]
        
        # Получаем самый старый доступный номер от выбранного пользователя
        cursor.execute('''
            SELECT n.NUMBER
            FROM numbers n
            LEFT JOIN users u ON n.ID_OWNER = u.ID
            WHERE n.ID_OWNER = ?
            AND n.TAKE_DATE = "0" 
            AND n.STATUS = "ожидает"
            AND u.IS_AFK = 0
            ORDER BY n.SUBMIT_DATE ASC LIMIT 1
        ''', (next_user_id,))
        number = cursor.fetchone()
        
        # Логируем все доступные номера для отладки
        cursor.execute('''
            SELECT NUMBER, ID_OWNER, STATUS, TAKE_DATE, TG_GROUP, SUBMIT_DATE 
            FROM numbers 
            WHERE TAKE_DATE = "0" AND STATUS = "ожидает"
        ''')
        all_available = cursor.fetchall()
        print(f"[DEBUG] Модератор {moderator_id} запросил номер. Выбран пользователь {next_user_id}. Доступные номера: {all_available}")
        
        return number[0] if number else None    

def get_group_name(group_id):
    """Возвращает имя группы по её ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def get_user_numbers(user_id):
    """Возвращает все номера пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT NUMBER, TAKE_DATE, SHUTDOWN_DATE, TG_GROUP, STATUS, HOLDS_COUNT FROM numbers WHERE ID_OWNER = ?''', (user_id,))
        numbers = cursor.fetchall()
    return numbers

def is_moderator(user_id):
    """Проверяет, является ли пользователь модератором."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT ID FROM personal WHERE ID = ? AND TYPE = 'moder'", (user_id,))
        return cursor.fetchone() is not None

def get_treasury_balance():
    """Возвращает актуальный баланс казны в USDT из API CryptoBot."""
    url = "https://pay.crypt.bot/api/getBalance"
    headers = {"Crypto-Pay-API-Token": config.CRYPTO_PAY_API_TOKEN}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                for currency in data.get("result", []):
                    if currency["currency_code"] == "USDT":
                        balance = float(currency["available"])
                        return balance
                return 0.0
            else:
                return 0.0
        else:
            return 0.0
    except Exception as e:
        return 0.0

def update_treasury_balance(amount):
    """Обновляет баланс казны в базе, основываясь на актуальном балансе из API."""
    current_balance = get_treasury_balance()
    new_balance = current_balance + amount
    if new_balance < 0:
        new_balance = max(0.0, new_balance)  # Предотвращаем отрицательный баланс
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO treasury (ID, BALANCE) VALUES (?, ?)', (1, new_balance))
        conn.commit()
    return new_balance

def set_treasury_balance(amount):
    """Устанавливает новый баланс казны."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE treasury SET BALANCE = ? WHERE ID = 1", (amount,))
        conn.commit()
        return amount

def get_auto_input_status():
    """Возвращает статус автоматического ввода."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT AUTO_INPUT FROM treasury WHERE ID = 1")
        result = cursor.fetchone()
        return bool(result[0]) if result else False    

def update_last_activity(self, user_id):
    """Обновляет время последней активности пользователя и сбрасывает статус АФК."""
    with self.get_db() as conn:
        cursor = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Проверяем, существует ли пользователь
        cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        if not result:
            # Если пользователь не существует, создаём его
            cursor.execute('INSERT OR IGNORE INTO users (ID, BALANCE, REG_DATE, IS_AFK, LAST_ACTIVITY) VALUES (?, ?, ?, ?, ?)',
                          (user_id, 0.0, current_time, 0, current_time))
        else:
            # Сбрасываем АФК, если он включён
            if result[0] == 1:
                cursor.execute('UPDATE users SET IS_AFK = 0 WHERE ID = ?', (user_id,))
        # Обновляем время активности
        cursor.execute('UPDATE users SET LAST_ACTIVITY = ? WHERE ID = ?', (current_time, user_id))
        conn.commit()

def toggle_auto_input():
    """Переключает статус автоматического ввода."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT AUTO_INPUT FROM treasury WHERE ID = 1")
        current_status = cursor.fetchone()[0]
        new_status = 1 if current_status == 0 else 0
        cursor.execute("UPDATE treasury SET AUTO_INPUT = ? WHERE ID = 1", (new_status,))
        conn.commit()
        return bool(new_status)

def log_treasury_operation(operation, amount, balance):
    """Логирует операции с казной."""
    log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] | {operation} | Сумма: {amount}$ | Баланс: {balance}$"
    with open("treasury_log.txt", "a", encoding="utf-8") as log_file:
        log_file.write(log_entry + "\n")

def get_afk_status(user_id):
    """Возвращает статус АФК пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        return bool(result[0]) if result else False

def toggle_afk_status(user_id):
    """Переключает статус АФК пользователя, если он не модератор."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Проверяем, является ли пользователь модератором
        cursor.execute('SELECT ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
        if cursor.fetchone():
            return False  # Модераторы не могут быть в АФК
        
        cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
        current_status = cursor.fetchone()
        if not current_status:
            add_user(user_id)
            current_status = [0]
        new_status = 1 if current_status[0] == 0 else 0
        cursor.execute('UPDATE users SET IS_AFK = ? WHERE ID = ?', (new_status, user_id))
        conn.commit()
        return bool(new_status)

def update_last_activity(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        if not result:
            cursor.execute('INSERT OR IGNORE INTO users (ID, BALANCE, REG_DATE, IS_AFK, LAST_ACTIVITY) VALUES (?, ?, ?, ?, ?)',
                          (user_id, 0.0, current_time, 0, current_time))
            print(f"[DEBUG] Новый пользователь {user_id} добавлен с LAST_ACTIVITY={current_time}")
        else:
            if result[0] == 1:
                cursor.execute('UPDATE users SET IS_AFK = 0 WHERE ID = ?', (user_id,))
        cursor.execute('UPDATE users SET LAST_ACTIVITY = ? WHERE ID = ?', (current_time, user_id))
        conn.commit()
        print(f"[DEBUG] Обновлено время активности для пользователя {user_id}: {current_time}")

# Вызов функций создания таблиц и миграции
create_tables()
migrate_db()