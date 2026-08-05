"""
Клиент GGSEL Seller API: авторизация и типизированные HTTP-методы.

``Account`` — единственная точка, которая работает с сетью для заказов и чатов.
Остальные модули получают уже разобранные модели из :mod:`GGSelAPI.types`.

Эндпоинты взяты из официальной документации GGSEL Seller API v1
(https://seller.ggsel.com/docs/seller-api-v-1). Базовый хост —
``https://seller.ggsel.com/api_sellers/api`` и переопределяется через
конфиг ([GGSEL] base_url).

Авторизация (раздел ApiLogin):
  POST /apilogin  —  тело {email|seller_id, timestamp, sign},
  где sign = SHA256(API_KEY + timestamp). В ответ приходит access-token,
  который далее передаётся параметром ?token=. Идентификатором может быть
  Email продавца или числовой Seller ID — что именно ввёл пользователь.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None  # type: ignore

from GGSelAPI.common.exceptions import (
    GGSelApiError,
    RequestFailedError,
    UnauthorizedError,
    UnexpectedResponseError,
)
from GGSelAPI.types import AccountProfile, Chat, LotShortcut, Message, Order, Review

logger = logging.getLogger("GGSelAPI")


class Account:
    """Авторизованный аккаунт продавца GGSEL."""

    # Базовый хост Seller API. Переопределяется через конфиг [GGSEL] base_url.
    BASE_URL = "https://seller.ggsel.com/api_sellers/api"

    # Эндпоинты (относительно BASE_URL).
    EP_LOGIN = "/apilogin"               # POST: получение access-token
    # Раздел debates (чаты) работает под базовым хостом Seller API
    # /api_sellers/api (ПОДТВЕРЖДЕНО ответом сервера: HTTP 200).
    # В документации пути указаны сокращённо (/api/debates/v2/...),
    # но реальный рабочий адрес — https://<хост>/api_sellers/api/debates/v2/...:
    #   GET  /api_sellers/api/debates/v2/chats  — список диалогов
    #   GET  /api_sellers/api/debates/v2        — сообщения диалога (id_i)
    #   POST /api_sellers/api/debates/v2        — отправка сообщения (id_i)
    EP_CHATS = "/debates/v2/chats"       # GET: список диалогов (на debates-хосте)
    EP_CHAT_MESSAGES = "/debates/v2"     # GET: сообщения диалога (требует id_i)
    EP_SEND_MESSAGE = "/debates/v2"      # POST: отправка сообщения (требует id_i)
    EP_SEEN = "/debates/v2/seen"         # POST: пометить прочитанным (требует id_i)
    EP_ORDERS = "/seller-last-sales"     # GET: последние продажи (заказы)
    EP_REVIEWS = "/reviews"              # GET: отзывы покупателей
    EP_GOODS = "/seller-goods"           # POST: список товаров (лотов) продавца

    # Сколько заказов/чатов/отзывов запрашивать за один опрос.
    ORDERS_TOP = 50
    CHATS_PAGE_SIZE = 100  # максимум по документации — 200
    CHATS_MAX_PAGES = 5
    REVIEWS_COUNT = 50

    def __init__(
        self,
        token: str,
        seller_id: str = "",
        base_url: Optional[str] = None,
        request_timeout: float = 10.0,
        retry_total: int = 1,
        user_agent: str = "GGSelCardinal/0.1",
    ) -> None:
        # Первый аргумент — это GGSEL API Key (используется для подписи sign).
        self.api_key = token
        # Идентификатор продавца: Email ИЛИ числовой Seller ID. Что именно —
        # определяется автоматически в _login() (наличие "@" → Email).
        self.seller_id = seller_id
        self.email = seller_id if "@" in str(seller_id) else ""
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.request_timeout = request_timeout
        self.retry_total = max(0, int(retry_total))
        self.user_agent = user_agent

        self.profile: Optional[AccountProfile] = None
        self._access_token: str = ""
        # Токены по шлюзам (ключ — base URL): /api_sellers/api и /api раздельны.
        self._tokens: Dict[str, str] = {}
        self._session = requests.Session()
        self._lock = threading.RLock()
        self._authorized = False
        # Хост, на котором реально отвечает раздел debates (диалоги). Определяется
        # при первом успешном get_chats() и далее используется для сообщений/отправки.
        self._debates_base = ""
        # Сырое содержимое ответа /apilogin — из него берём название магазина.
        self._login_content: Dict[str, Any] = {}
        # Отчёт последней проверки адресов чатов (для диагностики в панели).
        self._last_chat_probe: List[str] = []
        # Анти-эхо: что бот сам недавно отправил (chat_id -> [(ts, текст)]).
        self._recent_sent: Dict[str, list] = {}
        # Кэш email покупателя по id заказа/диалога (id_i) из списка чатов.
        self._email_by_id: Dict[str, str] = {}

        # Автоповторы на уровне HTTP-адаптера: сеть может кратковременно
        # обрываться (RemoteDisconnected / timeout), особенно через прокси/VPN.
        if Retry is not None and self.retry_total > 0:
            retry = Retry(
                total=self.retry_total,
                connect=self.retry_total,
                read=self.retry_total,
                backoff_factor=1.5,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)

    # ------------------------------------------------------------------ #
    # Низкоуровневый транспорт
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        with_token: bool = True,
        _retry: bool = True,
        base: Optional[str] = None,
    ) -> Any:
        """Выполняет запрос. access-token передаётся параметром ?token=."""
        login_base = (base or self.base_url).rstrip("/")
        url = f"{login_base}{endpoint}"
        query: Dict[str, Any] = dict(params or {})
        if with_token:
            query["token"] = self._token_for(login_base)

        request_headers = self._headers()
        if headers:
            request_headers.update(headers)

        with self._lock:
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=request_headers,
                    params=query,
                    json=json_body,
                    timeout=self.request_timeout,
                )
            except requests.RequestException as exc:
                # Сетевые сбои (обрыв соединения / таймаут): даём ещё одну попытку
                # после короткой паузы, прежде чем пробрасывать ошибку наверх.
                if _retry:
                    time.sleep(1.0)
                    return self._request(
                        method, endpoint, params=params, json_body=json_body,
                        headers=headers, with_token=with_token, _retry=False, base=base,
                    )
                raise RequestFailedError(url, None, str(exc)) from exc

        # Истёкший токен — пробуем переавторизоваться один раз.
        if response.status_code in (401, 403) and with_token and _retry:
            self._tokens.pop(login_base, None)
            self._login(login_base)
            return self._request(
                method, endpoint, params=params, json_body=json_body,
                headers=headers, with_token=with_token, _retry=False, base=base,
            )
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

    def _request_v2(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        _retry: bool = True,
    ) -> Any:
        """Запрос Seller API V2 с API Key в заголовке Authorization."""
        root = self.base_url.split("/api_sellers/", 1)[0].rstrip("/")
        url = root + "/api_sellers/v2" + endpoint
        headers = self._headers()
        headers.update({"Authorization": self.api_key, "locale": "ru"})
        with self._lock:
            try:
                response = self._session.request(
                    method, url, headers=headers, params=dict(params or {}),
                    json=json_body, timeout=self.request_timeout,
                )
            except requests.RequestException as exc:
                if _retry:
                    time.sleep(1.0)
                    return self._request_v2(
                        method, endpoint, params=params, json_body=json_body, _retry=False,
                    )
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
    def _unwrap_list(payload: Any, *keys: str) -> List[Dict[str, Any]]:
        """Извлекает список из ответа, учитывая разные форматы обёртки."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            for key in ("content", "data", "rows", "result"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    for nested in keys:
                        inner = value.get(nested)
                        if isinstance(inner, list):
                            return inner
        return []

    def _host_with(self, suffix: str) -> str:
        """Схема+хост из base_url с добавленным путём suffix."""
        parts = urlsplit(self.base_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{suffix}"
        return self.base_url.rstrip("/")

    def _request_raw(
        self,
        method: str,
        endpoint: str,
        *,
        base: str,
        params: Optional[Dict[str, Any]] = None,
        with_token: bool = True,
    ) -> Dict[str, Any]:
        """Низкоуровневый запрос БЕЗ исключений — для пробы/диагностики.

        Возвращает dict: url, status (int|None), text (сырой ответ),
        json (разобранный либо None), token_error.
        """
        login_base = (base or self.base_url).rstrip("/")
        url = f"{login_base}{endpoint}"
        query: Dict[str, Any] = dict(params or {})
        token_error = ""
        if with_token:
            try:
                query["token"] = self._token_for(login_base)
            except GGSelApiError as exc:
                token_error = str(exc)
        request_headers = self._headers()
        try:
            with self._lock:
                response = self._session.request(
                    method, url, headers=request_headers, params=query,
                    json=None, timeout=self.request_timeout,
                )
        except requests.RequestException as exc:
            return {"url": url, "status": None, "text": str(exc), "json": None, "token_error": token_error}
        text = ""
        try:
            text = response.text or ""
        except Exception:
            text = ""
        data = None
        try:
            if response.content:
                data = response.json()
        except ValueError:
            data = None
        return {
            "url": url, "status": response.status_code, "text": text,
            "json": data, "token_error": token_error,
        }

    # ------------------------------------------------------------------ #
    # Авторизация
    # ------------------------------------------------------------------ #
    def _token_for(self, login_base: str) -> str:
        """Возвращает (и при необходимости получает) токен для конкретного шлюза.

        У GGSEL раздел debates (/api) и каталог/заказы (/api_sellers/api) — разные
        шлюзы, и токен одного не принимается другим (именно поэтому чаты давали 401
        при рабочих заказах). Поэтому логинимся отдельно на каждом шлюзе.
        """
        token = self._tokens.get(login_base)
        if token:
            return token
        try:
            return self._login(login_base)
        except GGSelApiError:
            # У этого шлюза может не быть своего /apilogin — пробуем основной токен.
            main_base = self.base_url.rstrip("/")
            if login_base != main_base:
                return self._token_for(main_base)
            raise

    def _login(self, login_base: Optional[str] = None) -> str:
        """Получает access-token через /apilogin (sign = SHA256(api_key + timestamp)).

        login_base задаёт шлюз, у которого берём токен. По умолчанию — основной
        (base_url). Для чатов передаётся debates-хост (корень + /api).
        """
        base = (login_base or self.base_url).rstrip("/")
        timestamp = str(int(time.time()))
        sign = hashlib.sha256(f"{self.api_key}{timestamp}".encode("utf-8")).hexdigest()
        body: Dict[str, Any] = {"timestamp": timestamp, "sign": sign}
        identifier = str(self.seller_id or self.email or "").strip()
        if "@" in identifier:
            # Привязка по Email (пошагово: Email → API Key).
            body["email"] = identifier
        elif identifier.isdigit():
            body["seller_id"] = int(identifier)
        elif identifier:
            body["seller_id"] = identifier
        payload = self._request("POST", self.EP_LOGIN, json_body=body, with_token=False, base=base)
        token = ""
        content: Dict[str, Any] = {}
        if isinstance(payload, dict):
            token = payload.get("token") or payload.get("access_token") or ""
            if isinstance(payload.get("content"), dict):
                content = payload["content"]
                if not token:
                    token = content.get("token") or content.get("access_token") or ""
            # Если входили по Email — достаём числовой seller_id из ответа,
            # т.к. он нужен для заказов/товаров (seller-last-sales, seller-goods).
            found_id = (
                payload.get("seller_id") or payload.get("id_seller") or payload.get("id")
                or content.get("seller_id") or content.get("id_seller") or content.get("id")
            )
            if found_id and str(found_id).strip().isdigit():
                self.seller_id = str(found_id).strip()
        # Название магазина берём из ответа ОСНОВНОГО шлюза.
        if base == self.base_url.rstrip("/"):
            if content:
                self._login_content = content
            elif isinstance(payload, dict):
                self._login_content = payload
        if not token:
            raise UnauthorizedError()
        token = str(token)
        self._tokens[base] = token
        self._access_token = token
        logger.info("Получен access-token GGSEL (шлюз=%s, seller=%s)", base, self.seller_id or self.email)
        return token

    def authorize(self) -> AccountProfile:
        """Проверяет ключ и выполняет вход. Возвращает профиль продавца."""
        self._login()
        login = self._login_content if isinstance(self._login_content, dict) else {}
        shop_name = ""
        for key in ("shop_name", "name_shop", "shopname", "seller_name", "name_seller", "shop", "store", "nik"):
            if login.get(key):
                shop_name = str(login.get(key))
                break
        self.profile = AccountProfile.from_raw({
            "seller_id": self.seller_id,
            "username": self.seller_id,
            "shop_name": shop_name or str(self.seller_id or self.email or "GGSEL магазин"),
        })
        self._authorized = True
        logger.info("Авторизация успешна: seller_id=%s", self.seller_id)
        return self.profile

    @property
    def is_authorized(self) -> bool:
        return self._authorized

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #
    def _debates_root(self) -> str:
        """Базовый URL раздела debates (чаты).

        Подтверждено ответом сервера GGSEL: чаты (debates) работают под
        базовым хостом Seller API ``/api_sellers/api`` (HTTP 200), так же,
        как авторизация/лоты/заказы. Вариант ``/api`` на корне хоста
        отдаёт 401 и остаётся лишь запасным кандидатом.
        Хост берётся из конфига [GGSEL] base_url — ничего не зашито намертво.
        """
        return self.base_url.rstrip("/")

    def get_shop_name(self) -> str:
        """Название магазина продавца (best-effort).

        Ищем в ответе авторизации; если нет — пробуем из списка товаров.
        Названия полей у GGSEL/Digiseller разнятся, поэтому сканируем по подстрокам.
        """
        from GGSelAPI.common import utils
        candidates = ["shop_name", "name_shop", "shopname", "seller_name", "name_seller", "shop", "store", "nik"]
        # 1) из ответа авторизации
        for src_dict in (self._login_content,):
            if isinstance(src_dict, dict) and src_dict:
                for key in candidates:
                    val = src_dict.get(key)
                    if val:
                        return utils.as_str(val)
                scanned = utils.scan(src_dict, ["shop", "store", "name_seller", "seller_name", "nik"], exclude=["id", "url", "link"])
                if scanned:
                    return utils.as_str(scanned)
        # 2) из первого товара (в карточке часто есть имя продавца/магазина)
        try:
            lots = self.get_lots(max_items=1, rows_per_page=1)
            if lots and isinstance(lots[0].raw, dict):
                raw = lots[0].raw
                for key in candidates:
                    if raw.get(key):
                        return utils.as_str(raw.get(key))
                scanned = utils.scan(raw, ["shop", "store", "seller_name", "name_seller", "nik"], exclude=["id", "url", "link", "product"])
                if scanned:
                    return utils.as_str(scanned)
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------ #
    # Чаты (debates)
    # ------------------------------------------------------------------ #
    def _chat_base_candidates(self) -> List[str]:
        """Кандидаты базовых адресов для раздела debates (чаты).

        Поддержка указала путь /api/debates/v2/chats, но разные аккаунты/
        инсталляции GGSEL могут размещать его по-разному. Поэтому пробуем
        несколько вариантов и запоминаем рабочий.
        """
        candidates: List[str] = []
        seen = set()
        for base in (
            self._debates_base,                  # ранее найденный рабочий
            self._debates_root(),                # https://host/api_sellers/api (ПОДТВЕРЖДЕНО: HTTP 200)
            self.base_url,                       # https://host/api_sellers/api
            self._host_with("/api"),             # https://host/api (запасной)
            self._host_with("/api_sellers"),     # https://host/api_sellers
            self._host_with(""),                 # https://host
        ):
            if not base:
                continue
            base = base.rstrip("/")
            if base not in seen:
                seen.add(base)
                candidates.append(base)
        return candidates

    def _collect_chats(self, base: str) -> List[Chat]:
        """Постранично собирает диалоги с уже проверенного базового адреса."""
        from GGSelAPI.common import utils as _u
        chats: List[Chat] = []
        seen_ids = set()
        for page in range(1, self.CHATS_MAX_PAGES + 1):
            params = {"pagesize": self.CHATS_PAGE_SIZE, "page": page}  # без filter_new: берём все диалоги
            payload = self._request("GET", self.EP_CHATS, params=params, base=base)
            items = self._unwrap_list(payload, "chats", "items")
            if not items:
                break
            for item in items:
                chat = Chat.from_raw(item)
                if chat.id and chat.id in seen_ids:
                    continue
                if chat.id:
                    seen_ids.add(chat.id)
                    if chat.buyer_name and "@" in chat.buyer_name:
                        self._email_by_id[chat.id] = chat.buyer_name
                chats.append(chat)
            total_pages = 1
            if isinstance(payload, dict):
                total_pages = _u.as_int(
                    payload.get("cnt_pages") or payload.get("pages") or payload.get("total_pages"), 1
                ) or 1
            if page >= total_pages or len(items) < self.CHATS_PAGE_SIZE:
                break
        return chats

    def get_chats(self) -> List[Chat]:
        """Возвращает список диалогов с покупателями (debates).

        Автоматически пробует несколько кандидатных адресов и запоминает тот,
        где сервер ответил 200. По каждому адресу сохраняет результат (HTTP-код
        / тело) в self._last_chat_probe — это видно в панели «Проверка подключения».
        """
        probe: List[str] = []
        last_status: Optional[int] = None
        for base in self._chat_base_candidates():
            res = self._request_raw(
                "GET", self.EP_CHATS, base=base,
                params={"pagesize": self.CHATS_PAGE_SIZE, "page": 1},
            )
            status = res["status"]
            last_status = status if isinstance(status, int) else last_status
            if res["token_error"]:
                probe.append(f"{res['url']} — токен: {res['token_error'][:60]}")
            if status == 200 and res["json"] is not None:
                items = self._unwrap_list(res["json"], "chats", "items")
                probe.append(f"{res['url']} — HTTP 200, диалогов={len(items)} ✅")
                self._debates_base = base
                self._last_chat_probe = probe
                return self._collect_chats(base)
            snippet = " ".join((res["text"] or "").split())[:90]
            probe.append(f"{res['url']} — HTTP {status} {snippet}".rstrip())
        self._last_chat_probe = probe
        # Ни один адрес не ответил 200 — сообщаем понятную ошибку.
        if last_status in (401, 403):
            raise UnauthorizedError()
        raise RequestFailedError(
            self._debates_root() + self.EP_CHATS, last_status,
            "Ни один из кандидатных адресов чатов не ответил 200.",
        )

    def get_chat_messages(self, chat_id: str, limit: int = 30, newer: bool = False) -> List[Message]:
        """Возвращает сообщения диалога по номеру заказа (id_i).

        newer=True — запрашивает только новые/неполученные сообщения (параметр API
        ``newer=1``). Иначе берём последние ``limit`` штук. API отдаёт
        сообщения по возрастанию id, поэтому дополнительно сортируем по id —
        последний элемент списка всегда самый свежий.
        """
        params = {"id_i": chat_id}
        if newer:
            params["newer"] = 1
        else:
            params["count"] = limit
        payload = self._request("GET", self.EP_CHAT_MESSAGES, params=params, base=self._debates_base or self._debates_root())
        items = self._unwrap_list(payload, "messages", "items")
        messages = [Message.from_raw(chat_id, item, seller_id=self.seller_id) for item in items]

        def _mid(m: Message) -> int:
            try:
                return int(m.id)
            except (TypeError, ValueError):
                return 0

        messages.sort(key=_mid)
        return messages

    def send_message(self, chat_id: str, text: str) -> None:
        """Отправляет сообщение в диалог заказа (id_i)."""
        self._request(
            "POST", self.EP_SEND_MESSAGE,
            params={"id_i": chat_id},
            json_body={"message": text},
            base=self._debates_base or self._debates_root(),
        )
        # Запоминаем отправленный текст — чтобы НЕ среагировать на своё же эхо.
        self._remember_sent(str(chat_id), text)

    @staticmethod
    def _norm_text(text: str) -> str:
        """Нормализует текст для сравнения «это наше же сообщение?».

        Площадка может вернуть наш же текст чуть иначе: иные вариационные
        селекторы эмодзи, без markdown-разметки, другие пробелы. Чистим всё это,
        чтобы надёжно узнавать собственное эхо (приветствие/автоответ).
        """
        s = text or ""
        for junk in ("\ufe0f", "\ufe0e", "\u200d", "\u200b", "\u200c", "\u00a0"):
            s = s.replace(junk, "")
        for ch in ("*", "_", "~", "`", ">"):
            s = s.replace(ch, "")
        return " ".join(s.split()).lower()

    def _remember_sent(self, chat_id: str, text: str) -> None:
        norm = self._norm_text(text)
        if not norm:
            return
        bucket = self._recent_sent.setdefault(str(chat_id), [])
        bucket.append((time.time(), norm))
        cutoff = time.time() - 3600
        bucket[:] = [(ts, t) for (ts, t) in bucket if ts >= cutoff][-40:]

    def was_recently_sent(self, chat_id: str, text: str) -> bool:
        """True, если этот текст бот сам отправлял в этот диалог недавно."""
        norm = self._norm_text(text)
        if not norm:
            return False
        for _ts, t in self._recent_sent.get(str(chat_id), []):
            if t == norm:
                return True
            # Длинные сообщения (приветствие) площадка может обрезать в превью —
            # считаем эхом, если один текст является началом/частью другого.
            if len(norm) >= 24 and (norm in t or t in norm):
                return True
        return False

    def email_for(self, order_id: str) -> str:
        """Email покупателя по id заказа/диалога, если видели его в списке чатов."""
        return self._email_by_id.get(str(order_id), "")

    def mark_seen(self, chat_id: str) -> None:
        """Помечает сообщения диалога прочитанными."""
        try:
            self._request("POST", self.EP_SEEN, params={"id_i": chat_id}, base=self._debates_base or self._debates_root())
        except UnauthorizedError:
            raise
        except Exception:
            logger.debug("Не удалось пометить чат %s прочитанным.", chat_id)

    # ------------------------------------------------------------------ #
    # Заказы (seller-last-sales)
    # ------------------------------------------------------------------ #
    def get_orders(self) -> List[Order]:
        """Возвращает последние продажи (заказы)."""
        payload = self._request(
            "GET", self.EP_ORDERS,
            params={"seller_id": self.seller_id, "top": self.ORDERS_TOP, "group": "false"},
            headers={"locale": "ru"},
        )
        items = self._unwrap_list(payload, "sales", "orders", "items")
        return [Order.from_raw(item) for item in items]

    # ------------------------------------------------------------------ #
    # Товары (seller-goods) — лоты продавца для автопривязки
    # ------------------------------------------------------------------ #
    def get_lots(self, max_items: int = 500, rows_per_page: int = 200) -> List[LotShortcut]:
        """Возвращает товары (лоты) продавца через POST /seller-goods.

        Постранично собирает все товары и возвращает их карточками LotShortcut.
        Этот список используется для привязки автовыдачи по кнопке (как «слоты»
        продавца) — без ручного ввода ID.
        """
        seller = int(self.seller_id) if str(self.seller_id).isdigit() else self.seller_id
        lots: List[LotShortcut] = []
        page = 1
        while len(lots) < max_items:
            body = {
                "id_seller": seller,
                "page": page,
                "rows": rows_per_page,
                "currency": "RUB",
                "order_col": "name",
                "order_dir": "asc",
                "lang": "ru-RU",
                # В кабинете «Объявления» должны отображаться и активные, и
                # скрытые/снятые лоты. Раньше API запрашивался с 0, а затем
                # результат ещё раз фильтровался — поэтому пользователь видел
                # только 1–2 опубликованных позиции.
                "show_hidden": 1,
            }
            payload = self._request("POST", self.EP_GOODS, json_body=body)
            items = self._unwrap_list(payload, "rows", "products", "goods", "items")
            if not items:
                break
            lots.extend(LotShortcut.from_raw(item) for item in items)
            meta = payload if isinstance(payload, dict) else {}
            for key in ("data", "content", "result"):
                if isinstance(meta.get(key), dict):
                    meta = meta[key]
                    break
            try:
                total_pages = int(
                    meta.get("pages") or meta.get("cnt_pages") or meta.get("total_pages") or 0
                )
            except (TypeError, ValueError):
                total_pages = 0
            # По спецификации seller-goods размер страницы задаёт сам GGSEL,
            # а поле `pages` сообщает реальное число страниц. Нельзя завершать
            # обход по len(items) < rows_per_page: именно это раньше обрезало
            # список до двух объявлений.
            if total_pages and page >= total_pages:
                break
            if not total_pages and len(items) < rows_per_page:
                break
            page += 1
        # Убираем только реальные дубликаты страниц. Скрытые и выключенные
        # позиции оставляем: это панель продавца, а не публичная витрина.
        unique: List[LotShortcut] = []
        seen = set()
        for lot in lots:
            key = str(lot.id)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(lot)
        return unique[:max_items]

    # ------------------------------------------------------------------ #
    # Управление объявлениями — Seller API V2
    # ------------------------------------------------------------------ #
    def get_offer(self, offer_id: int) -> Dict[str, Any]:
        payload = self._request_v2("GET", "/offers/" + str(int(offer_id)))
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return dict(payload["data"])
        return dict(payload) if isinstance(payload, dict) else {}

    def patch_offer(self, offer_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        if not changes:
            return self.get_offer(offer_id)
        payload = self._request_v2(
            "PATCH", "/offers/" + str(int(offer_id)), json_body=dict(changes),
        )
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return dict(payload["data"])
        return dict(payload) if isinstance(payload, dict) else {}

    def set_offer_active(self, offer_id: int, active: bool) -> Dict[str, Any]:
        endpoint = "/offers/batch_activate" if active else "/offers/batch_pause"
        payload = self._request_v2(
            "POST", endpoint, json_body={"offer_ids": [int(offer_id)]},
        )
        return dict(payload) if isinstance(payload, dict) else {}

    def delete_offer(self, offer_id: int) -> Dict[str, Any]:
        payload = self._request_v2(
            "POST", "/offers/batch_delete", json_body={"offer_ids": [int(offer_id)]},
        )
        return dict(payload) if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ #
    # Отзывы (reviews)
    # ------------------------------------------------------------------ #
    def get_reviews(self) -> List[Review]:
        """Возвращает отзывы покупателей (раздел Reviews)."""
        payload = self._request(
            "GET", self.EP_REVIEWS,
            params={
                "seller_id": self.seller_id,
                "type": "all",
                "page": 1,
                "rows": self.REVIEWS_COUNT,
                "lang": "ru-RU",
            },
            headers={"locale": "ru-RU"},
        )
        items = self._unwrap_list(payload, "reviews", "items")
        return [Review.from_raw(item) for item in items]
