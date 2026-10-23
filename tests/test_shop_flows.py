from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from supplement_shop.customer_bot import CustomerBot
from supplement_shop.database import Database
from supplement_shop.formatting import manual_payment_text, split_html
from supplement_shop.pricing import parse_amount, quote
from supplement_shop.telegram_api import TelegramApiError


class ShopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'shop.sqlite3')
        self.db.init()
        self.product_id = self.db.create_product(category_id=self.db.get_default_category_id(), title='Тестовый товар',
            description='Описание товара', characteristics='30 капсул', price_rub_minor=100000, price_stars=109, stock=100)
        self.db.set_user_country(42, 'KZ')
        self.db.update_country('KZ', rate_per_rub='5', card_details='Kaspi: 1234, Тест')
        self.db.set_setting('manager_username', '@manager_test')
        self.client = Mock()
        self.client.send_invoice.return_value = {'message_id': 123}
        self.admin = Mock()
        self.bot = CustomerBot(client=self.client, admin_client=self.admin, db=self.db,
            settings=SimpleNamespace(db_path=self.db.path, admin_ids={7}, shop_title='Магазин', payment_mode='stars'))

    def order(self, user_id=42, method='card', **kwargs):
        self.db.add_to_cart(user_id, self.product_id)
        order_id = self.db.create_order_from_cart(user_id=user_id, customer_name='Тест', phone='+77000000000',
            address='Адрес доставки', payment_method=method, status=kwargs.pop('status', 'awaiting_payment'), **kwargs)
        return self.db.get_order(order_id)

    def message(self, text=None, **fields):
        self.bot.handle_update({'message': {'chat': {'id':42,'type':'private'}, 'from': {'id':42}, 'text':text, **fields}})

    def callback(self, data, user_id=42):
        self.bot.handle_update({'callback_query': {'id':'cb', 'from':{'id':user_id}, 'data':data,
            'message':{'chat':{'id':user_id,'type':'private'}, 'message_id':1, 'text':'Меню'}}})

    def choices(self):
        self.db.add_to_cart(42, self.product_id)
        self.bot.start_checkout(42, 42)
        for value in ('Тест', '@buyer_test', 'Адрес доставки', '-'):
            self.message(value)
        call = self.client.send_message.call_args
        return [item for row in call.kwargs['reply_markup']['inline_keyboard'] for item in row if item['callback_data'].startswith('pay:')]

    def test_checkout_requires_telegram_tag_and_explicit_consent(self):
        self.db.add_to_cart(42, self.product_id)
        self.bot.start_checkout(42, 42)
        self.message('Покупатель')
        self.message('не тег')
        self.assertEqual(self.bot.checkout_sessions[42]['step'], 'contact')
        self.message('@buyer_test')
        self.assertEqual(self.bot.checkout_sessions[42]['step'], 'address')
        self.message('Адрес доставки')
        self.message('-')
        self.assertEqual(self.bot.checkout_sessions[42]['step'], 'payment')
        self.assertTrue(self.bot.checkout_sessions[42]['data']['contact_consent'])

    def expire(self, order_id):
        with self.db.connect() as conn:
            conn.execute("UPDATE orders SET expires_at='2000-01-01 00:00:00' WHERE id=?", (order_id,))

    def test_manual_stars_and_legacy_commissions_ignored(self):
        settings = self.db.get_settings()
        self.assertEqual(quote(100000, 'stars', settings, stars_total=109)['total_stars'], 109)
        settings['card_fee_percent'] = '30'
        amount = quote(100000, 'card', settings, self.db.get_country('KZ'))
        self.assertEqual(amount['payable_minor'], 500000)
        self.assertEqual(amount['payable_currency'], 'KZT')

    def test_money_validation(self):
        for value in ('NaN', 'Infinity', '-1', '1.001'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_amount(value)
        self.assertEqual(parse_amount('12,50'), 1250)

    def test_bank_order_freezes_rate_amount_and_manager(self):
        order = self.order()
        self.assertEqual(order['payable_minor'], 500000)
        self.db.update_country('KZ', rate_per_rub='6', card_details='NEW CARD')
        self.db.set_setting('manager_username', '@other_manager')
        text = manual_payment_text(self.db.get_order(order['id']))
        self.assertIn('5 000 KZT', text)
        self.assertIn('1 000 RUB', text)
        self.assertIn('1234', text)
        self.assertIn('@manager_test', text)
        self.assertNotIn('NEW CARD', text)

    def test_crypto_amount_network_wallet_and_snapshot(self):
        option = self.db.list_crypto_options()[0]
        self.db.save_crypto_option(option['id'], address='T' + 'A'*33, rub_per_unit='90', enabled=True)
        self.db.set_payment_enabled('crypto', True)
        order = self.order(method='crypto', crypto_option_id=option['id'], require_available=True)
        self.assertEqual(order['payable_minor'], 1200)
        self.assertEqual(order['payable_currency'], 'USDT')
        self.db.save_crypto_option(option['id'], address='T' + 'B'*33, rub_per_unit='100', enabled=True)
        text = manual_payment_text(self.db.get_order(order['id']))
        self.assertIn('TRC20', text)
        self.assertIn('T' + 'A'*33, text)
        self.assertIn('12 USDT', text)

    def test_methods_require_actual_configuration_and_country(self):
        self.assertEqual([o['method'] for o in self.db.payment_options(42)], ['card','stars'])
        self.db.update_country('KZ', rate_per_rub='0')
        self.assertEqual([o['method'] for o in self.db.payment_options(42)], ['stars'])
        self.db.set_payment_enabled('stars', False)
        self.assertEqual(self.db.payment_options(42), [])
        self.db.update_country('KZ', rate_per_rub='5', enabled=False)
        self.assertEqual(self.db.payment_options(42), [])

    def test_country_specific_card_currency_is_exact_not_approximate(self):
        self.db.update_country('KZ', card_currency='RUB', card_details='RUB account')
        order = self.order()
        self.assertEqual(order['payable_currency'], 'RUB')
        self.assertEqual(order['payable_minor'], 100000)
        text = manual_payment_text(order)
        self.assertIn('5 000 KZT', text)
        self.assertIn('Перевод нужен именно в RUB', text)

    def test_shared_card_applies_to_every_country_and_freezes_details(self):
        self.db.set_setting('card_shared_details_enabled', '1')
        self.db.set_card_payment_details('Общая карта')
        self.db.set_setting('card_payment_currency', 'KZT')
        self.db.set_user_country(43, 'RU')
        for user in (42, 43):
            options = self.db.payment_options(user)
            self.assertEqual(options[0]['method'], 'card')
            order = self.order(user_id=user, require_available=True)
            self.assertEqual(order['payable_currency'], 'KZT')
            self.assertEqual(order['payable_minor'], 500000)
            self.assertIn('Общая карта', manual_payment_text(order))
        self.db.set_card_payment_details('Изменённая карта')
        self.assertIn('Общая карта', manual_payment_text(order))

    def test_shared_off_restores_country_details_without_deleting_them(self):
        self.db.set_setting('card_shared_details_enabled', '1')
        self.db.set_card_payment_details('Общая карта')
        self.db.set_user_country(43, 'RU')
        self.assertEqual(self.db.payment_options(43)[0]['method'], 'card')
        self.db.set_setting('card_shared_details_enabled', '0')
        self.assertEqual([o['method'] for o in self.db.payment_options(43)], ['stars'])
        order = self.order(require_available=True)
        self.assertIn('Kaspi: 1234', manual_payment_text(order))

    def test_payment_choices_do_not_describe_real_payment(self):
        self.choices()
        text = self.client.send_message.call_args.args[1]
        self.assertNotIn('настоящая оплата', text)
        self.assertNotIn('Stars будут списаны', text)

    def test_expiration_deletes_items_without_inventory_changes_and_is_idempotent(self):
        order = self.order()
        self.assertEqual(self.db.get_product(self.product_id)['stock'], 100)
        self.expire(order['id'])
        self.assertEqual(len(self.db.expire_unpaid_orders()), 1)
        self.assertIsNone(self.db.get_order(order['id']))
        self.assertEqual(self.db.list_order_items(order['id']), [])
        self.assertEqual(self.db.get_product(self.product_id)['stock'], 100)
        self.assertEqual(self.db.expire_unpaid_orders(), [])
        self.assertEqual(len(self.db.pending_expiry_notifications()), 1)

    def test_cancel_then_expire_never_restores_twice(self):
        order = self.order()
        self.db.update_order_status(order['id'], 'canceled')
        self.expire(order['id'])
        self.db.expire_unpaid_orders()
        self.assertEqual(self.db.get_product(self.product_id)['stock'], 100)
        self.assertEqual(self.db.pending_expiry_notifications(), [])

    def test_receipt_and_paid_orders_survive_deadline(self):
        reviewed = self.order()
        self.db.submit_receipt(reviewed['id'], 42)
        paid = self.order(user_id=43, is_paid=True, status='paid')
        for order in (reviewed, paid):
            self.expire(order['id'])
        self.assertEqual(self.db.expire_unpaid_orders(), [])
        self.db.mark_order_paid_manual(reviewed['id'])
        self.assertTrue(self.db.get_order(reviewed['id'])['is_paid'])

    def test_receipt_cannot_be_submitted_for_other_expired_or_canceled_order(self):
        order = self.order()
        with self.assertRaises(ValueError):
            self.db.submit_receipt(order['id'], 99)
        self.expire(order['id'])
        with self.assertRaises(ValueError):
            self.db.submit_receipt(order['id'], 42)
        with self.assertRaises(ValueError):
            self.db.mark_order_paid_manual(order['id'])

    def test_precheckout_checks_owner_currency_amount_status_and_deadline(self):
        order = self.order(method='stars')
        payload, amount = order['payment_payload'], order['total_stars']
        self.assertFalse(self.db.approve_pre_checkout(payload, 99, amount, 'XTR'))
        self.assertFalse(self.db.approve_pre_checkout(payload, 42, amount, 'RUB'))
        self.assertFalse(self.db.approve_pre_checkout(payload, 42, amount + 1, 'XTR'))
        self.assertTrue(self.db.approve_pre_checkout(payload, 42, amount, 'XTR'))
        self.expire(order['id'])
        self.assertFalse(self.db.approve_pre_checkout(payload, 42, amount, 'XTR'))

    def test_success_duplicate_does_not_reopen_delivered_order(self):
        order = self.order(method='stars')
        payment = dict(payload=order['payment_payload'], user_id=42, amount=order['total_stars'],currency='XTR',charge_id='charge')
        self.assertEqual(self.db.record_successful_payment(**payment), (order['id'], True))
        self.db.update_order_status(order['id'], 'done')
        self.assertEqual(self.db.record_successful_payment(**payment), (order['id'], False))
        self.assertEqual(self.db.get_order(order['id'])['status'], 'done')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_late_payment_is_durable_manual_issue_not_silently_lost(self):
        order = self.order(method='stars')
        self.expire(order['id'])
        self.db.expire_unpaid_orders()
        payment = dict(payload=order['payment_payload'], user_id=42, amount=order['total_stars'],currency='XTR',charge_id='late')
        self.assertEqual(self.db.record_successful_payment(**payment), (None, True))
        self.assertEqual(self.db.stats()['payment_issues'], 1)
        self.assertEqual(self.db.record_successful_payment(**payment), (None, False))
        self.assertEqual(self.db.get_product(self.product_id)['stock'], 100)

    def test_concurrent_orders_ignore_legacy_stock(self):
        self.db.update_product(self.product_id, stock=1)
        for user in (42, 43):
            self.db.add_to_cart(user, self.product_id)
        def create(user):
            try:
                return self.db.create_order_from_cart(user_id=user, customer_name='Имя', phone='12345', address='Адрес', payment_method='stars')
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, [42,43]))
        self.assertEqual(sum(value is not None for value in results), 2)
        self.assertEqual(self.db.get_product(self.product_id)['stock'], 1)

    def test_zero_stock_can_be_ordered_until_admin_hides_product(self):
        self.db.update_product(self.product_id, stock=0)
        self.assertTrue(self.db.add_to_cart(42, self.product_id, 3)[0])
        self.db.set_cart_qty(42, self.product_id, 5)
        self.assertEqual(self.db.get_cart(42)[0]['qty'], 5)
        order = self.db.create_order_from_cart(user_id=42, customer_name='Test', phone='12345', address='Address')
        self.assertEqual(self.db.list_order_items(order)[0]['qty'], 5)
        self.db.deactivate_product(self.product_id)
        self.assertFalse(self.db.add_to_cart(42, self.product_id)[0])

    def test_address_prompt_accepts_wildberries_pickup_link(self):
        self.db.add_to_cart(42, self.product_id)
        self.bot.start_checkout(42, 42)
        self.message('Покупатель')
        self.message('@buyer_test')
        prompt = 'Пришлите точный адрес или ссылку на выбранный ПВЗ Wildberries.'
        self.assertEqual(self.client.send_message.call_args.args[1], prompt)
        link = 'https://www.wildberries.ru/services/besplatnaya-dostavka?point=123'
        self.message(link)
        self.callback('checkout_back')
        self.assertEqual(self.client.edit_message_text.call_args.kwargs['text'], prompt)
        self.assertEqual(self.bot.checkout_sessions[42]['data']['address'], link)

    def test_all_orders_can_be_reached_without_duplicates(self):
        ids = [self.order(user_id=100 + index)['id'] for index in range(25)]
        pages = [self.db.list_orders(limit=10, offset=offset) for offset in (0,10,20)]
        result = [row['id'] for page in pages for row in page]
        self.assertEqual(result, list(reversed(ids)))
        self.assertEqual(self.db.count_orders(status='unpaid'), 25)
        self.assertEqual(self.db.count_orders(search=f'#{ids[11]}'), 1)

    def test_restart_preserves_country_order_and_deadline(self):
        order = self.order(method='stars')
        restarted = Database(self.db.path)
        restarted.init()
        self.assertEqual(restarted.get_user_country(42)['code'], 'KZ')
        self.assertEqual(restarted.get_order(order['id'])['expires_at'], order['expires_at'])
        self.assertEqual(restarted.get_order(order['id'])['payment_snapshot'], order['payment_snapshot'])

    def test_new_order_ignores_obsolete_timeout_setting(self):
        self.db.set_setting('payment_timeout_hours', '72')
        order = self.order(method='stars')
        self.assertEqual(json.loads(order['payment_snapshot'])['timeout_hours'], '3')
        deadline = datetime.fromisoformat(order['expires_at']).replace(tzinfo=timezone.utc)
        self.assertLess(deadline, datetime.now(timezone.utc) + timedelta(hours=3, minutes=1))
        self.assertGreater(deadline, datetime.now(timezone.utc) + timedelta(hours=2, minutes=59))

    def test_sqlite_backup_contains_committed_data(self):
        order = self.order()
        backup_path = self.db.backup()
        backup_db = Database(backup_path)
        self.assertEqual(backup_db.get_order(order['id'])['payable_minor'], 500000)
        self.assertEqual(backup_db.get_user_country(42)['code'], 'KZ')

    def test_duplicate_precheckout_cannot_start_two_payments_at_once(self):
        order = self.order(method='stars')
        args = (order['payment_payload'],42,order['total_stars'],'XTR')
        self.assertTrue(self.db.approve_pre_checkout(*args))
        self.assertFalse(self.db.approve_pre_checkout(*args))
        with self.db.connect() as conn:
            conn.execute("UPDATE payment_guards SET approved_until='2000-01-01 00:00:00'")
        self.assertTrue(self.db.approve_pre_checkout(*args))

    def test_card_transaction_does_not_report_fake_stars(self):
        order = self.order()
        self.db.mark_order_paid_manual(order['id'])
        with self.db.connect() as conn:
            transaction = conn.execute('SELECT * FROM transactions WHERE order_id=?',(order['id'],)).fetchone()
        self.assertEqual(transaction['amount_stars'],0)
        self.assertEqual(transaction['amount_minor'],500000)
        self.assertEqual(transaction['currency'],'KZT')
        self.assertEqual(self.db.stats()['paid_stars'],0)

    def test_stars_payment_requires_exact_amount_and_logs_mismatch(self):
        order = self.order(method='stars')
        linked, fresh = self.db.record_successful_payment(payload=order['payment_payload'],user_id=42,
            amount=1,currency='XTR',charge_id='wrong-amount')
        self.assertIsNone(linked)
        self.assertTrue(fresh)
        self.assertFalse(self.db.get_order(order['id'])['is_paid'])
        self.assertEqual(self.db.stats()['payment_issues'],1)

    def test_real_stars_checkout_sends_stored_invoice_and_records_receipt(self):
        choices = self.choices()
        self.callback(choices[-1]['callback_data'])
        self.client.send_invoice.assert_called_once()
        order = self.db.list_orders()[0]
        self.assertEqual(self.client.send_invoice.call_args.kwargs['prices'][0]['amount'],109)
        self.assertEqual(order['invoice_message_id'],123)
        self.message(successful_payment={'invoice_payload':order['payment_payload'],'total_amount':109,
            'currency':'XTR','telegram_payment_charge_id':'real-test-charge'})
        self.assertTrue(self.db.get_order(order['id'])['is_paid'])

    def test_crypto_checkout_respects_selected_network(self):
        for option in self.db.list_crypto_options():
            address = 'T'+'A'*33 if option['network']=='TRC20' else '0x'+'a'*40
            self.db.save_crypto_option(option['id'],network=option['network'],address=address,rub_per_unit='90',enabled=True)
        self.db.set_payment_enabled('crypto',True)
        choices = self.choices()
        selection = next(item for item in choices if 'ERC20' in item['text'])
        self.callback(selection['callback_data'])
        order = self.db.list_orders()[0]
        self.assertEqual(json.loads(order['payment_snapshot'])['crypto']['network'],'ERC20')
        self.assertFalse(order['is_paid'])

    def test_changed_crypto_network_requires_new_selection(self):
        option = self.db.list_crypto_options()[0]
        self.db.save_crypto_option(option['id'], address='T'+'A'*33, rub_per_unit='90', enabled=True)
        self.db.set_payment_enabled('crypto', True)
        choices = self.choices()
        selection = next(item for item in choices if 'TRC20' in item['text'])
        self.db.save_crypto_option(option['id'], network='BEP20', address='0x'+'a'*40, rub_per_unit='90', enabled=True)
        self.callback(selection['callback_data'])
        self.assertEqual(self.db.count_orders(), 0)
        self.assertTrue(self.db.get_cart(42))

    def test_legacy_product_migration_requires_price_confirmation(self):
        old_price = self.db.get_product(self.product_id)['price_stars']
        with self.db.connect() as conn:
            conn.execute('ALTER TABLE products DROP COLUMN price_rub_minor')
            conn.execute('ALTER TABLE products DROP COLUMN price_confirmed')
        self.db.init()
        product = self.db.get_product(self.product_id)
        self.assertEqual(product['price_stars'], old_price)
        self.assertFalse(product['price_confirmed'])
        self.assertFalse(self.db.add_to_cart(42,self.product_id)[0])
        self.db.update_product(self.product_id, price_rub_minor=125000)
        self.assertTrue(self.db.add_to_cart(42,self.product_id)[0])
        self.db.init()
        self.assertEqual(self.db.get_product(self.product_id)['price_rub_minor'], 125000)

    def test_start_country_gate_and_persistent_menu(self):
        with self.db.connect() as conn:
            conn.execute('DELETE FROM customers')
        self.message('/start')
        self.assertTrue(self.client.send_message.call_args_list[0].kwargs['reply_markup']['is_persistent'])
        self.assertIn('Выберите страну', self.client.send_message.call_args.args[1])
        self.callback('country:KZ')
        self.assertEqual(self.db.get_user_country(42)['code'], 'KZ')

    def test_catalog_has_prices_and_card_back_to_page(self):
        self.message('Товары')
        markup = self.client.send_message.call_args.kwargs['reply_markup']
        first = markup['inline_keyboard'][0][0]
        self.assertIn('5 000 KZT', first['text'])
        self.callback(first['callback_data'])
        markup = self.client.edit_message_text.call_args.kwargs['reply_markup']
        self.assertIn('catalog:0', [item['callback_data'] for row in markup['inline_keyboard'] for item in row])

    def test_navigation_does_not_get_recorded_as_customer_name(self):
        self.db.add_to_cart(42, self.product_id)
        self.bot.start_checkout(42,42)
        self.message('Товары')
        self.assertNotIn(42,self.bot.checkout_sessions)
        self.assertEqual(self.db.count_orders(),0)

    def test_checkout_choices_card_first_stars_last_creates_once(self):
        choices = self.choices()
        self.assertIn('Карта',choices[0]['text'])
        self.assertIn('109 Stars',choices[-1]['text'])
        self.callback(choices[0]['callback_data'])
        self.assertEqual(self.db.count_orders(),1)
        self.assertEqual(self.db.list_orders()[0]['payable_currency'],'KZT')
        self.callback(choices[0]['callback_data'])
        self.assertEqual(self.db.count_orders(),1)

    def test_changed_rate_requires_new_payment_selection(self):
        choices = self.choices()
        self.db.update_country('KZ',rate_per_rub='6')
        self.callback(choices[0]['callback_data'])
        self.assertEqual(self.db.count_orders(),0)
        self.assertTrue(self.db.get_cart(42))
        updated = self.client.send_message.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]
        self.assertIn('6 000 KZT', updated['text'])
        self.callback(updated['callback_data'])
        self.assertEqual(self.db.count_orders(),1)

    def test_changed_manual_stars_requires_reconfirmation(self):
        choices = self.choices()
        self.db.update_product(self.product_id, price_stars=200)
        self.callback(choices[-1]['callback_data'])
        self.assertEqual(self.db.count_orders(), 0)
        self.client.send_invoice.assert_not_called()

    def test_emoji_menu_is_navigation_not_checkout_input(self):
        self.db.add_to_cart(42, self.product_id)
        self.bot.start_checkout(42, 42)
        self.message('🛍 Товары')
        self.assertNotIn(42, self.bot.checkout_sessions)

    def test_inflight_stars_survives_expiry_and_cannot_cancel(self):
        order = self.order(method='stars')
        self.assertTrue(self.db.approve_pre_checkout(order['payment_payload'], 42, 109, 'XTR'))
        self.assertFalse(self.db.cancel_order(order['id'], 42)[0])
        self.expire(order['id'])
        self.assertEqual(self.db.expire_unpaid_orders(), [])
        self.assertEqual(self.db.get_order(order['id'])['status'], 'payment_check')
        self.assertEqual(len(self.db.list_order_items(order['id'])), 1)
        linked, _ = self.db.record_successful_payment(payload=order['payment_payload'], user_id=42, amount=109,
                                                      currency='XTR', charge_id='inflight')
        self.assertEqual(linked, order['id'])

    def test_payment_notification_retries_without_duplicate_transaction(self):
        order = self.order(method='stars')
        self.client.send_message.side_effect = TelegramApiError('sendMessage', 'timeout')
        self.admin.send_message.side_effect = TelegramApiError('sendMessage', 'timeout')
        payment = dict(invoice_payload=order['payment_payload'], total_amount=109, currency='XTR',
                       telegram_payment_charge_id='notify-retry')
        self.message(successful_payment=payment)
        self.client.send_message.side_effect = None
        self.admin.send_message.side_effect = None
        self.bot.maintenance()
        self.assertTrue(self.admin.send_message.call_count >= 2)
        self.client.send_message.assert_not_called()
        calls = self.admin.send_message.call_count
        self.message(successful_payment=payment)
        self.bot.maintenance()
        self.assertEqual(self.admin.send_message.call_count, calls)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_receipt_notification_retries_after_network_failure(self):
        order = self.order()
        self.db.submit_receipt(order['id'], 42)
        self.admin.send_message.side_effect = TelegramApiError('sendMessage', 'timeout')
        self.bot.maintenance()
        self.admin.send_message.side_effect = None
        self.bot.maintenance()
        self.assertEqual(self.admin.send_message.call_count, 2)

    def test_back_from_payment_invalidates_old_choice(self):
        choices = self.choices()
        self.callback('checkout_back')
        self.assertEqual(self.bot.checkout_sessions[42]['step'], 'comment')
        self.callback(choices[-1]['callback_data'])
        self.assertEqual(self.db.count_orders(), 0)
        self.message('Новый комментарий')
        self.assertEqual(self.bot.checkout_sessions[42]['data']['comment'], 'Новый комментарий')

    def test_customer_order_access_cannot_read_someone_else(self):
        order = self.order()
        self.callback(f"order:{order['id']}:0", user_id=99)
        for call in self.client.send_message.call_args_list:
            self.assertNotIn('+77000000000',call.args[1])
        self.db.set_user_country(99,'RU')
        self.callback(f"order:{order['id']}:0",user_id=99)
        self.assertIn('Заказ не найден',self.client.send_message.call_args.args[1])

    def test_disabling_country_does_not_hide_existing_order_history(self):
        order = self.order()
        self.db.update_country('KZ',enabled=False)
        self.message('Мои заказы')
        self.assertIn('Мои заказы',self.client.send_message.call_args.args[1])
        self.callback(f"order:{order['id']}:0")
        self.assertIn(f"#{order['id']}",self.client.edit_message_text.call_args.kwargs['text'])

    def test_old_receipt_button_cannot_change_order_status(self):
        order = self.order()
        self.callback(f"receipt:{order['id']}")
        updated = self.db.get_order(order['id'])
        self.assertEqual(updated['status'],'awaiting_payment')
        self.assertFalse(updated['receipt_submitted'])
        self.assertFalse(updated['is_paid'])
        self.admin.send_message.assert_not_called()
        self.callback(f"receipt:{order['id']}")
        self.admin.send_message.assert_not_called()

    def test_card_checkout_waits_for_manager_to_send_payment_details(self):
        choices = self.choices()
        self.client.reset_mock()
        self.callback(choices[0]['callback_data'])
        self.assertGreaterEqual(self.client.send_message.call_count, 1)
        message = self.client.send_message.call_args_list[0]
        self.assertIn('Заказ #', message.args[1])
        self.assertNotIn('нажмите «Чек отправлен»', message.args[1])
        callbacks = [b['callback_data'] for row in message.kwargs['reply_markup']['inline_keyboard'] for b in row]
        self.assertFalse(any(value.startswith(('receipt:', 'details:')) for value in callbacks))
        order = self.db.list_orders()[0]
        self.assertFalse(order['payment_details_sent'])
        self.client.reset_mock()
        self.callback(f"details:{order['id']}")
        self.client.send_message.assert_not_called()
        self.client.edit_message_text.assert_called_once()

    def test_expiry_notification_retries_after_network_error(self):
        order = self.order()
        self.expire(order['id'])
        self.client.send_message.side_effect = TelegramApiError('sendMessage','timeout')
        self.bot.maintenance()
        self.assertEqual(len(self.db.pending_expiry_notifications()),1)
        self.client.send_message.side_effect = None
        self.bot.maintenance()
        self.assertEqual(self.db.pending_expiry_notifications(),[])

    def test_buy_button_passes_user_info_and_auto_username(self):
        self.bot.handle_update({
            'callback_query': {
                'id': 'cb1',
                'from': {'id': 42, 'username': 'testuser', 'first_name': 'Ivan', 'last_name': 'Petrov'},
                'data': f'buy:{self.product_id}',
                'message': {'chat': {'id': 42, 'type': 'private'}, 'message_id': 1},
            }
        })
        session = self.bot.checkout_sessions.get(42)
        self.assertIsNotNone(session)
        self.assertEqual(session['data']['telegram_tag'], '@testuser')
        self.assertEqual(session['profile_name'], 'Ivan Petrov')

    def test_order_detail_text_without_contacts_does_not_crash(self):
        order = self.order(method='card')
        # Empty contacts
        with self.db.connect() as conn:
            conn.execute("UPDATE orders SET telegram_tag='', phone='' WHERE id=?", (order['id'],))
        order_row = self.db.get_order(order['id'])
        from supplement_shop.formatting import order_detail_text
        text = order_detail_text(order_row, self.db.list_order_items(order['id']))
        self.assertIn('tg://user?id=42', text)

    def test_manual_order_deadline_starts_on_details_send_and_survives_restart(self):
        order = self.order(method='card')
        self.assertEqual(order['expires_at'], '')
        
        # Restart DB migration shouldn't set deadline on manual unpaid order
        restarted = Database(self.db.path)
        restarted.init()
        self.assertEqual(restarted.get_order(order['id'])['expires_at'], '')

        # Send manual details
        self.db.mark_manual_details_sent(order['id'], message_id=999)
        updated = self.db.get_order(order['id'])
        self.assertTrue(updated['payment_details_sent'])
        self.assertNotEqual(updated['expires_at'], '')

    def test_disabled_shop_cannot_be_bypassed_by_old_payment_button(self):
        choices = self.choices()
        self.db.set_setting('shop_open','0')
        self.callback(choices[0]['callback_data'])
        self.assertEqual(self.db.count_orders(),0)

    def test_long_html_preserves_text_and_balanced_tags(self):
        from html.parser import HTMLParser
        text = '<b>' + '&lt;&amp;'*4000 + '</b>'
        parts = split_html(text)
        plain = []
        for part in parts:
            parser = HTMLParser()
            current = []
            parser.handle_data = current.append
            parser.feed(part)
            self.assertLessEqual(len(''.join(current)),3500)
            self.assertEqual(part.count('<b>'),part.count('</b>'))
            plain.extend(current)
        self.assertEqual(''.join(plain),'<&'*4000)


if __name__ == '__main__':
    unittest.main()
