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
        self.assertIsNone(canceled)
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

    def test_order_delivery_deletes_order_and_accumulates_revenue_stats(self) -> None:
        product = self.db.list_products(active_only=True)[0]
        user_id = 55
        self.db.add_to_cart(user_id, int(product["id"]))
        order_id = self.db.create_order_from_cart(
            user_id=user_id,
            customer_name="Buyer 55",
            phone="+77001112233",
            address="Street 1",
            status="paid",
            is_paid=True,
            payment_method="card",
        )
        # Update payable info to non-demo
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE orders SET is_demo = 0, is_paid = 1, payable_currency = 'RUB', payable_minor = 50000, total_rub_minor = 50000 WHERE id = ?",
                (order_id,),
            )
        stats_before = self.db.stats()
        self.assertEqual(stats_before["paid_orders"], 1)
        self.assertEqual(stats_before["paid_rub_minor"], 50000)

        # Complete and delete order
        self.db.complete_and_delete_order(order_id)

        # Order must be deleted from DB
        self.assertIsNone(self.db.get_order(order_id))
        self.assertEqual(len(self.db.list_order_items(order_id)), 0)

        # Revenue stats must still reflect the deleted order
        stats_after = self.db.stats()
        self.assertEqual(stats_after["paid_orders"], 1)
        self.assertEqual(stats_after["paid_rub_minor"], 50000)
        self.assertEqual(stats_after["orders_total"], 1)

    def test_complete_unpaid_order_fails(self) -> None:
        product = self.db.list_products(active_only=True)[0]
        user_id = 56
        self.db.add_to_cart(user_id, int(product["id"]))
        order_id = self.db.create_order_from_cart(
            user_id=user_id,
            customer_name="Buyer 56",
            phone="+77001112234",
            address="Street 2",
            status="awaiting_payment",
            is_paid=False,
            payment_method="card",
        )
        with self.assertRaises(ValueError):
            self.db.complete_and_delete_order(order_id)
        # Order still exists
        self.assertIsNotNone(self.db.get_order(order_id))

    def test_large_dataset_pagination_and_search(self) -> None:
        # Benchmark and verify pagination & search with 1000 orders
        import time
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for i in range(1, 1001):
                conn.execute(
                    """
                    INSERT INTO orders(
                        id, user_id, customer_name, phone, telegram_tag, address, comment,
                        status, is_paid, is_demo, payment_method, payable_currency, payable_minor,
                        total_rub_minor, total_stars, payment_payload, created_at, expires_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, 0, 'card', 'RUB', 10000,
                        10000, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """,
                    (
                        10000 + i, 1000 + i, f"Customer {i}", f"+7999{i:07d}", f"tag_{i}",
                        f"Address {i}", "", "awaiting_payment" if i % 2 == 0 else "paid",
                        1 if i % 2 != 0 else 0, f"payload_{i}"
                    )
                )

        t0 = time.perf_counter()
        count = self.db.count_orders()
        self.assertGreaterEqual(count, 1000)

        # Pagination on page 50 (offset 500)
        page_orders = self.db.list_orders(limit=10, offset=500)
        self.assertEqual(len(page_orders), 10)

        # Search by tag
        found_tag = self.db.list_orders(search="tag_999")
        self.assertEqual(len(found_tag), 1)
        self.assertEqual(found_tag[0]["telegram_tag"], "tag_999")

        # Search by ID
        found_id = self.db.list_orders(search="#10500")
        self.assertEqual(len(found_id), 1)
        self.assertEqual(found_id[0]["id"], 10500)

        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0)  # Should execute well under 1 second

    def test_scheduled_message_deletions(self) -> None:
        self.db.schedule_message_deletion(chat_id=123, message_id=456, delay_seconds=600)
        # Not yet due now
        pending = self.db.pending_message_deletions()
        self.assertEqual(len(pending), 0)

        # Due when checked with future timestamp
        future = "2099-01-01 00:00:00"
        pending_future = self.db.pending_message_deletions(now=future)
        self.assertEqual(len(pending_future), 1)
        self.assertEqual(pending_future[0]["chat_id"], 123)
        self.assertEqual(pending_future[0]["message_id"], 456)

        # Acknowledge deletion
        self.db.acknowledge_message_deletion(int(pending_future[0]["id"]))
        self.assertEqual(len(self.db.pending_message_deletions(now=future)), 0)


if __name__ == "__main__":
    unittest.main()
