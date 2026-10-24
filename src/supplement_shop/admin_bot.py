from __future__ import annotations

import threading
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from uuid import uuid4

from .admin_settings import (
    SETTINGS, SETTING_GROUPS, PAYMENT_CURRENCIES, bounded_text, decimal_value, validate_crypto_field, validate_setting,
)
from .config import Settings
from .formatting import split_html
from .database import Database, ORDER_STATUSES
from .formatting import admin_product_text, manual_payment_text, order_detail_text, stats_text
from .keyboards import (
    admin_menu_keyboard,
    admin_order_keyboard,
    admin_orders_keyboard,
    admin_persistent_menu,
    admin_product_card_keyboard,
    admin_products_keyboard,
    button,
    cancel_keyboard,
    keyboard,
)
from .pricing import money
from .telegram_api import BotClient, TelegramApiError


PAGE_SIZE = 10
MANUAL_METHODS = {"card", "crypto"}
COUNTRY_FIELDS = {"rate_per_rub": "Курс: единиц местной валюты за 1 рубль",
                  "card_details": "Реквизиты для перевода", "card_currency": "Валюта перевода"}
CRYPTO_FIELDS = {"token": "Токен", "network": "Сеть", "address": "Адрес кошелька",
                 "rub_per_unit": "Рублей за единицу токена"}


class AdminBot:
    def __init__(
        self,
        *,
        client: BotClient,
        buyer_client: BotClient,
        db: Database,
        settings: Settings,
    ):
        self.client = client
        self.buyer_client = buyer_client
        self.db = db
        self.settings = settings
        self.media_dir = settings.db_path.parent / "media"
        self.sessions: dict[int, dict[str, Any]] = {}
        self.order_views: dict[int, dict[str, Any]] = {}
        self.product_views: dict[int, dict[str, Any]] = {}

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        if "message" in update:
            self.handle_message(update["message"])

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        user_id = int(message.get("from", {}).get("id", 0))
        text = (message.get("text") or "").strip()

        if not self.is_admin(user_id):
            self.client.send_message(chat_id, "Доступ закрыт.")
            return

        if message["chat"].get("type") != "private" or chat_id != user_id:
            self.client.send_message(chat_id, "Админ-панель доступна только в личном чате с ботом.")
            return

        if text in {"/admin", "/start", "/menu", "🎛 Меню админки", "🎛 Меню"}:
            self.sessions.pop(user_id, None)
            if text in {"/admin", "/start"}:
                self.client.send_message(chat_id, "Добро пожаловать в админ-панель.", reply_markup=admin_persistent_menu())
            self.show_menu(chat_id)
            return

        if text in {"/orders", "📦 Заказы", "Заказы"}:
            self.sessions.pop(user_id, None)
            self.show_orders(chat_id)
            return

        if text == "/cancel":
            self.sessions.pop(user_id, None)
            self.client.send_message(chat_id, "Действие отменено.", reply_markup=admin_persistent_menu())
            self.show_menu(chat_id)
            return

        if user_id in self.sessions:
            try:
                self.process_session_message(message)
            except ValueError as exc:
                self.show_error(chat_id, exc)
            except TelegramApiError as exc:
                print(f"[admin] Telegram API error: {exc}")
            return

        self.client.send_message(chat_id, "Выберите действие.", reply_markup=admin_persistent_menu())
        self.show_menu(chat_id)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        user_id = int(callback.get("from", {}).get("id", 0))
        message = callback.get("message") or {}
        chat_id = int(message.get("chat", {}).get("id", user_id))
        data = callback.get("data", "")

        if not self.is_admin(user_id):
            self.client.answer_callback_query(callback_id, "Доступ закрыт.", show_alert=True)
            return

        if message.get("chat", {}).get("type") != "private" or chat_id != user_id:
            self.client.answer_callback_query(callback_id, "Откройте личный чат с админ-ботом.", show_alert=True)
            return

        try:
            self.client.answer_callback_query(callback_id)
            if data == "noop":
                return
            if not data.startswith(("a:confirm_paid:", "a:issue_confirm:", 'a:release_confirm:', 'a:done_confirm:')):
                self.sessions.pop(user_id, None)
            if data == "a:cancel":
                self.sessions.pop(user_id, None)
                self.show_menu(chat_id, message)
            elif data == "a:more":
                self.show_more(chat_id, message)
            elif data == "a:menu":
                self.sessions.pop(user_id, None)
                self.show_menu(chat_id, message)
            elif data == 'a:readiness':
                self.show_payment_settings(chat_id, message)
            elif data.startswith('a:release:'):
                order_id = int(data.split(':')[2])
                order = self.db.get_order(order_id)
                if not order or order['status'] != 'payment_check' or order['is_paid']:
                    raise ValueError('Заказ не ожидает проверки платежа Stars.')
                self.sessions[user_id] = {'action':'release_payment', 'order_id':order_id}
                self.edit_or_send_text(chat_id, message,
                    '⚠️ Вы проверили операции Stars и убедились, что оплата не поступила?\nПосле подтверждения просроченный заказ будет удалён. Это не возврат платежа. При сомнениях оставьте заказ на проверке.',
                    keyboard([[button('Подтверждаю: оплаты нет', f'a:release_confirm:{order_id}')],
                              [button('Назад', f'a:order:{order_id}')]]))
            elif data.startswith('a:release_confirm:'):
                order_id = int(data.split(':')[2])
                session = self.sessions.pop(user_id, {})
                if session.get('action') != 'release_payment' or session.get('order_id') != order_id:
                    raise ValueError('Сначала откройте заказ и подтвердите проверку платежа.')
                self.db.release_payment_hold(order_id)
                self.db.expire_unpaid_orders()
                self.show_orders(chat_id, message)
            elif data == "a:cats":
                self.show_categories(chat_id, message)
            elif data == "a:cat:add":
                self.sessions[user_id] = {"action": "category_add"}
                self.client.send_message(chat_id, "Введите название категории.", reply_markup=cancel_keyboard())
            elif data.startswith("a:cat:del:"):
                category_id = int(data.split(":")[3])
                self.db.deactivate_category(category_id)
                self.show_categories(chat_id, message)
            elif data == "a:products":
                self.product_views.pop(chat_id, None)
                self.show_products(chat_id, message=message)
            elif data.startswith("a:products:"):
                self.show_products(chat_id, message=message, page=int(data.split(":")[2]))
            elif data == "a:prod:add":
                self.start_product_add(chat_id, user_id)
            elif data.startswith("a:prod:add_cat:"):
                category_id = int(data.split(":")[3])
                if not self.db.get_category(category_id):
                    raise ValueError("Категория не найдена.")
                self.sessions[user_id] = {
                    "action": "product_add",
                    "step": "title",
                    "category_id": category_id,
                    "data": {},
                }
                self.client.send_message(chat_id, "Введите название товара.", reply_markup=cancel_keyboard())
            elif data.startswith("a:prod:list:"):
                category_id = int(data.split(":")[3])
                self.show_products(chat_id, category_id, message)
            elif data.startswith("a:prod:edit:"):
                _, _, _, product_id, field = data.split(":")
                self.start_product_edit(chat_id, user_id, int(product_id), field)
            elif data.startswith("a:prod:toggle:"):
                product_id = int(data.split(":")[3])
                product = self.db.get_product(product_id)
                if product:
                    self.db.update_product(product_id, is_active=0 if product["is_active"] else 1)
                    self.show_product_card(chat_id, product_id, message)
            elif data.startswith("a:prod:delete:"):
                product_id = int(data.split(":")[3])
                self.db.deactivate_product(product_id)
                self.show_products(chat_id, message=message)
            elif data.startswith("a:prod:"):
                product_id = int(data.split(":")[2])
                self.show_product_card(chat_id, product_id, message)
            elif data == "a:orders":
                self.order_views.pop(chat_id, None)
                self.show_orders(chat_id, message)
            elif data.startswith("a:orders:"):
                _, _, status, page = data.split(":")
                self.show_orders(chat_id, message, status=status, page=int(page))
            elif data == "a:ordersearch":
                self.prompt(chat_id, user_id, {"action": "order_search"},
                            "Введите #номер заказа, имя или телефон. «-» сбрасывает поиск.")
            elif data == 'a:orderfilters':
                rows = [[button(title, f'a:orders:{status}:0')] for status, title in ORDER_STATUSES.items()]
                rows.append([button('Все заказы', 'a:orders:all:0')])
                self.edit_or_send_text(chat_id, message, 'Выберите статус заказа.', keyboard(rows))
            elif data.startswith("a:order:"):
                parts = data.split(":")
                order_id = int(parts[2])
                if len(parts) == 5:
                    self.set_order_view(chat_id, status=parts[3], page=int(parts[4]))
                self.show_order(chat_id, order_id, message)
            elif data.startswith("a:note:") or data.startswith("a:tracking:"):
                _, action, order_id = data.split(":")
                if not self.db.get_order(int(order_id)):
                    raise ValueError("Заказ не найден.")
                self.prompt(chat_id, user_id, {"action": "order_metadata", "order_id": int(order_id),
                                             "field": "admin_note" if action == "note" else "tracking_number"},
                            "Введите внутреннюю заметку (до 800 символов). «-» удаляет заметку." if action == "note"
                            else "Введите трек-номер (до 120 символов). «-» удаляет трек-номер.")
            elif data == "a:payments":
                self.show_payment_settings(chat_id, message)
            elif data.startswith("a:pay:toggle:"):
                method = data.split(":")[3]
                self.toggle_payment_method(chat_id, method, message)
            elif data == "a:pay:details":
                self.prompt(chat_id, user_id, {"action": "payment_details"},
                            "Введите общие реквизиты карты (до 1500 символов). "
                            "Они используются для всех стран при включённой настройке «Одна карта для всех стран». "
                            "Валюту общей карты выберите в разделе «Оплата». «-» очищает реквизиты.")
            elif data.startswith("a:sendcard:"):
                order_id = int(data.split(":")[2])
                self.send_manual_payment_details(chat_id, order_id, message)
            elif data.startswith("a:markpaid:"):
                order_id = int(data.split(":")[2])
                self.confirm_payment_prompt(chat_id, user_id, order_id, message)
            elif data.startswith("a:confirm_paid:"):
                order_id = int(data.split(":")[2])
                session = self.sessions.pop(user_id, {})
                if session.get("action") != "confirm_paid" or session.get("order_id") != order_id:
                    raise ValueError("Сначала нажмите «Оплата получена» в заказе и подтвердите оплату.")
                self.mark_order_paid(chat_id, order_id, message)
            elif data.startswith("a:rejectreceipt:"):
                order_id = int(data.split(":")[2])
                self.require_manual_order(order_id)
                self.db.reject_receipt(order_id)
                order = self.db.get_order(order_id)
                self.show_order(chat_id, order_id, message)
            elif data.startswith("a:done:"):
                order_id = int(data.split(":")[2])
                order = self.db.get_order(order_id)
                if not order:
                    raise ValueError("Заказ не найден.")
                if not order["is_paid"]:
                    raise ValueError("Сначала подтвердите оплату заказа.")
                self.sessions[user_id] = {"action": "complete_order", "order_id": order_id}
                self.edit_or_send_text(
                    chat_id,
                    message,
                    f"⚠️ <b>Завершить и удалить заказ #{order_id}?</b>\n\n"
                    "После подтверждения заказ будет помечен как доставленный и удалён из базы для экономии места. "
                    "Сумма заказа будет сохранена в общей статистике выручки.",
                    keyboard([
                        [button("✅ Да, заказ доставлен (удалить)", f"a:done_confirm:{order_id}")],
                        [button("⬅️ Назад к заказу", f"a:order:{order_id}")],
                    ]),
                )
            elif data.startswith("a:done_confirm:"):
                order_id = int(data.split(":")[2])
                session = self.sessions.pop(user_id, {})
                if session.get("action") != "complete_order" or session.get("order_id") != order_id:
                    raise ValueError("Сначала откройте заказ и нажмите «Доставлен».")
                self.db.complete_and_delete_order(order_id)
                sent = self.client.send_message(
                    chat_id,
                    f"✅ Заказ #{order_id} доставлен и удалён из базы. Выручка сохранена в статистике.",
                )
                if isinstance(sent, dict) and sent.get("message_id"):
                    self.schedule_message_deletion(chat_id, int(sent["message_id"]), delay_seconds=600)
                self.show_orders(chat_id, message)
            elif data.startswith("a:status:"):
                _, _, order_id, status = data.split(":")
                self.update_order_status(chat_id, int(order_id), status, message)
            elif data == "a:stats":
                self.edit_or_send_text(chat_id, message, stats_text(self.db.stats()), self.menu_keyboard())
            elif data == "a:issues" or data.startswith("a:issues:"):
                self.show_payment_issues(chat_id, message, int(data.split(":")[2]) if data != "a:issues" else 0)
            elif data.startswith("a:issue:"):
                self.show_payment_issue(chat_id, int(data.split(":")[2]), message)
            elif data.startswith("a:issue_resolve:"):
                event_id = int(data.split(":")[2])
                event = self.payment_issue(event_id)
                self.sessions[user_id] = {"action": "resolve_issue", "event_id": event_id, "charge_id": event["charge_id"]}
                self.edit_or_send_text(chat_id, message,
                                      "<b>Платёж урегулирован вручную?</b>\n"
                                      "Подтвердите только после проверки и решения вопроса с покупателем. "
                                      "Бот лишь закроет обращение: возврат денег и изменение заказа не выполняются.",
                                      keyboard([[button("Да, вопрос решён", f"a:issue_confirm:{event_id}")],
                                                [button("Назад", f"a:issue:{event_id}")]]))
            elif data.startswith("a:issue_confirm:"):
                event_id = int(data.split(":")[2])
                session = self.sessions.pop(user_id, {})
                if session.get("action") != "resolve_issue" or session.get("event_id") != event_id:
                    raise ValueError("Сначала откройте платёжную проблему и подтвердите ручное урегулирование.")
                with self.db.connect() as conn:
                    conn.execute("UPDATE payment_events SET resolved=1 WHERE rowid=? AND charge_id=? AND resolved=0",
                                 (event_id, session["charge_id"]))
                self.show_payment_issues(chat_id, message)
            elif data == "a:settings":
                self.show_settings(chat_id, message)
            elif data.startswith("a:settings:"):
                self.show_settings(chat_id, message, group=data.split(":")[2])
            elif data.startswith("a:setting:"):
                self.start_setting_edit(chat_id, user_id, data.split(":")[2], message)
            elif data.startswith("a:set:"):
                _, _, key, value = data.split(":")
                spec = SETTINGS.get(key)
                if spec is None or spec.kind not in {"bool", "currency", 'payment_currency'}:
                    raise ValueError("Эта настройка не поддерживает переключение.")
                self.db.set_setting(key, validate_setting(key, value))
                self.show_settings(chat_id, message, spec.group)
            elif data == "a:countries":
                self.show_countries(chat_id, message)
            elif data.startswith("a:country:"):
                self.country_callback(chat_id, user_id, data.split(":"), message)
            elif data == "a:crypto" or data.startswith("a:crypto:page:"):
                page = int(data.split(":")[3]) if data.startswith("a:crypto:page:") else 0
                self.show_crypto_options(chat_id, message, page)
            elif data == "a:crypto:add":
                self.prompt(chat_id, user_id, {"action": "crypto_add", "step": "token", "data": {}},
                            "Введите обозначение токена, например USDT.")
            elif data.startswith("a:crypto:"):
                self.crypto_callback(chat_id, user_id, data.split(":"), message)
        except ValueError as exc:
            self.show_error(chat_id, exc)
        except TelegramApiError as exc:
            print(f"[admin] Telegram API error: {exc}")

    def show_error(self, chat_id: int, exc: ValueError) -> None:
        detail = str(exc).strip() or "Проверьте введённое значение."
        self.client.send_message(chat_id, f"Не удалось выполнить действие: {escape(detail[:700])}")

    def prompt(self, chat_id: int, user_id: int, session: dict[str, Any], text: str) -> None:
        self.sessions[user_id] = session
        self.client.send_message(chat_id, text, reply_markup=cancel_keyboard())

    def show_menu(self, chat_id: int, message: dict[str, Any] | None = None) -> None:
        stats = self.db.stats()
        pending = self.db.count_orders(status='pending_details')
        text = f"<b>⚙️ Управление магазином</b>\nТоваров: {stats['active_products']} · Заказов: {stats['orders_total']}"
        if pending:
            text += f"\n⏳ <b>Ждут отправки реквизитов: {pending}</b>"
        self.edit_or_send_text(chat_id, message, text, self.menu_keyboard(pending))

    def menu_keyboard(self, pending_count: int = 0) -> dict[str, Any]:
        return admin_menu_keyboard(pending_count)

    def show_more(self, chat_id: int, message: dict[str, Any] | None = None) -> None:
        from .keyboards import admin_more_keyboard
        self.edit_or_send_text(chat_id, message, "<b>⚙️ Настройки и статистика</b>\nВыберите раздел:", admin_more_keyboard())

    def show_categories(self, chat_id: int, message: dict[str, Any] | None = None) -> None:
        self.edit_or_send_text(
            chat_id,
            message,
            "Категории скрыты: магазин работает как единый каталог БАДов.",
            admin_menu_keyboard(),
        )

    def show_product_categories(self, chat_id: int, message: dict[str, Any] | None = None) -> None:
        self.show_products(chat_id, message=message)

    def show_products(
        self,
        chat_id: int,
        category_id: int | None = None,
        message: dict[str, Any] | None = None,
        page: int = 0,
    ) -> None:
        if category_id is None:
            category_id = self.product_views.get(chat_id, {}).get("category_id")
        products = self.db.list_products(category_id=category_id, active_only=False)
        total = len(products)
        page = min(max(0, page), max(0, (total - 1) // PAGE_SIZE))
        self.product_views[chat_id] = {"category_id": category_id, "page": page}
        self.edit_or_send_text(
            chat_id, message, f"<b>Товары БАДов</b>\nВсего: {total}. Страница {page + 1}. Цены в рублях.",
            admin_products_keyboard(products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE],
                                    category_id, page=page, total=total),
        )

    def start_product_add(self, chat_id: int, user_id: int) -> None:
        self.sessions[user_id] = {
            "action": "product_add",
            "step": "title",
            "category_id": self.db.get_default_category_id(),
            "data": {},
        }
        self.client.send_message(chat_id, "Введите название товара.", reply_markup=cancel_keyboard())

    def show_product_card(self, chat_id: int, product_id: int, message: dict[str, Any] | None = None) -> None:
        product = self.db.get_product(product_id)
        if not product:
            self.edit_or_send_text(chat_id, message, "Товар не найден.", admin_menu_keyboard())
            return
        markup = admin_product_card_keyboard(product_id, int(product["is_active"]))
        if not any(b.get("callback_data") == f"a:prod:edit:{product_id}:title"
                   for row in markup["inline_keyboard"] for b in row):
            markup["inline_keyboard"].insert(0, [button("Название", f"a:prod:edit:{product_id}:title")])
        for row in markup["inline_keyboard"]:
            for item in row:
                if item.get("callback_data") == f"a:prod:delete:{product_id}":
                    item["text"] = "В архив"
                elif item.get("callback_data") == "a:products":
                    item["callback_data"] = f"a:products:{self.product_views.get(chat_id, {}).get('page', 0)}"
        self.edit_or_send_text(
            chat_id,
            message,
            admin_product_text(product),
            markup,
        )

    def set_order_view(self, chat_id: int, *, status: str | None = None,
                       page: int | None = None, search: str | None = None) -> dict[str, Any]:
        view = self.order_views.setdefault(chat_id, {"status": "all", "page": 0, "search": ""})
        if status is not None:
            if status not in {"all", "paid", "unpaid", *ORDER_STATUSES}:
                raise ValueError("Неизвестный фильтр заказов.")
            view["status"] = status
        if page is not None:
            view["page"] = max(0, page)
        if search is not None:
            view["search"] = bounded_text(search, "Поиск", 100)
        return view

    def show_orders(self, chat_id: int, message: dict[str, Any] | None = None, *,
                    page: int | None = None, status: str | None = None, search: str | None = None) -> None:
        view = self.set_order_view(chat_id, status=status, page=page, search=search)
        db_status = None if view["status"] == "all" else view["status"]
        total = self.db.count_orders(status=db_status, search=view["search"])
        view["page"] = min(view["page"], max(0, (total - 1) // PAGE_SIZE))
        orders = self.db.list_orders(limit=PAGE_SIZE, offset=view["page"] * PAGE_SIZE,
                                     status=db_status, search=view["search"])
        labels = {**ORDER_STATUSES, "all": "Все", "paid": "Оплаченные", "unpaid": "Неоплаченные"}
        text = f"<b>Заказы</b>\nФильтр: {escape(labels[view['status']])}. Найдено: {total}."
        if view["search"]:
            text += f"\nПоиск: {escape(view['search'])}"
        if not orders:
            text += "\nЗаказов по этому запросу нет."
        else:
            text += f"\nСтраница {view['page'] + 1} из {max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)}."
        markup = admin_orders_keyboard(orders, page=view["page"], total=total,
                                       status=view["status"], search=view["search"])
        if view["search"]:
            markup["inline_keyboard"].append([button("Сбросить поиск и фильтр", "a:orders")])
        self.edit_or_send_text(chat_id, message, text, markup)

    def show_order(self, chat_id: int, order_id: int, message: dict[str, Any] | None = None) -> None:
        order = self.db.get_order(order_id)
        if not order:
            self.edit_or_send_text(chat_id, message, "Заказ не найден.", admin_menu_keyboard())
            return
        items = self.db.list_order_items(order_id)
        view = self.set_order_view(chat_id)
        self.edit_or_send_text(chat_id, message, order_detail_text(order, items),
                              admin_order_keyboard(order, page=view["page"], status=view["status"]))
        metadata = dict(order)
        if metadata.get("admin_note"):
            self.client.send_message(chat_id, f"<b>Внутренняя заметка:</b>\n{escape(metadata['admin_note'])}")

    def show_payment_settings(self, chat_id: int, message: dict[str, Any] | None = None) -> None:
        values = self.db.get_settings()
        shared = values.get('card_shared_details_enabled') == '1'
        lines = ['<b>💳 Оплата</b>',
                 'Менеджер: ' + escape(values.get('manager_username') or 'не указан'),
                 'Карта: ' + ('одна для всех стран' if shared else 'отдельная по странам')]
        rows = []
        for method, label in [('card', '💳 Карта'), ('crypto', '🪙 Криптовалюта'), ('stars', '⭐ Stars')]:
            state = '✅' if values.get(f'payment_{method}_enabled') == '1' else '⬜'
            rows.append([button(f'{state} {label}', f'a:pay:toggle:{method}')])
        rows.append([button('👤 Менеджер', 'a:setting:manager_username')])
        next_mode = '0' if shared else '1'
        rows.append([button('🌍 Одна карта для всех: ' + ('да' if shared else 'нет'), f'a:set:card_shared_details_enabled:{next_mode}')])
        if shared:
            lines.extend(['Валюта карты: ' + escape(values.get('card_payment_currency', 'RUB')),
                          '\n' + escape(values.get('card_payment_details') or 'Реквизиты карты не заполнены')])
            rows.append([button('💳 Реквизиты карты', 'a:setting:card_payment_details'), button('💱 Валюта карты', 'a:setting:card_payment_currency')])
        rows.extend([[button('🪙 Криптокошельки', 'a:crypto')], [button('🌍 Страны и курсы', 'a:countries')]])
        if self.db.stats()['payment_issues']:
            rows.append([button('⚠️ Платежи для проверки', 'a:issues:0')])
        if self.db.count_orders(status='payment_check'):
            rows.append([button('🔎 Проверить Stars', 'a:orders:payment_check:0')])
        rows.append([button('⬅️ Меню', 'a:menu')])
        self.edit_or_send_text(chat_id, message, '\n'.join(lines), keyboard(rows))

    def toggle_payment_method(
        self,
        chat_id: int,
        method: str,
        message: dict[str, Any] | None = None,
    ) -> None:
        payment_settings = self.db.get_payment_settings()
        if method not in {"stars", "card", "crypto"}:
            self.client.send_message(chat_id, "Неизвестный способ оплаты.", reply_markup=admin_menu_keyboard())
            return
        self.db.set_setting(f"payment_{method}_enabled", "0" if payment_settings[f"{method}_enabled"] else "1")
        self.show_payment_settings(chat_id, message)

    def require_manual_order(self, order_id: int) -> dict[str, Any]:
        row = self.db.get_order(order_id)
        if not row:
            raise ValueError("Заказ не найден.")
        order = dict(row)
        if order["payment_method"] not in MANUAL_METHODS:
            raise ValueError("Ручное подтверждение доступно только для карты и криптовалюты.")
        if order["status"] in {"canceled", "expired"}:
            raise ValueError("Заказ отменён или срок оплаты истёк.")
        if order["is_paid"]:
            raise ValueError("Заказ уже оплачен.")
        if not order.get("payable_currency") or not order.get("payable_minor"):
            raise ValueError("У старого заказа нет подтверждённой суммы и валюты. Отмените его и попросите покупателя оформить новый заказ.")
        if order.get("expires_at") and order["status"] != "receipt_review":
            expires = datetime.fromisoformat(order["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise ValueError("Срок оплаты заказа истёк.")
        return order

    def confirm_payment_prompt(self, chat_id: int, user_id: int, order_id: int,
                               message: dict[str, Any] | None = None) -> None:
        order = self.require_manual_order(order_id)
        self.sessions[user_id] = {"action": "confirm_paid", "order_id": order_id}
        amount = money(order["payable_minor"], order["payable_currency"])
        self.edit_or_send_text(chat_id, message,
                              f"<b>Подтвердить оплату заказа #{order_id}?</b>\n"
                              f"Проверьте фактическое поступление {escape(amount)}. Чек сам по себе не подтверждает оплату.",
                              keyboard([[button("Да, оплата получена", f"a:confirm_paid:{order_id}")],
                                        [button("Назад к заказу", f"a:order:{order_id}")]]))

    def mark_order_paid(
        self,
        chat_id: int,
        order_id: int,
        message: dict[str, Any] | None = None,
    ) -> None:
        order = self.require_manual_order(order_id)
        self.db.mark_order_paid_manual(order_id)
        message_id = int(order.get('payment_details_message_id') or 0)
        if message_id:
            try:
                self.buyer_client.delete_message(int(order['user_id']), message_id)
                self.db.clear_payment_details_message(order_id, message_id)
            except TelegramApiError as exc:
                print(f'[admin] could not delete payment details for order {order_id}: {exc}')
        self.show_order(chat_id, order_id, message)

    def send_manual_payment_details(
        self,
        chat_id: int,
        order_id: int,
        message: dict[str, Any] | None = None,
    ) -> None:
        order = self.require_manual_order(order_id)
        if order['payment_details_sent']:
            raise ValueError('Реквизиты уже отправлены покупателю.')
        sent = self.buyer_client.send_message(int(order['user_id']), manual_payment_text(order))
        message_id = int(sent.get('message_id', 0)) if isinstance(sent, dict) else 0
        if not message_id:
            raise ValueError('Telegram не вернул идентификатор сообщения с реквизитами.')
        self.db.mark_manual_details_sent(order_id, message_id)
        self.show_order(chat_id, order_id, message)

    def update_order_status(
        self,
        chat_id: int,
        order_id: int,
        status: str,
        message: dict[str, Any] | None = None,
    ) -> None:
        if status == "done":
            self.db.complete_and_delete_order(order_id)
            sent = self.client.send_message(
                chat_id,
                f"✅ Заказ #{order_id} доставлен и удалён из базы. Выручка сохранена в статистике.",
            )
            if isinstance(sent, dict) and sent.get("message_id"):
                self.schedule_message_deletion(chat_id, int(sent["message_id"]), delay_seconds=600)
            self.show_orders(chat_id, message)
            return
        if status not in {"processing", "delivery", "canceled"}:
            raise ValueError("Для подтверждения оплаты используйте кнопку «Оплата получена».")
        before = self.db.get_order(order_id)
        if not before:
            raise ValueError("Заказ не найден.")
        if status != "canceled" and not before["is_paid"]:
            raise ValueError("Сначала подтвердите оплату заказа.")
        self.db.update_order_status(order_id, status)
        if status == "canceled":
            sent = self.client.send_message(chat_id, f"✖️ Заказ #{order_id} отменён и удалён.")
            if isinstance(sent, dict) and sent.get("message_id"):
                self.schedule_message_deletion(chat_id, int(sent["message_id"]), delay_seconds=600)
            self.show_orders(chat_id, message)
        else:
            self.show_order(chat_id, order_id, message)

    def show_payment_issues(self, chat_id: int, message: dict[str, Any] | None = None, page: int = 0) -> None:
        with self.db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM payment_events WHERE resolved=0").fetchone()[0]
            page = min(max(0, page), max(0, (total - 1) // PAGE_SIZE))
            events = conn.execute("SELECT rowid AS event_id, * FROM payment_events WHERE resolved=0 "
                                  "ORDER BY rowid DESC LIMIT ? OFFSET ?", (PAGE_SIZE, page * PAGE_SIZE)).fetchall()
        rows = []
        for event in events:
            amount = f"{event['amount']} Stars" if event["currency"] == "XTR" else money(event["amount"], event["currency"])
            rows.append([button(f"#{event['event_id']} · {event['user_id']} · {amount}", f"a:issue:{event['event_id']}")])
        nav = []
        if page:
            nav.append(button("Назад", f"a:issues:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(button("Далее", f"a:issues:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("Меню", "a:menu")])
        self.edit_or_send_text(chat_id, message, f"<b>Платежи для ручного разбора</b>\nНерешённых: {total}. Страница {page + 1}.", keyboard(rows))

    def payment_issue(self, event_id: int):
        with self.db.connect() as conn:
            event = conn.execute("SELECT rowid AS event_id, * FROM payment_events WHERE rowid=? AND resolved=0", (event_id,)).fetchone()
        if not event:
            raise ValueError("Платёж не найден или вопрос уже решён.")
        return event

    def show_payment_issue(self, chat_id: int, event_id: int, message: dict[str, Any] | None = None) -> None:
        event = self.payment_issue(event_id)
        amount = f"{event['amount']} Stars" if event["currency"] == "XTR" else money(event["amount"], event["currency"])
        text = (f"<b>Платёжная проблема #{event_id}</b>\n"
                f"Покупатель: <code>{event['user_id']}</code>\nСумма: {escape(amount)}\n"
                f"Дата: {escape(event['created_at'])} UTC\n"
                f"Идентификатор платежа: <code>{escape(event['charge_id'][:600])}</code>\n"
                f"Данные счёта: <code>{escape(event['payload'][:600])}</code>\n"
                "Проверьте платёж и свяжитесь с покупателем для ручного урегулирования.")
        rows = [[button("Вопрос решён вручную", f"a:issue_resolve:{event_id}")]]
        if event["order_id"] and self.db.get_order(event["order_id"]):
            rows.append([button("Связанный заказ", f"a:order:{event['order_id']}")])
        rows.append([button("Платёжные проблемы", "a:issues:0")])
        self.edit_or_send_text(chat_id, message, text, keyboard(rows))

    def show_settings(self, chat_id: int, message: dict[str, Any] | None = None, group: str | None = None) -> None:
        if group in {'payment', 'payments'}:
            self.show_payment_settings(chat_id, message)
            return
        if group is None:
            rows = [[button(label, f"a:settings:{key}")] for key, label in SETTING_GROUPS.items() if key != 'payments']
            rows.append([button("⬅️ Меню", "a:menu")])
            self.edit_or_send_text(chat_id, message, "<b>Настройки магазина</b>", keyboard(rows))
            return
        if group not in SETTING_GROUPS:
            raise ValueError("Неизвестный раздел настроек.")
        values = self.db.get_settings()
        lines = [f"<b>{SETTING_GROUPS[group]}</b>"]
        rows = []
        for key, spec in SETTINGS.items():
            if spec.group != group:
                continue
            value = values.get(key, spec.default)
            if spec.kind == "bool":
                display = "Включено" if value == "1" else "Выключено"
            else:
                display = value[:85].replace("\n", " ") + ("…" if len(value) > 85 else "")
            lines.append(f"{spec.label}: {escape(display or 'Не задано')}")
            rows.append([button(spec.label, f"a:setting:{key}")])
        rows.append([button("Все настройки", "a:settings")])
        self.edit_or_send_text(chat_id, message, "\n".join(lines), keyboard(rows))

    def start_setting_edit(self, chat_id: int, user_id: int, key: str,
                           message: dict[str, Any] | None = None) -> None:
        spec = SETTINGS.get(key)
        if spec is None:
            raise ValueError("Эта настройка недоступна для изменения.")
        current = self.db.get_setting(key, spec.default)
        text = f"<b>{spec.label}</b>\nТекущее значение:\n{escape(current or 'Не задано')}"
        if spec.kind in {"bool", "currency", 'payment_currency'}:
            options = [("Включить", "1"), ("Выключить", "0")] if spec.kind == "bool" else [("Рубли", "RUB"), ("Тенге", "KZT")]
            if spec.kind == 'payment_currency':
                options = [(currency, currency) for currency in PAYMENT_CURRENCIES]
            rows = [[button(label, f"a:set:{key}:{value}") for label, value in options[index:index + 2]] for index in range(0, len(options), 2)]
            rows.append([button("Назад", 'a:payments' if spec.group == 'payments' else f"a:settings:{spec.group}")])
            if key == 'card_shared_details_enabled':
                text += '\nВключено: одна карта и валюта перевода для всех стран. Выключено: отдельные реквизиты каждой страны. Криптокошельки общие в обоих режимах.'
            if key == 'card_payment_currency':
                text += '\nВыберите валюту, которую принимает общая карта. Курс к RUB задаётся в разделе соответствующей страны.'
            self.edit_or_send_text(chat_id, message, text, keyboard(rows))
            return
        hint = "\nВведите новое значение."
        if spec.kind in {"decimal", "integer"}:
            hint += f" От {spec.minimum} до {spec.maximum}, знаков после запятой: {spec.places}."
        else:
            hint += f" До {spec.max_length} символов."
            if key != "shop_title":
                hint += " «-» очищает значение."
        self.prompt(chat_id, user_id, {"action": "setting_edit", "key": key}, text + hint)

    def show_countries(self, chat_id: int, message: dict[str, Any] | None = None) -> None:
        rows = []
        for country in self.db.list_countries(enabled_only=False):
            state = "Вкл" if country["enabled"] else "Выкл"
            rows.append([button(f"{country['title']} · {country['currency']} · {state}", f"a:country:{country['code']}")])
        rows.append([button("⬅️ Оплата", "a:payments")])
        self.edit_or_send_text(chat_id, message, "<b>Страны и реквизиты</b>\nКурс: единиц местной валюты за 1 рубль.", keyboard(rows))

    def show_country(self, chat_id: int, code: str, message: dict[str, Any] | None = None) -> None:
        country = self.db.get_country(code)
        if not country:
            raise ValueError("Страна не найдена.")
        text = (f"<b>{escape(country['title'])}</b>\n"
                f"Доступна: {'Да' if country['enabled'] else 'Нет'}\n"
                f"Курс: 1 RUB = {escape(str(country['rate_per_rub']))} {escape(country['currency'])}")
        if self.db.get_setting('card_shared_details_enabled') != '1':
            text += f"\nВалюта перевода: {escape(country['card_currency'])}\nРеквизиты:\n{escape(country['card_details'] or 'Не заданы')}"
        rows = [[button("Выключить" if country["enabled"] else "Включить", f"a:country:{code}:toggle")]]
        rows.extend([[button(label, f"a:country:{code}:edit:{field}")] for field, label in COUNTRY_FIELDS.items()
                     if field == 'rate_per_rub' or self.db.get_setting('card_shared_details_enabled') != '1'])
        rows.append([button("Страны", "a:countries")])
        self.edit_or_send_text(chat_id, message, text, keyboard(rows))

    def country_callback(self, chat_id: int, user_id: int, parts: list[str], message: dict[str, Any]) -> None:
        code = parts[2]
        country = self.db.get_country(code)
        if not country:
            raise ValueError("Страна не найдена.")
        if len(parts) == 3:
            self.show_country(chat_id, code, message)
        elif len(parts) == 4 and parts[3] == "toggle":
            self.db.update_country(code, enabled=not bool(country["enabled"]))
            self.show_country(chat_id, code, message)
        elif len(parts) == 5 and parts[3] == "edit" and parts[4] in COUNTRY_FIELDS:
            field = parts[4]
            if field == "card_currency":
                rows = [[button(currency, f"a:country:{code}:currency:{currency}")
                         for currency in dict.fromkeys(["RUB", country["currency"]])],
                        [button("Назад", f"a:country:{code}")]]
                self.edit_or_send_text(chat_id, message, "<b>Валюта перевода на карту</b>", keyboard(rows))
                return
            hint = "До 1500 символов. «-» очищает реквизиты." if field == "card_details" else "0 означает, что курс не настроен. Для RUB курс равен 1."
            self.prompt(chat_id, user_id, {"action": "country_edit", "code": code, "field": field},
                        f"{COUNTRY_FIELDS[field]}. {hint}")
        elif len(parts) == 5 and parts[3] == "currency":
            if parts[4] not in {"RUB", country["currency"]}:
                raise ValueError("Выберите рубли или валюту этой страны.")
            self.db.update_country(code, card_currency=parts[4])
            self.show_country(chat_id, code, message)
        else:
            raise ValueError("Неизвестное действие для страны.")

    def show_crypto_options(self, chat_id: int, message: dict[str, Any] | None = None, page: int = 0) -> None:
        options = self.db.list_crypto_options(active_only=False)
        page = min(max(0, page), max(0, (len(options) - 1) // PAGE_SIZE))
        rows = [[button("Добавить кошелёк", "a:crypto:add")]]
        for option in options[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            state = "Вкл" if option["enabled"] else "Выкл"
            rows.append([button(f"#{option['id']} · {option['token']} · {option['network']} · {state}", f"a:crypto:{option['id']}")])
        nav = []
        if page:
            nav.append(button("Назад", f"a:crypto:page:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(options):
            nav.append(button("Далее", f"a:crypto:page:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("⬅️ Оплата", "a:payments")])
        self.edit_or_send_text(chat_id, message, f"<b>Криптокошельки</b>\nВсего: {len(options)}. Страница {page + 1}.", keyboard(rows))

    def show_crypto_option(self, chat_id: int, option_id: int, message: dict[str, Any] | None = None) -> None:
        option = self.db.get_crypto_option(option_id)
        if not option:
            raise ValueError("Кошелёк не найден.")
        text = (f"<b>{escape(option['token'])} · {escape(option['network'])}</b>\n"
                f"Доступен: {'Да' if option['enabled'] else 'Нет'}\n"
                f"Рублей за единицу: {escape(str(option['rub_per_unit']))}\n"
                f"Адрес: <code>{escape(option['address'] or 'Не задан')}</code>")
        rows = [[button(label, f"a:crypto:{option_id}:edit:{field}")] for field, label in CRYPTO_FIELDS.items()]
        rows.append([button("Выключить" if option["enabled"] else "Включить", f"a:crypto:{option_id}:toggle")])
        rows.append([button("Кошельки", "a:crypto")])
        self.edit_or_send_text(chat_id, message, text, keyboard(rows))

    def save_crypto_option(self, option_id: int | None, values: dict[str, Any]) -> int:
        token = validate_crypto_field("token", values["token"])
        network = validate_crypto_field("network", values["network"])
        address = values["address"]
        if address or values["enabled"]:
            address = validate_crypto_field("address", address, network=network)
        rate = decimal_value(str(values["rub_per_unit"]), minimum="0.00000001" if values["enabled"] else "0")
        return self.db.save_crypto_option(option_id=option_id, token=token, network=network,
                                         address=address, rub_per_unit=rate, enabled=bool(values["enabled"]))

    def crypto_callback(self, chat_id: int, user_id: int, parts: list[str], message: dict[str, Any]) -> None:
        option_id = int(parts[2])
        row = self.db.get_crypto_option(option_id)
        if not row:
            raise ValueError("Кошелёк не найден.")
        option = dict(row)
        if len(parts) == 3:
            self.show_crypto_option(chat_id, option_id, message)
        elif len(parts) == 4 and parts[3] == "toggle":
            option["enabled"] = not bool(option["enabled"])
            self.save_crypto_option(option_id, option)
            self.show_crypto_option(chat_id, option_id, message)
        elif len(parts) == 5 and parts[3] == "edit" and parts[4] in CRYPTO_FIELDS:
            field = parts[4]
            self.prompt(chat_id, user_id, {"action": "crypto_edit", "option_id": option_id, "field": field},
                        f"Введите новое значение: {CRYPTO_FIELDS[field]}.\n"
                        "Для смены сети сначала отключите кошелёк. Изменение сети сбрасывает адрес.")
        else:
            raise ValueError("Неизвестное действие для кошелька.")

    def price_prompt(self) -> str:
        currency = self.db.get_setting("price_input_currency", "RUB")
        return f"💰 Введите целую цену в {'тенге (KZT)' if currency == 'KZT' else 'рублях (RUB)'}, без копеек. Цена Stars задаётся отдельно."

    def product_price(self, text: str) -> int:
        value = decimal_value(text, minimum="1", maximum="10000000", places=0)
        price = self.db.price_from_input(value)
        if not 1 <= price <= 1_000_000_000:
            raise ValueError("Цена в рублях должна быть от 1 до 10 000 000.")
        return price

    def start_product_edit(self, chat_id: int, user_id: int, product_id: int, field: str) -> None:
        prompts = {
            "title": "Введите новое название товара (2–80 символов).",
            "price": self.price_prompt(),
            "stars": "⭐ Введите отдельную цену товара в Stars: целое число от 1 до 10 000 000.",
            "desc": "Введите новое описание товара.",
            "chars": "Введите новые характеристики товара.",
            "media": "Отправьте новое фото/видео товара или напишите «-», чтобы убрать медиа.",
        }
        if field not in prompts:
            self.client.send_message(chat_id, "Неизвестное поле.", reply_markup=admin_menu_keyboard())
            return
        if not self.db.get_product(product_id):
            raise ValueError("Товар не найден.")
        self.sessions[user_id] = {"action": "product_edit", "product_id": product_id, "field": field}
        self.client.send_message(chat_id, prompts[field], reply_markup=cancel_keyboard())

    def process_session_message(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        user_id = int(message.get("from", {}).get("id", chat_id))
        session = self.sessions[user_id]
        action = session["action"]
        if not message.get('text') and not (action in {'product_add', 'product_edit'} and (session.get('step') == 'media' or session.get('field') == 'media')):
            self.client.send_message(chat_id, 'Отправьте текстовое значение. Для очистки используйте «-», для отмены /cancel.')
            return
        text = (message.get("text") or "").strip()

        if action == "confirm_paid":
            self.client.send_message(chat_id, "Подтвердите оплату кнопкой в заказе или отмените действие: /cancel.")
            return

        if action == "resolve_issue":
            self.client.send_message(chat_id, "Подтвердите решение кнопкой в обращении или отмените действие: /cancel.")
            return

        if action == "order_search":
            search = bounded_text(text, "Поиск", 100, 1)
            self.show_orders(chat_id, page=0, search="" if search == "-" else search)
            self.sessions.pop(user_id, None)
            return

        if action == "setting_edit":
            key = session["key"]
            value = validate_setting(key, "" if text == "-" else text)
            self.db.set_setting(key, value)
            self.sessions.pop(user_id, None)
            self.show_settings(chat_id, group=SETTINGS[key].group)
            return

        if action == "country_edit":
            field = session["field"]
            country = self.db.get_country(session["code"])
            if not country:
                raise ValueError("Страна не найдена.")
            if field == "card_details":
                value = bounded_text("" if text == "-" else text, "Реквизиты", 1500)
            elif field == "rate_per_rub":
                value = decimal_value(text, maximum="1000000")
                if country["currency"] == "RUB" and value != "1":
                    raise ValueError("Курс рубля к рублю всегда равен 1.")
            else:
                raise ValueError("Неизвестное поле страны.")
            self.db.update_country(session["code"], **{field: value})
            self.sessions.pop(user_id, None)
            self.show_country(chat_id, session["code"])
            return

        if action == "crypto_edit":
            row = self.db.get_crypto_option(session["option_id"])
            if not row:
                raise ValueError("Кошелёк не найден.")
            option = dict(row)
            field = session["field"]
            value = validate_crypto_field(field, text, network=option["network"])
            if field == "network" and value != option["network"]:
                if option["enabled"]:
                    raise ValueError("Перед сменой сети отключите кошелёк.")
                option["address"] = ""
            option[field] = value
            self.save_crypto_option(session["option_id"], option)
            self.sessions.pop(user_id, None)
            self.show_crypto_option(chat_id, session["option_id"])
            return

        if action == "crypto_add":
            step = session["step"]
            data = session["data"]
            data[step] = validate_crypto_field(step, text, network=data.get("network", ""))
            steps = list(CRYPTO_FIELDS)
            if step != steps[-1]:
                session["step"] = steps[steps.index(step) + 1]
                self.client.send_message(chat_id, f"Введите: {CRYPTO_FIELDS[session['step']]}.", reply_markup=cancel_keyboard())
                return
            option_id = self.save_crypto_option(None, {**data, "enabled": False})
            self.sessions.pop(user_id, None)
            self.show_crypto_option(chat_id, option_id)
            return

        if action == "order_metadata":
            field = session["field"]
            if field not in {"admin_note", "tracking_number"}:
                raise ValueError("Неизвестное поле заказа.")
            value = bounded_text("" if text == "-" else text, "Заметка" if field == "admin_note" else "Трек-номер",
                                 800 if field == "admin_note" else 120)
            before = self.db.get_order(session["order_id"])
            if not before:
                raise ValueError("Заказ не найден.")
            self.db.update_order_metadata(session["order_id"], **{field: value})
            self.sessions.pop(user_id, None)
            self.show_order(chat_id, session["order_id"])
            return

        if action == "category_add":
            title = bounded_text(text, "Название категории", 80, 2)
            self.db.create_category(title)
            self.sessions.pop(user_id, None)
            self.client.send_message(chat_id, "Категория добавлена.", reply_markup=admin_menu_keyboard())
            return

        if action == "payment_details":
            details = bounded_text("" if text == "-" else text, "Реквизиты", 1500)
            self.db.set_setting("card_payment_details", details)
            self.sessions.pop(user_id, None)
            self.client.send_message(chat_id, "Реквизиты сохранены.")
            self.show_payment_settings(chat_id)
            return

        if action == "product_add":
            self.process_product_add(message, session)
            return

        if action == "product_edit":
            self.process_product_edit(message, session)

    def process_product_add(self, message: dict[str, Any], session: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        user_id = int(message.get("from", {}).get("id", chat_id))
        text = (message.get("text") or "").strip()
        step = session["step"]
        data = session["data"]

        if step == "title":
            data["title"] = bounded_text(text, "Название товара", 80, 2)
            session["step"] = "description"
            self.client.send_message(chat_id, "Введите описание товара.")
            return

        if step == "description":
            data["description"] = bounded_text(text, "Описание", 1800, 5)
            session["step"] = "price"
            self.client.send_message(chat_id, self.price_prompt())
            return

        if step == "price":
            data["price_rub_minor"] = self.product_price(text)
            session['step'] = 'stars'
            self.client.send_message(chat_id, '⭐ Введите цену товара в Stars целым числом. Она не зависит от цены в рублях.')
            return

        if step == 'stars':
            data['price_stars'] = int(decimal_value(text, minimum='1', maximum='10000000', places=0))
            session["step"] = "characteristics"
            self.client.send_message(chat_id, "Введите характеристики товара.")
            return

        if step == "characteristics":
            data["characteristics"] = bounded_text("" if text == "-" else text, "Характеристики", 800)
            session["step"] = "media"
            self.client.send_message(chat_id, "Отправьте фото/видео товара или напишите «-», чтобы пропустить.")
            return

        if step == "media":
            media_type, media_file_id, media_path = self.extract_media(message)
            if not media_type and text != "-":
                self.client.send_message(chat_id, "Отправьте фото/видео или «-», чтобы пропустить.")
                return
            product_id = self.db.create_product(
                category_id=int(session["category_id"]),
                title=data["title"],
                description=data["description"],
                characteristics=data["characteristics"],
                price_rub_minor=int(data["price_rub_minor"]),
                price_stars=int(data['price_stars']),
                media_type=media_type or "none",
                media_file_id=media_file_id or "",
                media_path=media_path or "",
            )
            self.sessions.pop(user_id, None)
            self.client.send_message(chat_id, "Товар добавлен.")
            self.show_product_card(chat_id, product_id)

    def process_product_edit(self, message: dict[str, Any], session: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        user_id = int(message.get("from", {}).get("id", chat_id))
        text = (message.get("text") or "").strip()
        product_id = int(session["product_id"])
        field = session["field"]

        if not self.db.get_product(product_id):
            raise ValueError("Товар не найден.")
        if field == "price":
            self.db.update_product(product_id, price_rub_minor=self.product_price(text))
        elif field == 'stars':
            self.db.update_product(product_id, price_stars=int(decimal_value(text, minimum='1', maximum='10000000', places=0)))
        elif field == "title":
            self.db.update_product(product_id, title=bounded_text(text, "Название товара", 80, 2))
        elif field == "desc":
            self.db.update_product(product_id, description=bounded_text(text, "Описание", 1800, 5))
        elif field == "chars":
            self.db.update_product(product_id, characteristics=bounded_text("" if text == "-" else text, "Характеристики", 800))
        elif field == "media":
            media_type, media_file_id, media_path = self.extract_media(message)
            if text == "-":
                self.db.update_product(product_id, media_type="none", media_file_id="", media_path="", buyer_media_file_id="")
            elif media_type:
                self.db.update_product(product_id, media_type=media_type, media_file_id=media_file_id,
                                       media_path=media_path or "", buyer_media_file_id="")
            else:
                self.client.send_message(chat_id, "Отправьте фото/видео или «-», чтобы убрать медиа.")
                return
        else:
            raise ValueError("Неизвестное поле товара.")

        self.sessions.pop(user_id, None)
        self.client.send_message(chat_id, "Товар обновлён.")
        self.show_product_card(chat_id, product_id)

    def extract_media(self, message: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        if "video" in message:
            file_id = message["video"]["file_id"]
            return "video", file_id, self.download_product_media(file_id)
        if "photo" in message and message["photo"]:
            photo = sorted(
                message["photo"],
                key=lambda item: item.get("file_size", item.get("width", 0) * item.get("height", 0)),
            )[-1]
            file_id = photo["file_id"]
            return "photo", file_id, self.download_product_media(file_id)
        return None, None, None

    def download_product_media(self, file_id: str) -> str:
        file_info = self.client.get_file(file_id)
        telegram_path = file_info["file_path"]
        suffix = Path(telegram_path).suffix or ".bin"
        local_path = self.media_dir / f"{uuid4().hex}{suffix}"
        self.client.download_file(telegram_path, local_path)
        return str(local_path)

    def edit_or_send_text(
        self,
        chat_id: int,
        message: dict[str, Any] | None,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        parts = split_html(text)
        if len(parts) > 1:
            for index, part in enumerate(parts):
                self.edit_or_send_text(chat_id, message if index == 0 else None, part, reply_markup if index == len(parts) - 1 else None)
            return
        message_id = message.get("message_id") if message else None
        if message_id and "text" in message:
            try:
                self.client.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=text,
                    reply_markup=reply_markup,
                )
                return
            except TelegramApiError as exc:
                if "message is not modified" in exc.description.lower():
                    return
        self.client.send_message(chat_id, text, reply_markup=reply_markup)

    def schedule_message_deletion(self, chat_id: int, message_id: int, delay_seconds: int = 600) -> None:
        self.db.schedule_message_deletion(chat_id, message_id, delay_seconds)
        timer = threading.Timer(delay_seconds, self.safe_delete, args=[chat_id, message_id])
        timer.daemon = True
        timer.start()

    def safe_delete(self, chat_id: int, message_id: int) -> None:
        try:
            self.client.delete_message(chat_id, message_id)
        except Exception:
            pass

    def maintenance(self) -> None:
        for item in self.db.pending_message_deletions():
            self.safe_delete(int(item["chat_id"]), int(item["message_id"]))
            self.db.acknowledge_message_deletion(int(item["id"]))
