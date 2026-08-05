"""
Runner — цикл опроса площадки.

Работает как генератор: на каждой итерации запрашивает чаты, заказы и отзывы,
сравнивает с предыдущим состоянием и отдаёт разницу в виде событий. Ядро
просто итерируется по этому генератору.

ВАЖНО про устойчивость к схеме GGSel:
Мы НЕ зависим от конкретных имён полей в ответе. Изменение диалога
определяется по хэшу ВСЕГО сырого ответа по диалогу — любое изменение
(новое сообщение, дата, счётчик) меняет подпись. Автор определяется
мягко: уведомляем, если НЕ удалось точно установить, что писал продавец.

Какие ленты опрашивать — управляется тумблерами в конфиге (notify_*).
В консоли видно каждый опрос и каждое событие — это помогает диагностике.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Callable, Dict, Iterator, List, Set, Tuple

from GGSelAPI.account import Account
from GGSelAPI.common.enums import EventType, MessageAuthor, OrderStatus
from GGSelAPI.common.exceptions import GGSelApiError
from GGSelAPI.types import Chat, Message, Order, Review
from GGSelAPI.updater.events import (
    BaseEvent,
    ChatsChangedEvent,
    InitialChatsEvent,
    InitialOrdersEvent,
    InitialReviewsEvent,
    NewMessageEvent,
    NewOrderEvent,
    NewReviewEvent,
    OrderStatusChangedEvent,
)

logger = logging.getLogger("Runner")


def _hash_raw(raw: object) -> str:
    """Стабильный хэш произвольного сырого объекта ответа."""
    try:
        blob = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(raw)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _dump_sample(raw: object, limit: int = 1500) -> str:
    """Короткий JSON-дамп сырого объекта для диагностики реальной схемы."""
    try:
        blob = json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(raw)
    return blob if len(blob) <= limit else blob[:limit] + "…(обрезано)"


class Runner:
    """Генерирует события на основе изменений на площадке."""

    def __init__(self, account: Account, poll_interval: float = 2.0, config=None) -> None:
        self.account = account
        self.config = config
        self.poll_interval = max(1.0, poll_interval)
        self.running = False

        self._known_last_message: Dict[str, str] = {}
        self._known_order_status: Dict[str, OrderStatus] = {}
        self._known_reviews: Set[str] = set()

        self._chats_bootstrapped = False
        self._orders_bootstrapped = False
        self._reviews_bootstrapped = False
        self._poll_no = 0
        self._keys_logged = False
        # Обработанные id сообщений храним НА ДИСКЕ — чтобы после
        # перезапуска НЕ выдать товар/ответ повторно по уже обработанным.
        self._handled_path = os.path.join("storage", "handled_messages.json")
        self._handled_msg_id: Dict[str, int] = self._load_handled()
        self._msg_keys_logged = False

    def stop(self) -> None:
        self.running = False

    # ------------------------------------------------------------------ #
    # Какие ленты опрашивать (тумблеры вкл/выкл из конфига)
    # ------------------------------------------------------------------ #
    def _orders_enabled(self) -> bool:
        c = self.config
        if c is None:
            return True
        return bool(getattr(c, "notify_new_order", True) or getattr(c, "notify_order_status", True))

    def _reviews_enabled(self) -> bool:
        c = self.config
        if c is None:
            return True
        return bool(getattr(c, "notify_new_review", True))

    def listen(self) -> Iterator[BaseEvent]:
        """Бесконечный генератор событий. Останавливается через :meth:`stop`."""
        self.running = True
        network_fails = 0
        logger.info("\u25b6\ufe0f Цикл опроса запущен (интервал %.1fс).", self.poll_interval)
        while self.running:
            try:
                yield from self._poll_once()
                network_fails = 0
            except GGSelApiError as exc:
                status = getattr(exc, "status_code", "?")
                if status in (None, "?"):
                    network_fails += 1
                    logger.warning(
                        "\u26a0\ufe0f Нет связи с API (попытка %s). Ждём и пробуем снова\u2026", network_fails
                    )
                else:
                    logger.error("\u274c Ошибка опроса площадки (HTTP %s): %s", status, exc)
            except Exception:
                network_fails += 1
                logger.exception("\u26a0\ufe0f Сбой в цикле опроса, повторим позже.")
            delay = self.poll_interval
            if network_fails:
                delay = min(60.0, self.poll_interval * (2 ** min(network_fails, 5)))
            time.sleep(delay)

    @staticmethod
    def _safe(label: str, func: Callable[[], List]) -> Tuple[List, bool]:
        """Безопасно вызывает запрос ленты: ошибка одной не ломает остальные."""
        try:
            return func(), True
        except GGSelApiError as exc:
            logger.warning("\u26a0\ufe0f Не удалось получить %s (%s). Продолжаю работу.", label, exc)
            return [], False

    # ------------------------------------------------------------------ #
    def _poll_once(self) -> Iterator[BaseEvent]:
        self._poll_no += 1

        # Диалоги покупателей опрашиваем ВСЕГДА — даже при включённом вебхуке.
        # Вебхук срабатывает только если в кабинете GGSel задан публичный URL,
        # а опрос — надёжный путь и работает сразу. Ошибка ленты диалогов
        # (например, у API-ключа нет права «Переписка с покупателями») больше
        # НЕ ломает заказы и отзывы — вызов обёрнут в _safe().
        chats, _chats_ok = self._safe("диалоги", self.account.get_chats)

        orders: List[Order] = []
        orders_ok = False
        if self._orders_enabled():
            orders, orders_ok = self._safe("заказы", self.account.get_orders)

        reviews: List[Review] = []
        reviews_ok = False
        if self._reviews_enabled():
            reviews, reviews_ok = self._safe("отзывы", self.account.get_reviews)

        # Один раз печатаем РЕАЛЬНЫЕ поля и образец ответа — это сразу покажет,
        # если конкретный аккаунт GGSel называет поля иначе, чем в документации.
        if not self._keys_logged:
            self._keys_logged = True
            if chats and isinstance(chats[0].raw, dict):
                logger.info("\U0001f50e Поля диалога (сырые): %s", sorted(chats[0].raw.keys()))
                logger.info("\U0001f50e Образец диалога: %s", _dump_sample(chats[0].raw))
            if orders and isinstance(orders[0].raw, dict):
                logger.info("\U0001f50e Поля заказа (сырые): %s", sorted(orders[0].raw.keys()))
                logger.info("\U0001f50e Образец заказа: %s", _dump_sample(orders[0].raw))

        # Сколько НОВЫХ элементов в этом опросе (а не сколько всего загружено).
        new_chats = sum(
            1 for c in chats
            if self._chat_key(c) and self._chat_signature(c) != self._known_last_message.get(self._chat_key(c))
        ) if self._chats_bootstrapped else 0
        new_orders = sum(
            1 for o in orders if self._order_key(o) not in self._known_order_status
        ) if self._orders_bootstrapped else 0
        new_reviews = sum(
            1 for r in reviews if r.key not in self._known_reviews
        ) if self._reviews_bootstrapped else 0

        # Числа «загружено» равны размеру страницы запроса (это нормально):
        # бот каждый раз берёт последнюю пачку. Уведомления шлёт ТОЛЬКО по «новым».
        logger.info(
            "\U0001f504 Опрос #%s: диалогов %s (новых %s), заказов %s (новых %s), отзывов %s (новых %s)",
            self._poll_no, len(chats), new_chats, len(orders), new_orders, len(reviews), new_reviews,
        )

        # --- Чаты ---
        if not self._chats_bootstrapped:
            for chat in chats:
                key = self._chat_key(chat)
                if key:
                    self._known_last_message[key] = self._chat_signature(chat)
            self._chats_bootstrapped = True
            logger.info("\U0001f4e5 Стартовый снимок диалогов сохранён (%s).", len(chats))
            yield InitialChatsEvent(type=EventType.INITIAL_CHATS, chats=chats)
            # ВАЖНО: на старте делаем ТОЛЬКО снимок диалогов. Старые чаты НЕ
            # трогаем и НЕ отвечаем в них — реагируем только на сообщения и
            # заказы, которые появились ПОСЛЕ запуска бота.
        else:
            yield from self._diff_chats(chats)

        # --- Заказы ---
        if orders_ok:
            if not self._orders_bootstrapped:
                for order in orders:
                    self._known_order_status[self._order_key(order)] = order.status
                self._orders_bootstrapped = True
                logger.info("\U0001f4e5 Стартовый снимок заказов сохранён (%s).", len(orders))
                yield InitialOrdersEvent(type=EventType.INITIAL_ORDERS, orders=orders)
            else:
                yield from self._diff_orders(orders)

        # --- Отзывы ---
        if reviews_ok:
            if not self._reviews_bootstrapped:
                for review in reviews:
                    self._known_reviews.add(review.key)
                self._reviews_bootstrapped = True
                logger.info("\U0001f4e5 Стартовый снимок отзывов сохранён (%s).", len(reviews))
                yield InitialReviewsEvent(type=EventType.INITIAL_REVIEWS, reviews=reviews)
            else:
                yield from self._diff_reviews(reviews)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _chat_key(chat: Chat) -> str:
        """Стабильный ключ диалога. Пустой → диалог нельзя отслеживать.

        Ключ — ТОЛЬКО реальный id диалога. Без него мы всё равно не сможем
        добрать сообщения диалога, поэтому такой диалог пропускаем (иначе
        пласхолдер buyer_name="buyer" даёт спам и коллизии).
        """
        return chat.id or ""

    @staticmethod
    def _chat_signature(chat: Chat) -> str:
        """Подпись диалога по КОНКРЕТНЫМ полям (маркер последнего
        сообщения + счётчик непрочитанных + превью текста).

        По схеме chats_object список диалогов отдаёт поля: id_i, email,
        product, last_message (ТЕКСТ последнего сообщения), cnt_msg (всего
        сообщений), cnt_new (непрочитанных). Поля даты в списке НЕТ, поэтому
        ловим изменение по ТЕКСТУ последнего сообщения + счётчикам: любое
        новое сообщение меняет last_message и cnt_msg, а сообщение
        покупателя — ещё и cnt_new.
        """
        # Счётчики cnt_msg/cnt_new на реальных аккаунтах бывают null, а
        # last_message — это дата последнего сообщения. Поэтому самый
        # надёжный признак изменения — хэш ВСЕГО сырого объекта диалога:
        # при новом сообщении меняется last_message (дата) → хэш меняется.
        if isinstance(chat.raw, dict) and chat.raw:
            return _hash_raw(chat.raw)
        return f"{chat.total_messages}|{chat.unread_count}|{chat.last_date}|{chat.last_message_text}"

    @staticmethod
    def _msg_id_int(msg) -> int:
        try:
            return int(msg.id)
        except (TypeError, ValueError):
            return 0

    def _diff_chats(self, chats: List[Chat]) -> Iterator[BaseEvent]:
        changed = False
        for chat in chats:
            key = self._chat_key(chat)
            if not key:
                continue
            signature = self._chat_signature(chat)
            previous = self._known_last_message.get(key)
            if signature == previous:
                continue
            changed = True
            self._known_last_message[key] = signature
            if not chat.id:
                continue

            # Добираем РЕАЛЬНЫЕ сообщения диалога (отсортированы по возрастанию id).
            try:
                messages = self.account.get_chat_messages(chat.id, limit=30)
            except GGSelApiError as exc:
                logger.warning("Не удалось добрать сообщения диалога %s: %s", chat.id, exc)
                continue
            if not messages:
                continue

            # Один раз печатаем сырые поля сообщения — видно, как закодирован автор.
            if not self._msg_keys_logged and isinstance(messages[-1].raw, dict):
                self._msg_keys_logged = True
                logger.info("🔎 Поля сообщения (сырые): %s", sorted(messages[-1].raw.keys()))
                logger.info("🔎 Образец сообщения: %s", _dump_sample(messages[-1].raw))

            newest = messages[-1]  # самый свежий по id
            newest_id = self._msg_id_int(newest)
            handled = self._handled_msg_id.get(key, 0)
            self._handled_msg_id[key] = max(handled, newest_id)
            self._save_handled()

            if newest_id and newest_id <= handled:
                continue  # это сообщение уже обработано
            if self.account.was_recently_sent(chat.id, newest.text):
                logger.info("ℹ️ Диалог %s: свежее — наше же сообщение (эхо). Пропускаю.", chat.id)
                continue
            if not newest.is_from_buyer:
                logger.info("ℹ️ Диалог %s обновился, но свежее сообщение — от продавца. Пропускаю.", chat.id)
                continue

            logger.info(
                "💬 Новое сообщение от покупателя в диалоге %s (%s): %s",
                chat.id, chat.buyer_name, (newest.text or "")[:80],
            )
            yield NewMessageEvent(type=EventType.NEW_MESSAGE, chat=chat, message=newest)
        if changed:
            yield ChatsChangedEvent(type=EventType.CHATS_CHANGED, chats=chats)

    # ------------------------------------------------------------------ #
    def _load_handled(self) -> Dict[str, int]:
        """Читает с диска карту {id диалога: обработанный id сообщения}."""
        try:
            with open(self._handled_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {
                str(k): int(v)
                for k, v in data.items()
                if str(v).lstrip("-").isdigit()
            }
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_handled(self) -> None:
        """Сохраняет карту обработанных id на диск (защита от повторной выдачи)."""
        try:
            os.makedirs(os.path.dirname(self._handled_path) or ".", exist_ok=True)
            with open(self._handled_path, "w", encoding="utf-8") as fh:
                json.dump(self._handled_msg_id, fh, ensure_ascii=False)
        except OSError:
            pass

    def _catchup_chats(self, chats: List[Chat]) -> Iterator[BaseEvent]:
        """Стартовая обработка накопившихся диалогов (бот был выключен).

        Для каждого диалога добираем сообщения и, если самое свежее —
        от покупателя и его id больше уже обработанного (сохранён на
        диске), генерируем NewMessageEvent. Сохранение id на диск
        гарантирует, что после перезапуска мы НЕ выдадим товар повторно.
        """
        checked = 0
        fired = 0
        for chat in chats:
            if not chat.id:
                continue
            try:
                messages = self.account.get_chat_messages(chat.id, limit=30)
            except GGSelApiError as exc:
                logger.debug("Догонка: не удалось добрать сообщения диалога %s: %s", chat.id, exc)
                continue
            if not messages:
                continue
            checked += 1
            newest = messages[-1]
            nid = self._msg_id_int(newest)
            handled = self._handled_msg_id.get(chat.id, 0)
            if nid and nid <= handled:
                continue
            self._handled_msg_id[chat.id] = max(handled, nid)
            if self.account.was_recently_sent(chat.id, newest.text):
                continue
            if not newest.is_from_buyer:
                continue
            fired += 1
            logger.info(
                "\U0001f4e6 Догоняем диалог %s (%s): последнее сообщение от покупателя — обрабатываем.",
                chat.id, chat.buyer_name,
            )
            yield NewMessageEvent(type=EventType.NEW_MESSAGE, chat=chat, message=newest)
        self._save_handled()
        logger.info(
            "\u2705 Стартовая проверка диалогов: проверено %s, к обработке %s.",
            checked, fired,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _order_key(order: Order) -> str:
        """Стабильный ключ заказа. Если id не распознан — хэшируем сырой ответ."""
        if order.id:
            return order.id
        if order.raw:
            return "raw:" + _hash_raw(order.raw)
        return f"{order.buyer_name}|{order.lot_title}|{order.amount}"

    def _diff_orders(self, orders: List[Order]) -> Iterator[BaseEvent]:
        for order in orders:
            key = self._order_key(order)
            previous = self._known_order_status.get(key)
            if key not in self._known_order_status:
                self._known_order_status[key] = order.status
                logger.info("\U0001f4b8 Новый заказ #%s", order.id)
                yield NewOrderEvent(type=EventType.NEW_ORDER, order=order)
            elif previous != order.status:
                self._known_order_status[key] = order.status
                logger.info(
                    "\U0001f504 Заказ #%s: %s \u2192 %s",
                    order.id,
                    getattr(previous, "value", previous),
                    getattr(order.status, "value", order.status),
                )
                yield OrderStatusChangedEvent(
                    type=EventType.ORDER_STATUS_CHANGED,
                    order=order,
                    old_status=previous,
                    new_status=order.status,
                )

    def _diff_reviews(self, reviews: List[Review]) -> Iterator[BaseEvent]:
        for review in reviews:
            key = review.key
            if key in self._known_reviews:
                continue
            self._known_reviews.add(key)
            logger.info("\u2b50 Новый отзыв (%s) по «%s»", review.rating, review.product)
            yield NewReviewEvent(type=EventType.NEW_REVIEW, review=review)
