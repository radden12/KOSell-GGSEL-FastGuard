# -*- coding: utf-8 -*-
"""Event-система для ядра и совместимых плагинов GGSEL Cardinal."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List

log = logging.getLogger("ggsel.events")


class EventType(Enum):
    INIT = auto()
    NEW_ORDER = auto()
    ORDER_STATUS_CHANGED = auto()
    NEW_CHAT = auto()
    NEW_MESSAGE = auto()
    LAST_CHAT_MESSAGE_CHANGED = auto()
    PRE_INIT = auto()


@dataclass
class Event:
    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    cardinal: Any = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


Handler = Callable[[Event], None]


class EventBus:
    """Потокобезопасная регистрация и вызов обработчиков событий."""

    def __init__(self):
        self._handlers: Dict[EventType, List[Handler]] = {}
        self._lock = threading.RLock()

    def register(self, event_type: EventType, handler: Handler) -> None:
        if not isinstance(event_type, EventType):
            raise TypeError("event_type должен быть EventType")
        with self._lock:
            handlers = self._handlers.setdefault(event_type, [])
            if handler not in handlers:
                handlers.append(handler)
        log.debug("Обработчик зарегистрирован на %s", event_type.name)

    def on(self, event_type: EventType):
        def decorator(func: Handler) -> Handler:
            self.register(event_type, func)
            return func
        return decorator

    def emit(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._handlers.get(event.type, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка в обработчике события %s: %s", event.type.name, exc)
