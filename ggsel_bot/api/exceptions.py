# -*- coding: utf-8 -*-
"""Исключения слоя GGSEL API."""


class GGSELError(Exception):
    """Базовая ошибка API."""

    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ApiAuthError(GGSELError):
    """401 — токен отсутствует или недействителен."""


class ApiForbiddenError(GGSELError):
    """403 — интеграция неактивна или магазин заблокирован."""


class ApiNotFoundError(GGSELError):
    """404 — ресурс не найден."""


class ApiConflictError(GGSELError):
    """409 — неверный статус или нет активных заказов в чате."""

    def __init__(self, message: str, code: str = None, status_code: int = 409):
        super().__init__(message, status_code)
        self.code = code


class ApiRateLimitError(GGSELError):
    """429 — превышен лимит запросов."""


class ApiNetworkError(GGSELError):
    """Ошибка сети/соединения."""
