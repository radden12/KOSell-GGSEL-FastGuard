"""
Официальный партнёрский (каталожный) клиент GGSEL.

ВСЕ эндпоинты взяты СТРОГО из официальной документации GGSel.net API 0.5
(Google-таблица). Ничего не угадано. Авторизация — по токену в query-параметре ``token``.

Базовый хост: https://a.ggsel.com

Документированные запросы:
  GET /partner/paginate/categories?token=&page=1&with[0]=...   — категории со связями
  GET /partner/paginate/goods?token=&page=1                    — товары постранично
  GET /partner/search/goods?token=&filter[id_section]=&count=-1 — поиск товаров по разделу
  GET /partner/first/goods?token=&filter[id_goods]=            — товар по ID Digi
  GET /partner/search/custom_platforms?token=&count=-1         — виртуальные платформы
  GET /partner/first/custom_platform?token=&filter[id]=&with=categories
  GET /partner/paginate/redirects?token=&page=1&count=1000     — редиректы
  GET /currencies                                              — курс валют

ВАЖНО: хост разрешается через DNS, IP не хардкодятся. GGSEL может менять
исходящие IP (например, 111.88.151.151 / 81.26.176.96) — это адреса для
белого списка на стороне пользователя, а не для подключения клиента.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from GGSelAPI.common.exceptions import (
    RequestFailedError,
    UnauthorizedError,
    UnexpectedResponseError,
)

logger = logging.getLogger("GGSelAPI.partner")


class PartnerAPI:
    """Клиент партнёрского API GGSEL (каталог товаров, категории, валюты)."""

    BASE_URL = "https://a.ggsel.com"

    def __init__(
        self,
        token: str,
        base_url: Optional[str] = None,
        request_timeout: float = 30.0,
        user_agent: str = "GGSelCardinal/0.1",
    ) -> None:
        self.token = token
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.request_timeout = request_timeout
        self.user_agent = user_agent
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # Низкоуровневый транспорт
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        query: Dict[str, Any] = {"token": self.token}
        if params:
            query.update(params)
        try:
            response = self._session.get(
                url,
                params=query,
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise RequestFailedError(url, None, str(exc)) from exc

        if response.status_code in (401, 403):
            raise UnauthorizedError()
        if response.status_code >= 400:
            raise RequestFailedError(url, response.status_code, response.text)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise UnexpectedResponseError(f"Ответ {url} не является JSON.") from exc

    @staticmethod
    def _items(payload: Any) -> List[Dict[str, Any]]:
        """Извлекает список из пагинированного/полного ответа площадки."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "items", "result"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    # ------------------------------------------------------------------ #
    # Документированные методы (1-в-1 с таблицей API)
    # ------------------------------------------------------------------ #
    def currencies(self) -> Any:
        """Курс валют. GET /currencies"""
        return self._get("/currencies")

    def paginate_categories(
        self,
        page: int = 1,
        count: Optional[int] = None,
        with_relations: Optional[List[str]] = None,
        content_type_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Категории постранично. GET /partner/paginate/categories"""
        params: Dict[str, Any] = {"page": page}
        if count is not None:
            params["count"] = count
        if with_relations:
            for index, relation in enumerate(with_relations):
                params[f"with[{index}]"] = relation
        if content_type_id is not None:
            params["filter[contentType][id]"] = content_type_id
        return self._items(self._get("/partner/paginate/categories", params))

    def paginate_goods(
        self,
        page: int = 1,
        content_type_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Товары постранично. GET /partner/paginate/goods"""
        params: Dict[str, Any] = {"page": page}
        if content_type_id is not None:
            params["filter[category.contentType][id]"] = content_type_id
        return self._items(self._get("/partner/paginate/goods", params))

    def search_goods_by_section(self, id_section: Any, count: int = -1) -> List[Dict[str, Any]]:
        """Поиск товаров по ID раздела. GET /partner/search/goods"""
        params = {"filter[id_section]": id_section, "count": count}
        return self._items(self._get("/partner/search/goods", params))

    def first_good(self, id_goods: Any) -> Dict[str, Any]:
        """Информация о товаре по ID Digi. GET /partner/first/goods"""
        payload = self._get("/partner/first/goods", {"filter[id_goods]": id_goods})
        if isinstance(payload, dict):
            data = payload.get("data")
            return data if isinstance(data, dict) else payload
        return {}

    def custom_platforms(self, count: int = -1) -> List[Dict[str, Any]]:
        """Виртуальные платформы. GET /partner/search/custom_platforms"""
        return self._items(self._get("/partner/search/custom_platforms", {"count": count}))

    def custom_platform_categories(self, platform_id: Any) -> Dict[str, Any]:
        """Категории виртуальной платформы. GET /partner/first/custom_platform"""
        payload = self._get(
            "/partner/first/custom_platform",
            {"filter[id]": platform_id, "with": "categories"},
        )
        if isinstance(payload, dict):
            data = payload.get("data")
            return data if isinstance(data, dict) else payload
        return {}

    def redirects(self, page: int = 1, count: int = 1000) -> List[Dict[str, Any]]:
        """Список настроенных редиректов. GET /partner/paginate/redirects"""
        return self._items(self._get("/partner/paginate/redirects", {"page": page, "count": count}))

    def check_token(self) -> bool:
        """Лёгкая проверка токена: дёргаем первую страницу категорий.

        Бросает UnauthorizedError при неверном токене.
        """
        self.paginate_categories(page=1)
        return True
