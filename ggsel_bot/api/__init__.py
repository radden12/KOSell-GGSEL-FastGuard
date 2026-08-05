# -*- coding: utf-8 -*-
"""Модуль интеграции с GGSEL Seller API v1."""
from .client import GGSELAPI
from .enums import OrderStatus, SenderType, ACTIVE_ORDER_STATUSES
from .exceptions import (
    GGSELError,
    ApiAuthError,
    ApiForbiddenError,
    ApiNotFoundError,
    ApiConflictError,
    ApiRateLimitError,
    ApiNetworkError,
)
from .models import Shop, Customer, Sender, Order, Message, Ad, Page

__all__ = [
    "GGSELAPI",
    "OrderStatus",
    "SenderType",
    "ACTIVE_ORDER_STATUSES",
    "GGSELError",
    "ApiAuthError",
    "ApiForbiddenError",
    "ApiNotFoundError",
    "ApiConflictError",
    "ApiRateLimitError",
    "ApiNetworkError",
    "Shop",
    "Customer",
    "Sender",
    "Order",
    "Message",
    "Ad",
    "Page",
]
