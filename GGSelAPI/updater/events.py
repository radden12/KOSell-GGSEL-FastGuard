"""
События площадки.

Каждое событие несёт тип (:class:`GGSelAPI.common.enums.EventType`) и полезную нагрузку.
Ядро маршрутизирует события по типу на соответствующие списки обработчиков.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from GGSelAPI.common.enums import EventType, OrderStatus
from GGSelAPI.types import Chat, Message, Order, Review


@dataclass
class BaseEvent:
    """Базовый класс события."""

    type: EventType


@dataclass
class InitialChatsEvent(BaseEvent):
    """Стартовый снимок чатов (вызывается один раз при старте)."""

    chats: List[Chat]


@dataclass
class ChatsChangedEvent(BaseEvent):
    """Список чатов изменился."""

    chats: List[Chat]


@dataclass
class NewMessageEvent(BaseEvent):
    """Новое сообщение в диалоге."""

    chat: Chat
    message: Message


@dataclass
class InitialOrdersEvent(BaseEvent):
    """Стартовый снимок заказов."""

    orders: List[Order]


@dataclass
class NewOrderEvent(BaseEvent):
    """Появился новый заказ."""

    order: Order


@dataclass
class OrderStatusChangedEvent(BaseEvent):
    """Статус заказа изменился."""

    order: Order
    old_status: OrderStatus
    new_status: OrderStatus


@dataclass
class InitialReviewsEvent(BaseEvent):
    """Стартовый снимок отзывов."""

    reviews: List[Review]


@dataclass
class NewReviewEvent(BaseEvent):
    """Покупатель оставил новый отзыв."""

    review: Review
