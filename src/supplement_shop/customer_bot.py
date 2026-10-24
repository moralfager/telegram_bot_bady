from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Settings
from .database import Database, utc_now
from .formatting import (
    cart_text, h, manual_payment_text, order_detail_text, product_caption, split_html, send_html,
)
from .keyboards import (
    back_home_keyboard, button, cart_keyboard, catalog_keyboard,
    checkout_cancel_keyboard, country_keyboard, customer_order_keyboard,
    customer_orders_keyboard, invoice_keyboard, keyboard, persistent_menu, product_keyboard, MENU_LABELS,
)
from .pricing import money, quote
from .telegram_api import BotClient, TelegramApiError

DELIVERY_PROMPT = 'Укажите ваш город и точный адрес или ссылку на выбранный ПВЗ Wildberries.'
PHONE_OR_TAG_RE = re.compile(r'(@[A-Za-z][A-Za-z0-9_]{4,31}|\+?[0-9]{7,15})\Z')

class CustomerBot:
    def __init__(self, *, client: BotClient, admin_client: BotClient, db: Database, settings: Settings):
        self.client = client
        self.admin_client = admin_client
        self.db = db
        self.settings = settings
        self.media_dir = settings.db_path.parent / "media"
        self.checkout_sessions: dict[int, dict[str, Any]] = {}
        self.notification_lock = threading.Lock()

    def handle_update(self, update: dict[str, Any]) -> None:
        if "pre_checkout_query" in update:
            self.handle_pre_checkout(update["pre_checkout_query"])
        elif "callback_query" in update:
            self.db.expire_unpaid_orders()
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            message = update["message"]
            if "successful_payment" not in message:
                self.db.expire_unpaid_orders()
            self.handle_message(message)

    def handle_message(self, message: dict[str, Any]) -> None:
        if message["chat"].get("type", "private") != "private":
            return
        if "successful_payment" in message:
            self.handle_successful_payment(message)
            return
        chat_id = int(message["chat"]["id"])
        user_id = int(message.get("from", {}).get("id", chat_id))
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0] if text.startswith("/") else text
        command = next((plain for plain, label in MENU_LABELS.items() if command == label), command)
        menu = {
            "/start": "home", "/menu": "home", "Товары": "home",
            "/cart": "cart", "Корзина": "cart",
            "/country": "country", "Страна": "country", "Техническая поддержка": "support",
            "/support": "support", "/paysupport": "support", "Отзывы": "reviews",
            "Как купить?": "how", "/terms": "terms", "Условия покупки": "terms",
        }
        if command in {"/orders", "Мои заказы"}:
            self.client.send_message(
                chat_id,
                "Раздел «Мои заказы» отключен. Информация о заказе отправляется сразу после оформления.",
                reply_markup=persistent_menu(),
            )
            return
        if command == "/cancel":
            self.cancel_checkout_or_order(chat_id, user_id)
            return
        if command in menu:
            self.checkout_sessions.pop(user_id, None)
            action = menu[command]
            if command in {"/start", "/menu"}:
                title = self.db.get_setting("shop_title") or self.settings.shop_title
                self.client.send_message(chat_id, f"<b>{h(title)}</b>\n{h(self.db.get_setting('welcome_text'))}", reply_markup=persistent_menu())
            if action == "country":
                self.show_countries(chat_id)
            elif action in {"support", "reviews", "how", "terms"}:
                self.show_information(chat_id, action)
            elif not self.ensure_country(chat_id, user_id):
                return
            elif action == "home":
                self.show_home(chat_id)
            elif action == "cart":
                self.show_cart(chat_id, user_id)
            return
        if not self.ensure_country(chat_id, user_id):
            return
        if user_id in self.checkout_sessions:
            self.process_checkout_message(message)
            return
        self.client.send_message(chat_id, "Выберите раздел меню.", reply_markup=persistent_menu())

    def ensure_country(self, chat_id: int, user_id: int) -> bool:
        country = self.db.get_user_country(user_id)
        if not country or not country["enabled"]:
            self.checkout_sessions.pop(user_id, None)
            self.show_countries(chat_id)
            return False
        return True

    def show_countries(self, chat_id, message=None):
        countries = self.db.list_countries(enabled_only=True)
        self.edit_or_send_text(chat_id, message, "Выберите страну доставки." if countries else "Приём заказов временно приостановлен.", country_keyboard(countries))

    def show_information(self, chat_id, action):
        settings = self.db.get_settings()
        markup = persistent_menu()
        if action == "support":
            username = settings.get("support_username") or settings.get("manager_username")
            text = f"Техническая поддержка: {h(username)}" if username else "Контакт поддержки пока не указан."
            if username:
                markup = keyboard([[{"text": "Написать менеджеру", "url": f"https://t.me/{username.lstrip('@')}"}]])
        elif action == "reviews":
            text = "Отзывы покупателей." if settings.get("reviews_url") else "Раздел отзывов скоро появится."
            if settings.get("reviews_url"):
                markup = keyboard([[{"text": "Отзывы", "url": settings["reviews_url"]}]])
        elif action == "how":
            text = h(settings.get("how_to_buy", ""))
        else:
            text = h(settings.get("terms_text") or "Условия покупки уточните у менеджера.")
        self.client.send_message(chat_id, text, reply_markup=markup)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        message = callback.get("message") or {}
        if message.get("chat", {}).get("type", "private") != "private":
            self.client.answer_callback_query(callback_id, "Откройте личный чат с ботом.", show_alert=True)
            return
        user_id = int(callback.get("from", {}).get("id", 0))
        chat_id = int(message.get("chat", {}).get("id", user_id))
        data = callback.get("data", "")
        try:
            self.client.answer_callback_query(callback_id)
            if data == "noop":
                return
            if data.startswith("country:"):
                self.db.set_user_country(user_id, data.split(":")[1])
                self.checkout_sessions.pop(user_id, None)
                self.client.send_message(chat_id, "Страна сохранена.", reply_markup=persistent_menu())
                self.show_home(chat_id, message=message)
                return
            historical = data == 'orders' or data.startswith(('orders:', 'order:', 'details:', 'receipt:', 'invoice:', 'order_cancel:'))
            if not historical and not self.ensure_country(chat_id, user_id):
                return
            if data == "home" or data.startswith("catalog:") or data.startswith("cat:"):
                self.checkout_sessions.pop(user_id, None)
                page = int(data.split(":")[1]) if data.startswith("catalog:") else 0
                self.show_home(chat_id, message=message, page=page)
            elif data.startswith("product:"):
                _, product_id, page = data.split(":")
                self.checkout_sessions.pop(user_id, None)
                self.show_product_by_id(chat_id, message, int(product_id), int(page))
            elif data.startswith("p:"):
                self.show_home(chat_id, message=message)
            elif data.startswith(("add:", "buy:")):
                ok, reason = self.db.add_to_cart(user_id, int(data.split(":")[1]))
                if ok and data.startswith("buy:"):
                    self.start_checkout(chat_id, user_id, user_info=callback.get("from"))
                else:
                    self.client.send_message(chat_id, reason, reply_markup=back_home_keyboard())
            elif data == "cart":
                self.checkout_sessions.pop(user_id, None)
                self.show_cart(chat_id, user_id, message=message)
            elif data == 'checkout_back':
                session = self.checkout_sessions.get(user_id)
                if not session:
                    raise ValueError('Оформление уже завершено. Откройте корзину.')
                has_tag = bool(session['data'].get('telegram_tag'))
                steps = ['name', 'address', 'comment', 'payment'] if has_tag else ['name', 'contact', 'address', 'comment', 'payment']
                index = steps.index(session['step']) if session['step'] in steps else 0
                session.pop('offers', None)
                session.pop('nonce', None)
                if index == 0:
                    self.checkout_sessions.pop(user_id, None)
                    self.show_cart(chat_id, user_id, message=message)
                else:
                    previous_step = steps[index - 1]
                    session['step'] = previous_step
                    prompts = {
                        'name': '👤 Введите имя получателя.',
                        'contact': '📱 Укажите ваш @тег или номер телефона для связи:',
                        'address': DELIVERY_PROMPT,
                        'comment': '📝 Комментарий к заказу или «-».',
                    }
                    self.edit_or_send_text(chat_id, message, prompts[previous_step], checkout_cancel_keyboard())
            elif data.startswith("qty:"):
                _, product_id, delta = data.split(":")
                self.checkout_sessions.pop(user_id, None)
                self.update_cart_qty(user_id, int(product_id), int(delta))
                self.show_cart(chat_id, user_id, message=message)
            elif data == "checkout":
                user_info = callback.get("from")
                self.start_checkout(chat_id, user_id, user_info=user_info)
            elif data == "checkout_use_profile_name":
                session = self.checkout_sessions.get(user_id)
                if not session or session.get("step") != "name":
                    raise ValueError("Оформление устарело. Откройте корзину.")
                session["data"]["customer_name"] = session.get("profile_name", "Покупатель")
                has_tag = bool(session["data"].get("telegram_tag"))
                if has_tag:
                    session["step"] = "address"
                    self.edit_or_send_text(chat_id, message, DELIVERY_PROMPT, checkout_cancel_keyboard())
                else:
                    session["step"] = "contact"
                    self.edit_or_send_text(chat_id, message, "Укажите ваш @тег в Telegram или номер телефона для связи:", checkout_cancel_keyboard())
            elif data == "checkout_cancel":
                self.checkout_sessions.pop(user_id, None)
                self.edit_or_send_text(chat_id, message, "Оформление отменено.", back_home_keyboard())
            elif data.startswith("pay:"):
                self.finish_checkout_from_payment_choice(chat_id, user_id, data)
            elif data == "orders" or data.startswith("orders:"):
                self.checkout_sessions.pop(user_id, None)
                self.edit_or_send_text(
                    chat_id,
                    message,
                    "Раздел «Мои заказы» отключен. Информация о заказе отправлена при его оформлении.",
                    back_home_keyboard(),
                )
            elif data.startswith("order:"):
                _, order_id, page = data.split(":")
                self.show_order(chat_id, user_id, int(order_id), int(page), message)
            elif data.startswith(("details:", "receipt:", "invoice:", "order_cancel:")):
                order_id = int(data.split(":")[1])
                order = self.owned_order(user_id, order_id)
                if data.startswith(('details:', 'receipt:')):
                    self.show_order(chat_id, user_id, order_id, 0, message)
                elif data.startswith("invoice:"):
                    self.send_stars_invoice(chat_id, order)
                else:
                    ok, reason = self.db.cancel_order(order_id, user_id)
                    self.client.send_message(chat_id, reason, reply_markup=back_home_keyboard())
                    if ok:
                        self.notify_admins(order_id, "Покупатель отменил заказ")
                        if order["invoice_message_id"]:
                            self.safe_delete(chat_id, order["invoice_message_id"])
            else:
                self.client.send_message(chat_id, "Эта кнопка устарела. Откройте меню.", reply_markup=persistent_menu())
        except ValueError as exc:
            self.client.send_message(chat_id, h(exc), reply_markup=back_home_keyboard())
        except TelegramApiError as exc:
            print(f"[customer] Telegram API error: {exc}")

    def show_home(self, chat_id: int, *, message=None, page=0) -> None:
        products = self.db.list_products(active_only=True)
        page = max(0, min(page, max(0, (len(products) - 1) // 12)))
        country = self.db.get_user_country(chat_id)
        title = self.db.get_setting("shop_title") or self.settings.shop_title
        text = f"<b>{h(title)} · Каталог</b>\n"
        text += "Выберите товар." if products else "Товары скоро появятся."
        if self.db.get_setting("shop_open", "1") != "1":
            text += "\nПриём новых заказов временно приостановлен."
        if country:
            text += f"\nДоставка: {h(country['title'])}"
        self.edit_or_send_text(chat_id, message, text, catalog_keyboard(products[page * 12:(page + 1) * 12], page, len(products), country))

    def show_product_by_id(self, chat_id, message, product_id, page=0):
        product = self.db.get_product(product_id, active_only=True)
        if not product:
            raise ValueError("Товар больше не доступен.")
        caption = product_caption(product, country=self.db.get_user_country(chat_id))
        markup = product_keyboard(product_id, max(0, page))
        if product["media_type"] in {"photo", "video"}:
            if len(caption) > 950:
                self.send_product_media(chat_id, message, product, f"<b>{h(product['title'])}</b>", {}, send_kind=product["media_type"])
                self.client.send_message(chat_id, caption, reply_markup=markup)
            else:
                self.send_product_media(chat_id, message, product, caption, markup, send_kind=product["media_type"])
        else:
            self.edit_or_send_text(chat_id, message, caption, markup)

    def show_cart(self, chat_id, user_id, *, message=None):
        cart = self.db.get_cart(user_id)
        self.edit_or_send_text(chat_id, message, cart_text(cart, self.db.get_user_country(user_id)), cart_keyboard(cart) if cart else back_home_keyboard())

    def show_orders(self, chat_id, user_id, *, message=None, page=0):
        total = self.db.count_orders(user_id=user_id)
        page = max(0, min(page, max(0, (total - 1) // 10)))
        orders = self.db.list_orders(user_id=user_id, limit=10, offset=page * 10)
        self.edit_or_send_text(chat_id, message, f"<b>Мои заказы</b>\nВсего: {total}.", customer_orders_keyboard(orders, page, total))

    def owned_order(self, user_id, order_id):
        order = self.db.get_order(order_id)
        if not order or int(order["user_id"]) != user_id:
            raise ValueError("Заказ не найден или срок его оплаты истёк.")
        return order

    def show_order(self, chat_id, user_id, order_id, page=0, message=None):
        order = self.owned_order(user_id, order_id)
        self.edit_or_send_text(
            chat_id,
            message,
            order_detail_text(order, self.db.list_order_items(order_id)),
            customer_order_keyboard(order, page),
        )

    def update_cart_qty(self, user_id, product_id, delta):
        current = next((item for item in self.db.get_cart(user_id) if int(item["id"]) == product_id), None)
        if current:
            self.db.set_cart_qty(user_id, product_id, 0 if delta == 0 else int(current["qty"]) + delta)

    def start_checkout(self, chat_id, user_id, user_info=None):
        if not self.ensure_country(chat_id, user_id):
            return
        if not self.db.get_cart(user_id):
            self.client.send_message(chat_id, "Корзина пустая.", reply_markup=back_home_keyboard())
            return
        if not self.db.payment_options(user_id):
            self.client.send_message(chat_id, "Для вашей страны оплата пока недоступна. Свяжитесь с поддержкой.", reply_markup=persistent_menu())
            return

        username = (user_info or {}).get("username", "").strip()
        telegram_tag = f"@{username}" if username else ""
        first_name = (user_info or {}).get("first_name", "").strip()
        last_name = (user_info or {}).get("last_name", "").strip()
        full_name = f"{first_name} {last_name}".strip()

        self.checkout_sessions[user_id] = {
            "step": "name",
            "data": {
                "telegram_tag": telegram_tag,
                "phone": "",
                "contact_consent": True,
            },
            "profile_name": full_name,
        }

        if full_name and len(full_name) >= 2:
            markup = keyboard([
                [button(f"✅ Оставить: {full_name[:30]}", "checkout_use_profile_name")],
                [button("Отменить оформление", "checkout_cancel")],
            ])
            self.client.send_message(chat_id, f"Введите имя получателя или оставьте имя из профиля ({h(full_name)}):", reply_markup=markup)
        else:
            self.client.send_message(chat_id, "Введите имя получателя.", reply_markup=checkout_cancel_keyboard())

    def process_checkout_message(self, message):
        chat_id = int(message["chat"]["id"])
        user_id = int(message.get("from", {}).get("id", chat_id))
        text = (message.get("text") or "").strip()
        session = self.checkout_sessions[user_id]
        step = session["step"]
        has_tag = bool(session["data"].get("telegram_tag"))

        if step == "name":
            if not 2 <= len(text) <= 120:
                self.client.send_message(chat_id, "Введите от 2 до 120 символов для имени.")
                return
            session["data"]["customer_name"] = text
            if has_tag:
                session["step"] = "address"
                self.client.send_message(chat_id, DELIVERY_PROMPT, reply_markup=checkout_cancel_keyboard())
            else:
                session["step"] = "contact"
                self.client.send_message(chat_id, "Укажите ваш @тег в Telegram или номер телефона для связи:", reply_markup=checkout_cancel_keyboard())
            return

        if step == "contact":
            if not PHONE_OR_TAG_RE.fullmatch(text):
                self.client.send_message(chat_id, "Введите корректный @username или номер телефона (от 7 до 15 цифр).")
                return
            if text.startswith("@"):
                session["data"]["telegram_tag"] = text
            else:
                session["data"]["phone"] = text
            session["step"] = "address"
            self.client.send_message(chat_id, DELIVERY_PROMPT, reply_markup=checkout_cancel_keyboard())
            return

        if step == "address":
            if not 5 <= len(text) <= 500:
                self.client.send_message(chat_id, "Введите от 5 до 500 символов для адреса.")
                return
            session["data"]["address"] = text
            session["step"] = "comment"
            self.client.send_message(chat_id, "Комментарий к заказу. Если его нет, отправьте «-».", reply_markup=checkout_cancel_keyboard())
            return

        if step == "comment":
            if not 1 <= len(text) <= 500:
                self.client.send_message(chat_id, "Введите от 1 до 500 символов.")
                return
            session["data"]["comment"] = "" if text in {"-", "нет", "Нет"} else text
            session["step"] = "payment"
            self.show_payment_choices(chat_id, user_id)
            return

        if step == "payment":
            self.show_payment_choices(chat_id, user_id)
            return

    def show_contact_consent(self, chat_id: int, message=None) -> None:
        text = ('<b>📩 Связь с менеджером</b>\n'
                'Ваш Telegram-тег будет передан менеджеру только для связи по этому заказу. Согласны?')
        markup = keyboard([[button('✅ Согласен', 'checkout_consent')],
                           [button('⬅️ Назад', 'checkout_back'), button('Отмена', 'checkout_cancel')]])
        self.edit_or_send_text(chat_id, message, text, markup)

    def show_payment_choices(self, chat_id, user_id):
        session = self.checkout_sessions[user_id]
        cart = self.db.get_cart(user_id)
        if not cart:
            raise ValueError("Корзина пуста.")
        settings = self.db.get_settings()
        total = sum(item["price_rub_minor"] * item["qty"] for item in cart)
        stars_total = sum(item['price_stars'] * item['qty'] for item in cart) if all(item['stars_price_confirmed'] for item in cart) else 0
        options = [option for option in self.db.payment_options(user_id) if option['method'] != 'stars' or stars_total]
        if not options:
            self.client.send_message(chat_id, "Способы оплаты сейчас недоступны.", reply_markup=back_home_keyboard())
            return
        nonce = uuid4().hex[:10]
        session["nonce"] = nonce
        session["offers"] = []
        session["items"] = [(item["id"], item["qty"], item["price_rub_minor"], item['price_stars'], item['stars_price_confirmed']) for item in cart]
        rows = []
        for index, option in enumerate(options):
            amount = quote(total, option["method"], settings, option.get("country"), option.get("crypto"), stars_total=stars_total)
            session["offers"].append({**option, "amount": amount})
            label = f"{amount['total_stars']} Stars" if option["method"] == "stars" else money(amount["payable_minor"], amount["payable_currency"])
            rows.append([button(f"{option['label']} · {label}", f"pay:{nonce}:{index}")])
        rows.append([button('⬅️ Изменить данные', 'checkout_back'), button("Отмена", "checkout_cancel")])
        data = session['data']
        contact_display = data.get('telegram_tag') or data.get('phone') or 'Не указан'
        text = (f"<b>🧾 Проверьте заказ</b>\n👤 {h(data.get('customer_name', ''))}\n📱 {h(contact_display)}\n📍 {h(data.get('address', ''))}"
                f"\n\n🛍 Товары: {money(total)}\n🚚 Доставка: {settings.get('delivery_rub', '0')} RUB / {settings.get('delivery_stars', '0')} Stars."
                f"\n\n<b>💳 Выберите способ оплаты</b>\nНа кнопках итоговые суммы, без наценок. Переводы округлены вверх до целой единицы."
                '\n⏳ На оплату: 3 ч с момента отправки реквизитов менеджером.')
        if self.settings.payment_mode == 'mock':
            text += '\n🧪 Stars: тест, без списания.'
        if settings.get("terms_text"):
            text += "\n\n" + h(settings["terms_text"]) + "\n\nВыбирая способ оплаты, вы принимаете условия покупки."
        self.client.send_message(chat_id, text, reply_markup=keyboard(rows))

    def finish_checkout_from_payment_choice(self, chat_id, user_id, callback_data):
        session = self.checkout_sessions.get(user_id)
        parts = callback_data.split(":")
        if not session or session.get("step") != "payment" or len(parts) != 3 or parts[1] != session.get("nonce"):
            raise ValueError("Этот выбор оплаты устарел. Откройте корзину и оформите заказ заново.")
        index = int(parts[2])
        if index < 0 or index >= len(session.get("offers", [])):
            raise ValueError("Способ оплаты не найден.")
        offer = session["offers"][index]
        self.finish_checkout(chat_id, user_id, session["data"], offer["method"], offer=offer, expected_items=session["items"])

    def finish_checkout(self, chat_id, user_id, data, payment_method, *, offer=None, expected_items=None):
        is_mock = payment_method == "stars" and self.settings.payment_mode == "mock"
        status = "paid" if is_mock else "awaiting_payment"
        try:
            order_id = self.db.create_order_from_cart(
                user_id=user_id,
                customer_name=data.get("customer_name", ""),
                phone=data.get("phone", ""),
                telegram_tag=data.get("telegram_tag", ""),
                contact_consent=True,
                address=data.get("address", ""),
                comment=data.get("comment", ""),
                status=status,
                is_paid=is_mock,
                is_demo=is_mock,
                payment_method=payment_method,
                crypto_option_id=(offer or {}).get("crypto", {}).get("id"),
                require_available=True,
                expected_quote=(offer or {}).get("amount"),
                expected_items=expected_items,
            )
        except ValueError as exc:
            self.client.send_message(chat_id, h(exc), reply_markup=back_home_keyboard())
            if user_id in self.checkout_sessions and self.db.get_cart(user_id):
                self.show_payment_choices(chat_id, user_id)
            return
        self.checkout_sessions.pop(user_id, None)
        order = self.db.get_order(order_id)
        self.notify_admins(order_id, "Новый заказ" + (" (тестовая оплата Stars)" if is_mock else ""))
        try:
            if payment_method in {"card", "crypto"}:
                self.show_order(chat_id, user_id, order_id)
                self.client.send_message(chat_id, "Заказ принят! Менеджер проверит заказ и отправит реквизиты для оплаты.", reply_markup=persistent_menu())
            elif is_mock:
                self.show_order(chat_id, user_id, order_id)
                self.client.send_message(chat_id, "Тестовый режим: Stars не списаны.", reply_markup=persistent_menu())
            else:
                self.show_order(chat_id, user_id, order_id)
                self.send_stars_invoice(chat_id, order)
        except TelegramApiError:
            self.client.send_message(chat_id, "Заказ сохранён. Повторите попытку или обратитесь в поддержку.", reply_markup=persistent_menu())

    def send_stars_invoice(self, chat_id, order):
        if (order["is_paid"] or order["status"] == "canceled" or order["payment_method"] != "stars"
                or order["expires_at"] <= utc_now() or not self.db.get_payment_settings()["stars_enabled"]):
            raise ValueError("Этот счёт уже нельзя оплатить.")
        sent = self.client.send_invoice(
            chat_id, title=f"Заказ #{order['id']}",
            description=f"Заказ #{order['id']}. Оплатите до {order['expires_at']} UTC.",
            payload=order["payment_payload"], prices=[{"label": f"Заказ #{order['id']}", "amount": int(order["total_stars"])}],
            currency="XTR", reply_markup=invoice_keyboard(order["id"], order["total_stars"]),
        )
        if isinstance(sent, dict) and sent.get("message_id"):
            self.db.update_order_metadata(order["id"], invoice_message_id=int(sent["message_id"]))

    def cancel_checkout_or_order(self, chat_id, user_id):
        if self.checkout_sessions.pop(user_id, None) is not None:
            self.client.send_message(chat_id, "Оформление отменено.", reply_markup=persistent_menu())
            return
        order = self.db.find_latest_cancelable_order(user_id)
        if not order:
            self.client.send_message(chat_id, "Нет неоплаченного заказа для отмены.", reply_markup=persistent_menu())
            return
        ok, reason = self.db.cancel_order(order["id"], user_id)
        self.client.send_message(chat_id, reason, reply_markup=persistent_menu())
        if ok:
            self.notify_admins(order["id"], "Покупатель отменил заказ")

    def handle_pre_checkout(self, query):
        ok = self.db.approve_pre_checkout(
            query.get("invoice_payload", ""), int(query.get("from", {}).get("id", 0)),
            int(query.get("total_amount", 0)), query.get("currency", ""),
        )
        self.client.answer_pre_checkout_query(query["id"], ok=ok, error_message="" if ok else "Заказ недоступен, уже оплачен или срок оплаты истёк.")

    def handle_successful_payment(self, message):
        payment = message["successful_payment"]
        user_id = int(message.get("from", {}).get("id", message["chat"]["id"]))
        order_id, fresh = self.db.record_successful_payment(
            payload=payment.get("invoice_payload", ""), user_id=user_id,
            amount=int(payment.get("total_amount", 0)), currency=payment.get("currency", ""),
            charge_id=payment.get("telegram_payment_charge_id", ""),
            provider_charge_id=payment.get("provider_payment_charge_id", ""),
        )
        self.flush_notifications()

    def flush_notifications(self):
        if not self.notification_lock.acquire(blocking=False):
            return
        try:
            for event in self.db.pending_notifications():
                delivered = set(json.loads(event['delivered']))
                recipients = [('admin', admin_id) for admin_id in sorted(self.settings.admin_ids)]
                if event['kind'] == 'issue':
                    recipients.insert(0, ('buyer', event['user_id']))
                for role, recipient in recipients:
                    key = f'{role}:{recipient}'
                    if key in delivered:
                        continue
                    try:
                        if role == 'buyer':
                            self.client.send_message(recipient, '⚠️ Платёж сохранён и требует ручной проверки. Свяжитесь с поддержкой.', reply_markup=persistent_menu())
                        elif event['kind'] == 'issue':
                            body = json.loads(event['body'])
                            self.admin_client.send_message(recipient, h(f"⚠️ Платёж требует проверки: пользователь {event['user_id']}, {body['amount']} {body['currency']}, charge_id: {body['charge_id']}"))
                        else:
                            order = self.db.get_order(event['order_id'])
                            title = '✅ Заказ оплачен через Telegram Stars' if event['kind'] == 'paid' else '🧾 Чек отправлен. Проверьте поступление средств.'
                            if order:
                                send_html(self.admin_client, recipient, f'<b>{title}</b>\n\n' + order_detail_text(order, self.db.list_order_items(order['id'])),
                                          reply_markup=keyboard([[button('Открыть заказ', f"a:order:{order['id']}")]]))
                        self.db.acknowledge_notification(event['event_key'], key)
                        delivered.add(key)
                    except TelegramApiError as exc:
                        if exc.error_code == 403:
                            self.db.acknowledge_notification(event['event_key'], key)
                            delivered.add(key)
                if all(f'{role}:{recipient}' in delivered for role, recipient in recipients):
                    self.db.finish_notification(event['event_key'])
        finally:
            self.notification_lock.release()

    def notify_admins(self, order_id, title):
        order = self.db.get_order(order_id)
        if not order:
            return
        text = f"<b>{h(title)}</b>\n\n{order_detail_text(order, self.db.list_order_items(order_id))}"
        
        # Action buttons for admin
        buttons = []
        if order['payment_method'] in {'card', 'crypto'} and not order['is_paid'] and not order['payment_details_sent'] and order['status'] != 'canceled':
            buttons.append([button("💳 Отправить реквизиты", f"a:sendcard:{order_id}"), button("✖️ Отменить", f"a:status:{order_id}:canceled")])
        buttons.append([button("Открыть заказ", f"a:order:{order_id}")])

        for admin_id in self.settings.admin_ids:
            try:
                send_html(self.admin_client, admin_id, text, reply_markup=keyboard(buttons))
            except Exception as exc:
                print(f"[customer] admin notification failed for {admin_id}: {exc}")

    def maintenance(self):
        self.db.expire_unpaid_orders()
        self.flush_notifications()
        for notification in self.db.pending_expiry_notifications():
            try:
                if notification["invoice_message_id"]:
                    self.safe_delete(notification["user_id"], notification["invoice_message_id"])
                self.client.send_message(notification["user_id"], f"Срок оплаты заказа #{notification['order_id']} истёк. Неоплаченный заказ удалён. Можно оформить новый.", reply_markup=persistent_menu())
                self.db.acknowledge_expiry_notification(notification["order_id"])
            except TelegramApiError as exc:
                if exc.error_code == 403:
                    self.db.acknowledge_expiry_notification(notification["order_id"])

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
        if message_id:
            if "text" in message:
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
            else:
                self.safe_delete(chat_id, int(message_id))
        self.client.send_message(chat_id, text, reply_markup=reply_markup)

    def edit_or_send_media(
        self,
        chat_id: int,
        message: dict[str, Any],
        media: dict[str, Any],
        reply_markup: dict[str, Any],
        *,
        send_kind: str,
    ) -> dict[str, Any] | bool | None:
        message_id = message.get("message_id")
        if message_id and ("photo" in message or "video" in message):
            try:
                return self.client.edit_message_media(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    media=media,
                    reply_markup=reply_markup,
                )
                return
            except TelegramApiError as exc:
                if "message is not modified" in exc.description.lower():
                    return

        if send_kind == "photo":
            sent = self.client.send_photo(chat_id, media["media"], caption=media["caption"], reply_markup=reply_markup)
        else:
            sent = self.client.send_video(chat_id, media["media"], caption=media["caption"], reply_markup=reply_markup)
        if message_id:
            self.safe_delete(chat_id, int(message_id))
        return sent

    def send_product_media(
        self,
        chat_id: int,
        message: dict[str, Any],
        product,
        caption: str,
        reply_markup: dict[str, Any],
        *,
        send_kind: str,
    ) -> None:
        product_id = int(product["id"])
        buyer_file_id = product["buyer_media_file_id"]
        if buyer_file_id:
            media = {
                "type": send_kind,
                "media": buyer_file_id,
                "caption": caption,
                "parse_mode": "HTML",
            }
            try:
                self.edit_or_send_media(chat_id, message, media, reply_markup, send_kind=send_kind)
                return
            except TelegramApiError:
                self.db.update_product(product_id, buyer_media_file_id="")

        media_path = self.ensure_local_media(product)
        if media_path:
            try:
                sent = self.upload_product_media(chat_id, message, media_path, caption, reply_markup, send_kind=send_kind)
                file_id = self.extract_sent_media_file_id(sent, send_kind)
                if file_id:
                    self.db.update_product(product_id, buyer_media_file_id=file_id)
                return
            except TelegramApiError as exc:
                print(f"[customer] media upload failed for product {product_id}: {exc}")

        warning = "\n\nМедиа товара пока недоступно. Попросите администратора заново загрузить фото или видео товара."
        self.edit_or_send_text(chat_id, message, caption + warning, reply_markup)

    def ensure_local_media(self, product) -> Path | None:
        media_path = str(product["media_path"] or "").strip()
        if media_path:
            path = Path(media_path)
            if path.exists():
                return path

        admin_file_id = str(product["media_file_id"] or "").strip()
        if not admin_file_id:
            return None

        try:
            file_info = self.admin_client.get_file(admin_file_id)
            telegram_path = file_info["file_path"]
            suffix = Path(telegram_path).suffix or ".bin"
            local_path = self.media_dir / f"{uuid4().hex}{suffix}"
            self.admin_client.download_file(telegram_path, local_path)
            self.db.update_product(int(product["id"]), media_path=str(local_path))
            return local_path
        except TelegramApiError as exc:
            print(f"[customer] media download failed for product {product['id']}: {exc}")
            return None

    def upload_product_media(
        self,
        chat_id: int,
        message: dict[str, Any],
        media_path: Path,
        caption: str,
        reply_markup: dict[str, Any],
        *,
        send_kind: str,
    ) -> dict[str, Any]:
        message_id = message.get("message_id")
        if message_id and ("photo" in message or "video" in message):
            try:
                edited = self.client.edit_message_media_upload(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    media_type=send_kind,
                    media_path=media_path,
                    caption=caption,
                    reply_markup=reply_markup,
                )
                if isinstance(edited, dict):
                    return edited
                return {}
            except TelegramApiError as exc:
                print(f"[customer] media edit upload failed: {exc}")

        if message_id:
            self.safe_delete(chat_id, int(message_id))

        if send_kind == "photo":
            sent = self.client.upload_photo(chat_id, media_path, caption=caption, reply_markup=reply_markup)
        else:
            sent = self.client.upload_video(chat_id, media_path, caption=caption, reply_markup=reply_markup)
        return sent

    def extract_sent_media_file_id(self, sent: dict[str, Any] | bool | None, send_kind: str) -> str:
        if not isinstance(sent, dict):
            return ""
        if send_kind == "photo" and sent.get("photo"):
            return sent["photo"][-1]["file_id"]
        if send_kind == "video" and sent.get("video"):
            return sent["video"]["file_id"]
        return ""

    def safe_delete(self, chat_id: int, message_id: int) -> None:
        try:
            self.client.delete_message(chat_id, message_id)
        except Exception:
            pass
