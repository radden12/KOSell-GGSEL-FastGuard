"""
GGSelAPI — самостоятельный клиент торговой площадки GGSEL.

Слой инкапсулирует HTTP-взаимодействие с площадкой и предоставляет ядру удобные
типизированные методы и модели. Ядро не должно работать с ``requests`` напрямую.

Модули:
- :class:`Account`     — seller-клиент (чаты/заказы). См. предупреждение в account.py.
- :class:`PartnerAPI`  — официальный партнёрский каталожный API (a.ggsel.com/partner).
"""
from GGSelAPI.account import Account
from GGSelAPI.partner import PartnerAPI
from GGSelAPI import types
from GGSelAPI.common import enums, exceptions

__all__ = ["Account", "PartnerAPI", "types", "enums", "exceptions"]
