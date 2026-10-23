# Manual Prices and Demo Shop Implementation Plan

> Execute inline using Superpowers TDD and verification-before-completion. A separate reviewer checks runtime and payment durability.

**Goal:** Independent integer RUB/Stars prices, no markups, understandable emoji navigation, and a freshly seeded demo database with real Stars invoices retained.

**Architecture:** Keep the existing stdlib SQLite/Telegram architecture. Prices remain integer minor units internally; manual Stars have an explicit confirmation flag. Demo transfers are a separate setting and frozen order attribute, never an implicit successful payment.

**Spec:** User request in this conversation, 2026-10-17.
**Tech Stack:** Python >=3.11, sqlite3, unittest, Telegram Bot API.

## Constraints
- Do not change real bot tokens or disable real Stars.
- User explicitly authorized replacing the database; create a consistent backup first.
- No invented live manager handles or usable payment destinations in demo data.
- No commits or unrelated refactors. Existing checkout contains untracked project files, so work in place.
- Round converted final amounts upward to whole currency/token units; rates may be fractional. Preserve precision when displaying historical fractional orders.

## Review Focus
- Existing Stars invoices must retain their original amounts after price edits.
- Changes between payment selection and confirmation must require reconfirmation.
- Disabling demo transfers must not expose dummy details as live payment destinations.
- Demo revenue must not inflate real sales statistics.
- Restart and expiry must not lose paid orders or send messages to invented customers.

## Tasks
- [x] 1. Add failing manual-pricing tests; implement independent product prices, frozen quotes and integer input in pricing.py/database.py/admin_bot.py/admin_settings.py. Run unit tests.
- [x] 2. Add demo reset tests; implement demo_data.py, guarded reset-demo CLI and explicit demo transfer snapshots. Populate 12 sample products, sample countries/rates and payment options without real transfers. Run tests.
- [x] 3. Add emoji/menu tests; improve keyboards, checkout summaries and admin readiness. Update documentation. Address material review findings with regression tests.
- [x] 4. Run full tests/compile/demo, stop the identified bot process, back up and reset the requested database, restart and check process/log health. Record actual results.

## Progress
- Manual prices, demo reset, admin controls, emoji navigation and checkout back implemented with failing-then-passing tests.
- Independent runtime review found payment-in-flight expiry, startup retry, lost notifications and interrupted shutdown. Fixes implemented; regressions cover holds, delivery retry/deduplication and startup failures.
- Final reviewer was unavailable due to account usage limit; final integration review performed locally. It caught demo wallet editing and changed-network selection, both covered by failing-then-passing tests.
- Full suite: 95 tests passed. compileall passed. Local demo checkout passed without network payments.
- No old bot process remained at reset time. Original database had 6 products / 4 orders.
- Backup: data/shop-backup-20261017-093545-8fac05.sqlite3.
- Actual database reset verified: 12 products / 0 orders, all integer RUB and manually confirmed Stars, 10 countries, two USDT networks, all methods enabled, transfers DEMO, PAYMENT_MODE=stars, integrity_check=ok.
- Both Telegram polling workers started successfully, PID 24268. Error log empty at launch. Real payments were not executed during verification.
