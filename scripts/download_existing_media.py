from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from supplement_shop.config import load_settings
from supplement_shop.database import Database
from supplement_shop.telegram_api import BotClient


def main() -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    admin = BotClient(settings.admin_bot_token, settings.telegram_api_base)
    media_dir = settings.db_path.parent / "media"

    downloaded = 0
    skipped = 0
    failed = 0

    for product in db.list_products(active_only=False):
        file_id = str(product["media_file_id"] or "").strip()
        media_path = str(product["media_path"] or "").strip()
        if product["media_type"] == "none" or not file_id or media_path:
            skipped += 1
            continue

        try:
            file_info = admin.get_file(file_id)
            suffix = Path(file_info["file_path"]).suffix or ".bin"
            destination = media_dir / f"{uuid4().hex}{suffix}"
            admin.download_file(file_info["file_path"], destination)
            db.update_product(
                int(product["id"]),
                media_path=str(destination),
                buyer_media_file_id="",
            )
            downloaded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"Failed product #{product['id']}: {exc}")

    print(f"downloaded={downloaded}")
    print(f"skipped={skipped}")
    print(f"failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

