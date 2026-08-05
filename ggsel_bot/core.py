from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api import GGSELAPI
from .events import Event, EventBus, EventType
from .panel import BotPanel
from .plugin_manager import PluginManager
from .runner import Runner
from .storage import StateStore

log = logging.getLogger("singlebot.core")
BOT_VERSION = "1.2.0"


class SingleBotCore:
    """Однопользовательское ядро с совместимостью GGSEL Cardinal plugins."""

    BIND_TO_PRE_INIT = EventType.PRE_INIT
    BIND_TO_INIT = EventType.INIT
    BIND_TO_NEW_CHAT = EventType.NEW_CHAT
    BIND_TO_NEW_ORDER = EventType.NEW_ORDER
    BIND_TO_NEW_MESSAGE = EventType.NEW_MESSAGE
    BIND_TO_LAST_CHAT_MESSAGE_CHANGED = EventType.LAST_CHAT_MESSAGE_CHANGED
    BIND_TO_ORDER_STATUS_CHANGED = EventType.ORDER_STATUS_CHANGED

    def __init__(self, settings):
        self.config = settings
        self.bus = EventBus()
        self.events = self.bus
        self.state = StateStore(path=str(settings.state_path))
        self.api = GGSELAPI(
            base_url=settings.ggsel_base_url,
            api_key=settings.ggsel_api_key,
            seller_id=settings.ggsel_seller_id,
            request_timeout=12.0,
            retry_total=1,
        )
        self.account = self.api.account
        self.input_claim_hooks: list[Any] = []
        self.telegram = BotPanel(self)
        self.plugins = PluginManager(
            self,
            bundled_dir=settings.plugins_dir,
            user_dir=settings.user_plugins_dir,
            state_dir=settings.config_dir,
        )
        self.runner = Runner(self)
        # Aliases frequently used by plugins from GGSEL Unified / Cardinal.
        self.bot = self.telegram.bot
        self.plugin_manager = self.plugins
        self.manager = self.plugins
        self.ggsel_ready = False
        self._restart_lock = threading.Lock()
        self._file_lock = threading.RLock()
        self._ad_cache: Dict[int, Any] = {}
        self._ad_cache_ts: Dict[int, float] = {}
        self._ad_cache_ttl = 180.0
        self.products_dir = settings.data_dir / "products"
        self.products_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        self._check_ggsel()
        self.bus.emit(Event(type=EventType.PRE_INIT, cardinal=self))
        self.plugins.load_all()
        self.telegram.finalize_handlers()
        self.bus.emit(Event(type=EventType.INIT, cardinal=self))
        runner_thread = threading.Thread(
            target=self.runner.run,
            daemon=True,
            name="ggsel-event-poller",
        )
        runner_thread.start()
        try:
            self.telegram.prepare_owner_menu()
            self.telegram.notify_admins(
                "🚀 <b>Личный GGSEL бот запущен</b> · v" + BOT_VERSION + "\n"
                + ("GGSEL подключён." if self.ggsel_ready else "Проверьте GGSEL-ключ в логах."),
                kind="bot_start",
            )
            self.telegram.run_polling()
        finally:
            self.runner.stop()

    def _check_ggsel(self) -> None:
        try:
            shop = self.api.check()
            self.ggsel_ready = True
            log.info("GGSEL connected: %s", shop.title)
        except Exception as exc:
            self.ggsel_ready = False
            log.exception("GGSEL connection check failed: %s", exc)

    # ------------------------------------------------------------------ legacy
    def add_handler(self, bind: Any, handler) -> None:
        """Legacy API: core.add_handler(core.BIND_TO_..., lambda core, event: ...)."""
        event_type = bind if isinstance(bind, EventType) else self._event_type_from_legacy(bind)
        if event_type is None:
            raise ValueError("Неизвестный тип события плагина: " + str(bind))

        def wrapped(event):
            return handler(self, event)

        self.bus.register(event_type, wrapped)

    @staticmethod
    def _event_type_from_legacy(value: Any):
        text = str(getattr(value, "name", value) or "").upper()
        aliases = {
            "PRE_INIT": EventType.PRE_INIT,
            "BIND_TO_PRE_INIT": EventType.PRE_INIT,
            "INIT": EventType.INIT,
            "BIND_TO_INIT": EventType.INIT,
            "NEW_CHAT": EventType.NEW_CHAT,
            "BIND_TO_NEW_CHAT": EventType.NEW_CHAT,
            "NEW_ORDER": EventType.NEW_ORDER,
            "BIND_TO_NEW_ORDER": EventType.NEW_ORDER,
            "NEW_MESSAGE": EventType.NEW_MESSAGE,
            "BIND_TO_NEW_MESSAGE": EventType.NEW_MESSAGE,
            "LAST_CHAT_MESSAGE_CHANGED": EventType.LAST_CHAT_MESSAGE_CHANGED,
            "BIND_TO_LAST_CHAT_MESSAGE_CHANGED": EventType.LAST_CHAT_MESSAGE_CHANGED,
            "ORDER_STATUS_CHANGED": EventType.ORDER_STATUS_CHANGED,
        }
        return aliases.get(text)

    def send_message(self, chat_id: Any, text: str, _chat_name: Any = None):
        return self.api.send_message(int(chat_id), str(text))

    def reload_api(self) -> None:
        self.api = GGSELAPI(
            base_url=self.config.ggsel_base_url,
            api_key=self.config.ggsel_api_key,
            seller_id=self.config.ggsel_seller_id,
            request_timeout=12.0,
            retry_total=1,
        )
        self.account = self.api.account
        self._check_ggsel()

    def get_ad_cached(self, ad_id: int) -> Optional[Any]:
        if not ad_id:
            return None
        now = time.monotonic()
        cached = self._ad_cache.get(int(ad_id))
        if cached is not None and now - self._ad_cache_ts.get(int(ad_id), 0.0) < self._ad_cache_ttl:
            return cached
        try:
            value = self.api.get_ad(int(ad_id))
            self._ad_cache[int(ad_id)] = value
            self._ad_cache_ts[int(ad_id)] = now
            return value
        except Exception:
            log.debug("Unable to load ad %s", ad_id, exc_info=True)
            return cached

    @staticmethod
    def _safe_delivery_name(name: str) -> str:
        keep = "-_.() "
        cleaned = "".join(char for char in str(name or "goods") if char.isalnum() or char in keep).strip()
        return (cleaned.replace(" ", "_") or "goods")[:120]

    def create_delivery_file(self, name: str, items: List[str]) -> str:
        with self._file_lock:
            safe = self._safe_delivery_name(name)
            path = self.products_dir / (safe + ".txt")
            counter = 1
            while path.exists():
                path = self.products_dir / f"{safe}_{counter}.txt"
                counter += 1
            clean = [str(item).strip() for item in items if str(item).strip()]
            path.write_text(("\n".join(clean) + ("\n" if clean else "")), encoding="utf-8")
            return str(path)

    def pop_delivery_item(self, file_path: str) -> Optional[str]:
        with self._file_lock:
            path = Path(str(file_path or ""))
            if not path.is_file():
                return None
            items = [line.rstrip("\n") for line in path.read_text("utf-8").splitlines() if line.strip()]
            if not items:
                return None
            first, rest = items[0], items[1:]
            path.write_text(("\n".join(rest) + ("\n" if rest else "")), encoding="utf-8")
            return first

    def unshift_delivery_item(self, file_path: str, item: str) -> None:
        with self._file_lock:
            path = Path(str(file_path or ""))
            if not str(path):
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text("utf-8") if path.exists() else ""
            path.write_text(str(item) + "\n" + existing, encoding="utf-8")

    def count_delivery_items(self, file_path: str) -> int:
        with self._file_lock:
            path = Path(str(file_path or ""))
            if not path.is_file():
                return 0
            return sum(1 for line in path.read_text("utf-8").splitlines() if line.strip())

    def append_delivery_items(self, file_path: str, items: List[str]) -> None:
        clean = [str(item).strip() for item in items if str(item).strip()]
        if not file_path or not clean:
            return
        with self._file_lock:
            path = Path(str(file_path))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(clean) + "\n")

    def request_restart(self, delay: float = 1.2) -> None:
        """Чистый перезапуск Python-процесса внутри текущего контейнера."""
        if not self._restart_lock.acquire(blocking=False):
            return

        def restart_worker():
            time.sleep(max(0.2, float(delay)))
            try:
                self.runner.stop()
            except Exception:
                pass
            try:
                self.telegram.bot.stop_polling()
            except Exception:
                pass
            logging.shutdown()
            argv = [sys.executable] + sys.argv
            os.execv(sys.executable, argv)

        threading.Thread(target=restart_worker, daemon=True, name="singlebot-restart").start()
