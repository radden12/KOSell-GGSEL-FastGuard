# -*- coding: utf-8 -*-
"""Совместимый адаптер профессиональной панели к GGSEL Seller API v1/v2."""
import threading
import time
from typing import Any, Iterator, List, Optional
from GGSelAPI.account import Account
from .exceptions import GGSELError, ApiAuthError
from .models import Shop, Order, Customer, Message, Sender, Ad, Page

DEFAULT_BASE_URL = "https://seller.ggsel.com/api_sellers/api"

class GGSELAPI:
    """Имя сохранено только для совместимости с неизменённой первой панелью."""
    def __init__(self, base_url: str, api_key: str, seller_id: str = "", request_timeout: float = 8.0, retry_total: int = 0, **_):
        requested_base=(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        if requested_base in {"https://api.digiseller.ru/api", "https://api.digiseller.com/api"}:
            requested_base=DEFAULT_BASE_URL
        self.base_url=requested_base
        self.api_key=(api_key or "").strip()
        self.seller_id=str(seller_id or "").strip()
        self.account=Account(token=self.api_key, seller_id=self.seller_id, base_url=self.base_url, request_timeout=request_timeout, retry_total=retry_total)
        self._profile=None
        self._ads_cache=[]
        self._ads_cache_at=0.0
        self._ads_cache_ttl=60.0
        self._ads_cache_lock=threading.RLock()
        self._chat_index_at=0.0
        self._chat_index_ttl=60.0
        self._chat_by_invoice={}
        self._chat_index_lock=threading.RLock()
        self._orders_cache=[]
        self._orders_cache_at=0.0
        self._orders_cache_ttl=1.5
        self._orders_cache_lock=threading.RLock()

    def _wrap(self, fn):
        try: return fn()
        except Exception as exc: raise GGSELError(str(exc)) from exc

    def check(self) -> Shop:
        if not self.api_key or not self.seller_id:
            raise ApiAuthError("Укажите Email/Seller ID и API Key GGSEL.", status_code=401)
        try: self._profile=self.account.authorize()
        except Exception as exc: raise ApiAuthError("Ошибка авторизации GGSEL: "+str(exc),status_code=401) from exc
        self.seller_id=str(self.account.seller_id or self.seller_id)
        return Shop(id=int(self.seller_id) if self.seller_id.isdigit() else 0,
                    title=self._profile.shop_name or self._profile.username or "GGSEL магазин")

    def _ready(self):
        if not self.account.is_authorized: self.check()

    @staticmethod
    def _order(item) -> Order:
        created=item.created_at.isoformat() if getattr(item,"created_at",None) else None
        raw=item.raw or {}; product=raw.get("product") if isinstance(raw.get("product"),dict) else {}
        product_id=product.get("id") or raw.get("product_id") or raw.get("id_goods")
        original=str(getattr(item.status,"value",item.status) or "").lower()
        status="success" if original in {"success","completed","delivered","finished","closed","done"} else ("refunded" if "refund" in original else "work")
        buyer_name=str(item.buyer_name or "").strip()
        if buyer_name.casefold() in {"покупатель","buyer","customer","клиент","user","unknown","неизвестно","—"}:
            buyer_name=""
        return Order(id=int(item.id) if str(item.id).isdigit() else abs(hash(str(item.id)))%2147483647,
                     ad_id=int(product_id) if str(product_id or "").isdigit() else None, chat_id=int(item.chat_id) if str(item.chat_id).isdigit() else None,
                     customer=Customer(id=int(item.buyer_id) if str(item.buyer_id or "").isdigit() else 0,
                                       name=buyer_name),
                     status=status, created_at=created,
                     raw=dict(raw, _ggsel_id=str(item.id), lot_title=item.lot_title,
                              amount=item.amount, quantity=getattr(item, "quantity", 1),
                              buyer_id=item.buyer_id, buyer_name=item.buyer_name,
                              currency=item.currency,profit=item.profit))

    def get_orders(self, cursor: Optional[str]=None, statuses: Optional[List[Any]]=None) -> Page:
        self._ready(); now=time.monotonic()
        with self._orders_cache_lock:
            if self._orders_cache and now-self._orders_cache_at<self._orders_cache_ttl:
                items=list(self._orders_cache)
            else:
                items=[]
        if not items:
            items=[self._order(x) for x in self._wrap(self.account.get_orders)]
            with self._orders_cache_lock:
                self._orders_cache=list(items); self._orders_cache_at=time.monotonic()
        try:
            chat_index=self._load_chat_index()
            for order in items:
                chat=chat_index.get(str(order.id)) or chat_index.get(str(order.chat_id or ""))
                if not chat:
                    continue
                order.raw["web_chat_id"]=chat.get("web_chat_id")
                if not order.customer.name and chat.get("buyer_name"):
                    order.customer.name=chat["buyer_name"]
                if not order.customer.id and str(chat.get("buyer_id") or "").isdigit():
                    order.customer.id=int(chat["buyer_id"])
        except Exception:
            # Заказы остаются доступными даже при временной ошибке раздела чатов.
            pass
        return Page(items=items,has_more=False)
    def iter_orders(self,statuses=None,max_pages=20)->Iterator[Order]:
        yield from self.get_orders(statuses=statuses).items
    def get_order(self,order_id:int)->Order:
        for x in self.get_orders().items:
            if x.id==int(order_id): return x
        raise GGSELError("Заказ GGSEL не найден.")
    def order_work(self,order_id:int): raise GGSELError("GGSEL Seller API v1 не поддерживает перевод продажи в работу.")
    def order_confirm(self,order_id:int): raise GGSELError("GGSEL Seller API v1 не поддерживает подтверждение продажи продавцом.")
    def order_refund(self,order_id:int): raise GGSELError("Возврат выполняется в кабинете GGSEL.")

    def _load_chat_index(self,force:bool=False):
        now=time.monotonic()
        with self._chat_index_lock:
            if not force and self._chat_by_invoice and now-self._chat_index_at<self._chat_index_ttl:
                return dict(self._chat_by_invoice)
        chats=self._wrap(self.account.get_chats)
        index={}
        for chat in chats:
            raw=getattr(chat,"raw",{}) or {}
            invoice=raw.get("id_i") or raw.get("invoice_id") or raw.get("invoice") or chat.id
            # Веб-кабинет ожидает внутренний id_d, а не номер заказа id_i.
            web_id=raw.get("id_d") or raw.get("dialog_id") or raw.get("id_dialog")
            if not invoice:
                continue
            index[str(invoice)]={
                "web_chat_id":str(web_id) if web_id else "",
                "buyer_name":str(getattr(chat,"buyer_name","") or "").strip(),
                "buyer_id":str(getattr(chat,"buyer_id","") or "").strip(),
            }
        with self._chat_index_lock:
            self._chat_by_invoice=dict(index); self._chat_index_at=time.monotonic()
        return index

    def get_web_chat_id(self,invoice_id,force:bool=False)->str:
        try:
            item=self._load_chat_index(force=force).get(str(invoice_id)) or {}
            return str(item.get("web_chat_id") or "")
        except Exception:
            return ""

    def get_messages(self,chat_id:int,cursor:Optional[str]=None)->Page:
        self._ready(); raw=self._wrap(lambda:self.account.get_chat_messages(str(chat_id),limit=30))
        out=[]
        for x in raw:
            created=x.created_at.isoformat() if getattr(x,"created_at",None) else None
            source=getattr(x,"raw",{}) or {}; attachments=[]
            def flag(value):
                if isinstance(value,bool): return value
                if isinstance(value,(int,float)): return value!=0
                return str(value or "").strip().lower() in {"1","true","yes","on","да"}
            filename=str(source.get("filename") or source.get("file_name") or "")
            url=str(source.get("url") or source.get("file_url") or "")
            if flag(source.get("is_img")):
                attachments.append({"type":"image","filename":filename,"url":url})
            elif flag(source.get("is_file")) or filename or url:
                attachments.append({"type":"file","filename":filename,"url":url})
            out.append(Message(id=int(x.id) if str(x.id).isdigit() else abs(hash(str(x.id)))%2147483647,
                created_at=created,sender=Sender(id=0,name=x.author_name or ("Покупатель" if x.is_from_buyer else "Магазин"),type="client" if x.is_from_buyer else "seller"),
                is_read=True,text=x.text or "",attachments=attachments,options=source))
        return Page(items=out,has_more=False)
    def send_message(self,chat_id:int,text:str)->Message:
        self._ready(); self._wrap(lambda:self.account.send_message(str(chat_id),text))
        return Message(id=0,created_at=None,sender=Sender(0,"Магазин","seller"),is_read=True,text=text)

    @staticmethod
    def _ad(item)->Ad:
        raw=item.raw or {}; price=raw.get("price") or raw.get("price_rub") or 0
        try: price=float(price)
        except Exception: price=0
        return Ad(id=int(item.id) if str(item.id).isdigit() else abs(hash(str(item.id)))%2147483647,
                  title=item.title or "GGSEL лот",slug=None,type="ggsel",category_id=None,content=None,
                  status=("hidden" if getattr(item,"hidden",False) else "publish"),views=None,price_amount=price,base_amount=price,currency="RUB",
                  stock=None,has_chat=True,has_points=False,created_at=None,images=[],raw=dict(raw,_ggsel_id=str(item.id)))
    def invalidate_ads_cache(self):
        if not hasattr(self,"_ads_cache_lock"):
            self._ads_cache_lock=threading.RLock(); self._ads_cache=[]; self._ads_cache_at=0.0
        with self._ads_cache_lock:
            self._ads_cache=[]; self._ads_cache_at=0.0

    def get_ads(self,cursor:Optional[str]=None,force_refresh:bool=False)->Page:
        self._ready()
        now=time.monotonic()
        with self._ads_cache_lock:
            if not force_refresh and self._ads_cache and now-self._ads_cache_at<self._ads_cache_ttl:
                return Page(items=list(self._ads_cache),has_more=False)
        items=[self._ad(x) for x in self._wrap(self.account.get_lots)]
        with self._ads_cache_lock:
            self._ads_cache=list(items); self._ads_cache_at=time.monotonic()
        return Page(items=items,has_more=False)
    def iter_ads(self,only_visible=False,max_pages=50):
        for item in self.get_ads().items:
            if only_visible and item.status != "publish":
                continue
            yield item
    def get_ad(self,ad_id:int)->Ad:
        for x in self.get_ads().items:
            if x.id==int(ad_id):
                try:
                    details=self.account.get_offer(int(ad_id))
                    x.raw.update(details)
                    status=str(details.get("status") or "").lower()
                    if status in {"active","publish","published"}: x.status="publish"
                    elif status in {"paused","draft","hidden"}: x.status="hidden"
                    if details.get("price") is not None:
                        x.price_amount=float(details["price"])
                        x.base_amount=x.price_amount
                    if details.get("is_unlimited_quantity"):
                        x.stock=None
                    elif details.get("quantity") is not None:
                        x.stock=int(details["quantity"])
                except Exception as exc:
                    x.raw["_v2_error"]=str(exc)
                return x
        raise GGSELError("Лот GGSEL не найден.")

    def get_ad_settings(self,ad_id:int):
        self._ready(); return self._wrap(lambda:self.account.get_offer(int(ad_id)))

    def update_ad(self,ad_id:int,**changes):
        self._ready()
        allowed={"title_ru","title_en","description_ru","description_en","instructions_ru","instructions_en",
                 "price","currency","is_autoselling","category_id","min_quantity","max_quantity","quantity",
                 "is_unlimited_quantity","post_payment_url","delivery","pre_payment_settings","notification_settings"}
        body={key:value for key,value in changes.items() if key in allowed}
        if not body: raise GGSELError("Нет поддерживаемых полей для изменения.")
        result=self._wrap(lambda:self.account.patch_offer(int(ad_id),body)); self.invalidate_ads_cache(); return result

    def update_ad_price(self,ad_id:int,amount:float,discount=None):
        # Seller API V2 меняет базовую цену предложения. Отдельное поле скидки
        # в PATCH /offers/:id не предусмотрено документацией.
        return self.update_ad(ad_id,price=float(amount),currency="RUB")

    def publish_ad(self,ad_id:int):
        self._ready(); result=self._wrap(lambda:self.account.set_offer_active(int(ad_id),True)); self.invalidate_ads_cache(); return result

    def unpublish_ad(self,ad_id:int):
        self._ready(); result=self._wrap(lambda:self.account.set_offer_active(int(ad_id),False)); self.invalidate_ads_cache(); return result

    def delete_ad(self,ad_id:int):
        self._ready(); result=self._wrap(lambda:self.account.delete_offer(int(ad_id))); self.invalidate_ads_cache(); return result

    def add_ad_items(self,*_): raise GGSELError("Встроенный склад настраивается через раздел «Автовыдача».")
