from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or ROOT_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            raise ValueError(f"ADMIN_IDS contains a non-numeric value: {item!r}") from None
    return result


@dataclass(frozen=True)
class Settings:
    buyer_bot_token: str
    admin_bot_token: str
    admin_ids: set[int]
    shop_title: str
    db_path: Path
    payment_mode: str
    polling_timeout: int
    telegram_api_base: str

    @property
    def has_bot_tokens(self) -> bool:
        return bool(self.buyer_bot_token and self.admin_bot_token)


def load_settings() -> Settings:
    load_dotenv()

    db_raw = os.getenv("DB_PATH", "data/shop.sqlite3")
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = ROOT_DIR / db_path

    payment_mode = os.getenv("PAYMENT_MODE", "stars").strip().lower()
    if payment_mode not in {"mock", "stars"}:
        raise ValueError("PAYMENT_MODE must be either 'mock' or 'stars'")

    return Settings(
        buyer_bot_token=os.getenv("BUYER_BOT_TOKEN", "").strip(),
        admin_bot_token=os.getenv("ADMIN_BOT_TOKEN", "").strip(),
        admin_ids=parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        shop_title=os.getenv("SHOP_TITLE", "BioMarket").strip() or "BioMarket",
        db_path=db_path,
        payment_mode=payment_mode,
        polling_timeout=int(os.getenv("POLLING_TIMEOUT", "30")),
        telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
    )
