from __future__ import annotations

import argparse
import tempfile
import threading
from pathlib import Path

from .admin_bot import AdminBot
from .config import load_settings
from .customer_bot import CustomerBot
from .database import Database
from .telegram_api import BotClient, run_polling
from .runtime import database_lock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram-магазин БАДов")
    parser.add_argument(
        "command",
        choices=["seed-products", "bots", "migrate"],
        help="seed-products: создать базу и загрузить товары из media; migrate: обновление схемы; bots: запустить ботов",
    )
    parser.add_argument('--confirm-reset', action='store_true', help='Подтверждение удаления старой базы при seed-products')
    return parser


def command_seed_products(confirm_reset: bool = False) -> int:
    settings = load_settings()
    db_path = settings.db_path
    media_dir = db_path.parent / "media"

    if db_path.exists() and not confirm_reset:
        print("База данных уже существует. Для полного пересоздания передайте флаг --confirm-reset")
        return 1

    with database_lock(db_path):
        if db_path.exists() and confirm_reset:
            # Delete old database files
            for ext in ["", "-wal", "-shm"]:
                target = db_path.with_name(f"{db_path.name}{ext}")
                if target.exists():
                    try:
                        target.unlink()
                    except OSError as exc:
                        print(f"Предупреждение при удалении {target}: {exc}")

        db = Database(db_path)
        db.init()
        from .catalog_import import seed_from_media
        created_ids = seed_from_media(db, media_dir)
        print(f"База создана: {db_path}")
        print(f"Загружено реальных товаров: {len(created_ids)}")
        return 0


def command_migrate() -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    backup = db.backup()
    db.init()
    print(f'Backup: {backup or "new database"}')
    print(f'Database migrated: {settings.db_path}')
    products = db.list_products(active_only=False)
    print(f'Products: {len(products)}; prices requiring confirmation: {sum(not row["price_confirmed"] for row in products)}')
    print(f'Orders preserved: {db.count_orders()}')
    return 0


def command_bots() -> int:
    settings = load_settings()
    if not settings.has_bot_tokens:
        print("Заполните BUYER_BOT_TOKEN и ADMIN_BOT_TOKEN в .env.")
        print("Пример настроек находится в .env.example.")
        return 2
    if not settings.admin_ids:
        print("Заполните ADMIN_IDS в .env, иначе админ-бот будет закрыт для всех.")
        return 2

    db = Database(settings.db_path)
    db.init()

    buyer_client = BotClient(settings.buyer_bot_token, settings.telegram_api_base)
    admin_client = BotClient(settings.admin_bot_token, settings.telegram_api_base)

    customer_bot = CustomerBot(
        client=buyer_client,
        admin_client=admin_client,
        db=db,
        settings=settings,
    )
    admin_bot = AdminBot(
        client=admin_client,
        buyer_client=buyer_client,
        db=db,
        settings=settings,
    )

    print(f"DB: {settings.db_path}")
    print(f"PAYMENT_MODE: {settings.payment_mode}")
    print("Нажмите Ctrl+C для остановки.")

    stop_event = threading.Event()

    def maintain_orders():
        while not stop_event.is_set():
            try:
                customer_bot.maintenance()
            except Exception as exc:
                print(f'[maintenance] {exc}')
            stop_event.wait(60)

    threads = [
        threading.Thread(
            target=run_polling,
            kwargs={
                "name": "buyer",
                "client": buyer_client,
                "timeout": settings.polling_timeout,
                "handler": customer_bot.handle_update,
                "stop_event": stop_event,
            },
            name="buyer-bot",
            daemon=False,
        ),
        threading.Thread(
            target=run_polling,
            kwargs={
                "name": "admin",
                "client": admin_client,
                "timeout": settings.polling_timeout,
                "handler": admin_bot.handle_update,
                "stop_event": stop_event,
            },
            name="admin-bot",
            daemon=False,
        ),
        threading.Thread(target=maintain_orders, name='order-expiry', daemon=False),
    ]

    for thread in threads:
        thread.start()

    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        print("Остановка ботов...")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "seed-products":
        return command_seed_products(confirm_reset=args.confirm_reset)
    if args.command == "bots":
        with database_lock(load_settings().db_path):
            return command_bots()
    if args.command == 'migrate':
        with database_lock(load_settings().db_path):
            return command_migrate()
    raise AssertionError(args.command)
