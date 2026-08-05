"""Вспомогательные функции парсинга ответов GGSEL."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    # Строка вида "1 234,56" / "1,234.56" / "199.00 RUB" → число.
    text = "".join(ch for ch in str(value) if ch.isdigit() or ch in ".,-")
    if not text:
        return default
    if "," in text and "." in text:
        text = text.replace(",", "")          # запятая — разделитель тысяч
    elif "," in text:
        text = text.replace(",", ".")          # запятая — десятичный разделитель
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    # ГОСЕЛ/Digiseller иногда отдаёт вложенный объект товара
    # ({'id':..., 'name':..., 'price_rub':...}) вместо строки. Берём имя.
    if isinstance(value, dict):
        for key in ("name_goods", "name_good", "name", "title", "product", "value", "text"):
            inner = value.get(key)
            if inner not in (None, ""):
                return str(inner)
        return default
    if isinstance(value, (list, tuple)):
        parts = [as_str(item) for item in value if item not in (None, "")]
        joined = ", ".join(part for part in parts if part)
        return joined or default
    return str(value)


def pick(data: Any, *keys: str, default: Any = None) -> Any:
    """Возвращает первое непустое значение по списку точных имён полей."""
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, "", [], {}):
                return value
    return default


def scan(
    data: Any,
    substrings,
    *,
    exclude=(),
    numeric: bool = False,
    default: Any = None,
) -> Any:
    """Мягкий поиск значения по ЧАСТИ имени поля (без учёта регистра).

    Нужен как запасной вариант, когда GGSEL называет поля не так, как мы ждём:
    например, сумма заказа может прийти в ``summ``, ``amount_in`` или ``price``.
    """
    if not isinstance(data, dict):
        return default
    subs = [s.lower() for s in substrings]
    exc = [e.lower() for e in exclude]
    fallback = default
    for key, value in data.items():
        kl = str(key).lower()
        if exc and any(e in kl for e in exc):
            continue
        if not any(s in kl for s in subs):
            continue
        if value in (None, "", [], {}):
            continue
        if numeric:
            number = as_float(value, None)  # type: ignore[arg-type]
            if number is None:
                continue
            if number != 0.0:
                return number
            if fallback in (None, default):
                fallback = number
        else:
            return value
    return fallback


def parse_timestamp(value: Any) -> Optional[datetime]:
    """
    Пытается распарсить отметку времени из ответа площадки.

    Поддерживает unix-время (int/float) и ISO-8601 строки.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
