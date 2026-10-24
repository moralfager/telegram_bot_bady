from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .database import ORDER_STATUSES
from .formatting import order_amount, product_price
from .pricing import money

MENU_LABELS = {
    'Товары': '🛍 Товары', 'Корзина': '🛒 Корзина', 'Мои заказы': '📦 Мои заказы',
    'Техническая поддержка': '💬 Техническая поддержка', 'Отзывы': '⭐ Отзывы',
    'Как купить?': '❓ Как купить?', 'Страна': '🌍 Страна', 'Условия покупки': '📋 Условия покупки',
}

BUTTON_LABELS = MENU_LABELS | {
    'Купить': '🛍 Купить', 'Назад к списку': '⬅️ К списку', 'Назад к заказам': '⬅️ К заказам',
    'Оформить заказ': '✅ Оформить заказ', 'Убрать': '🗑 Убрать', 'Отмена': '✖️ Отмена',
    'Отменить заказ': '✖️ Отменить заказ', 'Отменить оформление': '✖️ Отменить оформление',
    'Каталог': '🛍 Каталог', 'Название': '✏️ Название', 'Цена': '💰 Цена RUB', 'Остаток': '📦 Остаток',
    'Описание': '📝 Описание', 'Характеристики': '📋 Характеристики', 'Фото/видео': '📷 Фото/видео',
    'Скрыть': '⏸ Скрыть', 'Показать': '▶️ Показать', 'Удалить': '🗑 Убрать из продажи',
    'Все': '📋 Все', 'Неоплаченные': '⏳ Неоплаченные', 'Проверка чеков': '🔎 Проверка чеков',
    'Оплаченные': '✅ Оплаченные', 'Обработка': '📦 Обработка', 'Доставка': '🚚 Доставка',
    'Завершённые': '✅ Завершённые', 'Отменённые': '✖️ Отменённые', 'Поиск заказа': '🔍 Поиск заказа',
    'Все статусы': '📋 Все статусы', 'Отправить реквизиты': '💳 Отправить реквизиты',
    'Оплата получена': '✅ Оплата получена', 'Отклонить чек': '✖️ Отклонить чек',
    'В обработке': '📦 В обработке', 'Передан в доставку': '🚚 Передан в доставку',
    'Доставлен': '✅ Доставлен', 'Заметка': '📝 Заметка', 'Трек-номер': '🚚 Трек-номер',
    'Назад': '⬅️ Назад', 'Далее': 'Вперёд ➡️', 'Чек отправлен': '🧾 Чек отправлен',
    'Реквизиты и менеджер': '💳 Реквизиты и менеджер', 'Оплатить Stars': '⭐ Оплатить Stars',
}


def button(text: str, data: str) -> dict[str, Any]:
    return {"text": BUTTON_LABELS.get(text, text), "callback_data": data}


def pay_button(text: str) -> dict[str, Any]:
    return {"text": text, "pay": True}


def keyboard(rows: Iterable[Iterable[dict[str, Any]]]) -> dict[str, Any]:
    return {"inline_keyboard": [list(row) for row in rows]}


def product_keyboard(product_id: int, page: int = 0) -> dict[str, Any]:
    rows = [
        [button("🛒 В корзину", f"add:{product_id}"), button("Купить", f"buy:{product_id}")],
        [button("Назад к списку", f"catalog:{page}"), button("🛒 Корзина", "cart")],
    ]
    return keyboard(rows)


def cart_keyboard(cart_items) -> dict[str, Any]:
    rows = []
    for item in cart_items:
        rows.append(
            [
                button("➖", f"qty:{item['id']}:-1"),
                button(f"{item['title'][:22]} x{item['qty']}", "noop"),
                button("➕", f"qty:{item['id']}:1"),
            ]
        )
        rows.append([button("Убрать", f"qty:{item['id']}:0")])
    rows.append([button("Оформить заказ", "checkout")])
    rows.append([button("📂 Каталог", "home")])
    return keyboard(rows)


def back_home_keyboard() -> dict[str, Any]:
    return keyboard([[button("📂 Каталог", "home"), button("🛒 Корзина", "cart")]])


def checkout_cancel_keyboard() -> dict[str, Any]:
    return keyboard([[button('⬅️ Назад', 'checkout_back')], [button("Отменить оформление", "checkout_cancel")]])


def invoice_keyboard(order_id: int, amount_stars: int) -> dict[str, Any]:
    return keyboard(
        [
            [pay_button(f"Заплатить ⭐ {amount_stars}")],
            [button("Отменить заказ", f"order_cancel:{order_id}")],
        ]
    )


def admin_persistent_menu() -> dict[str, Any]:
    return {
        'keyboard': [[{'text': '🎛 Меню админки'}, {'text': '📦 Заказы'}]],
        'resize_keyboard': True,
        'is_persistent': True,
        'one_time_keyboard': False,
    }


def admin_menu_keyboard(pending_count: int = 0) -> dict[str, Any]:
    orders_label = f"🧾 Заказы ({pending_count})" if pending_count > 0 else "🧾 Заказы"
    return keyboard(
        [
            [button("📦 Товары", "a:products"), button(orders_label, "a:orders")],
            [button("⚙️ Ещё (настройки)", "a:more")],
        ]
    )


def admin_more_keyboard() -> dict[str, Any]:
    return keyboard(
        [
            [button("💳 Оплата и реквизиты", "a:payments"), button("📊 Статистика", "a:stats")],
            [button("⚙️ Магазин и тексты", "a:settings")],
            [button("⬅️ Меню", "a:menu")],
        ]
    )


def admin_products_keyboard(products, category_id: int | None = None, page=0, total=None) -> dict[str, Any]:
    rows = [[button("➕ Добавить товар", "a:prod:add")]]
    for product in products:
        active = "✅" if product["is_active"] else "⏸"
        rows.append([button(f"{active} {product['title'][:45]} · {money(product['price_rub_minor'])}", f"a:prod:{product['id']}")])
    rows.extend(page_rows(page, total if total is not None else len(products), 10, 'a:products'))
    rows.append([button("⬅️ Меню", "a:menu")])
    return keyboard(rows)


def admin_product_card_keyboard(product_id: int, is_active: int) -> dict[str, Any]:
    active_text = "⏸ Остановить продажи" if is_active else "▶️ Возобновить продажи"
    return keyboard(
        [
            [button('Название', f'a:prod:edit:{product_id}:title')],
            [button("Цена", f"a:prod:edit:{product_id}:price")],
            [button('⭐ Цена Stars', f'a:prod:edit:{product_id}:stars')],
            [button("Описание", f"a:prod:edit:{product_id}:desc"), button("Характеристики", f"a:prod:edit:{product_id}:chars")],
            [button("Фото/видео", f"a:prod:edit:{product_id}:media"), button(active_text, f"a:prod:toggle:{product_id}")],
            [button("Удалить", f"a:prod:delete:{product_id}")],
            [button("⬅️ Товары", "a:products")],
        ]
    )


def admin_orders_keyboard(orders, page=0, total=None, status='all', search='') -> dict[str, Any]:
    rows = []
    for order in orders:
        paid = "✅" if order["is_paid"] else "⏳"
        title = ORDER_STATUSES.get(order["status"], order["status"])
        rows.append([button(f"{paid} #{order['id']} · {title} · {order_amount(order)}", f"a:order:{order['id']}:{status}:{page}")])
    rows.extend(page_rows(page, total if total is not None else len(orders), 10, f'a:orders:{status}'))
    rows.extend([[button('⏳ Ждут реквизитов', 'a:orders:pending_details:0')],
                 [button('Все', 'a:orders:all:0'), button('Неоплаченные', 'a:orders:unpaid:0')],
                 [button('Оплаченные', 'a:orders:paid:0')],
                 [button('Поиск заказа', 'a:ordersearch')]])
    rows.append([button("⬅️ Меню", "a:menu")])
    return keyboard(rows)


def admin_order_keyboard(order, page=0, status='all') -> dict[str, Any]:
    order_id = int(order["id"])
    rows = []
    if order['status'] == 'payment_check' and not order['is_paid']:
        rows.append([button('🔎 Проверил: платёж не поступил', f'a:release:{order_id}')])
    manual = order['payment_method'] in {'card', 'crypto'} and not order['is_paid'] and order['status'] != 'canceled'
    if manual:
        if not order['payment_details_sent']:
            rows.append([button('💳 Отправить реквизиты', f'a:sendcard:{order_id}')])
        rows.append([button("Оплата получена", f"a:markpaid:{order_id}")])
        if order['receipt_submitted']:
            rows.append([button('Отклонить чек', f'a:rejectreceipt:{order_id}')])
    if order['is_paid']:
        rows.extend([
            [button("Доставлен", f"a:done:{order_id}")],
        ])
    elif order['status'] != 'canceled':
        rows.append([button('Отменить заказ', f'a:status:{order_id}:canceled')])
    rows.extend([[button('Заметка', f'a:note:{order_id}')],
                 [button('Назад к заказам', f'a:orders:{status}:{page}')]])
    return keyboard(rows)

def cancel_keyboard() -> dict[str, Any]:
    return keyboard([[button("Отмена", "a:cancel")]])


def page_rows(page, total, size, prefix):
    pages = max(1, (total + size - 1) // size)
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(button('Назад', f'{prefix}:{page - 1}'))
    row.append(button(f'{page + 1} / {pages}', 'noop'))
    if page + 1 < pages:
        row.append(button('Далее', f'{prefix}:{page + 1}'))
    return [row]


def persistent_menu():
    rows = [('Товары', 'Корзина'), ('Техническая поддержка',),
            ('Отзывы', 'Как купить?'), ('Страна', 'Условия покупки')]
    return {'keyboard': [[{'text': MENU_LABELS[label]} for label in row] for row in rows],
            'resize_keyboard': True, 'is_persistent': True, 'one_time_keyboard': False}


def country_keyboard(countries):
    return keyboard([[button(''.join(chr(127397 + ord(c)) for c in country['code']) + ' ' + country['title'], f"country:{country['code']}")] for country in countries])


def catalog_keyboard(products, page, total, country=None):
    rows = [[button(f"{product['title'][:42]} · {product_price(product, country)}", f"product:{product['id']}:{page}")] for product in products]
    rows.extend(page_rows(page, total, 12, 'catalog'))
    rows.append([button('Корзина', 'cart')])
    return keyboard(rows)


def customer_order_action_keyboard(order):
    rows = []
    if not order['is_paid'] and order['status'] != 'canceled':
        if order['payment_method'] == 'stars':
            rows.append([button('⭐ Оплатить Stars', f"invoice:{order['id']}")])
        if not order['receipt_submitted']:
            rows.append([button('✖️ Отменить заказ', f"order_cancel:{order['id']}")])
    rows.append([button('📂 Каталог', 'home')])
    return keyboard(rows)


def customer_orders_keyboard(orders, page, total):
    rows = [[button(f"#{order['id']} · {ORDER_STATUSES.get(order['status'])} · {order_amount(order)}", f"order:{order['id']}:{page}")] for order in orders]
    rows.extend(page_rows(page, total, 10, 'orders'))
    rows.append([button('Каталог', 'home')])
    return keyboard(rows)


def customer_order_keyboard(order, page=0):
    rows = []
    if not order['is_paid'] and order['status'] != 'canceled':
        if order['payment_method'] == 'stars':
            rows.append([button('Оплатить Stars', f"invoice:{order['id']}")])
        if not order['receipt_submitted']:
            rows.append([button('Отменить заказ', f"order_cancel:{order['id']}")])
    rows.append([button('Назад к каталогу', 'home')])
    return keyboard(rows)
