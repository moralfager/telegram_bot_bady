from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from supplement_shop.database import Database
from supplement_shop.pricing import money, quote


class ManualPricingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'shop.sqlite3')
        self.db.init()

    def product(self, **overrides):
        values = dict(category_id=self.db.get_default_category_id(), title='Demo product',
                      description='Demo', characteristics='30 capsules',
                      price_rub_minor=100000, price_stars=80, stock=20)
        return self.db.create_product(**(values | overrides))

    def test_manual_stars_survive_ruble_edit_and_restart(self):
        product = self.product()
        self.assertEqual(self.db.get_product(product)['price_stars'], 80)
        self.db.update_product(product, price_rub_minor=250000)
        self.db.init()
        self.assertEqual(self.db.get_product(product)['price_stars'], 80)
        self.db.update_product(product, price_stars=125)
        self.assertEqual(self.db.get_product(product)['price_rub_minor'], 250000)

    def test_no_hidden_markup_and_whole_converted_amounts(self):
        settings = self.db.get_settings() | {'card_markup_percent': '30', 'card_fee_percent': '30',
                                            'stars_markup_percent': '999'}
        country = {'card_currency': 'KZT', 'rate_per_rub': '5.12345'}
        self.assertEqual(quote(100000, 'card', settings, country)['payable_minor'], 512400)
        self.assertEqual(quote(100000, 'stars', settings, stars_total=80)['payable_minor'], 80)
        self.assertEqual(quote(100000, 'crypto', settings, crypto={'token':'USDT', 'rub_per_unit':'90'})['payable_minor'], 1200)

    def test_integer_price_validation_and_display(self):
        self.assertEqual(self.db.price_from_input('1000'), 100000)
        for value in ('1000.50', '0', 'NaN', '-5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.db.price_from_input(value)
        for fields in ({'price_rub_minor': 100050}, {'price_stars': 3.5}, {'price_stars': 0}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.product(**fields)
        self.assertEqual(money(100000), '1 000 RUB')
        self.assertEqual(money(12345), '123,45 RUB')

    def test_order_uses_manual_stars_quantity_and_separate_delivery(self):
        product = self.product()
        self.db.set_setting('delivery_rub', '100')
        self.db.set_setting('delivery_stars', '10')
        self.db.add_to_cart(42, product, 2)
        order_id = self.db.create_order_from_cart(user_id=42, customer_name='Test', phone='123456', address='Test street')
        self.assertEqual(self.db.get_order(order_id)['total_stars'], 170)
        self.assertEqual(self.db.get_order(order_id)['total_rub_minor'], 210000)
        self.db.update_product(product, price_stars=200)
        self.assertEqual(self.db.list_order_items(order_id)[0]['price_stars'], 80)
        self.assertEqual(self.db.get_order(order_id)['total_stars'], 170)

    def test_missing_manual_stars_cannot_produce_invoice(self):
        product = self.product(price_stars=None)
        self.db.add_to_cart(42, product)
        with self.assertRaises(ValueError):
            self.db.create_order_from_cart(user_id=42, customer_name='Test', phone='123456', address='Test street')
        self.assertEqual(self.db.count_orders(), 0)

    def test_demo_methods_do_not_require_fake_contacts_and_never_auto_pay(self):
        self.db.set_payment_enabled('crypto', True)
        self.db.update_country('KZ', rate_per_rub='5', card_details='1234')
        self.db.set_user_country(42, 'KZ')
        self.db.set_setting('manager_username', '@manager')
        with self.db.connect() as conn:
            conn.execute("UPDATE crypto_options SET enabled=1,rub_per_unit='100',address='0x1234567890123456789012345678901234567890' WHERE network='ERC20'")
        options = self.db.payment_options(42)
        self.assertIn('card', [item['method'] for item in options])
        self.db.add_to_cart(42, self.product())
        order_id = self.db.create_order_from_cart(user_id=42, customer_name='Test', phone='123456', address='Test street',
                                                 payment_method='card', require_available=True)
        order = self.db.get_order(order_id)
        self.assertFalse(order['is_paid'])
        self.db.mark_order_paid_manual(order_id)
        self.assertEqual(self.db.stats()['paid_orders'], 1)

    def test_reset_requires_confirmation_and_preserves_backup(self):
        pass

    def test_reset_refuses_running_instance(self):
        pass
