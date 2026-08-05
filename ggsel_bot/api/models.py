# -*- coding: utf-8 -*-
"""Датаклассы-модели ответов GGSEL Seller API."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Shop:
    id: int
    title: str
    integration_id: Optional[int] = None
    api_id: Optional[int] = None
    server_time: Optional[str] = None

    @classmethod
    def from_check(cls, data: Dict[str, Any]) -> "Shop":
        data = data or {}
        shop = data.get("shop") or {}
        integration = data.get("integration") or {}
        return cls(
            id=_to_int(shop.get("id")),
            title=str(shop.get("title", "")),
            integration_id=integration.get("id"),
            api_id=integration.get("api_id"),
            server_time=data.get("ts"),
        )


@dataclass
class Customer:
    id: int
    name: str

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Customer":
        data = data or {}
        return cls(id=_to_int(data.get("id")), name=str(data.get("name", "")))


@dataclass
class Sender:
    id: int
    name: str
    type: str

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Sender":
        data = data or {}
        return cls(
            id=_to_int(data.get("id")),
            name=str(data.get("name", "")),
            type=str(data.get("type", "")),
        )


@dataclass
class Order:
    id: int
    ad_id: Optional[int]
    chat_id: Optional[int]
    customer: Customer
    status: str
    created_at: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Order":
        data = data or {}
        return cls(
            id=_to_int(data.get("id")),
            ad_id=data.get("ad_id"),
            chat_id=data.get("chat_id"),
            customer=Customer.from_dict(data.get("customer")),
            status=str(data.get("status", "")),
            created_at=data.get("created_at"),
            raw=data,
        )


@dataclass
class Message:
    id: int
    created_at: Optional[str]
    sender: Sender
    is_read: bool
    text: str
    attachments: List[Any] = field(default_factory=list)
    options: Any = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        data = data or {}
        return cls(
            id=_to_int(data.get("id")),
            created_at=data.get("created_at"),
            sender=Sender.from_dict(data.get("sender")),
            is_read=bool(data.get("isRead", data.get("is_read", False))),
            text=str(data.get("text", "") or ""),
            attachments=list(data.get("attachments") or []),
            options=data.get("options"),
        )


@dataclass
class Ad:
    id: int
    title: str
    slug: Optional[str]
    type: Optional[str]
    category_id: Optional[int]
    content: Optional[str]
    status: Optional[str]
    views: Optional[int]
    price_amount: Optional[float]
    base_amount: Optional[float]
    currency: str
    stock: Optional[float]
    has_chat: bool
    has_points: bool
    created_at: Optional[str]
    images: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Ad":
        data = data or {}
        price = data.get("price") or {}
        return cls(
            id=_to_int(data.get("id")),
            title=str(data.get("title", "")),
            slug=data.get("slug"),
            type=data.get("type"),
            category_id=data.get("category_id"),
            content=data.get("content"),
            status=data.get("status"),
            views=data.get("views"),
            price_amount=price.get("amount"),
            base_amount=price.get("base_amount"),
            currency=str(price.get("currency", "RUB") or "RUB"),
            stock=data.get("stock"),
            has_chat=bool(data.get("has_chat", False)),
            has_points=bool(data.get("has_points", False)),
            created_at=data.get("created_at"),
            images=list(data.get("images") or []),
            raw=data,
        )

    @property
    def is_visible(self) -> bool:
        """Лот видим покупателям (опубликован, не скрыт и не удалён)."""
        return str(self.status).lower() == "publish"


@dataclass
class Page:
    """Страница с cursor-пагинацией."""

    items: List[Any]
    has_more: bool = False
    per_page: Optional[int] = None
    next_cursor: Optional[str] = None
    prev_cursor: Optional[str] = None

    @classmethod
    def parse(cls, payload: Dict[str, Any], item_cls) -> "Page":
        payload = payload or {}
        data = payload.get("data") or []
        meta = payload.get("meta") or {}
        links = payload.get("links") or {}
        items = [item_cls.from_dict(x) for x in data]
        return cls(
            items=items,
            has_more=bool(meta.get("has_more", False)),
            per_page=meta.get("per_page"),
            next_cursor=links.get("next_cursor"),
            prev_cursor=links.get("prev_cursor"),
        )
