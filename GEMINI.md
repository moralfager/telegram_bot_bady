# Project Map (`telegram-supplement-shop`)

## 1. What the Project Does
A single-process Telegram supplement shop consisting of two bots: a public customer bot (`CustomerBot`) and a private admin bot (`AdminBot`).
- **Customer bot**: Country selection (10 CIS countries), paginated catalog (12/page) with photos/videos, shopping cart (up to 30 distinct items per order), step-by-step checkout (recipient name, `@username` Telegram tag, delivery address or Wildberries pickup link, comment, explicit contact consent), and multi-method checkout: bank card transfer (country-specific or shared card), cryptocurrency transfer (manual wallet transfer, e.g., USDT TRC20/ERC20), and Telegram Stars (`XTR`).
- **Admin bot**: Restricted to `ADMIN_IDS` in private chat (`/admin`). Manages product CRUD (independent RUB and Stars prices, media, manual active/paused toggle), order filtering/search, manual payment details dispatch (`a:sendcard`), two-step payment confirmation, Stars hold release, unresolved payment events (`payment_events`), shop settings/texts, countries/rates, crypto wallets, and revenue statistics.
- **Background maintenance**: Runs every 60s (`maintain_orders`) to expire unpaid orders after a fixed 3-hour window (`PAYMENT_TIMEOUT_HOURS = 3`) and retry transactional notifications via SQLite outbox tables.

## 2. Stack and Versions
- **Language**: Python `>=3.11` (currently running on Python `3.14.3` in this environment).
- **Dependencies**: Zero third-party runtime dependencies (`dependencies = []` in `pyproject.toml`).
- **Standard Library Modules**: `sqlite3` (WAL mode), `urllib.request` / `urllib.error` (JSON & multipart HTTP to Telegram Bot API), `threading`, `decimal`, `html.parser`, `unittest`, `msvcrt` / `fcntl` (file locks).
- **Database**: SQLite 3 (`data/shop.sqlite3` by default).
- **External API**: Telegram Bot API (`https://api.telegram.org`), Telegram Stars (`XTR`).

## 3. How to Run / Test / Lint / Build
All CLI commands go through `run.py` (which injects `src/` into `sys.path` and calls `supplement_shop.main:main`):
- **Run bots**: `python -X utf8 -u run.py bots` (requires `BUYER_BOT_TOKEN`, `ADMIN_BOT_TOKEN`, and `ADMIN_IDS` in `.env`).
- **Seed catalog from media**: `python run.py seed-products --confirm-reset` (creates a clean DB and imports 4 products from `data/media/` with images).
- **Migrate DB (with automatic backup)**: `python run.py migrate`
- **Run unit tests**: `python -m unittest discover -s tests` (110 tests total).
- **Syntax / bytecode check (lint equivalent)**: `python -m compileall -q src`.
- **Build**: None needed (pure Python scripts executed directly).

## 4. Folder Structure
- `src/supplement_shop/` — Core application package (bots, SQLite persistence, pricing, Telegram HTTP client, CLI entrypoint).
- `tests/` — `unittest` suite (`test_admin.py`, `test_shop_flows.py`, `test_database.py`, `test_manual_pricing.py`, `test_catalog_import.py`, `test_runtime.py`; 110 tests total).
- `data/` — Runtime SQLite database (`shop.sqlite3`), lock file (`shop.sqlite3.lock`), logs, and `data/media/` for product photos/videos and text catalog.
- `scripts/` — Standalone maintenance utilities (`download_existing_media.py` downloads product media from Telegram to `data/media/`).
- `docs/` — Project review notes (`PROJECT_REVIEW.md`) and implementation plans.

## 5. Key Modules and How They Connect
- `run.py` -> `src/supplement_shop/main.py`: Parses CLI args (`bots`, `migrate`, `seed-products`), acquires cross-process file lock (`runtime.database_lock`), initializes `Database`, `BotClient`s, `CustomerBot`, `AdminBot`, and starts 3 threads (`buyer-bot`, `admin-bot`, `order-expiry`).
- `catalog_import.py`: Parses `data/media/Цены и название.txt` and pairs it with corresponding PNG product photos.
- `config.py`: Loads `.env` manually (`load_dotenv`) and returns frozen `Settings` dataclass.
- `runtime.py`: Provides `database_lock(path)` using `msvcrt.locking` (Windows) or `fcntl.flock` (POSIX) on `<db>.lock` to prevent concurrent bot/CLI instances.
- `telegram_api.py`: `BotClient` wraps `urllib.request` for Telegram Bot API and `run_polling` loop.
- `database.py`: `Database` class managing schema creation, catalog/cart/order queries, payment guards, and notification queues.
- `pricing.py`: `Decimal`-based money parsing/formatting, upward rounding (`ROUND_CEILING`) to whole units.
- `customer_bot.py` & `admin_bot.py`: Update handlers for buyer and admin workflows.

## 6. Coding Conventions Actually Used Here
- `from __future__ import annotations` at the top of every Python module.
- **Money representation**: Base RUB and fiat amounts are stored as integer minor units (`price_rub_minor`, `total_rub_minor`, `payable_minor` in kopeks/cents). Converted card and crypto payable amounts are rounded **upward** (`ROUND_CEILING`) to whole currency/token units (`ceil_currency`). Stars (`price_stars`, `total_stars`) are whole integers.
- **Formatting & escaping**: All Telegram messages use `parse_mode="HTML"`. Dynamic values are escaped via `formatting.h()`. Long messages are split at 3500 UTF-16 code units with balanced HTML tags via `formatting.split_html()`.
- **Language**: Code identifiers and docstrings are in English; user-facing bot strings, error messages in `ValueError`, and CLI output are in Russian.
- **State management**: In-memory dicts (`self.checkout_sessions` in `CustomerBot`, `self.sessions` / `self.order_views` in `AdminBot`) hold multi-step dialog state; everything durable lives in SQLite.

## 7. Known Pitfalls and Oddities
- **Git repository has no commits**: `.git` is initialized on branch `master`, but all project files are currently untracked (`git status` shows `??` for everything).
- **README vs. code discrepancy on checkout & manual card flow**:
  - `README.md` states checkout asks for phone number and immediately displays payment details for card/crypto.
  - **Actual code** (`customer_bot.py` + `test_shop_flows.py`): Checkout asks for `@username` (`telegram_tag`) and explicit contact consent (`checkout_consent`), leaving `phone=""`. Furthermore, for manual card orders (`payment_method == 'card'`), `CustomerBot.finish_checkout_from_payment_choice` sets status to `awaiting_payment` with `payment_details_sent = 0` and tells the buyer to wait for the manager; an admin must click `💳 Отправить реквизиты` (`a:sendcard:<id>`) in `AdminBot` to send the card details (whereas crypto orders show wallet details immediately).
- **Dual media `file_id`s**: Because buyer and admin bots have different tokens, Telegram `file_id`s are bot-specific. `products` stores admin `media_file_id`, local disk `media_path` (`data/media/`), and caches `buyer_media_file_id` after the buyer bot uploads the local file once.
- **Legacy DB columns**: `products.stock` is kept in SQLite for backward compatibility (`DEFAULT 0`), but inventory is not tracked or deducted; sales are toggled manually via `products.is_active`.
- **Real Stars in Demo mode**: Even after `reset-demo` (`manual_payments_demo=1`), if `.env` has `PAYMENT_MODE=stars`, Telegram Stars invoices charge real `XTR`. Only `PAYMENT_MODE=mock` simulates Stars locally.
- **Successful payment retry behavior**: In `telegram_api.run_polling`, if `handler(update)` raises an exception on a `successful_payment` message, the loop deliberately breaks without advancing `offset` so the payment event is retried.

## 8. Current State
- **What works**:
  - Full customer and admin bot flows, multi-currency pricing, frozen order payment snapshots (`payment_snapshot`), 3-hour order expiration, in-flight Stars payment protection (`payment_guards` / `payment_check` status), unmatched payment logging (`payment_events`), and retryable notification outboxes.
  - All 107 unit tests (`unittest`) and `compileall` pass (`data/verification-test-error.log`).
  - Local SQLite DB (`data/shop.sqlite3`) and multiple backups exist in `data/`; bots have been tested against live Telegram bot tokens (`data/bots-output.log`).
- **What is unfinished / manual by design** (per `docs/PROJECT_REVIEW.md`):
  - No automated bank or blockchain API verification (managers verify receipts/tx hashes manually).
  - No automated exchange-rate fetching (rates are set manually in `/admin`).
  - No external backup scheduler, admin audit log, OS service supervisor, catalog search, or saved customer profiles.
