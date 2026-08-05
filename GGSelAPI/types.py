"""
Модели данных GGSelAPI.

Dataclass-структуры, которыми оперируют ядро, обработчики и плагины.
Каждая модель строится из «сырого» словаря ответа площадки через ``from_raw``.

Имена полей приведены СТРОГО к документации GGSEL Seller API v1, которая
полностью совпадает с API Digiseller (GGSEL работает на бэкенде Digiseller):

  • Диалоги  GET /debates/v2/chats  → items[]: id_i, email, product,
             last_date, cnt_msg, cnt_new
  • Сообщения GET /debates/v2       → []: id, message, buyer(0/1), seller(0/1),
             deleted, date_written, date_seen, is_file, filename, url, is_img
  • Продажи  GET /seller-last-sales → sales[]: invoice_id, name, amount,
             currency_type/type_curr, profit, date_pay, email, cnt_goods,
             invoice_state   (+ возможен вложенный buyer_info.email)

Где у части аккаунтов имена могут отличаться — оставлен мягкий fallback
(pick + scan), но первыми идут ДОКУМЕНТИРОВАННЫЕ имена.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from GGSelAPI.common.enums import MessageAuthor, OrderStatus
from GGSelAPI.common import utils


# Коды валют Digiseller (type_curr/currency_type) → человекочитаемо.
_CURRENCY_MAP = {
    "WMR": "RUB", "WMRUB": "RUB", "RUR": "RUB", "RUB": "RUB", "643": "RUB",
    "WMZ": "USD", "USD": "USD", "840": "USD",
    "WME": "EUR", "EUR": "EUR", "978": "EUR",
    "WMU": "UAH", "UAH": "UAH", "980": "UAH",
}


def _normalize_currency(value: Any, default: str = "RUB") -> str:
    text = utils.as_str(value, "").strip()
    if not text:
        return default
    return _CURRENCY_MAP.get(text.upper(), text)


@dataclass
class AccountProfile:
    """Профиль авторизованного продавца."""

    seller_id: str
    username: str
    shop_name: str = ""
    balance: float = 0.0
    currency: str = "RUB"
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "AccountProfile":
        return cls(
            seller_id=utils.as_str(data.get("seller_id") or data.get("id")),
            username=utils.as_str(data.get("username") or data.get("name"), "unknown"),
            shop_name=utils.as_str(data.get("shop_name") or data.get("name_shop") or data.get("seller_name")),
            balance=utils.as_float(data.get("balance")),
            currency=utils.as_str(data.get("currency"), "RUB"),
            raw=data,
        )


@dataclass
class Message:
    """Сообщение в диалоге с покупателем (GET /debates/v2)."""

    id: str
    chat_id: str
    author: MessageAuthor
    text: str
    author_name: str = ""
    created_at: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_from_buyer(self) -> bool:
        return self.author is MessageAuthor.BUYER

    @staticmethod
    def _flag(value: Any) -> Optional[bool]:
        """Булево/0-1/«да-нет» -> True/False; иначе None (непонятно)."""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "y", "on", "да", "истина", "t"):
            return True
        if s in ("0", "false", "no", "n", "off", "нет", "ложь", "f", ""):
            return False
        return None

    @classmethod
    def from_raw(cls, chat_id: str, data: Dict[str, Any], seller_id: str = "") -> "Message":
        # Документировано: автор определяется флагами buyer/seller (0|1).
        author: Optional[MessageAuthor] = None

        # 1) Документированные флаги buyer/seller (0|1) — доверяем В ОБЕ
        #    СТОРОНЫ. Раньше учитывался только buyer==1 / seller==1, из-за
        #    чего СВОЁ сообщение продавца (buyer==0) считалось
        #    покупательским. Теперь buyer==0 трактуется как «не покупатель».
        buyer_flag = cls._flag(data.get("buyer")) if "buyer" in data else None
        seller_flag = cls._flag(data.get("seller")) if "seller" in data else None
        if buyer_flag is True:
            author = MessageAuthor.BUYER
        elif seller_flag is True:
            author = MessageAuthor.SELLER
        elif buyer_flag is False:
            # buyer=0 -> это НЕ покупатель, значит писали мы (продавец).
            author = MessageAuthor.SELLER
        elif seller_flag is False:
            # seller=0 и поля buyer нет -> сообщение от покупателя.
            author = MessageAuthor.BUYER

        # 2) Если флагов нет — сверяем id автора с НАШИМ seller_id.
        if author is None and str(seller_id or "").strip():
            sid = str(seller_id).strip()
            for key in (
                "id_user", "user_id", "id_seller", "seller_id", "from_id",
                "sender_id", "author_id", "id_author", "writer_id",
                "id_user_from", "id_from",
            ):
                val = data.get(key)
                if val not in (None, "") and str(val).strip() == sid:
                    author = MessageAuthor.SELLER
                    break

        # 3) Запасные имена флага продавца.
        if author is None:
            for key in ("is_seller", "from_seller", "by_seller", "owner"):
                if key in data:
                    flag = cls._flag(data.get(key))
                    if flag is not None:
                        author = MessageAuthor.SELLER if flag else MessageAuthor.BUYER
                        break

        # 4) Текстовая роль автора (последний резерв).
        if author is None:
            raw_author = utils.as_str(
                data.get("author") or data.get("sender") or data.get("type") or data.get("role"),
                "buyer",
            ).lower().strip()
            if raw_author in ("seller", "продавец", "1", "true", "owner", "admin", "shop", "store", "me"):
                author = MessageAuthor.SELLER
            else:
                author = MessageAuthor.BUYER
        return cls(
            id=utils.as_str(utils.pick(data, "id", "message_id", "id_message", "msg_id")),
            chat_id=chat_id,
            author=author,
            # Документировано: текст в поле message.
            text=utils.as_str(
                utils.pick(data, "message", "text", "msg", "content", "body", "comment")
            ),
            author_name=utils.as_str(utils.pick(data, "author_name", "sender_name", "email")),
            created_at=utils.parse_timestamp(
                utils.pick(data, "date_written", "created_at", "date", "datetime")
            ),
            raw=data,
        )


@dataclass
class Chat:
    """Диалог продавца с покупателем (элемент GET /debates/v2/chats).

    Документированные поля элемента списка:
      id_i (номер заказа = id диалога), email, product, last_date,
      cnt_msg (всего сообщений), cnt_new (непрочитанных).
    """

    id: str
    buyer_id: str
    buyer_name: str
    product: str = ""
    last_date: str = ""
    total_messages: int = 0
    last_message_text: str = ""
    last_message_id: str = ""
    unread: bool = False
    unread_count: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "Chat":
        # id диалога — это номер заказа id_i. Без него диалог не отследить.
        chat_id = utils.pick(data, "id_i", "invoice", "invoice_id", "id_d", "dialog_id", "id")
        if not chat_id:
            chat_id = utils.scan(data, ["id_i", "invoice", "dialog"], exclude=["id_ds", "valid"])

        cnt_new = utils.as_int(
            utils.pick(data, "cnt_new", "count_new", "unread_count", "unread", "new"), 0
        )
        cnt_msg = utils.as_int(
            utils.pick(data, "cnt_msg", "cnt_message", "messages", "count_msg"), 0
        )
        last_date = utils.as_str(
            utils.pick(data, "last_message", "last_date", "date_modify", "date", "modify_date")
        )
        return cls(
            id=utils.as_str(chat_id),
            buyer_id=utils.as_str(utils.pick(data, "id_ds", "buyer_id", "user_id")),
            buyer_name=utils.as_str(
                utils.pick(data, "email", "buyer_email", "buyer", "username"), "покупатель"
            ),
            product=utils.as_str(utils.pick(data, "product", "name", "name_goods")),
            last_date=last_date,
            total_messages=cnt_msg,
            # В списке диалогов last_message — это ДАТА, а не текст;
            # реальный текст добираем из GET /debates/v2. Здесь — только
            # настоящие текстовые поля (если вдруг появятся), иначе пусто.
            last_message_text=utils.as_str(utils.pick(data, "text", "last_text", "message")),
            last_message_id=last_date,  # для обратной совместимости
            unread=cnt_new > 0,
            unread_count=cnt_new,
            raw=data,
        )


def _detect_refund_state(payload: object) -> int | None:
    """Находит возврат/отмену даже во вложенной схеме seller-last-sales.

    GGSEL менял названия полей в разных версиях ответа. Поэтому одного
    top-level ``status`` недостаточно: проверяем invoice_state 2/5/35,
    refund-флаги и refund_amount/return_amount во вложенных объектах.
    """
    refund_words = ("refund", "return", "chargeback", "reversal", "cancel", "void", "возврат", "отмен")
    status_words = ("status", "state", "invoice_state", "pay_status", "payment_status", "order_status", "sale_status")
    false_values = {"", "0", "false", "no", "none", "null", "off", "нет"}

    def walk(value, depth=0):
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                yield str(key or ""), child
                yield from walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value[:100]:
                yield from walk(child, depth + 1)

    for key, value in walk(payload):
        key_low = key.strip().casefold()
        actual = getattr(value, "value", value)
        rendered = str(actual or "").strip().casefold()
        if key_low in {"invoice_state", "state_id", "invoice_status", "status_id"}:
            try:
                code = int(actual)
                if code in {2, 5, 35}:
                    return code
            except (TypeError, ValueError):
                pass
        if any(word in key_low for word in status_words) and any(word in rendered for word in refund_words):
            return 5
        if any(word in key_low for word in refund_words):
            if isinstance(actual, bool) and actual:
                return 5
            if isinstance(actual, (int, float)) and float(actual) != 0:
                return 5
            if rendered not in false_values:
                return 5
    return None


@dataclass
class Order:
    """Заказ/продажа (элемент GET /seller-last-sales)."""

    id: str
    buyer_id: str
    buyer_name: str
    lot_title: str
    amount: float
    currency: str
    status: OrderStatus
    profit: float = 0.0
    chat_id: str = ""
    quantity: int = 1
    created_at: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "Order":
        if isinstance(data, dict):
            for wrap in ("order", "sale", "content", "data", "invoice_info"):
                inner = data.get(wrap)
                if isinstance(inner, dict):
                    merged = dict(data)
                    merged.update(inner)
                    data = merged
                    break
        buyer_info = data.get("buyer_info") if isinstance(data.get("buyer_info"), dict) else {}
        # На реальном GGSEL продажа отдаёт вложенный product: {id, name, price_rub, ...}.
        product_obj = data.get("product") if isinstance(data.get("product"), dict) else {}

        # --- Номер заказа (invoice_id = id_i диалога) ---
        order_id = utils.pick(data, "invoice_id", "invoice", "id_i", "id_sale", "sale_id", "id")
        if not order_id:
            order_id = utils.scan(data, ["invoice", "id_i", "sale"], exclude=["valid", "id_ds", "state"])
        order_id = utils.as_str(order_id)

        # --- Покупатель (email) ---
        buyer = utils.pick(data, "email", "buyer_email", "buyer", "purchaser", "client")
        if not buyer and buyer_info:
            buyer = utils.pick(buyer_info, "email", "account", "phone")
        if not buyer:
            buyer = utils.scan(data, ["email", "buyer", "purchas"], exclude=["id", "cnt", "count"])

        # --- Сумма ---
        amount = utils.as_float(
            utils.pick(data, "amount", "amount_in", "summ", "summa", "sum", "price_rub", "price", "cost"),
            0.0,
        )
        if not amount:
            scanned = utils.scan(
                data, ["amount", "summ", "sum", "price", "cost", "total"],
                exclude=["agent", "count", "cnt", "perc", "commiss", "id", "curr", "usd"],
                numeric=True,
            )
            if scanned is not None:
                amount = utils.as_float(scanned, 0.0)

        if not amount and product_obj:
            amount = utils.as_float(utils.pick(product_obj, "price_rub", "price", "amount", "cost"), 0.0)

        profit = utils.as_float(utils.pick(
            data, "profit", "profit_rub", "seller_profit", "seller_amount",
            "seller_income", "payout", "payout_amount", "income",
            "amount_out", "net_amount", "credited_amount", "receive_amount",
        ), 0.0)
        if not profit:
            scanned_profit = utils.scan(
                data, ["profit", "payout", "income", "seller_amount", "amount_out", "net_amount", "receive"],
                exclude=["percent", "perc", "rate", "id", "count", "cnt"], numeric=True,
            )
            if scanned_profit is not None:
                profit = utils.as_float(scanned_profit, 0.0)
        if not profit and product_obj:
            profit = utils.as_float(utils.pick(
                product_obj, "profit", "profit_rub", "seller_profit", "seller_amount", "payout", "income"
            ), 0.0)

        # --- Статус (invoice_state по документации purchase/info) ---
        state = utils.pick(
            data, "invoice_state", "state", "status", "pay_status",
            "payment_status", "order_status", "sale_status", "status_id",
        )
        refund_state = _detect_refund_state(data)
        if refund_state is not None:
            state = refund_state
        status = OrderStatus.from_raw(state)

        return cls(
            id=order_id,
            buyer_id=utils.as_str(utils.pick(data, "buyer_id", "id_ds", "id_buyer")),
            buyer_name=utils.as_str(buyer, "") or "покупатель",
            lot_title=utils.as_str(
                utils.pick(data, "name", "name_goods", "name_good", "good", "title")
                or utils.pick(product_obj, "name", "title", "name_goods"),
                "",
            ),
            amount=amount,
            profit=profit,
            currency=_normalize_currency(
                utils.pick(data, "currency_type", "type_curr", "currency", "cur", "curr")
            ),
            status=status,
            # Диалог покупателя по этому заказу — это debates id_i = invoice_id.
            chat_id=utils.as_str(utils.pick(data, "id_i", "invoice_id", "invoice") or order_id),
            quantity=utils.as_int(
                utils.pick(data, "cnt_goods", "cnt_item", "quantity", "cnt", "count"), 1
            ),
            created_at=utils.parse_timestamp(
                utils.pick(data, "date_pay", "purchase_date", "created_at", "date", "date_sale")
            ),
            raw=data,
        )


@dataclass
class LotShortcut:
    """Краткая карточка товара (лота) продавца."""

    id: str
    title: str
    price: float
    currency: str = "RUB"
    active: bool = True
    hidden: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "LotShortcut":
        def _truthy(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if value is None:
                return False
            if isinstance(value, (int, float)):
                return value != 0
            return str(value).strip().lower() not in (
                "", "0", "false", "no", "off", "нет", "none", "null"
            )

        def _flag(*keys: str) -> bool:
            return any(key in data and _truthy(data.get(key)) for key in keys)

        hidden = _flag("hidden", "is_hidden", "hide")
        # Скрытым считаем также явно выключенный лот (но ВСЁ равно показываем его в списке).
        if any(k in data and not _truthy(data.get(k)) for k in ("enabled", "active", "in_sale", "visible")):
            hidden = True
        deleted = _flag("del", "deleted", "is_deleted", "removed", "archived")
        return cls(
            id=utils.as_str(
                data.get("id_goods") or data.get("id_d") or data.get("id")
                or data.get("lot_id") or data.get("product_id")
            ),
            title=utils.as_str(
                data.get("name_goods") or data.get("name") or data.get("title")
                or data.get("product")
            ),
            price=utils.as_float(data.get("price") or data.get("price_rub") or data.get("cost")),
            currency=_normalize_currency(data.get("currency") or data.get("type_curr")),
            active=not deleted,
            hidden=hidden,
            raw=data,
        )


@dataclass
class Review:
    """Отзыв покупателя (раздел Reviews)."""

    id: str
    rating: str
    text: str
    product: str = ""
    buyer_name: str = "покупатель"
    created_at: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_negative(self) -> bool:
        return str(self.rating).lower() in ("bad", "negative", "-1", "0")

    @property
    def key(self) -> str:
        if self.id:
            return str(self.id)
        base = f"{self.product}|{self.buyer_name}|{self.text}|{self.created_at}"
        return hashlib.md5(base.encode("utf-8")).hexdigest()

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "Review":
        return cls(
            id=utils.as_str(
                data.get("id") or data.get("review_id") or data.get("id_i") or data.get("invoice")
            ),
            rating=utils.as_str(
                data.get("type") or data.get("rating") or data.get("mark") or data.get("evaluation"),
                "all",
            ),
            text=utils.as_str(
                data.get("info") or data.get("text") or data.get("comment")
                or data.get("message") or data.get("review")
            ),
            product=utils.as_str(
                data.get("product") or data.get("name_good") or data.get("good")
                or data.get("title") or data.get("name")
            ),
            buyer_name=utils.as_str(
                data.get("email") or data.get("buyer") or data.get("name") or data.get("user"),
                "покупатель",
            ),
            created_at=utils.parse_timestamp(
                data.get("date") or data.get("date_written") or data.get("created_at")
            ),
            raw=data,
        )
