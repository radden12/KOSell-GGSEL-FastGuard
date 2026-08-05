"""
Приёмник вебхуков GGSel о новых сообщениях покупателей.

ВАЖНО: GGSel НЕ отдаёт новые сообщения через опрос API. Вместо этого он
сам шлёт вебхук (GET или POST) на URL, указанный в кабинете продавца:
«Настройки API → URL для получения сообщений». Именно так работает их родной
бот уведомлений (мгновенный push, а не опрос).

Поля (GET — в query-строке, POST — в JSON-теле):
    MessageId   — id сообщения (может быть null)
    DebateId    — id диалога/сообщения от покупателя
    OwnerId     — id владельца (может быть null)
    MessageDate — дата сообщения
    Message     — текст сообщения (в GET — до 500 символов)
    InvoiceId   — номер заказа
    ImagePath   — URL вложения (пусто, если нет)

Сервер построен только на стандартной библиотеке (http.server) — без доп. зависимостей.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("Webhook")

# Исходящие IP-адреса GGSel/Digiseller, с которых приходят вебхуки
# (обновление площадки от 10.06). Если приёмник вебхуков стоит за
# firewall/whitelist — обязательно разрешите входящие соединения с этих адресов,
# иначе push-уведомления о сообщениях покупателей доходить не будут.
GGSEL_WEBHOOK_SOURCE_IPS = ("111.88.151.151", "81.26.176.96")


@dataclass
class WebhookMessage:
    """Разобранное сообщение из вебхука GGSel."""

    invoice_id: str = ""
    debate_id: str = ""
    message_id: str = ""
    owner_id: str = ""
    date: str = ""
    text: str = ""
    image_path: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_payload(self) -> bool:
        """Есть ли хоть что-то полезное (текст, вложение или номер заказа)."""
        return bool(self.text or self.image_path or self.invoice_id or self.debate_id)


def _first(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if value is None:
        return ""
    return str(value)


def _ci_get(data: Dict[str, Any], *names: str) -> str:
    """Регистронезависимый поиск по ключам (GGSel может менять регистр)."""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        if name.lower() in lowered:
            return _first(lowered[name.lower()])
    return ""


def parse_webhook_payload(data: Dict[str, Any]) -> WebhookMessage:
    """Разбирает query/JSON GGSel в :class:`WebhookMessage` (толерантно к именам)."""
    text = _ci_get(data, "Message", "message", "MessageText", "text")
    image = _ci_get(data, "ImagePath", "image_path", "image")
    if image in ("null", "None"):
        image = ""
    return WebhookMessage(
        invoice_id=_ci_get(data, "InvoiceId", "invoice_id", "InvoiceID", "invoice", "inv", "ID_I"),
        debate_id=_ci_get(data, "DebateId", "debate_id", "ID_D"),
        message_id=_ci_get(data, "MessageId", "message_id", "ID_M"),
        owner_id=_ci_get(data, "OwnerId", "owner_id"),
        date=_ci_get(data, "MessageDate", "message_date", "date"),
        text=text,
        image_path=image,
        raw=dict(data),
    )


class _Handler(BaseHTTPRequestHandler):
    """Обработчик HTTP: принимает GET и POST, отвечает 200 OK."""

    server_version = "GGSelCardinalWebhook/1.0"

    # ------------------------------------------------------------------ #
    def _path_ok(self) -> bool:
        expected = getattr(self.server, "path_prefix", "") or ""
        if not expected:
            return True
        return urlparse(self.path).path.rstrip("/") == expected.rstrip("/")

    def _secret_ok(self, params: Dict[str, Any]) -> bool:
        secret = getattr(self.server, "secret", "") or ""
        if not secret:
            return True
        provided = _ci_get(params, "secret", "token", "key")
        return provided == secret

    def _dispatch(self, data: Dict[str, Any]) -> None:
        try:
            msg = parse_webhook_payload(data)
            if not msg.has_payload:
                logger.warning("\u26a0\ufe0f Вебхук без полезных данных: %s", data)
                return
            callback: Optional[Callable[[WebhookMessage], None]] = getattr(self.server, "on_message", None)
            if callback is not None:
                callback(msg)
        except Exception:
            logger.exception("\u274c Ошибка обработки вебхука")

    def _reply(self, code: int, body: bytes = b"OK") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802 (имя требует http.server)
        if not self._path_ok():
            self._reply(404, b"not found")
            return
        params = parse_qs(urlparse(self.path).query)
        if not self._secret_ok(params):
            self._reply(403, b"forbidden")
            return
        self._dispatch(params)
        self._reply(200)

    def do_POST(self) -> None:  # noqa: N802
        if not self._path_ok():
            self._reply(404, b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        data: Dict[str, Any] = {}
        if body:
            decoded = body.decode("utf-8", "ignore")
            try:
                parsed = json.loads(decoded)
                if isinstance(parsed, dict):
                    data = parsed
            except ValueError:
                data = parse_qs(decoded)
        # Секрет может прийти и в query, и в теле.
        query = parse_qs(urlparse(self.path).query)
        if not self._secret_ok({**query, **data}):
            self._reply(403, b"forbidden")
            return
        self._dispatch(data)
        self._reply(200)

    def log_message(self, fmt: str, *args: Any) -> None:  # глушим шум в stderr
        logger.debug("webhook %s", fmt % args if args else fmt)


class GGSelWebhookServer:
    """Лёгкий HTTP-сервер приёма вебхуков GGSel в фоновом потоке."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        on_message: Callable[[WebhookMessage], None],
        secret: str = "",
    ) -> None:
        self.host = host or "0.0.0.0"
        self.port = int(port)
        self.path = path or ""
        self.on_message = on_message
        self.secret = secret or ""
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        # Прокидываем настройки в экземпляр сервера — хендлер читает их из self.server.
        httpd.on_message = self.on_message  # type: ignore[attr-defined]
        httpd.path_prefix = self.path  # type: ignore[attr-defined]
        httpd.secret = self.secret  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="ggsel-webhook", daemon=True
        )
        self._thread.start()
        logger.info(
            "\U0001f310 Сервер приёма сообщений GGSel запущен на %s:%s%s",
            self.host, self.port, self.path or "/",
        )

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            logger.exception("Ошибка при остановке веб-сервера вебхуков.")
        finally:
            self._httpd = None
            self._thread = None
