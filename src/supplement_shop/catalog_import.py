"""Import products from text catalog and PNG media in data/media."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database


def parse_catalog_file(txt_path: Path) -> list[dict[str, Any]]:
    if not txt_path.exists():
        raise FileNotFoundError(f"Файл каталога не найден: {txt_path}")

    content = txt_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    # Every product is 3 lines:
    # Line 1: Title (may contain 'RR: ')
    # Line 2: Description/characteristics (e.g. '40 капсул, Оригинал ✅')
    # Line 3: Price line (e.g. 'Цена: 1290 ₽ в звездах 600')
    if len(lines) % 3 != 0:
        raise ValueError(f"Неожиданный формат файла: {len(lines)} строк, ожидалось кратно 3.")

    price_re = re.compile(r"Цена:\s*(\d+)\s*₽\s*в\s*звездах\s*(\d+)", re.IGNORECASE)
    items: list[dict[str, any]] = []

    for i in range(0, len(lines), 3):
        raw_title = lines[i]
        title = re.sub(r"^RR:\s*", "", raw_title).strip()
        description = lines[i + 1].strip()
        price_line = lines[i + 2].strip()

        match = price_re.search(price_line)
        if not match:
            raise ValueError(f"Не удалось распознать цены в строке: {price_line!r}")

        rub_amount = int(match.group(1))
        stars_amount = int(match.group(2))

        items.append({
            "raw_title": raw_title,
            "title": title,
            "description": description,
            "price_rub_minor": rub_amount * 100,
            "price_stars": stars_amount,
        })

    return items


def seed_from_media(db: Database, media_dir: Path) -> list[int]:
    txt_path = media_dir / "Цены и название.txt"
    items = parse_catalog_file(txt_path)
    created_ids: list[int] = []
    category_id = db.get_default_category_id()

    for item in items:
        # Match PNG image file
        png_path = media_dir / f"{item['title']}.PNG"
        if not png_path.exists():
            # Try case-insensitive matching if exact name doesn't exist
            matched = False
            for f in media_dir.glob("*.PNG"):
                if f.stem.lower() == item["title"].lower():
                    png_path = f
                    matched = True
                    break
            if not matched:
                for f in media_dir.glob("*.png"):
                    if f.stem.lower() == item["title"].lower():
                        png_path = f
                        matched = True
                        break
            if not matched:
                raise FileNotFoundError(f"Не найдено изображение для товара '{item['title']}' в {media_dir}")

        existing = db.get_product_by_title(item["title"])
        if existing:
            product_id = int(existing["id"])
            db.update_product(
                product_id,
                description=item["description"],
                price_rub_minor=item["price_rub_minor"],
                price_stars=item["price_stars"],
                media_type="photo",
                media_path=str(png_path.resolve()),
                is_active=1,
            )
            # Ensure price confirmed
            with db.connect() as conn:
                conn.execute(
                    "UPDATE products SET price_confirmed=1, stars_price_confirmed=1 WHERE id=?",
                    (product_id,),
                )
        else:
            product_id = db.create_product(
                category_id=category_id,
                title=item["title"],
                description=item["description"],
                characteristics="",
                price_rub_minor=item["price_rub_minor"],
                price_stars=item["price_stars"],
                stock=0,
                media_type="photo",
                media_file_id="",
                media_path=str(png_path.resolve()),
            )
            with db.connect() as conn:
                conn.execute(
                    "UPDATE products SET price_confirmed=1, stars_price_confirmed=1 WHERE id=?",
                    (product_id,),
                )
        created_ids.append(product_id)

    return created_ids
