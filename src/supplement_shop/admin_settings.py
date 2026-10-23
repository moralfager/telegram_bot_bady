"""Editable shop settings and validation shared by the admin workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Setting:
    label: str
    group: str
    kind: str = "text"
    default: str = ""
    max_length: int = 1800
    minimum: str = "0"
    maximum: str = "1000"
    places: int = 4


SETTING_GROUPS = {"shop": "🏪 Магазин", "texts": "📝 Тексты", "pricing": "💰 Цены и доставка", "payments": "💳 Оплата"}
PAYMENT_CURRENCIES = ('RUB', 'KZT', 'BYN', 'AMD', 'AZN', 'KGS', 'UZS', 'TJS', 'MDL', 'TMT')
SETTINGS = {
    "shop_title": Setting("Название магазина", "shop", default="Магазин БАДов", max_length=80),
    "shop_open": Setting("Приём заказов", "shop", "bool", "1"),
    "support_username": Setting("Контакт поддержки", "shop", "username"),
    "reviews_url": Setting("Ссылка на отзывы", "shop", "url", max_length=500),
    "manager_username": Setting("👤 Менеджер", "payments", "username"),
    "welcome_text": Setting("Приветствие", "texts"),
    "how_to_buy": Setting("Как купить", "texts"),
    "terms_text": Setting("Условия покупки", "texts", max_length=2500),
    "price_input_currency": Setting("Валюта ввода цен", "pricing", "currency", "RUB"),
    "delivery_rub": Setting("🚚 Доставка, руб.", "pricing", "integer", "0", maximum="10000000", places=0),
    "delivery_stars": Setting("⭐ Доставка, Stars", "pricing", "integer", "0", maximum="10000000", places=0),
    'card_shared_details_enabled': Setting('🌍 Одна карта для всех стран', 'payments', 'bool', '0'),
    'card_payment_currency': Setting('💱 Валюта общей карты', 'payments', 'payment_currency', 'RUB'),
    'card_payment_details': Setting('💳 Общие реквизиты карты', 'payments', max_length=1500),
    "payment_stars_enabled": Setting("Оплата Stars", "payments", "bool", "1"),
    "payment_card_enabled": Setting("Оплата картой", "payments", "bool", "1"),
    "payment_crypto_enabled": Setting("Оплата криптовалютой", "payments", "bool", "0"),
}


def bounded_text(text: str, label: str, maximum: int, minimum: int = 0) -> str:
    value = text.strip()
    length = len(value.encode("utf-16-le")) // 2
    if not minimum <= length <= maximum:
        raise ValueError(f"{label}: от {minimum} до {maximum} символов.")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{label}: недопустимые управляющие символы.")
    return value


def decimal_value(text: str, *, minimum: str = "0", maximum: str = "1000000000", places: int = 8) -> str:
    value = text.strip().replace(",", ".")
    if len(value) > 40 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("Введите обычное положительное число или ноль, без знака и экспоненты.")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Введите корректное число.") from None
    if not number.is_finite() or not Decimal(minimum) <= number <= Decimal(maximum):
        raise ValueError(f"Допустимое значение: от {minimum} до {maximum}.")
    if number.as_tuple().exponent < -places:
        raise ValueError(f"Допускается не более {places} знаков после запятой.")
    return format(number.normalize(), "f")


def validate_setting(key: str, text: str) -> str:
    spec = SETTINGS.get(key)
    if spec is None:
        raise ValueError("Эта настройка недоступна для изменения.")
    value = bounded_text(text, spec.label, spec.max_length)
    if spec.kind == "bool":
        if value not in {"0", "1"}:
            raise ValueError("Выберите: включено или выключено.")
    elif spec.kind == 'payment_currency':
        value = value.upper()
        if value not in PAYMENT_CURRENCIES:
            raise ValueError('Выберите поддерживаемую валюту карты.')
    elif spec.kind == "currency":
        value = value.upper()
        if value not in {"RUB", "KZT"}:
            raise ValueError("Валюта ввода: RUB или KZT.")
    elif spec.kind in {"decimal", "integer"}:
        value = decimal_value(value, minimum=spec.minimum, maximum=spec.maximum, places=spec.places)
    elif spec.kind == "username":
        if value and not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("Введите Telegram-имя вида @username: 5–32 латинских буквы, цифры или _, первая буква.")
    elif spec.kind == "url" and value:
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and not parsed.username and not parsed.password and not any(c.isspace() for c in value))
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Введите полную ссылку http:// или https:// без пробелов.")
    elif key == "shop_title" and not value:
        raise ValueError("Название магазина не может быть пустым.")
    return value


def validate_crypto_field(field: str, text: str, *, network: str = "") -> str:
    value = text.strip()
    if field == "token":
        value = value.upper()
        if not re.fullmatch(r"[A-Z0-9]{2,12}", value):
            raise ValueError("Токен: 2–12 латинских букв или цифр.")
    elif field == "network":
        value = bounded_text(value, "Сеть", 32, 2).upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9 ()-]*", value):
            raise ValueError("Название сети должно состоять из латинских букв, цифр, пробелов, ( ) или -.")
    elif field == "address":
        value = bounded_text(value, "Адрес кошелька", 200, 1)
        if any(c.isspace() for c in value):
            raise ValueError("Адрес кошелька не должен содержать пробелы.")
        canonical_network = re.sub(r"[^A-Z0-9]", "", network.upper())
        if canonical_network == "TRC20" and not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", value):
            raise ValueError("Адрес TRC20 должен начинаться с T и содержать 34 символа Base58.")
        if canonical_network == "ERC20" and not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            raise ValueError("Адрес ERC20: 0x и 40 шестнадцатеричных символов.")
    elif field == "rub_per_unit":
        value = decimal_value(value, minimum="0.00000001")
    else:
        raise ValueError("Неизвестное поле кошелька.")
    return value
