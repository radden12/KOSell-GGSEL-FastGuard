# -*- coding: utf-8 -*-
"""Персистентное состояние поллера (JSON): последние ID заказов/сообщений."""
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

log = logging.getLogger("ggsel.state")


class StateStore:
    """Потокобезопасное хранилище состояния."""

    def __init__(self, path: str = os.path.join("storage", "state.json")):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {
            "primed": False,
            "last_order_id": 0,
            # chat_id (str) -> last_message_id (int)
            "chats_last_message": {},
            # чаты, которым уже отправлено приветствие
            "greeted_chats": [],
            # order_id (str) -> последний известный статус
            "order_status": {},
            # заказы, по которым уже была автовыдача
            "delivered_orders": [],
        }
        self.load()

    def load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        self._data.update(loaded)
                except (ValueError, OSError) as exc:
                    log.warning("Не удалось загрузить state.json: %s", exc)

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # --------------------------------------------------------------- getters
    @property
    def primed(self) -> bool:
        with self._lock:
            return bool(self._data.get("primed", False))

    def set_primed(self, value: bool = True) -> None:
        with self._lock:
            self._data["primed"] = bool(value)
            self.save()

    @property
    def last_order_id(self) -> int:
        with self._lock:
            return int(self._data.get("last_order_id", 0) or 0)

    def set_last_order_id(self, value: int) -> None:
        with self._lock:
            self._data["last_order_id"] = int(value)
            self.save()

    def get_chat_last_message(self, chat_id: int) -> int:
        with self._lock:
            return int(self._data.get("chats_last_message", {}).get(str(chat_id), 0) or 0)

    def set_chat_last_message(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            self._data.setdefault("chats_last_message", {})[str(chat_id)] = int(message_id)
            self.save()

    def is_known_chat(self, chat_id: int) -> bool:
        with self._lock:
            return str(chat_id) in self._data.get("chats_last_message", {})

    def is_greeted(self, chat_id: int) -> bool:
        with self._lock:
            return str(chat_id) in set(map(str, self._data.get("greeted_chats", [])))

    def mark_greeted(self, chat_id: int) -> None:
        with self._lock:
            lst = self._data.setdefault("greeted_chats", [])
            if str(chat_id) not in set(map(str, lst)):
                lst.append(str(chat_id))
                self.save()

    def get_order_status(self, order_id: int) -> Optional[str]:
        with self._lock:
            return self._data.get("order_status", {}).get(str(order_id))

    def set_order_status(self, order_id: int, status: str) -> None:
        with self._lock:
            self._data.setdefault("order_status", {})[str(order_id)] = status
            self.save()

    def is_delivered(self, order_id: int) -> bool:
        with self._lock:
            return str(order_id) in set(map(str, self._data.get("delivered_orders", [])))

    def mark_delivered(self, order_id: int) -> None:
        with self._lock:
            lst = self._data.setdefault("delivered_orders", [])
            if str(order_id) not in set(map(str, lst)):
                lst.append(str(order_id))
                self.save()

    # ------------------------------------------------------- generic helpers
    def get_value(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set_value(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self.save()

    def list_contains(self, key: str, value) -> bool:
        with self._lock:
            return str(value) in set(map(str, self._data.get(key, [])))

    def add_to_list(self, key: str, value) -> None:
        with self._lock:
            items = self._data.setdefault(key, [])
            if str(value) not in set(map(str, items)):
                items.append(str(value))
                self.save()
