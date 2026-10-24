from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from supplement_shop.admin_bot import AdminBot
from supplement_shop.database import Database
from supplement_shop.telegram_api import TelegramApiError


class AdminTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.init()
        self.client = Mock()
        self.client.send_message.return_value = {"message_id": 701}
        self.customer = Mock()
        self.bot = AdminBot(
            client=self.client,
            buyer_client=self.customer,
            db=self.db,
            settings=SimpleNamespace(admin_ids={7}, db_path=self.db.path, shop_title="Тест", payment_mode='stars'),
        )

    def message(self, text: str, user_id: int = 7, chat_type: str = "private") -> None:
        self.bot.handle_message({
            "from": {"id": user_id},
            "chat": {"id": user_id if chat_type == "private" else -100, "type": chat_type},
            "text": text,
        })

    def callback(self, data: str, user_id: int = 7, chat_type: str = "private") -> None:
        self.bot.handle_callback({
            "id": "callback", "from": {"id": user_id}, "data": data,
            "message": {"chat": {"id": user_id if chat_type == "private" else -100,
                                   "type": chat_type}, "message_id": 1, "text": "Меню"},
        })

    def last_text(self) -> str:
        call = self.client.mock_calls[-1]
        return call.kwargs.get("text", call.args[1] if len(call.args) > 1 else "")

    def test_unauthorized_user_cannot_start_edit(self) -> None:
        self.callback("a:prod:add", user_id=99)
        self.assertNotIn(99, self.bot.sessions)
        self.message("/admin", user_id=99)
        self.assertIn("Доступ закрыт", self.last_text())

    def test_admin_group_callback_cannot_start_edit(self) -> None:
        self.callback("a:prod:add", chat_type="supergroup")
        self.assertNotIn(7, self.bot.sessions)

    def test_admin_group_message_cannot_consume_private_session(self) -> None:
        self.callback("a:prod:add")
        self.message("Подмена товара", chat_type="group")
        self.assertEqual(self.bot.sessions[7]["step"], "title")

    def test_title_limit_keeps_product_wizard_at_title(self) -> None:
        self.callback("a:prod:add")
        self.message("Я" * 81)
        self.assertEqual(self.bot.sessions[7]["step"], "title")

    def test_description_limit_keeps_product_wizard_at_description(self) -> None:
        self.callback("a:prod:add")
        self.message("Витамины")
        self.message("Я" * 1801)
        self.assertEqual(self.bot.sessions[7]["step"], "description")

    def product(self) -> int:
        return self.db.create_product(
            category_id=self.db.get_default_category_id(), title="Витамины",
            description="Описание товара", characteristics="30 капсул",
            price_rub_minor=10000, price_stars=15, stock=100,
        )

    def order(self, *, status="awaiting_card_payment", method="card", user_id=42) -> int:
        self.db.set_setting("manager_username", "@manager_test")
        self.db.update_country("RU", enabled=True, rate_per_rub="1",
                               card_details="Карта 1234, получатель Тест", card_currency="RUB")
        self.db.set_user_country(user_id, "RU")
        ok, error = self.db.add_to_cart(user_id, self.product())
        self.assertTrue(ok, error)
        return self.db.create_order_from_cart(
            user_id=user_id, customer_name=f"Покупатель {user_id}", phone="+79991234567",
            address="Москва", status=status, is_paid=False, payment_method=method,
        )

    def test_product_price_title_and_archive(self) -> None:
        product_id = self.product()
        self.callback(f"a:prod:edit:{product_id}:price")
        self.message("123")
        product = self.db.get_product(product_id)
        self.assertEqual(product["price_rub_minor"], 12300)
        self.assertEqual(product['price_stars'], 15)
        self.callback(f'a:prod:edit:{product_id}:stars')
        self.message('25')
        self.assertEqual(self.db.get_product(product_id)['price_stars'], 25)
        self.assertEqual(product["price_confirmed"], 1)
        self.callback(f"a:prod:edit:{product_id}:title")
        self.message("Новое название")
        self.assertEqual(self.db.get_product(product_id)["title"], "Новое название")
        self.callback(f"a:prod:delete:{product_id}")
        self.assertEqual(self.db.get_product(product_id)["is_active"], 0)

    def test_kzt_price_input_converts_to_rubles(self) -> None:
        product_id = self.product()
        self.db.set_setting("price_input_currency", "KZT")
        self.db.update_country("KZ", rate_per_rub="5")
        self.callback(f"a:prod:edit:{product_id}:price")
        self.message("500")
        self.assertEqual(self.db.get_product(product_id)["price_rub_minor"], 10000)

    def test_readiness_shows_missing_configuration_and_demo(self):
        self.callback('a:readiness')
        self.assertIn('Оплата', self.last_text())
        self.assertIn('Менеджер', self.last_text())

    def test_payment_settings_have_one_destination_and_no_timeout_or_demo_controls(self):
        self.assertFalse(hasattr(self.bot, 'customer_client'))
        from supplement_shop.admin_settings import SETTINGS, validate_setting
        self.assertNotIn('payment_timeout_hours', SETTINGS)
        self.assertNotIn('manual_payments_demo', SETTINGS)
        for route in ('a:payments', 'a:settings:payment', 'a:settings:payments'):
            self.callback(route)
            markup = self.client.edit_message_text.call_args.kwargs['reply_markup']
            links = [b['callback_data'] for row in markup['inline_keyboard'] for b in row]
            self.assertIn('a:setting:manager_username', links)
            self.assertFalse(any('payment_timeout' in value or 'manual_payments_demo' in value or 'a:settings:pay' in value for value in links))
        with self.assertRaises(ValueError):
            validate_setting('payment_timeout_hours', '5')

    def test_demo_wallet_rate_can_be_edited_without_real_address(self):
        option = self.db.list_crypto_options()[0]
        self.callback(f"a:crypto:{option['id']}:edit:rub_per_unit")
        self.message('95')
        self.assertEqual(self.db.get_crypto_option(option['id'])['rub_per_unit'], '95')
        self.assertEqual(self.db.get_crypto_option(option['id'])['address'], '')

    def test_stars_hold_release_requires_explicit_confirmation(self):
        order_id = self.order(method='stars', status='awaiting_payment')
        order = self.db.get_order(order_id)
        self.assertTrue(self.db.approve_pre_checkout(order['payment_payload'], 42, order['total_stars'], 'XTR'))
        with self.db.connect() as conn:
            conn.execute("UPDATE orders SET expires_at='2000-01-01 00:00:00' WHERE id=?", (order_id,))
            conn.execute("UPDATE payment_guards SET approved_until='2000-01-01 00:00:00'")
        self.db.expire_unpaid_orders()
        self.callback(f'a:release_confirm:{order_id}')
        self.assertEqual(self.db.get_order(order_id)['status'], 'payment_check')
        self.callback(f'a:release:{order_id}')
        self.callback(f'a:release_confirm:{order_id}')
        self.db.expire_unpaid_orders()
        self.assertIsNone(self.db.get_order(order_id))

    def test_settings_persist_and_escape_plain_text(self) -> None:
        self.callback("a:setting:shop_title")
        self.message("<b>Магазин</b>")
        self.assertEqual(self.db.get_setting("shop_title"), "<b>Магазин</b>")
        self.assertIn("&lt;b&gt;Магазин&lt;/b&gt;", self.last_text())
        self.callback("a:setting:delivery_stars")
        self.message("NaN")
        self.assertNotEqual(self.db.get_setting("delivery_stars"), "NaN")
        self.message("25")
        self.assertEqual(self.db.get_setting("delivery_stars"), "25")

    def test_unknown_setting_cannot_write_credentials(self) -> None:
        self.callback("a:setting:admin_bot_token")
        self.message("secret")
        self.assertEqual(self.db.get_setting("admin_bot_token"), "")

    def test_orders_page_search_and_filter_keep_context(self) -> None:
        ids = [self.order(user_id=100 + i) for i in range(12)]
        self.callback("a:orders:unpaid:1")
        markup = self.client.edit_message_text.call_args.kwargs["reply_markup"]
        links = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        order_links = [value for value in links if value.startswith("a:order:")]
        self.assertEqual(len(order_links), 2)
        self.assertTrue(all(value.endswith(":unpaid:1") for value in order_links))
        self.callback("a:ordersearch")
        self.message(f"#{ids[0]}")
        self.assertIn("1", self.last_text())
        self.callback("a:orders:unpaid:0")
        markup = self.client.edit_message_text.call_args.kwargs["reply_markup"]
        links = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertEqual([v for v in links if v.startswith("a:order:")],
                         [f"a:order:{ids[0]}:unpaid:0"])

    def test_product_list_is_paged(self) -> None:
        for _ in range(12):
            self.product()
        self.callback("a:products:1")
        markup = self.client.edit_message_text.call_args.kwargs["reply_markup"]
        links = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertEqual(len([v for v in links if v.startswith("a:prod:")
                              and v.split(":")[-1].isdigit()]), 2)

    def test_manual_payment_needs_confirmation_and_notifies_once(self) -> None:
        order_id = self.order()
        self.callback(f"a:confirm_paid:{order_id}")
        self.assertFalse(self.db.get_order(order_id)["is_paid"])
        self.callback(f"a:markpaid:{order_id}")
        self.assertFalse(self.db.get_order(order_id)["is_paid"])
        self.customer.send_message.assert_not_called()
        self.callback(f"a:confirm_paid:{order_id}")
        self.assertTrue(self.db.get_order(order_id)["is_paid"])
        self.customer.send_message.assert_not_called()
        self.callback(f"a:confirm_paid:{order_id}")
        self.customer.send_message.assert_not_called()

    def test_canceled_order_cannot_be_marked_paid(self) -> None:
        order_id = self.order()
        self.callback(f"a:markpaid:{order_id}")
        self.db.update_order_status(order_id, "canceled")
        self.callback(f"a:confirm_paid:{order_id}")
        order = self.db.get_order(order_id)
        self.assertIsNone(order)
        self.customer.send_message.assert_not_called()

    def test_stars_and_unpaid_status_bypass_rejected(self) -> None:
        order_id = self.order(method="stars", status="awaiting_payment")
        self.callback(f"a:markpaid:{order_id}")
        self.callback(f"a:confirm_paid:{order_id}")
        self.callback(f"a:status:{order_id}:paid")
        self.callback(f"a:status:{order_id}:processing")
        order = self.db.get_order(order_id)
        self.assertFalse(order["is_paid"])
        self.assertEqual(order["status"], "awaiting_payment")

    def test_details_use_order_snapshot(self) -> None:
        order_id = self.order()
        self.db.update_country("RU", card_details="НОВЫЕ РЕКВИЗИТЫ")
        self.customer.send_message.return_value = {"message_id": 501}
        self.callback(f"a:sendcard:{order_id}")
        text = self.customer.send_message.call_args.args[1]
        self.assertIn("1234", text)
        self.assertNotIn("НОВЫЕ РЕКВИЗИТЫ", text)
        order = self.db.get_order(order_id)
        self.assertTrue(order["payment_details_sent"])
        self.assertEqual(order["payment_details_message_id"], 501)

    def test_paid_order_deletes_sent_payment_details_from_buyer_chat(self) -> None:
        order_id = self.order()
        self.customer.send_message.return_value = {"message_id": 501}
        self.callback(f"a:sendcard:{order_id}")
        self.callback(f"a:markpaid:{order_id}")
        self.callback(f"a:confirm_paid:{order_id}")
        self.customer.delete_message.assert_called_once_with(42, 501)
        self.assertEqual(self.db.get_order(order_id)["payment_details_message_id"], 0)

    def test_notes_stay_private_tracking_is_sent_to_customer(self) -> None:
        order_id = self.order()
        self.callback(f"a:note:{order_id}")
        self.message("Внутренняя заметка <тест>")
        self.assertEqual(self.db.get_order(order_id)["admin_note"], "Внутренняя заметка <тест>")
        self.customer.send_message.assert_not_called()
        self.callback(f"a:tracking:{order_id}")
        self.message("TRACK<123>")
        self.assertEqual(self.db.get_order(order_id)["tracking_number"], "TRACK<123>")
        self.customer.send_message.assert_not_called()

    def test_payment_settings_alias_opens_useful_settings(self) -> None:
        self.callback("a:settings:payment")
        self.assertNotIn("Не удалось", self.last_text())
        markup = self.client.edit_message_text.call_args.kwargs["reply_markup"]
        links = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertIn("a:setting:manager_username", links)
        self.assertIn('a:crypto', links)
        self.assertNotIn('a:settings:payments', links)

    def test_legacy_manual_order_requires_recreation(self) -> None:
        order_id = self.order()
        with self.db.connect() as conn:
            conn.execute("UPDATE orders SET payable_currency='' WHERE id=?", (order_id,))
        self.callback(f"a:markpaid:{order_id}")
        self.callback(f"a:confirm_paid:{order_id}")
        self.assertFalse(self.db.get_order(order_id)["is_paid"])

    def test_crypto_blank_default_can_be_configured_address_first(self) -> None:
        option_id = self.db.list_crypto_options()[0]["id"]
        address = "T" + "A" * 33
        self.callback(f"a:crypto:{option_id}:edit:address")
        self.message(address)
        self.assertEqual(self.db.get_crypto_option(option_id)["address"], address)
        self.callback(f"a:crypto:{option_id}:toggle")
        self.assertFalse(self.db.get_crypto_option(option_id)["enabled"])
        self.callback(f"a:crypto:{option_id}:edit:rub_per_unit")
        self.message("90")
        self.callback(f"a:crypto:{option_id}:toggle")
        option = self.db.get_crypto_option(option_id)
        self.assertTrue(option["enabled"])
        self.assertEqual(option["token"], "USDT")
        self.assertEqual(option["network"], "TRC20")
        self.assertEqual(option["address"], address)

    def test_crypto_add_and_invalid_address_does_not_advance(self) -> None:
        self.callback("a:crypto:add")
        self.message("USDC")
        self.message("ERC20")
        self.message("not an address")
        self.assertEqual(self.bot.sessions[7]["step"], "address")
        self.message("0x" + "a" * 40)
        self.message("90,5")
        option = self.db.list_crypto_options()[-1]
        self.assertEqual(option["token"], "USDC")
        self.assertEqual(option["network"], "ERC20")
        self.assertFalse(option["enabled"])

    def test_country_rate_details_currency_and_toggle(self) -> None:
        self.callback("a:country:KZ:edit:rate_per_rub")
        self.message("5,4")
        self.assertEqual(self.db.get_country("KZ")["rate_per_rub"], "5.4")
        self.callback("a:country:KZ:edit:card_details")
        self.message("Карта <1234>")
        self.assertIn("&lt;1234&gt;", self.last_text())
        self.callback("a:country:KZ:currency:RUB")
        self.assertEqual(self.db.get_country("KZ")["card_currency"], "RUB")
        enabled = bool(self.db.get_country("KZ")["enabled"])
        self.callback("a:country:KZ:toggle")
        self.assertEqual(bool(self.db.get_country("KZ")["enabled"]), not enabled)
        self.callback("a:country:RU:edit:rate_per_rub")
        self.message("2")
        self.assertEqual(self.db.get_country("RU")["rate_per_rub"], "1")

    def test_reject_receipt_renews_deadline_and_notifies(self) -> None:
        order_id = self.order()
        self.db.submit_receipt(order_id, 42)
        with self.db.connect() as conn:
            conn.execute("UPDATE orders SET expires_at='2000-01-01 00:00:00' WHERE id=?", (order_id,))
        self.db.set_setting("payment_timeout_hours", "4")
        self.callback(f"a:rejectreceipt:{order_id}")
        order = self.db.get_order(order_id)
        self.assertEqual(order["status"], "awaiting_card_payment")
        self.assertFalse(order["receipt_submitted"])
        expires = datetime.fromisoformat(order["expires_at"]).replace(tzinfo=timezone.utc)
        self.assertGreater(expires, datetime.now(timezone.utc) + timedelta(hours=2, minutes=59))
        self.assertLess(expires, datetime.now(timezone.utc) + timedelta(hours=3, minutes=1))
        self.customer.send_message.assert_not_called()

    def test_cancel_restores_stock_and_paid_progression_notifies(self) -> None:
        order_id = self.order()
        product_id = self.db.list_order_items(order_id)[0]["product_id"]
        self.callback(f"a:status:{order_id}:canceled")
        self.callback(f"a:status:{order_id}:canceled")
        self.assertEqual(self.db.get_product(product_id)["stock"], 100)
        self.customer.send_message.assert_not_called()
        order_id = self.order(user_id=43)
        self.callback(f"a:markpaid:{order_id}")
        self.callback(f"a:confirm_paid:{order_id}")
        for status in ("processing", "delivery"):
            self.callback(f"a:status:{order_id}:{status}")
            self.assertEqual(self.db.get_order(order_id)["status"], status)
        self.callback(f"a:done:{order_id}")
        self.callback(f"a:done_confirm:{order_id}")
        self.assertIsNone(self.db.get_order(order_id))
        self.customer.send_message.assert_not_called()

    def test_details_delivery_failure_keeps_unsent_flag(self) -> None:
        order_id = self.order()
        self.customer.send_message.side_effect = TelegramApiError("sendMessage", "blocked", 403)
        self.callback(f"a:sendcard:{order_id}")
        self.assertFalse(self.db.get_order(order_id)["payment_details_sent"])

    def test_media_upload_and_remove_preserve_existing_flow(self) -> None:
        product_id = self.product()
        self.client.get_file.return_value = {"file_path": "photos/photo.jpg"}
        self.callback(f"a:prod:edit:{product_id}:media")
        self.bot.handle_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"},
                                 "photo": [{"file_id": "small", "file_size": 50},
                                           {"file_id": "large", "file_size": 100}]})
        product = self.db.get_product(product_id)
        self.assertEqual(product["media_file_id"], "large")
        self.assertEqual(product["media_type"], "photo")
        self.assertTrue(product["media_path"].endswith(".jpg"))
        self.client.download_file.assert_called_once()
        self.callback(f"a:prod:edit:{product_id}:media")
        self.message("-")
        self.assertEqual(self.db.get_product(product_id)["media_type"], "none")

    def test_payment_issue_resolve_requires_confirmation_and_never_refunds(self) -> None:
        self.db.record_successful_payment(payload="missing-order", user_id=42, amount=50,
                                           currency="XTR", charge_id="charge<>" * 20)
        self.callback("a:issues:0")
        markup = self.client.edit_message_text.call_args.kwargs["reply_markup"]
        links = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        issue_link = next(value for value in links if value.startswith("a:issue:"))
        self.assertLessEqual(len(issue_link.encode()), 64)
        event_id = issue_link.split(":")[2]
        self.callback(f"a:issue_confirm:{event_id}")
        self.assertEqual(self.db.stats()["payment_issues"], 1)
        self.callback(issue_link)
        self.assertIn("charge&lt;&gt;", self.last_text())
        self.callback(f"a:issue_resolve:{event_id}")
        self.assertEqual(self.db.stats()["payment_issues"], 1)
        self.callback(f"a:issue_confirm:{event_id}")
        self.assertEqual(self.db.stats()["payment_issues"], 0)
        self.assertEqual(self.db.count_orders(), 0)
        self.customer.refund_star_payment.assert_not_called()
        self.customer.send_message.assert_not_called()

    def test_nontext_message_cannot_clear_a_setting(self) -> None:
        self.db.set_setting("manager_username", "@manager_test")
        self.callback("a:setting:manager_username")
        self.bot.handle_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"},
                                 "photo": [{"file_id": "image"}]})
        self.assertEqual(self.db.get_setting("manager_username"), "@manager_test")
        self.assertIn(7, self.bot.sessions)

    def test_each_database_status_is_available_as_filter(self) -> None:
        from supplement_shop.database import ORDER_STATUSES

        self.callback("a:orderfilters")
        self.assertIsNotNone(self.client.edit_message_text.call_args)
        markup = self.client.edit_message_text.call_args.kwargs["reply_markup"]
        links = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        for status in ORDER_STATUSES:
            self.assertIn(f"a:orders:{status}:0", links)

    def test_large_order_is_not_sent_as_oversized_telegram_message(self) -> None:
        order_id = self.order()
        item = self.db.list_order_items(order_id)[0]
        with self.db.connect() as conn:
            for index in range(45):
                conn.execute("INSERT INTO order_items(order_id,product_id,title,price_stars,price_rub_minor,qty) "
                             "VALUES (?,?,?,?,?,?)", (order_id, item["product_id"], f"Товар {index:02} " + "Я" * 70, 1, 10000, 1))
        self.callback(f"a:order:{order_id}")
        messages = []
        for call in self.client.mock_calls:
            if call[0] in {"send_message", "edit_message_text"}:
                text = call.kwargs.get("text", call.args[1] if len(call.args) > 1 else "")
                parser = HTMLParser()
                plain = []
                parser.handle_data = plain.append
                parser.feed(text)
                messages.append("".join(plain))
        self.assertTrue(messages)
        self.assertTrue(all(len(value.encode("utf-16-le")) // 2 <= 4096 for value in messages))
        self.assertIn("Товар 44", "\n".join(messages))

    def test_navigation_invalidates_payment_confirmation(self) -> None:
        order_id = self.order()
        self.callback(f"a:markpaid:{order_id}")
        self.callback("a:menu")
        self.callback(f"a:confirm_paid:{order_id}")
        self.assertFalse(self.db.get_order(order_id)["is_paid"])

    def test_expired_manual_order_refused_even_after_prompt(self) -> None:
        order_id = self.order()
        self.callback(f"a:markpaid:{order_id}")
        with self.db.connect() as conn:
            conn.execute("UPDATE orders SET expires_at='2000-01-01 00:00:00' WHERE id=?", (order_id,))
        self.callback(f"a:confirm_paid:{order_id}")
        self.assertFalse(self.db.get_order(order_id)["is_paid"])

    def test_database_errors_are_escaped_and_session_can_retry(self) -> None:
        product_id = self.product()
        self.callback(f"a:prod:edit:{product_id}:title")
        with patch.object(self.db, "update_product", side_effect=ValueError("Ошибка <script>")):
            self.message("Название")
        self.assertIn("Ошибка &lt;script&gt;", self.last_text())
        self.message("Название")
        self.assertEqual(self.db.get_product(product_id)["title"], "Название")

    def test_product_add_wizard_and_boundaries(self) -> None:
        self.callback("a:prod:add")
        for value in ("Товар", "Описание товара", "123", "25", "Я" * 801):
            self.message(value)
        self.assertEqual(self.bot.sessions[7]["step"], "characteristics")
        self.message("30 капсул")
        self.assertEqual(self.bot.sessions[7]['step'], 'media')
        self.message("-")
        product = self.db.list_products()[0]
        self.assertEqual(product["price_rub_minor"], 12300)
        self.assertEqual(product['price_stars'], 25)
        self.assertEqual(product["media_type"], "none")

    def test_boolean_currency_and_legacy_payment_callbacks(self) -> None:
        self.callback('a:set:card_shared_details_enabled:1')
        self.assertEqual(self.db.get_setting('card_shared_details_enabled'), '1')
        self.callback('a:set:card_payment_currency:KZT')
        self.assertEqual(self.db.get_setting('card_payment_currency'), 'KZT')
        self.callback('a:set:card_payment_currency:INVALID')
        self.assertEqual(self.db.get_setting('card_payment_currency'), 'KZT')
        self.callback("a:set:shop_open:0")
        self.assertEqual(self.db.get_setting("shop_open"), "0")
        self.callback("a:set:price_input_currency:KZT")
        self.assertEqual(self.db.get_setting("price_input_currency"), "KZT")
        for method in ("stars", "card", "crypto"):
            before = self.db.get_payment_settings()[f"{method}_enabled"]
            self.callback(f"a:pay:toggle:{method}")
            self.assertEqual(self.db.get_payment_settings()[f"{method}_enabled"], not before)
        self.callback("a:pay:details")
        self.message("Карта RUB 1234")
        self.assertEqual(self.db.get_payment_settings()["card_details"], "Карта RUB 1234")

    def test_crypto_manual_details_and_confirmation(self) -> None:
        option_id = self.db.list_crypto_options()[0]["id"]
        self.db.save_crypto_option(option_id=option_id, token="USDT", network="TRC20",
                                   address="T" + "A" * 33, rub_per_unit="90", enabled=True)
        self.db.set_setting("manager_username", "@manager_test")
        self.db.set_user_country(42, "RU")
        self.db.add_to_cart(42, self.product())
        order_id = self.db.create_order_from_cart(user_id=42, customer_name="Тест", phone="12345",
                                                  address="Москва", payment_method="crypto",
                                                  crypto_option_id=option_id, status="awaiting_crypto_payment")
        self.customer.send_message.return_value = {'message_id': 502}
        self.callback(f"a:sendcard:{order_id}")
        from supplement_shop.formatting import manual_payment_text
        self.assertIn("T" + "A" * 33, manual_payment_text(self.db.get_order(order_id)))
        self.assertIn("T" + "A" * 33, self.customer.send_message.call_args.args[1])
        self.callback(f"a:markpaid:{order_id}")
        self.callback(f"a:confirm_paid:{order_id}")
        self.assertTrue(self.db.get_order(order_id)["is_paid"])

    def test_delivered_order_notification_scheduled_for_deletion(self) -> None:
        order_id = self.order()
        self.callback(f"a:markpaid:{order_id}")
        self.callback(f"a:confirm_paid:{order_id}")
        self.callback(f"a:done:{order_id}")
        self.callback(f"a:done_confirm:{order_id}")

        # Check that message was sent and scheduled for deletion
        pending_future = self.db.pending_message_deletions(now="2099-01-01 00:00:00")
        self.assertEqual(len(pending_future), 1)
        self.assertEqual(pending_future[0]["message_id"], 701)

        # Trigger maintenance with future date simulated
        with self.db.connect() as conn:
            conn.execute("UPDATE scheduled_deletions SET delete_at = '2000-01-01 00:00:00'")
        self.bot.maintenance()
        self.client.delete_message.assert_called_with(7, 701)
        self.assertEqual(len(self.db.pending_message_deletions()), 0)

    def test_admin_persistent_menu_and_quick_access(self) -> None:
        from supplement_shop.keyboards import admin_persistent_menu
        menu = admin_persistent_menu()
        texts = [b["text"] for row in menu["keyboard"] for b in row]
        self.assertIn("🎛 Меню админки", texts)
        self.assertIn("📦 Заказы", texts)

        # Test /start sends persistent menu
        self.message("/start")
        calls = [c for c in self.client.send_message.call_args_list if c.kwargs.get("reply_markup") == menu]
        self.assertTrue(calls)

        # Test clicking "🎛 Меню админки" opens menu
        self.message("🎛 Меню админки")
        self.assertIn("Управление магазином", self.last_text())

        # Test clicking "📦 Заказы" opens orders
        self.message("📦 Заказы")
        self.assertIn("Заказы", self.last_text())


class SettingsValidationTest(unittest.TestCase):
    def test_invalid_values_are_rejected(self) -> None:
        from supplement_shop.admin_settings import validate_setting

        cases = [
            ("secret", "value"), ("shop_open", "yes"),
            ("payment_timeout_hours", "0"), ("payment_timeout_hours", "73"),
            ("payment_timeout_hours", "1.5"), ("stars_rub_per_star", "0"),
            ("stars_fee_percent", "100"), ("crypto_fee_percent", "NaN"),
            ("card_markup_percent", "Infinity"), ("delivery_rub", "-1"),
            ("delivery_rub", "1.001"), ("price_input_currency", "USD"),
            ("manager_username", "@ab"), ("manager_username", "@12345"),
            ("support_username", "https://t.me/testuser"),
            ("reviews_url", "javascript:alert(1)"), ("reviews_url", "https://"),
            ("reviews_url", "https://host/\nattack"), ("shop_title", "Я" * 81),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_setting(key, value)

    def test_values_are_normalized_without_html_interpretation(self) -> None:
        from supplement_shop.admin_settings import validate_setting

        for key, value, expected in [
            ("delivery_stars", " 25 ", "25"),
            ("manager_username", "@Manager_test", "@Manager_test"),
            ("price_input_currency", "kzt", "KZT"),
            ("welcome_text", "<b>Привет</b>", "<b>Привет</b>"),
            ("reviews_url", "https://example.com/reviews", "https://example.com/reviews"),
        ]:
            with self.subTest(key=key):
                self.assertEqual(validate_setting(key, value), expected)


if __name__ == "__main__":
    unittest.main()
