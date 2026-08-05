"""Перечисления уровня API GGSEL."""
from __future__ import annotations

from enum import Enum, auto


class EventType(Enum):
    """Типы событий, которые порождает :class:`GGSelAPI.updater.runner.Runner`."""

    #: Первичная инициализация: получен стартовый снимок чатов.
    INITIAL_CHATS = auto()
    #: Список чатов изменился (появился новый чат или новое последнее сообщение).
    CHATS_CHANGED = auto()
    #: В существующем чате новое сообщение от покупателя.
    NEW_MESSAGE = auto()
    #: Первичная инициализация: получен стартовый снимок заказов.
    INITIAL_ORDERS = auto()
    #: Появился новый заказ.
    NEW_ORDER = auto()
    #: У заказа изменился статус.
    ORDER_STATUS_CHANGED = auto()
    #: Первичная инициализация: получен стартовый снимок отзывов.
    INITIAL_REVIEWS = auto()
    #: Покупатель оставил новый отзыв.
    NEW_REVIEW = auto()


# Числовые состояния invoice_state из документации Digiseller/GGSEL:
#   1 — ожидает оплаты, 2 — отмена, 3 — успешная оплата,
#   4 — просрочен, 5 — возврат, 35 — возврат (не завершён покупателем).
_INVOICE_STATE_MAP = {
    1: "pending", 2: "closed", 3: "paid",
    4: "pending", 5: "refunded", 35: "refunded",
}


class OrderStatus(Enum):
    """Статусы заказа на GGSEL."""

    PENDING = "pending"      # ожидает оплаты
    PAID = "paid"            # оплачен
    DELIVERED = "delivered"  # товар выдан
    REFUNDED = "refunded"    # возврат
    DISPUTE = "dispute"      # открыт спор
    CLOSED = "closed"        # завершён

    @classmethod
    def from_raw(cls, value) -> "OrderStatus":
        if value is None or value == "":
            return cls.PENDING
        # invoice_state приходит числом — маппим по документации.
        try:
            mapped = _INVOICE_STATE_MAP.get(int(value))
            if mapped:
                return cls(mapped)
        except (TypeError, ValueError):
            pass
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.PENDING


class MessageAuthor(Enum):
    """Кто отправил сообщение в чате."""

    BUYER = "buyer"
    SELLER = "seller"
    SYSTEM = "system"
