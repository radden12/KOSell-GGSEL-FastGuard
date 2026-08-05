from __future__ import annotations

import logging
import threading
from typing import Dict, Set

from .api import SenderType, GGSELError
from .events import Event, EventType

log = logging.getLogger("singlebot.runner")


class Runner:
    """Опрос заказов/чатов GGSEL и генерация событий для плагинов."""

    def __init__(self, core):
        self.core = core
        self.state = core.state
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("GGSEL polling started, interval %.1f sec", self.core.config.poll_interval)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except GGSELError as exc:
                log.warning("GGSEL polling error: %s", exc)
            except Exception:
                log.exception("Unexpected polling failure")
            self._stop.wait(self.core.config.poll_interval)

    def _poll_once(self) -> None:
        api = self.core.api
        primed = self.state.primed
        orders = list(api.iter_orders(max_pages=10))
        max_order_id = self.state.last_order_id
        chat_ids: Set[int] = set()
        chat_names: Dict[int, str] = {}

        for order in orders:
            if order.chat_id:
                chat_id = int(order.chat_id)
                chat_ids.add(chat_id)
                customer = getattr(order, "customer", None)
                name = str(getattr(customer, "name", "") or "").strip()
                if name:
                    chat_names[chat_id] = name
            max_order_id = max(max_order_id, int(order.id or 0))
            old_status = self.state.get_order_status(order.id)

            if primed:
                if int(order.id or 0) > self.state.last_order_id:
                    self._emit(EventType.NEW_ORDER, {"order": order})
                elif old_status is not None and old_status != order.status:
                    self._emit(
                        EventType.ORDER_STATUS_CHANGED,
                        {"order": order, "old_status": old_status},
                    )
            self.state.set_order_status(order.id, order.status)

        if max_order_id > self.state.last_order_id:
            self.state.set_last_order_id(max_order_id)

        for chat_id in chat_ids:
            try:
                self._poll_chat(chat_id, primed, chat_names.get(chat_id, ""))
            except GGSELError as exc:
                log.warning("GGSEL chat %s: %s", chat_id, exc)

        if not primed:
            self.state.set_primed(True)
            log.info("Initial GGSEL state primed; new events are now enabled")

    def _poll_chat(self, chat_id: int, primed: bool, customer_name: str = "") -> None:
        was_known = self.state.is_known_chat(chat_id)
        messages = list(self.core.api.get_messages(chat_id).items)
        if not messages:
            return
        messages.sort(key=lambda item: item.id)
        last_seen = self.state.get_chat_last_message(chat_id)
        max_id = last_seen
        new_buyer_messages = []

        for message in messages:
            max_id = max(max_id, int(message.id or 0))
            if (
                int(message.id or 0) > last_seen
                and str(message.sender.type) == SenderType.CLIENT.value
            ):
                new_buyer_messages.append(message)

        if primed and not was_known:
            self._emit(
                EventType.NEW_CHAT,
                {"chat_id": chat_id, "customer_name": customer_name},
            )

        if primed:
            for message in new_buyer_messages:
                payload = {"chat_id": chat_id, "message": message}
                self._emit(EventType.NEW_MESSAGE, payload)
                self._emit(EventType.LAST_CHAT_MESSAGE_CHANGED, payload)
        if max_id > last_seen:
            self.state.set_chat_last_message(chat_id, max_id)

    def _emit(self, event_type: EventType, data: Dict) -> None:
        self.core.bus.emit(Event(type=event_type, data=data, cardinal=self.core))
