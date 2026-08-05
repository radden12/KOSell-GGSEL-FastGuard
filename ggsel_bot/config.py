from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _int_env(*names: str, default: int = 0) -> int:
    raw = _env(*names)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {names[0]} должна быть числом.") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int
    ggsel_api_key: str
    ggsel_seller_id: str
    ggsel_base_url: str
    poll_interval: float
    data_dir: Path
    config_dir: Path
    state_path: Path
    plugins_dir: Path
    user_plugins_dir: Path

    # Совместимость с плагинами основной сборки GGSEL Cardinal.
    @property
    def base_url(self) -> str:
        return self.ggsel_base_url

    @property
    def api_key(self) -> str:
        return self.ggsel_api_key

    @property
    def seller_id(self) -> str:
        return self.ggsel_seller_id

    @property
    def telegram_token(self) -> str:
        return self.bot_token

    @property
    def admins(self) -> List[int]:
        return [self.owner_id]

    @property
    def shop_title(self) -> str:
        return "Личный GGSEL бот"

    @property
    def is_ggsel_bound(self) -> bool:
        return bool(self.ggsel_api_key and self.ggsel_seller_id)

    def is_notify_enabled(self, _chat_id: int, _kind: str = "other") -> bool:
        return True

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_env("DATA_DIR", default="/app/data")).expanduser().resolve()
        config_dir = data_dir / "configs"
        state_path = data_dir / "state.json"
        plugins_dir = Path(_env("PLUGINS_DIR", default="plugins")).expanduser().resolve()
        user_plugins_dir = data_dir / "plugins"

        token = _env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
        owner_id = _int_env("OWNER_TELEGRAM_ID", "OWNER_ID")
        api_key = _env("GGSEL_API_KEY")
        seller_id = _env("GGSEL_SELLER_ID", "GGSEL_EMAIL")
        base_url = _env(
            "GGSEL_BASE_URL",
            default="https://seller.ggsel.com/api_sellers/api",
        ).rstrip("/")
        try:
            poll_interval = max(2.0, float(_env("POLL_INTERVAL", default="6")))
        except ValueError as exc:
            raise RuntimeError("POLL_INTERVAL должен быть числом.") from exc

        missing = []
        if not token:
            missing.append("BOT_TOKEN")
        if owner_id <= 0:
            missing.append("OWNER_TELEGRAM_ID")
        if not api_key:
            missing.append("GGSEL_API_KEY")
        if not seller_id:
            missing.append("GGSEL_SELLER_ID")
        if missing:
            raise RuntimeError(
                "Не заданы обязательные переменные Bothost: " + ", ".join(missing)
            )

        data_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        plugins_dir.mkdir(parents=True, exist_ok=True)
        user_plugins_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            bot_token=token,
            owner_id=owner_id,
            ggsel_api_key=api_key,
            ggsel_seller_id=seller_id,
            ggsel_base_url=base_url,
            poll_interval=poll_interval,
            data_dir=data_dir,
            config_dir=config_dir,
            state_path=state_path,
            plugins_dir=plugins_dir,
            user_plugins_dir=user_plugins_dir,
        )
