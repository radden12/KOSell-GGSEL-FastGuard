"""Исключения уровня GGSelAPI (сеть, авторизация, ответы площадки)."""
from __future__ import annotations

from typing import Optional


class GGSelApiError(Exception):
    """Базовое исключение клиента GGSEL."""


class RequestFailedError(GGSelApiError):
    """HTTP-запрос завершился ошибкой."""

    def __init__(self, url: str, status_code: Optional[int], body: str = ""):
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"Запрос к {url} завершился ошибкой (код {status_code}). {body[:200]}")


class UnauthorizedError(GGSelApiError):
    """Неверный или просроченный ключ авторизации."""

    def __init__(self) -> None:
        super().__init__("Авторизация на GGSEL не удалась: проверьте token / seller_id.")


class UnexpectedResponseError(GGSelApiError):
    """Структура ответа не соответствует ожиданиям."""
