from __future__ import annotations

import json
from decimal import Decimal
from html import escape
from html.parser import HTMLParser

from .database import ORDER_STATUSES, PAYMENT_METHODS
from .pricing import ceil_currency, money


def h(value: object) -> str:
    return escape(str(value), quote=True)


def split_html(text: str, limit: int = 3500) -> list[str]:
    class Splitter(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self.current = []
            self.stack = []
            self.length = 0

        def flush(self):
            if self.length:
                self.parts.append(''.join(self.current) + ''.join(f'</{tag}>' for tag, _ in reversed(self.stack)))
                self.current = [raw for _, raw in self.stack]
                self.length = 0

        def handle_starttag(self, tag, attrs):
            raw = self.get_starttag_text()
            self.stack.append((tag, raw))
            self.current.append(raw)

        def handle_endtag(self, tag):
            self.current.append(f'</{tag}>')
            if self.stack and self.stack[-1][0] == tag:
                self.stack.pop()

        def handle_data(self, data):
            for char in data:
                units = 2 if ord(char) > 0xFFFF else 1
                if self.length + units > limit:
                    self.flush()
                self.current.append(h(char))
                self.length += units

    parser = Splitter()
    parser.feed(text)
    parser.close()
    parser.flush()
    return parser.parts or [text]


def send_html(client, chat_id, text, reply_markup=None):
    parts = split_html(text)
    result = None
    for index, part in enumerate(parts):
        result = client.send_message(chat_id, part, reply_markup=reply_markup if index == len(parts) - 1 else None)
    return result


def product_price(product, country=None) -> str:
    if not product['price_confirmed']:
        return 'Цена уточняется'
    base = money(int(product['price_rub_minor']))
    if country and country['currency'] != 'RUB' and Decimal(country['rate_per_rub']) > 0:
        local = money(ceil_currency(Decimal(product['price_rub_minor']) * Decimal(country['rate_per_rub'])), country['currency'])
        return f'{local} (≈ {base})'
    return base


def product_caption(product, position=None, total=None, *, country=None) -> str:
    parts = [f"<b>{h(product['title'])}</b>", h(product['description']),
             f"💰 <b>Цена:</b> {h(product_price(product, country))}"]
    if product['stars_price_confirmed']:
        parts.insert(3, f"⭐ Оплата Stars: {product['price_stars']}")
    if product['characteristics']:
        parts.append(f"<b>Характеристики:</b>\n{h(product['characteristics'])}")
    return '\n\n'.join(parts)


def cart_text(cart_items, country=None, settings=None) -> str:
    if not cart_items:
        return 'Корзина пустая. Выберите товары в каталоге.'
    lines = ['<b>🛒 Корзина</b>']
    total = 0
    for item in cart_items:
        line = int(item['price_rub_minor']) * int(item['qty'])
        total += line
        lines.append(f"{h(item['title'][:80])}: {item['qty']} шт. = {money(line)}")
    lines.append(f'\n<b>Товары:</b> {money(total)}')
    if country and country['currency'] != 'RUB' and Decimal(country['rate_per_rub']) > 0:
        lines.append(f"В вашей валюте: ≈ {money(ceil_currency(Decimal(total) * Decimal(country['rate_per_rub'])), country['currency'])}")
    if all(item['stars_price_confirmed'] for item in cart_items):
        lines.append(f"⭐ Товары в Stars: {sum(item['price_stars'] * item['qty'] for item in cart_items)}")
    lines.append('🚚 Доставка и итоговая сумма будут показаны перед оплатой. Наценок нет.')
    return '\n'.join(lines)


def order_amount(order) -> str:
    if order['payment_method'] == 'stars':
        return f"{order['total_stars']} Stars"
    if order['payable_currency']:
        return money(order['payable_minor'], order['payable_currency'])
    return 'Сумма требует проверки менеджером'


def orders_text(orders) -> str:
    if not orders:
        return 'У вас пока нет заказов.'
    return '<b>История заказов</b>\n' + '\n'.join(
        f"#{order['id']} · {h(ORDER_STATUSES.get(order['status'], order['status']))} · {h(order_amount(order))}"
        for order in orders)


def payment_deadline_text(order) -> str:
    if order['is_paid'] or order['status'] == 'canceled':
        return ''
    if order['receipt_submitted']:
        return 'Чек передан на проверку. Заказ сохранён до решения менеджера.'
    if order['status'] == 'payment_check':
        return '🔎 Telegram начал оплату Stars. Заказ сохранён до проверки результата; повторно не платите.'
    if not order['expires_at']:
        return 'Ожидайте: менеджер примет заказ и отправит реквизиты для оплаты.'
    snapshot = json.loads(order['payment_snapshot'])
    hours = snapshot.get('timeout_hours', '3')
    return f"На оплату: {hours} ч. с момента отправки реквизитов. Срок: {order['expires_at']} UTC.\nПосле истечения срока неоплаченный заказ удаляется."


def order_detail_text(order, items) -> str:
    lines = [f"<b>📦 Заказ #{order['id']}</b>",
             f"Статус: {h(ORDER_STATUSES.get(order['status'], order['status']))}",
             f"Способ оплаты: {h(PAYMENT_METHODS.get(order['payment_method'], order['payment_method']))}",
             f"Оплачен: {'Да' if order['is_paid'] else 'Нет'}",
             f"<b>К оплате: {h(order_amount(order))}</b>"]
    if order['total_rub_minor']:
        lines.append(f"Базовая сумма с доставкой: {money(order['total_rub_minor'])}")
    lines.extend(['', '<b>Товары:</b>'])
    for item in items:
        price = money(item['price_rub_minor']) if item['price_rub_minor'] else f"{item['price_stars']} Stars (старая цена)"
        lines.append(f"{h(item['title'][:80])}: {item['qty']} × {price}")
    lines.extend(['', '<b>Доставка:</b>', f"Страна: {h(order['country_code'])}",
                  f"Имя: {h(order['customer_name'][:120])}"])
    telegram_tag = order['telegram_tag'] if 'telegram_tag' in order.keys() else ''
    if telegram_tag:
        lines.append(f"Telegram: {h(telegram_tag[:33])}")
    elif order['phone']:
        lines.append(f"Телефон: {h(order['phone'][:40])}")
    else:
        user_id = int(order['user_id'] or 0)
        if user_id:
            lines.append(f'Telegram: <a href="tg://user?id={user_id}">Написать покупателю (ID {user_id})</a>')
    lines.append(f"Адрес: {h(order['address'][:500])}")
    if order['comment']:
        lines.append(f"Комментарий: {h(order['comment'][:500])}")
    if order['tracking_number']:
        lines.append(f"Трек-номер: {h(order['tracking_number'])}")
    deadline = payment_deadline_text(order)
    if deadline:
        lines.extend(['', h(deadline)])
    return '\n'.join(lines)


def manual_payment_text(order) -> str:
    snapshot = json.loads(order['payment_snapshot'])
    lines = [f"<b>💳 Оплата заказа #{order['id']}</b>"]
    lines.append(f"<b>Отправить: {h(order_amount(order))}</b>")
    if not order['payable_currency']:
        return '\n'.join(lines) + '\nСтарый заказ: уточните сумму у менеджера, не переводите до согласования.'
    lines.append(f"Базовая сумма: {money(order['total_rub_minor'])}")
    if snapshot.get('gross_rub_minor') and snapshot['gross_rub_minor'] != order['total_rub_minor']:
        lines.append(f"По условиям старого заказа: {money(snapshot['gross_rub_minor'])}")
    if order['payment_method'] == 'crypto':
        crypto = snapshot.get('crypto') or {}
        lines.extend([f"Токен: <b>{h(crypto.get('token', ''))}</b>",
                      f"Сеть: <b>{h(crypto.get('network', ''))}</b>",
                      f"Адрес кошелька:\n<code>{h(crypto.get('address', ''))}</code>",
                      'Используйте только указанную сеть. Комиссию сети оплатите сверх суммы заказа.'])
    else:
        lines.append(h(snapshot.get('card_details', '')))
    if order['payment_method'] == 'crypto':
        crypto = snapshot.get('crypto') or {}
        lines.append(f"Курс расчёта: 1 {h(crypto.get('token', ''))} = {h(crypto.get('rub_per_unit', ''))} RUB")
    elif order['payable_currency'] not in {'RUB', 'XTR'}:
        lines.append(f"Курс расчёта: 1 RUB = {h(snapshot.get('rate', ''))} {h(order['payable_currency'])}")
    elif snapshot.get('local_currency', 'RUB') != 'RUB' and Decimal(snapshot.get('local_rate', '0')) > 0:
        local = money(ceil_currency(Decimal(order['payable_minor']) * Decimal(snapshot['local_rate'])), snapshot['local_currency'])
        lines.append(f'Примерно {local}. Перевод нужен именно в RUB.')
    manager = snapshot.get('manager_username', '')
    if manager:
        username = manager.lstrip('@')
        lines.append(f'Чек / хеш транзакции отправьте менеджеру <a href="https://t.me/{h(username)}">@{h(username)}</a> с номером #{order["id"]}.')
    lines.extend(['🧾 Менеджер проверит поступление средств и подтвердит оплату.', payment_deadline_text(order)])
    return '\n\n'.join(line for line in lines if line)


def card_payment_text(order, card_details: str = '') -> str:
    return manual_payment_text(order)


def admin_product_text(product) -> str:
    lines = [f"<b>{h(product['title'])}</b>", f"Цена: {money(product['price_rub_minor'])}",
             f"Продажи: {'открыты' if product['is_active'] else 'остановлены'}",
             f"Медиа: {h(product['media_type'])}"]
    lines.insert(2, f"⭐ Цена Stars: {product['price_stars']}" if product['stars_price_confirmed'] else '⚠️ Цена Stars не задана вручную. Оплата Stars недоступна.')
    if not product['price_confirmed']:
        lines.append('Рублёвая цена предварительная, рассчитана из старой цены Stars. Введите правильную цену для возобновления продажи.')
    lines.extend([f"\nОписание:\n{h(product['description'])}", f"\nХарактеристики:\n{h(product['characteristics'])}"])
    return '\n'.join(lines)


def stats_text(stats: dict) -> str:
    text = '\n'.join(['<b>Статистика</b>', f"Активных товаров: {stats['active_products']}",
                      f"Заказов в базе: {stats['orders_total']}", f"Оплаченных: {stats['paid_orders']}",
                      f"Базовая стоимость оплаченных заказов: {money(stats.get('paid_rub_minor', 0))}",
                      f"Получено Stars: {stats['paid_stars']}",
                      f"Платежей для ручного разбора: {stats.get('payment_issues', 0)}"])
    if stats.get('demo_orders'):
        text += f"\n🧪 Демо-заказов: {stats.get('demo_orders', 0)}"
    for payment in stats.get('payments', []):
        amount = f"{payment['amount']} Stars" if payment['currency'] == 'XTR' else money(payment['amount'], payment['currency'])
        text += f"\n{h(PAYMENT_METHODS[payment['payment_method']])}: {h(amount)}"
    return text


