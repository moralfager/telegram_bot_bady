from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from supplement_shop.catalog_import import parse_catalog_file, seed_from_media
from supplement_shop.database import Database


class CatalogImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media_dir = Path(self.tmp.name) / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.init()

    def test_parse_catalog_file_and_strip_prefix(self):
        txt_path = self.media_dir / "Цены и название.txt"
        txt_path.write_text(
            "RR: Товар 1\n"
            "40 капсул, Оригинал ✅\n"
            "Цена: 1290 ₽ в звездах 600\n"
            "Товар 2\n"
            "30 капсул\n"
            "Цена: 1390 ₽ в звездах 650\n",
            encoding="utf-8",
        )

        items = parse_catalog_file(txt_path)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Товар 1")
        self.assertEqual(items[0]["price_rub_minor"], 129000)
        self.assertEqual(items[0]["price_stars"], 600)
        self.assertEqual(items[1]["title"], "Товар 2")
        self.assertEqual(items[1]["price_rub_minor"], 139000)
        self.assertEqual(items[1]["price_stars"], 650)

    def test_seed_from_media_creates_products(self):
        txt_path = self.media_dir / "Цены и название.txt"
        txt_path.write_text(
            "RR: Молекула плюс\n"
            "40 капсул, Оригинал ✅\n"
            "Цена: 1290 ₽ в звездах 600\n",
            encoding="utf-8",
        )
        (self.media_dir / "Молекула плюс.PNG").write_bytes(b"\x89PNG\r\n\x1a\nfake")

        ids = seed_from_media(self.db, self.media_dir)
        self.assertEqual(len(ids), 1)
        product = self.db.get_product(ids[0])
        self.assertIsNotNone(product)
        self.assertEqual(product["title"], "Молекула плюс")
        self.assertEqual(product["price_rub_minor"], 129000)
        self.assertEqual(product["price_stars"], 600)
        self.assertTrue(product["price_confirmed"])
        self.assertTrue(product["stars_price_confirmed"])
        self.assertEqual(product["media_type"], "photo")
