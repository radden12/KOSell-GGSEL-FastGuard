# -*- coding: utf-8 -*-
"""Перечисления GGSEL Seller API."""
from enum import Enum


class OrderStatus(str, Enum):
    """Статусы заказа (enum OrderStatus из документации)."""

    CREATED = "created"
    PROCESSING = "processing"
    WAIT = "wait"
    WORK = "work"
    CONFIRMED = "confirmed"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"

    @classmethod
    def from_value(cls, value):
        try:
            return cls(str(value))
        except ValueError:
            return None


class SenderType(str, Enum):
    """Тип отправителя сообщения."""

    SHOP = "shop"
    CLIENT = "client"
    INTEGRATION = "integration"


# Активные заказы — те, у которых ещё возможна переписка/работа.
ACTIVE_ORDER_STATUSES = [
    OrderStatus.CREATED,
    OrderStatus.PROCESSING,
    OrderStatus.WAIT,
    OrderStatus.WORK,
    OrderStatus.CONFIRMED,
]
