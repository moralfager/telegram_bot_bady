from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from supplement_shop.database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.init()
        cat = self.db.get_default_category_id()
        pid = self.db.create_product(
            category_id=cat,
            title="Тестовый товар",
            description="Описание",
            characteristics="30 капсул",
            price_rub_minor=100000,
            price_stars=50,
            stock=10,
        )
        with self.db.connect() as conn:
            conn.execute("UPDATE products SET price_confirmed=1, stars_price_confirmed=1 WHERE id=?", (pid,))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cart_order_and_stats_flow(self) -> None:
        product = self.db.list_products(active_only=True)[0]
        user_id = 42

        ok, message = self.db.add_to_cart(user_id, int(product["id"]), qty=2)
        self.assertTrue(ok, message)
        self.assertEqual(len(self.db.get_cart(user_id)), 1)

        order_id = self.db.create_order_from_cart(
            user_id=user_id,
            customer_name="Test User",
            phone="+77000000000",
            address="Test Address",
            status="paid",
            is_paid=True,
            payment_method="stars",
        )
        order = self.db.get_order(order_id)
        self.assertIsNotNone(order)
        self.assertEqual(order["is_paid"], 1)
        self.assertEqual(order["total_stars"], int(product["price_stars"]) * 2)
        self.assertEqual(self.db.get_cart(user_id), [])

        stats = self.db.stats()
        self.assertEqual(stats["orders_total"], 1)
        self.assertEqual(stats["paid_orders"], 1)
        self.assertEqual(stats["paid_stars"], order["total_stars"])

    def test_mark_order_paid_by_payload(self) -> None:
        product = self.db.list_products(active_only=True)[0]
        user_id = 43
        ok, message = self.db.add_to_cart(user_id, int(product["id"]))
        self.assertTrue(ok, message)
        order_id = self.db.create_order_from_cart(
            user_id=user_id,
            customer_name="Test User",
            phone="+77000000000",
            address="Test Address",
            status="awaiting_payment",
            is_paid=False,
            payment_method="stars",
        )
        order = self.db.get_order(order_id)
        marked_id = self.db.mark_order_paid(
            payload=order["payment_payload"],
            telegram_payment_charge_id="tg-charge",
            provider_payment_charge_id="provider-charge",
        )
        self.assertEqual(marked_id, order_id)
        paid = self.db.get_order(order_id)
        self.assertEqual(paid["is_paid"], 1)
        self.assertEqual(paid["status"], "paid")

    def test_cancel_unpaid_order_preserves_legacy_stock(self) -> None:
        product = self.db.list_products(active_only=True)[0]
        user_id = 45
        stock_before = int(product["stock"])

        ok, message = self.db.add_to_cart(user_id, int(product["id"]), qty=2)
        self.assertTrue(ok, message)
        order_id = self.db.create_order_from_cart(
            user_id=user_id,
            customer_name="Test User",
            phone="+77000000000",
            address="Test Address",
            status="awaiting_payment",
            is_paid=False,
            payment_method="stars",
        )

        after_order = self.db.get_product(int(product["id"]))
        self.assertEqual(after_order["stock"], stock_before)

        ok, message = self.db.cancel_order(order_id, user_id=user_id)
        self.assertTrue(ok, message)
        canceled = self.db.get_order(order_id)
        self.assertEqual(canceled["status"], "canceled")
        after_cancel = self.db.get_product(int(product["id"]))
        self.assertEqual(after_cancel["stock"], stock_before)

        ok, _ = self.db.cancel_order(order_id, user_id=user_id)
        self.assertFalse(ok)
        after_second_cancel = self.db.get_product(int(product["id"]))
        self.assertEqual(after_second_cancel["stock"], stock_before)

    def test_payment_settings_and_card_order_flow(self) -> None:
        settings = self.db.get_payment_settings()
        self.assertTrue(settings["stars_enabled"])
        self.assertTrue(settings["card_enabled"])

        self.db.set_payment_enabled("stars", False)
        self.db.set_card_payment_details("Kaspi Gold 4400 0000 0000 0000\nПолучатель: Test User")
        settings = self.db.get_payment_settings()
        self.assertFalse(settings["stars_enabled"])
        self.assertIn("Kaspi", settings["card_details"])

        product = self.db.list_products(active_only=True)[0]
        user_id = 44
        ok, message = self.db.add_to_cart(user_id, int(product["id"]))
        self.assertTrue(ok, message)
        order_id = self.db.create_order_from_cart(
            user_id=user_id,
            customer_name="Test User",
            phone="+77000000000",
            address="Test Address",
            status="review",
            is_paid=False,
            payment_method="card",
        )

        order = self.db.get_order(order_id)
        self.assertEqual(order["payment_method"], "card")
        self.assertEqual(order["payment_details_sent"], 0)
        self.assertEqual(order["status"], "review")

        self.db.mark_card_details_sent(order_id)
        order = self.db.get_order(order_id)
        self.assertEqual(order["payment_details_sent"], 1)
        self.assertEqual(order["status"], "awaiting_card_payment")

        self.db.mark_order_paid_manual(order_id)
        order = self.db.get_order(order_id)
        self.assertEqual(order["is_paid"], 1)
        self.assertEqual(order["status"], "paid")


if __name__ == "__main__":
    unittest.main()
