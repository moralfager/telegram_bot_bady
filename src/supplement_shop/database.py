from __future__ import annotations

import sqlite3
import uuid
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .pricing import ceil_currency, decimal_value, legacy_rubles, quote, whole_number


Row = sqlite3.Row


ORDER_STATUSES = {
    "new": "Новый",
    "review": "На проверке",
    "awaiting_payment": "Ожидает оплаты",
    "awaiting_card_payment": "Ожидает перевода на карту",
    "awaiting_crypto_payment": "Ожидает перевода криптовалюты",
    "receipt_review": "Чек на проверке",
    "payment_check": "Проверка платежа Stars",
    "paid": "Оплачен",
    "processing": "В обработке",
    "delivery": "Передан в доставку",
    "done": "Доставлен",
    "canceled": "Отменён",
}


PAYMENT_METHODS = {
    "stars": "Telegram Stars",
    "card": "Перевод на карту",
    "crypto": "Криптовалюта",
}


DEFAULT_CATEGORY_TITLE = "БАДы"
PAYMENT_TIMEOUT_HOURS = 3

COUNTRIES = [
    ('RU', 'Россия', 'RUB'), ('KZ', 'Казахстан', 'KZT'),
    ('BY', 'Беларусь', 'BYN'), ('AM', 'Армения', 'AMD'),
    ('AZ', 'Азербайджан', 'AZN'), ('KG', 'Кыргызстан', 'KGS'),
    ('UZ', 'Узбекистан', 'UZS'), ('TJ', 'Таджикистан', 'TJS'),
    ('MD', 'Молдова', 'MDL'), ('TM', 'Туркменистан', 'TMT'),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    characteristics TEXT NOT NULL DEFAULT '',
                    price_stars INTEGER NOT NULL CHECK(price_stars > 0),
                    stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
                    media_type TEXT NOT NULL DEFAULT 'none',
                    media_file_id TEXT NOT NULL DEFAULT '',
                    media_path TEXT NOT NULL DEFAULT '',
                    buyer_media_file_id TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS cart_items (
                    user_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    qty INTEGER NOT NULL CHECK(qty > 0),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, product_id)
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    customer_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    address TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    payment_method TEXT NOT NULL DEFAULT 'stars',
                    payment_details_sent INTEGER NOT NULL DEFAULT 0,
                    payment_details_message_id INTEGER NOT NULL DEFAULT 0,
                    telegram_tag TEXT NOT NULL DEFAULT '',
                    contact_consent INTEGER NOT NULL DEFAULT 0,
                    total_stars INTEGER NOT NULL,
                    is_paid INTEGER NOT NULL DEFAULT 0,
                    payment_payload TEXT NOT NULL UNIQUE,
                    telegram_payment_charge_id TEXT NOT NULL DEFAULT '',
                    provider_payment_charge_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    product_id INTEGER,
                    title TEXT NOT NULL,
                    price_stars INTEGER NOT NULL,
                    qty INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    payment_method TEXT NOT NULL DEFAULT 'stars',
                    amount_stars INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    telegram_payment_charge_id TEXT NOT NULL DEFAULT '',
                    provider_payment_charge_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS shop_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS countries (
                    code TEXT PRIMARY KEY, title TEXT NOT NULL, currency TEXT NOT NULL,
                    rate_per_rub TEXT NOT NULL DEFAULT '0', enabled INTEGER NOT NULL DEFAULT 1,
                    card_details TEXT NOT NULL DEFAULT '', card_currency TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customers (
                    user_id INTEGER PRIMARY KEY, country_code TEXT NOT NULL REFERENCES countries(code)
                );
                CREATE TABLE IF NOT EXISTS crypto_options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, network TEXT NOT NULL,
                    address TEXT NOT NULL DEFAULT '', rub_per_unit TEXT NOT NULL DEFAULT '0',
                    enabled INTEGER NOT NULL DEFAULT 0, UNIQUE(token, network)
                );
                CREATE TABLE IF NOT EXISTS expiry_notifications (
                    order_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                    invoice_message_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS payment_events (
                    charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL, user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL, currency TEXT NOT NULL,
                    order_id INTEGER, resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS payment_guards (
                    payload TEXT PRIMARY KEY, order_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL, approved INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    event_key TEXT PRIMARY KEY, kind TEXT NOT NULL, order_id INTEGER,
                    user_id INTEGER NOT NULL, body TEXT NOT NULL DEFAULT '{}',
                    delivered TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at DESC);
                """
            )
            self._migrate(conn)
            self._seed_default_settings(conn)

    def backup(self) -> Path | None:
        if not self.path.exists():
            return None
        destination = self.path.with_name(f"{self.path.stem}-backup-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.sqlite3")
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        return destination

    def _migrate(self, conn: sqlite3.Connection) -> None:
        product_columns = {row['name'] for row in conn.execute('PRAGMA table_info(products)')}
        self._ensure_column(conn, 'products', 'price_rub_minor', 'INTEGER NOT NULL DEFAULT 0')
        self._ensure_column(conn, 'products', 'price_confirmed', 'INTEGER NOT NULL DEFAULT 0')
        self._ensure_column(conn, 'products', 'stars_price_confirmed', 'INTEGER NOT NULL DEFAULT 0')
        if 'price_rub_minor' not in product_columns:
            for row in conn.execute('SELECT id, price_stars FROM products').fetchall():
                conn.execute('UPDATE products SET price_rub_minor = ? WHERE id = ?', (legacy_rubles(row['price_stars']), row['id']))
        for column, definition in {
            'total_rub_minor': 'INTEGER NOT NULL DEFAULT 0',
            'payable_minor': 'INTEGER NOT NULL DEFAULT 0',
            'payable_currency': "TEXT NOT NULL DEFAULT ''",
            'country_code': "TEXT NOT NULL DEFAULT 'RU'",
            'payment_snapshot': "TEXT NOT NULL DEFAULT '{}'",
            'expires_at': "TEXT NOT NULL DEFAULT ''",
            'receipt_submitted': 'INTEGER NOT NULL DEFAULT 0',
            'admin_note': "TEXT NOT NULL DEFAULT ''",
            'tracking_number': "TEXT NOT NULL DEFAULT ''",
            'invoice_message_id': 'INTEGER NOT NULL DEFAULT 0',
            'is_demo': 'INTEGER NOT NULL DEFAULT 0',
            'telegram_tag': "TEXT NOT NULL DEFAULT ''",
            'contact_consent': 'INTEGER NOT NULL DEFAULT 0',
            'payment_details_message_id': 'INTEGER NOT NULL DEFAULT 0',
        }.items():
            self._ensure_column(conn, 'orders', column, definition)
        self._ensure_column(conn, 'order_items', 'price_rub_minor', 'INTEGER NOT NULL DEFAULT 0')
        self._ensure_column(conn, 'payment_guards', 'approved_until', "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, 'transactions', 'amount_minor', 'INTEGER NOT NULL DEFAULT 0')
        self._ensure_column(conn, 'transactions', 'currency', "TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE transactions SET amount_minor = (SELECT payable_minor FROM orders WHERE orders.id = transactions.order_id), currency = (SELECT payable_currency FROM orders WHERE orders.id = transactions.order_id) WHERE currency = ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_expiry ON orders(is_paid, receipt_submitted, expires_at)")
        # Manual (card/crypto) orders keep an empty deadline until an admin sends payment details.
        conn.execute("UPDATE orders SET expires_at = datetime(created_at, '+3 hours') "
                     "WHERE expires_at = '' AND is_paid = 0 AND payment_method = 'stars'")
        self._ensure_column(conn, "products", "media_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "products", "buyer_media_file_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "orders", "payment_method", "TEXT NOT NULL DEFAULT 'stars'")
        self._ensure_column(conn, "orders", "payment_details_sent", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "transactions", "payment_method", "TEXT NOT NULL DEFAULT 'stars'")

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _seed_default_settings(self, conn: sqlite3.Connection) -> None:
        defaults = {
            "payment_stars_enabled": "1",
            "payment_card_enabled": "1",
            "payment_crypto_enabled": "0",
            "card_payment_details": "",
            'card_shared_details_enabled': '0', 'card_payment_currency': 'RUB',
            'shop_title': '', 'shop_open': '1', 'welcome_text': 'Выберите товар в каталоге.',
            'manager_username': '', 'support_username': '', 'reviews_url': '',
            'how_to_buy': 'Выберите товар, добавьте его в корзину и оформите заказ. Менеджер примет заказ и отправит реквизиты для оплаты. После отправки чека менеджер подтвердит получение средств.',
            'terms_text': 'Перед оплатой проверьте товары, сумму и данные доставки. После оформления менеджер пришлёт реквизиты. Срок оплаты (3 ч) действует с момента отправки реквизитов.',
            'price_input_currency': 'RUB',
            'delivery_rub': '0', 'delivery_stars': '0',
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO shop_settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        for code, title, currency in COUNTRIES:
            conn.execute('INSERT OR IGNORE INTO countries(code,title,currency,rate_per_rub,card_currency) VALUES (?,?,?,?,?)',
                         (code, title, currency, '1' if code == 'RU' else '0', currency))
        for network in ('TRC20', 'ERC20'):
            conn.execute("INSERT OR IGNORE INTO crypto_options(token,network) VALUES ('USDT',?)", (network,))
        conn.execute(
            "INSERT OR IGNORE INTO categories(title, is_active) VALUES (?, 1)",
            (DEFAULT_CATEGORY_TITLE,),
        )

    def seed_demo(self) -> None:
        self.init()

    def create_category(self, title: str) -> int:
        title = title.strip()
        if not title:
            raise ValueError("Category title is required")
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM categories WHERE title = ?", (title,)).fetchone()
            if row:
                conn.execute("UPDATE categories SET is_active = 1 WHERE id = ?", (row["id"],))
                return int(row["id"])
            cur = conn.execute("INSERT INTO categories(title) VALUES (?)", (title,))
            return int(cur.lastrowid)

    def get_default_category_id(self) -> int:
        self.init()
        category = self.get_category_by_title(DEFAULT_CATEGORY_TITLE)
        if category:
            return int(category["id"])
        return self.create_category(DEFAULT_CATEGORY_TITLE)

    def get_category_by_title(self, title: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM categories WHERE title = ?", (title,)).fetchone()

    def get_category(self, category_id: int) -> Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()

    def list_categories(self, active_only: bool = True) -> list[Row]:
        query = """
            SELECT c.*,
                   (SELECT COUNT(*) FROM products p
                    WHERE p.category_id = c.id AND p.is_active = 1) AS product_count
            FROM categories c
        """
        params: list[Any] = []
        if active_only:
            query += " WHERE c.is_active = 1"
        query += " ORDER BY c.title"
        with self.connect() as conn:
            return list(conn.execute(query, params).fetchall())

    def deactivate_category(self, category_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE categories SET is_active = 0 WHERE id = ?", (category_id,))
            conn.execute("UPDATE products SET is_active = 0 WHERE category_id = ?", (category_id,))

    def create_product(
        self,
        *,
        category_id: int,
        title: str,
        description: str,
        characteristics: str,
        price_stars: int | None = None,
        price_rub_minor: int | None = None,
        stock: int = 0,
        media_type: str = "none",
        media_file_id: str = "",
        media_path: str = "",
        buyer_media_file_id: str = "",
    ) -> int:
        if media_type not in {"none", "photo", "video"}:
            raise ValueError("media_type must be none, photo or video")
        if price_rub_minor is None:
            raise ValueError('Укажите отдельную цену в рублях.')
        price_rub_minor = whole_number(Decimal(str(price_rub_minor)) / 100, minimum=1) * 100
        stars_confirmed = price_stars is not None
        price_stars = whole_number(price_stars, minimum=1) if stars_confirmed else 1
        stock = whole_number(stock, maximum=1000000)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO products(
                    category_id, title, description, characteristics, price_stars,
                    stock, media_type, media_file_id, media_path, buyer_media_file_id,
                    price_rub_minor, price_confirmed, stars_price_confirmed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    category_id,
                    title.strip(),
                    description.strip(),
                    characteristics.strip(),
                    int(price_stars),
                    int(stock),
                    media_type,
                    media_file_id,
                    media_path,
                    buyer_media_file_id,
                    price_rub_minor,
                    int(stars_confirmed),
                ),
            )
            return int(cur.lastrowid)

    def get_product(self, product_id: int, active_only: bool = False) -> Row | None:
        query = "SELECT p.*, c.title AS category_title FROM products p JOIN categories c ON c.id = p.category_id WHERE p.id = ?"
        params: list[Any] = [product_id]
        if active_only:
            query += " AND p.is_active = 1 AND c.is_active = 1"
        with self.connect() as conn:
            return conn.execute(query, params).fetchone()

    def get_product_by_title(self, title: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM products WHERE title = ?", (title,)).fetchone()

    def list_products(self, category_id: int | None = None, active_only: bool = True) -> list[Row]:
        query = """
            SELECT p.*, c.title AS category_title
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if category_id is not None:
            query += " AND p.category_id = ?"
            params.append(category_id)
        if active_only:
            query += " AND p.is_active = 1 AND c.is_active = 1"
        query += " ORDER BY c.title, p.title"
        with self.connect() as conn:
            return list(conn.execute(query, params).fetchall())

    def update_product(self, product_id: int, **fields: Any) -> None:
        allowed = {
            "category_id",
            "title",
            "description",
            "characteristics",
            "price_stars",
            "price_rub_minor",
            "price_confirmed",
            "stock",
            "media_type",
            "media_file_id",
            "media_path",
            "buyer_media_file_id",
            "is_active",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if 'price_rub_minor' in updates:
            updates['price_rub_minor'] = whole_number(Decimal(str(updates['price_rub_minor'])) / 100, minimum=1) * 100
            updates['price_confirmed'] = 1
        if 'price_stars' in updates:
            updates['price_stars'] = whole_number(updates['price_stars'], minimum=1)
            updates['stars_price_confirmed'] = 1
        if 'stock' in updates:
            updates['stock'] = whole_number(updates['stock'], maximum=1000000)
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        values.append(product_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE products SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )

    def deactivate_product(self, product_id: int) -> None:
        self.update_product(product_id, is_active=0)

    def add_to_cart(self, user_id: int, product_id: int, qty: int = 1) -> tuple[bool, str]:
        product = self.get_product(product_id, active_only=True)
        if not product:
            return False, "Товар не найден."
        if not product['price_confirmed']:
            return False, 'Цена уточняется. Обратитесь к менеджеру.'
        if qty <= 0:
            return False, 'Количество должно быть положительным.'
        with self.connect() as conn:
            current = conn.execute(
                "SELECT qty FROM cart_items WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            ).fetchone()
            if not current and conn.execute('SELECT COUNT(*) FROM cart_items WHERE user_id=?', (user_id,)).fetchone()[0] >= 30:
                return False, 'В одном заказе можно оформить до 30 разных товаров.'
            new_qty = int(qty) + (int(current["qty"]) if current else 0)
            if current:
                conn.execute(
                    """
                    UPDATE cart_items
                    SET qty = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND product_id = ?
                    """,
                    (new_qty, user_id, product_id),
                )
            else:
                conn.execute(
                    "INSERT INTO cart_items(user_id, product_id, qty) VALUES (?, ?, ?)",
                    (user_id, product_id, new_qty),
                )
        return True, "Товар добавлен в корзину."

    def set_cart_qty(self, user_id: int, product_id: int, qty: int) -> None:
        with self.connect() as conn:
            if qty <= 0:
                conn.execute(
                    "DELETE FROM cart_items WHERE user_id = ? AND product_id = ?",
                    (user_id, product_id),
                )
                return
            product = conn.execute(
                "SELECT id FROM products WHERE id = ? AND is_active = 1",
                (product_id,),
            ).fetchone()
            if not product:
                conn.execute(
                    "DELETE FROM cart_items WHERE user_id = ? AND product_id = ?",
                    (user_id, product_id),
                )
                return
            conn.execute(
                """
                UPDATE cart_items
                SET qty = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND product_id = ?
                """,
                (qty, user_id, product_id),
            )

    def clear_cart(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))

    def get_cart(self, user_id: int) -> list[Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT ci.qty, p.*, c.title AS category_title,
                           ci.qty * p.price_stars AS line_total,
                           ci.qty * p.price_rub_minor AS line_rub_minor
                    FROM cart_items ci
                    JOIN products p ON p.id = ci.product_id
                    JOIN categories c ON c.id = p.category_id
                    WHERE ci.user_id = ? AND p.is_active = 1 AND c.is_active = 1
                    ORDER BY ci.updated_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM shop_settings WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO shop_settings(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_payment_settings(self) -> dict[str, str | bool]:
        return {
            "stars_enabled": self.get_setting("payment_stars_enabled", "1") == "1",
            "card_enabled": self.get_setting("payment_card_enabled", "1") == "1",
            "card_details": self.get_setting("card_payment_details", ""),
            'shared_card': self.get_setting('card_shared_details_enabled', '0') == '1',
            'card_currency': self.get_setting('card_payment_currency', 'RUB'),
            "crypto_enabled": self.get_setting('payment_crypto_enabled', '0') == '1',
        }

    def set_payment_enabled(self, method: str, enabled: bool) -> None:
        if method not in PAYMENT_METHODS:
            raise ValueError(f"Unknown payment method: {method}")
        self.set_setting(f"payment_{method}_enabled", "1" if enabled else "0")

    def set_card_payment_details(self, details: str) -> None:
        details = details.strip()
        if not details:
            raise ValueError("Card payment details cannot be empty")
        self.set_setting("card_payment_details", details)

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            return {row['key']: row['value'] for row in conn.execute('SELECT * FROM shop_settings')}

    def list_countries(self, enabled_only: bool = False) -> list[Row]:
        with self.connect() as conn:
            return list(conn.execute('SELECT * FROM countries' + (' WHERE enabled = 1' if enabled_only else '') + ' ORDER BY title'))

    def get_country(self, code: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute('SELECT * FROM countries WHERE code = ?', (code,)).fetchone()

    def update_country(self, code: str, **fields) -> None:
        country = self.get_country(code)
        if not country:
            raise ValueError('Страна не найдена.')
        updates = {key: value for key, value in fields.items() if key in {'enabled', 'rate_per_rub', 'card_details', 'card_currency'}}
        if 'rate_per_rub' in updates:
            updates['rate_per_rub'] = str(decimal_value(updates['rate_per_rub']))
            if code == 'RU' and Decimal(updates['rate_per_rub']) != 1:
                raise ValueError('Курс RUB всегда равен 1.')
        if 'card_currency' in updates and updates['card_currency'] not in {'RUB', country['currency']}:
            raise ValueError('Валюта карты: RUB или валюта страны.')
        if 'card_details' in updates and len(updates['card_details']) > 1500:
            raise ValueError('Реквизиты: максимум 1500 символов.')
        if updates:
            with self.connect() as conn:
                conn.execute('UPDATE countries SET ' + ', '.join(f'{key} = ?' for key in updates) + ' WHERE code = ?', [*updates.values(), code])

    def set_user_country(self, user_id: int, code: str) -> None:
        country = self.get_country(code)
        if not country or not country['enabled']:
            raise ValueError('Эта страна сейчас недоступна.')
        with self.connect() as conn:
            conn.execute('INSERT INTO customers(user_id,country_code) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET country_code=excluded.country_code', (user_id, code))

    def get_user_country(self, user_id: int) -> Row | None:
        with self.connect() as conn:
            return conn.execute('SELECT c.* FROM countries c JOIN customers u ON u.country_code=c.code WHERE u.user_id=?', (user_id,)).fetchone()

    def price_from_input(self, text: str) -> int:
        price = whole_number(text, minimum=1) * 100
        if self.get_setting('price_input_currency', 'RUB') == 'KZT':
            country = self.get_country('KZ')
            rate = decimal_value(country['rate_per_rub'], minimum=Decimal('0.00000001'))
            price = ceil_currency(Decimal(price) / rate)
        if price <= 0:
            raise ValueError('Цена должна быть больше нуля.')
        return price

    def list_crypto_options(self, active_only: bool = False) -> list[Row]:
        with self.connect() as conn:
            return list(conn.execute('SELECT * FROM crypto_options' + (' WHERE enabled=1' if active_only else '') + ' ORDER BY id'))

    def get_crypto_option(self, option_id: int) -> Row | None:
        with self.connect() as conn:
            return conn.execute('SELECT * FROM crypto_options WHERE id=?', (option_id,)).fetchone()

    def save_crypto_option(self, option_id=None, *, token='USDT', network='TRC20', address='', rub_per_unit='0', enabled=True) -> int:
        token, network, address = token.strip().upper(), network.strip().upper(), address.strip()
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,15}', token) or not re.fullmatch(r'[A-Z0-9][A-Z0-9 ._()-]{0,31}', network):
            raise ValueError('Проверьте название токена и сети.')
        rate = decimal_value(rub_per_unit)
        if enabled and (rate <= 0 or not address):
            raise ValueError('Для включения нужны адрес и курс в рублях за 1 токен.')
        canonical_network = re.sub(r'[^A-Z0-9]', '', network)
        if address and canonical_network == 'ERC20' and not re.fullmatch(r'0x[0-9a-fA-F]{40}', address):
            raise ValueError('Адрес ERC20 должен начинаться с 0x и содержать 40 шестнадцатеричных символов.')
        if address and canonical_network == 'TRC20' and not re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', address):
            raise ValueError('Проверьте адрес TRC20: 34 символа, начинается с T.')
        if len(address) > 256 or any(char.isspace() for char in address):
            raise ValueError('Проверьте адрес кошелька.')
        try:
            with self.connect() as conn:
                if option_id is None:
                    cur = conn.execute('INSERT INTO crypto_options(token,network,address,rub_per_unit,enabled) VALUES (?,?,?,?,?)', (token, network, address, str(rate), int(enabled)))
                    return int(cur.lastrowid)
                cur = conn.execute('UPDATE crypto_options SET token=?,network=?,address=?,rub_per_unit=?,enabled=? WHERE id=?', (token, network, address, str(rate), int(enabled), option_id))
                if cur.rowcount != 1:
                    raise ValueError('Кошелёк не найден.')
                return int(option_id)
        except sqlite3.IntegrityError:
            raise ValueError('Такая пара токен/сеть уже существует.') from None

    @staticmethod
    def card_context(settings, country, countries):
        context = dict(country)
        if settings.get('card_shared_details_enabled') == '1':
            currency = settings.get('card_payment_currency', 'RUB')
            rate = next((row['rate_per_rub'] for row in countries if row['currency'] == currency), '0')
            context.update(card_details=settings.get('card_payment_details', ''),
                           card_currency=currency, rate_per_rub='1' if currency == 'RUB' else rate)
        return context

    def payment_options(self, user_id: int) -> list[dict]:
        settings = self.get_settings()
        if settings.get('shop_open') != '1':
            return []
        country = self.get_user_country(user_id)
        if not country or not country['enabled']:
            return []
        options = []
        manager = settings.get('manager_username', '')
        card = self.card_context(settings, country, self.list_countries())
        details = card['card_details']
        if settings.get('payment_card_enabled') == '1' and manager and details and 'ещё не заполнены' not in details:
            if card['card_currency'] == 'RUB' or Decimal(card['rate_per_rub']) > 0:
                options.append({'method': 'card', 'label': '💳 Карта', 'country': card})
        if settings.get('payment_crypto_enabled') == '1' and manager:
            for option in self.list_crypto_options(active_only=True):
                if option['address'] and Decimal(option['rub_per_unit']) > 0:
                    options.append({'method': 'crypto', 'label': f"🪙 {option['token']} / {option['network']}", 'crypto': dict(option), 'country': dict(country)})
        if settings.get('payment_stars_enabled') == '1':
            options.append({'method': 'stars', 'label': '⭐ Telegram Stars', 'country': dict(country)})
        return options

    def create_order_from_cart(
        self,
        *,
        user_id: int,
        customer_name: str,
        phone: str,
        address: str,
        telegram_tag: str = '',
        contact_consent: bool = False,
        comment: str = "",
        status: str = "new",
        is_paid: bool = False,
        payment_method: str = "stars",
        crypto_option_id: int | None = None,
        require_available: bool = False,
        expected_quote: dict | None = None,
        expected_items: list | None = None,
        is_demo: bool = False,
    ) -> int:
        if payment_method not in PAYMENT_METHODS:
            raise ValueError(f"Unknown payment method: {payment_method}")
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM shop_settings')}
            country = conn.execute('SELECT c.* FROM countries c JOIN customers u ON u.country_code=c.code WHERE u.user_id=?', (user_id,)).fetchone()
            if country is None and not require_available:
                country = conn.execute("SELECT * FROM countries WHERE code='RU'").fetchone()
            if country is None or not country['enabled']:
                raise ValueError('Выберите доступную страну доставки.')
            crypto = conn.execute('SELECT * FROM crypto_options WHERE id=?', (crypto_option_id,)).fetchone() if crypto_option_id else None
            if require_available:
                if settings.get('shop_open') != '1' or settings.get(f'payment_{payment_method}_enabled') != '1':
                    raise ValueError('Выбранный способ оплаты сейчас недоступен.')
                if payment_method != 'stars' and not settings.get('manager_username'):
                    raise ValueError('Контакт менеджера ещё не настроен.')
                if payment_method == 'crypto' and (not crypto or not crypto['enabled'] or not crypto['address']):
                    raise ValueError('Кошелёк сейчас недоступен.')
            cart = list(conn.execute('SELECT p.*,ci.qty FROM cart_items ci JOIN products p ON p.id=ci.product_id JOIN categories c ON c.id=p.category_id WHERE ci.user_id=? AND p.is_active=1 AND c.is_active=1', (user_id,)))
            if not cart:
                raise ValueError('Корзина пуста.')
            if any(not item['price_confirmed'] for item in cart):
                raise ValueError('Администратор ещё не подтвердил рублёвую цену товара.')
            stars_total = sum(item['price_stars'] * item['qty'] for item in cart) if all(item['stars_price_confirmed'] for item in cart) else 0
            card = self.card_context(settings, country, list(conn.execute('SELECT * FROM countries')))
            amount = quote(sum(int(item['price_rub_minor']) * int(item['qty']) for item in cart), payment_method, settings, card if payment_method == 'card' else country, crypto, stars_total=stars_total)
            current_items = [(item['id'],item['qty'],item['price_rub_minor'],item['price_stars'],item['stars_price_confirmed']) for item in cart]
            if ((expected_quote is not None and expected_quote != amount)
                    or (expected_items is not None and sorted(expected_items) != sorted(current_items))):
                raise ValueError('Цена, курс или корзина изменились. Проверьте новые суммы и выберите оплату ещё раз.')
            total = amount['total_stars']
            details = card['card_details']
            if require_available and payment_method == 'card' and (not details or 'ещё не заполнены' in details):
                raise ValueError('Реквизиты для вашей страны ещё не настроены.')
            snapshot = {**amount, 'manager_username': settings.get('manager_username', ''),
                        'card_details': details, 'country_title': country['title'],
                        'local_currency': country['currency'], 'local_rate': country['rate_per_rub'],
                        'crypto': dict(crypto) if crypto else None,
                        'timeout_hours': str(PAYMENT_TIMEOUT_HOURS)}
            # Manual (card/crypto) orders get a deadline only when an admin sends payment details.
            deadline = (datetime.now(timezone.utc) + timedelta(hours=PAYMENT_TIMEOUT_HOURS)).strftime('%Y-%m-%d %H:%M:%S') if payment_method == 'stars' else ''
            payload = f'order:{uuid.uuid4().hex}'
            cur = conn.execute(
                """
                INSERT INTO orders(
                    user_id, customer_name, phone, address, comment, status,
                    payment_method, total_stars, is_paid, payment_payload, telegram_tag, contact_consent
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    customer_name.strip(),
                    phone.strip(),
                    address.strip(),
                    comment.strip(),
                    status,
                    payment_method,
                    total,
                    1 if is_paid else 0,
                    payload,
                    telegram_tag.strip(),
                    int(contact_consent),
                ),
            )
            order_id = int(cur.lastrowid)
            conn.execute('UPDATE orders SET is_demo=? WHERE id=?', (int(is_demo), order_id))
            conn.execute('UPDATE orders SET total_rub_minor=?,payable_minor=?,payable_currency=?,country_code=?,payment_snapshot=?,expires_at=? WHERE id=?',
                         (amount['total_rub_minor'], amount['payable_minor'], amount['payable_currency'], country['code'], json.dumps(snapshot, ensure_ascii=False), deadline, order_id))
            if payment_method == 'stars':
                conn.execute('INSERT INTO payment_guards(payload,order_id,user_id,amount) VALUES (?,?,?,?)', (payload,order_id,user_id,total))

            for item in cart:
                conn.execute(
                    """
                    INSERT INTO order_items(order_id, product_id, title, price_stars, qty, price_rub_minor)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, item["id"], item["title"], item['price_stars'] if item['stars_price_confirmed'] else 0, item["qty"], item['price_rub_minor']),
                )

            conn.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))

            if is_paid:
                conn.execute(
                    """
                    INSERT INTO transactions(order_id, user_id, payment_method, amount_stars, payload, amount_minor, currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, user_id, payment_method, total if payment_method == 'stars' else 0, payload, amount['payable_minor'], amount['payable_currency']),
                )

            return order_id

    def get_order(self, order_id: int) -> Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def get_order_by_payload(self, payload: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM orders WHERE payment_payload = ?", (payload,)).fetchone()

    def list_order_items(self, order_id: int) -> list[Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
                    (order_id,),
                ).fetchall()
            )

    def _order_filter(self, user_id=None, status=None, search='') -> tuple[str, list]:
        clauses = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append('user_id = ?')
            params.append(user_id)
        if status == 'unpaid':
            clauses.append("is_paid=0 AND status != 'canceled'")
        elif status == 'pending_details':
            clauses.append("payment_details_sent=0 AND payment_method IN ('card', 'crypto') AND is_paid=0 AND status != 'canceled'")
        elif status == 'paid':
            clauses.append('is_paid=1')
        elif status and status != 'all':
            clauses.append('status=?')
            params.append(status)
        if search.strip():
            value = search.strip().lstrip('#')
            if value.isdigit():
                clauses.append('id=?')
                params.append(int(value))
            else:
                clauses.append("(instr(lower(customer_name), lower(?)) > 0 OR instr(phone, ?) > 0 OR instr(lower(telegram_tag), lower(?)) > 0)")
                params.extend([value, value, value])
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params

    def list_orders(self, *, user_id=None, limit=10, offset=0, status=None, search='') -> list[Row]:
        where, params = self._order_filter(user_id, status, search)
        with self.connect() as conn:
            return list(conn.execute('SELECT * FROM orders' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?', [*params, min(100, max(1, limit)), max(0, offset)]))

    def count_orders(self, *, user_id=None, status=None, search='') -> int:
        where, params = self._order_filter(user_id, status, search)
        with self.connect() as conn:
            return int(conn.execute('SELECT COUNT(*) FROM orders' + where, params).fetchone()[0])

    def update_order_status(self, order_id: int, status: str) -> None:
        if status not in ORDER_STATUSES:
            raise ValueError(f"Unknown order status: {status}")
        if status == 'canceled':
            ok, reason = self.cancel_order(order_id)
            if not ok:
                raise ValueError(reason)
            return
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not order or not order['is_paid'] or status not in {'paid', 'processing', 'delivery', 'done'}:
                raise ValueError('Сначала подтвердите оплату заказа.')
            conn.execute(
                """
                UPDATE orders
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, order_id),
            )

    def cancel_order(self, order_id: int, user_id: int | None = None) -> tuple[bool, str]:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                return False, "Заказ не найден."
            if user_id is not None and int(order["user_id"]) != user_id:
                return False, "Это не ваш заказ."
            if int(order["is_paid"]):
                return False, "Оплаченный заказ нельзя отменить через эту кнопку."
            if order["status"] == "canceled":
                return False, "Заказ уже отменён."
            guard = conn.execute('SELECT approved FROM payment_guards WHERE payload=?', (order['payment_payload'],)).fetchone()
            if guard and guard['approved']:
                return False, 'Платёж Stars уже начат. Дождитесь результата или обратитесь к менеджеру.'
            if user_id is not None and order['receipt_submitted']:
                return False, 'Чек проверяет менеджер. Для отмены свяжитесь с ним.'

            conn.execute(
                """
                UPDATE orders
                SET status = 'canceled',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            return True, f"Заказ #{order_id} отменён."

    def find_latest_cancelable_order(self, user_id: int) -> Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM orders
                WHERE user_id = ?
                  AND is_paid = 0
                  AND status != 'canceled'
                ORDER BY datetime(created_at) DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()

    def mark_manual_details_sent(self, order_id: int, message_id: int = 0) -> None:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            self._require_manual_order(order)
            deadline = (datetime.now(timezone.utc) + timedelta(hours=PAYMENT_TIMEOUT_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                """
                UPDATE orders
                SET status = ?,
                    payment_details_sent = 1,
                    payment_details_message_id = ?,
                    expires_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                ('receipt_review' if order['receipt_submitted'] else ('awaiting_card_payment' if order['payment_method'] == 'card' else 'awaiting_crypto_payment'), message_id, deadline, order_id),
            )

    def mark_card_details_sent(self, order_id: int) -> None:
        self.mark_manual_details_sent(order_id)

    def clear_payment_details_message(self, order_id: int, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                'UPDATE orders SET payment_details_message_id=0 WHERE id=? AND payment_details_message_id=?',
                (order_id, message_id),
            )

    @staticmethod
    def _require_manual_order(order) -> None:
        if not order or order['payment_method'] not in {'card', 'crypto'}:
            raise ValueError('Заказ с ручной оплатой не найден.')
        if order['status'] == 'canceled' or order['is_paid']:
            raise ValueError('Заказ уже отменён или оплачен.')
        if order['expires_at'] and order['expires_at'] <= utc_now() and not order['receipt_submitted']:
            raise ValueError('Срок оплаты истёк.')

    def submit_receipt(self, order_id: int, user_id: int) -> bool:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
            self._require_manual_order(order)
            if order['receipt_submitted']:
                return False
            conn.execute("UPDATE orders SET receipt_submitted=1,status='receipt_review',updated_at=CURRENT_TIMESTAMP WHERE id=?", (order_id,))
            conn.execute('INSERT INTO notification_outbox(event_key,kind,order_id,user_id) VALUES (?,?,?,?)',
                         ('receipt:' + uuid.uuid4().hex, 'receipt', order_id, user_id))
            return True

    def reject_receipt(self, order_id: int) -> None:
        hours = PAYMENT_TIMEOUT_HOURS
        deadline = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            self._require_manual_order(order)
            if not order['receipt_submitted']:
                raise ValueError('Чек ещё не отправлен.')
            snapshot = json.loads(order['payment_snapshot'])
            snapshot['timeout_hours'] = str(hours)
            conn.execute('UPDATE orders SET receipt_submitted=0,status=?,expires_at=?,payment_snapshot=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                         ('awaiting_card_payment' if order['payment_method'] == 'card' else 'awaiting_crypto_payment', deadline, json.dumps(snapshot, ensure_ascii=False), order_id))

    def update_order_metadata(self, order_id: int, **fields) -> None:
        updates = {key: value for key, value in fields.items() if key in {'admin_note','tracking_number','invoice_message_id'}}
        if updates:
            with self.connect() as conn:
                conn.execute('UPDATE orders SET ' + ', '.join(f'{key}=?' for key in updates) + ' WHERE id=?', [*updates.values(),order_id])

    def mark_order_paid_manual(self, order_id: int) -> None:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order and order['is_paid'] and order['payment_method'] in {'card', 'crypto'}:
                return
            self._require_manual_order(order)
            conn.execute(
                """
                UPDATE orders
                SET is_paid = 1,
                    status = 'paid',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order_id,),
            )
            exists = conn.execute(
                "SELECT id FROM transactions WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO transactions(order_id, user_id, payment_method, amount_stars, payload, amount_minor, currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        order["user_id"],
                        order["payment_method"],
                        0,
                        order["payment_payload"],
                        order['payable_minor'],
                        order['payable_currency'],
                    ),
                )

    def mark_order_paid(
        self,
        *,
        payload: str,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str,
    ) -> int | None:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute(
                "SELECT * FROM orders WHERE payment_payload = ?",
                (payload,),
            ).fetchone()
            if not order:
                return None
            if order['is_paid']:
                return int(order['id'])
            if order['status'] == 'canceled' or order['payment_method'] != 'stars':
                return None

            conn.execute(
                """
                UPDATE orders
                SET is_paid = 1,
                    status = 'paid',
                    telegram_payment_charge_id = ?,
                    provider_payment_charge_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (telegram_payment_charge_id, provider_payment_charge_id, order["id"]),
            )
            exists = conn.execute(
                "SELECT id FROM transactions WHERE order_id = ?",
                (order["id"],),
            ).fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO transactions(
                        order_id, user_id, payment_method, amount_stars, payload,
                        telegram_payment_charge_id, provider_payment_charge_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order["id"],
                        order["user_id"],
                        order["payment_method"],
                        order["total_stars"],
                        payload,
                        telegram_payment_charge_id,
                        provider_payment_charge_id,
                    ),
                )
            return int(order["id"])

    def approve_pre_checkout(self, payload: str, user_id: int, amount: int, currency: str) -> bool:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE payment_payload=?', (payload,)).fetchone()
            enabled = conn.execute("SELECT value FROM shop_settings WHERE key='payment_stars_enabled'").fetchone()
            if (not order or order['user_id'] != user_id or currency != 'XTR' or order['total_stars'] != amount
                    or order['payment_method'] != 'stars' or order['is_paid'] or order['status'] == 'canceled'
                    or (order['expires_at'] and order['expires_at'] <= utc_now()) or not enabled or enabled[0] != '1'):
                return False
            guard = conn.execute('SELECT * FROM payment_guards WHERE payload=?', (payload,)).fetchone()
            if guard and guard['approved_until'] > utc_now():
                return False
            approved_until = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute('INSERT INTO payment_guards(payload,order_id,user_id,amount,approved,approved_until) VALUES (?,?,?,?,1,?) ON CONFLICT(payload) DO UPDATE SET approved=1,approved_until=excluded.approved_until',
                         (payload,order['id'],user_id,amount,approved_until))
            return True

    def record_successful_payment(self, *, payload: str, user_id: int, amount: int, currency: str, charge_id: str, provider_charge_id: str = '') -> tuple[int | None, bool]:
        if not charge_id:
            raise ValueError('Отсутствует идентификатор платежа.')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM payment_events WHERE charge_id=?', (charge_id,)).fetchone()
            if existing:
                linked = conn.execute('SELECT id FROM orders WHERE id=? AND is_paid=1', (existing['order_id'],)).fetchone()
                return (linked['id'] if linked and existing['resolved'] else None), False
            order = conn.execute('SELECT * FROM orders WHERE payment_payload=?', (payload,)).fetchone()
            valid = (order and order['user_id'] == user_id and order['total_stars'] == amount and currency == 'XTR'
                     and order['payment_method'] == 'stars' and not order['is_paid'] and order['status'] != 'canceled')
            order_id = order['id'] if valid else None
            guard = conn.execute('SELECT order_id FROM payment_guards WHERE payload=?', (payload,)).fetchone()
            event_order_id = order_id or (guard['order_id'] if guard else None)
            conn.execute('INSERT INTO payment_events(charge_id,payload,user_id,amount,currency,order_id,resolved) VALUES (?,?,?,?,?,?,?)',
                         (charge_id,payload,user_id,amount,currency,event_order_id,int(bool(valid))))
            if valid:
                conn.execute("UPDATE orders SET is_paid=1,status='paid',telegram_payment_charge_id=?,provider_payment_charge_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (charge_id,provider_charge_id,order_id))
                conn.execute('INSERT INTO transactions(order_id,user_id,payment_method,amount_stars,payload,telegram_payment_charge_id,provider_payment_charge_id,amount_minor,currency) VALUES (?,?,?,?,?,?,?,?,?)',
                             (order_id,user_id,'stars',amount,payload,charge_id,provider_charge_id,amount,currency))
            conn.execute('INSERT INTO notification_outbox(event_key,kind,order_id,user_id,body) VALUES (?,?,?,?,?)',
                         ('payment:' + charge_id, 'paid' if valid else 'issue', order_id, user_id,
                          json.dumps({'amount': amount, 'currency': currency, 'charge_id': charge_id})))
            return order_id, True

    def release_payment_hold(self, order_id: int):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            guard = conn.execute('SELECT * FROM payment_guards WHERE order_id=?', (order_id,)).fetchone()
            if not order or order['is_paid'] or order['status'] != 'payment_check' or not guard or guard['approved_until'] > utc_now():
                raise ValueError('Снять ожидание сейчас нельзя. Проверьте статус платежа.')
            conn.execute('UPDATE payment_guards SET approved=0 WHERE order_id=?', (order_id,))
            conn.execute("UPDATE orders SET status='awaiting_payment' WHERE id=?", (order_id,))

    def pending_notifications(self):
        with self.connect() as conn:
            return list(conn.execute('SELECT * FROM notification_outbox ORDER BY created_at,event_key LIMIT 100'))

    def acknowledge_notification(self, event_key: str, recipient: str):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT delivered FROM notification_outbox WHERE event_key=?', (event_key,)).fetchone()
            if row:
                delivered = set(json.loads(row['delivered'])) | {recipient}
                conn.execute('UPDATE notification_outbox SET delivered=? WHERE event_key=?', (json.dumps(sorted(delivered)), event_key))

    def finish_notification(self, event_key: str):
        with self.connect() as conn:
            conn.execute('DELETE FROM notification_outbox WHERE event_key=?', (event_key,))

    def expire_unpaid_orders(self, now: str | None = None) -> list[dict]:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE orders SET status='payment_check' WHERE is_paid=0 AND expires_at <= ? AND EXISTS (SELECT 1 FROM payment_guards g WHERE g.payload=orders.payment_payload AND g.approved=1)", (now or utc_now(),))
            orders = list(conn.execute("SELECT * FROM orders WHERE is_paid=0 AND receipt_submitted=0 AND expires_at != '' AND expires_at <= ? AND NOT EXISTS (SELECT 1 FROM payment_guards g WHERE g.payload=orders.payment_payload AND g.approved=1)", (now or utc_now(),)))
            for order in orders:
                if order['status'] != 'canceled':
                    conn.execute('INSERT OR IGNORE INTO expiry_notifications(order_id,user_id,invoice_message_id) VALUES (?,?,?)', (order['id'],order['user_id'],order['invoice_message_id']))
                conn.execute('DELETE FROM orders WHERE id=?', (order['id'],))
            return [dict(order) for order in orders]

    def pending_expiry_notifications(self) -> list[Row]:
        with self.connect() as conn:
            return list(conn.execute('SELECT * FROM expiry_notifications ORDER BY order_id LIMIT 100'))

    def acknowledge_expiry_notification(self, order_id: int) -> None:
        with self.connect() as conn:
            conn.execute('DELETE FROM expiry_notifications WHERE order_id=?', (order_id,))

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS orders_total,
                    COALESCE(SUM(CASE WHEN is_demo = 0 AND is_paid = 1 AND payment_method = 'stars' THEN total_stars ELSE 0 END), 0) AS paid_stars,
                    COALESCE(SUM(CASE WHEN is_demo = 0 AND is_paid = 1 THEN total_rub_minor ELSE 0 END), 0) AS paid_rub_minor,
                    COALESCE(SUM(CASE WHEN is_demo = 0 AND is_paid = 1 THEN 1 ELSE 0 END), 0) AS paid_orders,
                    COALESCE(SUM(is_demo), 0) AS demo_orders
                FROM orders
                """
            ).fetchone()
            products = conn.execute(
                "SELECT COUNT(*) AS count FROM products WHERE is_active = 1"
            ).fetchone()
            return {
                "orders_total": int(row["orders_total"]),
                "demo_orders": int(row['demo_orders']),
                "paid_stars": int(row["paid_stars"]),
                "paid_orders": int(row["paid_orders"]),
                "paid_rub_minor": int(row['paid_rub_minor']),
                "payment_issues": int(conn.execute('SELECT COUNT(*) FROM payment_events WHERE resolved=0').fetchone()[0]),
                "payments": [dict(item) for item in conn.execute("SELECT payment_method, payable_currency AS currency, SUM(payable_minor) AS amount FROM orders WHERE is_demo=0 AND is_paid=1 AND payable_currency != '' GROUP BY payment_method,payable_currency")],
                "active_products": int(products["count"]),
            }
