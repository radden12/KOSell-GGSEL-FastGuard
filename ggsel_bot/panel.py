from __future__ import annotations

import html
import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from telebot import TeleBot, types

log = logging.getLogger("singlebot.telegram")
APP_UPLOAD_STATE = "__app_plugin_upload__"
MAX_MSG_LEN = 3900


class BotPanel:
    """Личная Telegram-панель и совместимый интерфейс для плагинов Cardinal."""

    def __init__(self, core):
        self.core = core
        self.cardinal = core
        self.config = core.config
        self.owner_id = int(core.config.owner_id)
        self.bot = TeleBot(core.config.bot_token, parse_mode="HTML", threaded=True, num_threads=8)

        # Совместимый формат: chat_id -> {"action": str, "buffer": dict}.
        self._states: Dict[int, Dict[str, Any]] = {}
        self._state_handlers: Dict[str, Callable[[int, str], None]] = {}
        self._media_handlers: Dict[str, Callable[[int, Any], None]] = {}
        self._plugin_callbacks: List[Tuple[str, Callable]] = []
        self._callback_busy: Dict[Tuple[int, str], float] = {}
        self._lock = threading.RLock()
        self._callback_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="plugin-callback")
        self._finalized = False
        self._register_base_handlers()

    # ---------------------------------------------------------------- utility
    def is_admin(self, chat_id: int) -> bool:
        return int(chat_id) == self.owner_id

    def _get_state(self, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._states.get(int(chat_id))
            return dict(state) if isinstance(state, dict) else None

    def _clear_state(self, chat_id: int) -> None:
        with self._lock:
            self._states.pop(int(chat_id), None)

    def register_callback(self, prefix: str, callback: Callable) -> None:
        prefix = str(prefix or "")
        if not prefix:
            raise ValueError("Префикс callback не может быть пустым")
        with self._lock:
            self._plugin_callbacks[:] = [item for item in self._plugin_callbacks if item[0] != prefix]
            self._plugin_callbacks.append((prefix, callback))
            self._plugin_callbacks.sort(key=lambda item: len(item[0]), reverse=True)

    def register_state(self, state: str, handler: Callable[[int, str], None]) -> None:
        with self._lock:
            self._state_handlers[str(state)] = handler

    def register_media_state(self, state: str, handler: Callable[[int, Any], None]) -> None:
        with self._lock:
            self._media_handlers[str(state)] = handler

    def set_state(self, chat_id: int, state: str, buffer: Optional[Dict] = None) -> None:
        if not self.is_admin(chat_id):
            return
        for hook in list(getattr(self.core, "input_claim_hooks", []) or []):
            try:
                hook(int(chat_id))
            except Exception:
                log.debug("input_claim_hook failed", exc_info=True)
        with self._lock:
            self._states[int(chat_id)] = {
                "action": str(state),
                "buffer": dict(buffer or {}),
            }

    def _safe_send(self, chat_id: int, text: Any, **kwargs):
        if not self.is_admin(chat_id):
            return None
        kwargs.pop("_force_new", None)
        raw = str(text or "")
        chunks = self._split(raw, MAX_MSG_LEN)
        result = None
        for index, chunk in enumerate(chunks):
            current = dict(kwargs)
            if index < len(chunks) - 1:
                current.pop("reply_markup", None)
            result = self._send_chunk_with_retry(chat_id, chunk, current)
        return result

    def _send_chunk_with_retry(self, chat_id: int, text: str, kwargs: Dict[str, Any]):
        last_error: Optional[Exception] = None
        for attempt in range(3):
            current = dict(kwargs)
            try:
                return self.bot.send_message(chat_id, text, **current)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    current.pop("parse_mode", None)
                    try:
                        return self.bot.send_message(chat_id, text, parse_mode=None, **current)
                    except Exception as fallback_exc:  # noqa: BLE001
                        last_error = fallback_exc
                time.sleep(0.35 * (attempt + 1))
        log.warning("Telegram send failed chat=%s: %s", chat_id, last_error)
        return None

    def notify_admins(self, text: str, kind: str = "plugin", **kwargs) -> None:
        if self.config.is_notify_enabled(self.owner_id, kind):
            self._safe_send(self.owner_id, text, **kwargs)

    def send_notification(self, text: Optional[str] = None, *args, **kwargs) -> None:
        value = text if text is not None else kwargs.get("text", "")
        self.notify_admins(str(value), kind="plugin")

    def send_document(
        self,
        chat_id: int,
        content: Any,
        *,
        filename: str = "report.txt",
        caption: str = "",
    ):
        if not self.is_admin(chat_id):
            return None
        data = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
        stream = io.BytesIO(bytes(data))
        stream.name = filename
        try:
            return self.bot.send_document(chat_id, stream, caption=caption or None)
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram document send failed %s: %s", filename, exc)
            try:
                body = bytes(data).decode("utf-8", "replace")
                return self._safe_send(chat_id, "📄 <b>" + self._esc(filename) + "</b>\n\n" + body)
            except Exception:
                return None

    # --------------------------------------------------------------- handlers
    def _register_base_handlers(self) -> None:
        @self.bot.message_handler(commands=["start"])
        def start(message):
            if not self.is_admin(message.chat.id):
                self._deny(message.chat.id)
                return
            self._clear_state(message.chat.id)
            self.send_navigation_keyboard(message.chat.id)
            self.send_main_menu(message.chat.id)

        @self.bot.message_handler(commands=["menu"])
        def menu(message):
            if self.is_admin(message.chat.id):
                self._clear_state(message.chat.id)
                self.send_main_menu(message.chat.id)

        @self.bot.message_handler(commands=["plugins"])
        def plugins(message):
            if self.is_admin(message.chat.id):
                self._clear_state(message.chat.id)
                self.send_plugins(message.chat.id)

        @self.bot.message_handler(commands=["cancel"])
        def cancel(message):
            if not self.is_admin(message.chat.id):
                return
            self._clear_state(message.chat.id)
            self.send_main_menu(message.chat.id, "✅ Ввод отменён.")

        @self.bot.callback_query_handler(func=lambda call: str(call.data or "").startswith("app:"))
        def app_callback(call):
            chat_id = int(call.message.chat.id)
            if not self.is_admin(chat_id):
                self._answer(call, "Нет доступа", alert=True)
                return
            self._answer(call)
            try:
                self._handle_app_callback(call, str(call.data or "")[4:])
            except Exception as exc:  # noqa: BLE001
                log.exception("App callback failed: %s", exc)
                self._safe_send(chat_id, "❌ Ошибка панели: <code>" + self._esc(exc) + "</code>")

        # Кнопки «В панель» в старых/текущих плагинах.
        @self.bot.callback_query_handler(
            func=lambda call: str(call.data or "") in {
                "menu:main", "menu:plugins", "set:plugins", "store:home"
            }
        )
        def legacy_navigation(call):
            chat_id = int(call.message.chat.id)
            if not self.is_admin(chat_id):
                self._answer(call, "Нет доступа", alert=True)
                return
            self._answer(call)
            self._clear_state(chat_id)
            if str(call.data or "") == "menu:main":
                self.send_main_menu(chat_id)
            else:
                self.send_plugins(chat_id)

        # Должен быть зарегистрирован ДО плагинов: FazerCards имеет собственный
        # общий document-handler, который иначе перехватывал загрузку .py.
        @self.bot.message_handler(
            content_types=["document"],
            func=lambda message: self.is_admin(message.chat.id)
            and self._state_action(message.chat.id) == APP_UPLOAD_STATE,
        )
        def app_plugin_document(message):
            self._handle_plugin_upload(message)

    def finalize_handlers(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        # Прямые callback handlers плагинов, зарегистрированные через bot.*,
        # уже стоят выше. Этот диспетчер обслуживает panel.register_callback().
        @self.bot.callback_query_handler(func=lambda call: self.is_admin(call.message.chat.id))
        def plugin_callback_router(call):
            data = str(call.data or "")
            callback = self._find_plugin_callback(data)
            if callback is None:
                self._answer(call, "Кнопка устарела. Откройте меню заново.", alert=False)
                return
            key = (int(call.message.chat.id), data)
            now = time.monotonic()
            with self._lock:
                busy_until = self._callback_busy.get(key, 0.0)
                if busy_until > now:
                    self._answer(call, "⏳ Уже выполняется")
                    return
                self._callback_busy[key] = now + 90.0
            self._answer(call)

            def run_callback():
                try:
                    callback(call)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Plugin callback failed data=%s: %s", data, exc)
                    self._safe_send(
                        int(call.message.chat.id),
                        "❌ Кнопка плагина завершилась ошибкой:\n<code>" + self._esc(exc) + "</code>",
                    )
                finally:
                    with self._lock:
                        self._callback_busy.pop(key, None)

            self._callback_pool.submit(run_callback)

        @self.bot.message_handler(content_types=["text"], func=lambda message: self.is_admin(message.chat.id))
        def state_router(message):
            chat_id = int(message.chat.id)
            text = str(message.text or "").strip()
            if self._handle_navigation_text(chat_id, text):
                return
            state = self._get_state(chat_id)
            if not state:
                self.send_main_menu(chat_id)
                return
            action = str(state.get("action") or "")
            handler = self._state_handlers.get(action)
            if handler is None:
                self._clear_state(chat_id)
                self._safe_send(chat_id, "⚠️ Диалог устарел. Откройте нужный раздел заново.")
                self.send_main_menu(chat_id)
                return
            try:
                handler(chat_id, text)
            except Exception as exc:  # noqa: BLE001
                log.exception("Plugin state handler failed action=%s: %s", action, exc)
                self._safe_send(chat_id, "❌ Ошибка обработки ввода: <code>" + self._esc(exc) + "</code>")

        @self.bot.message_handler(
            content_types=["photo", "document"],
            func=lambda message: self.is_admin(message.chat.id),
        )
        def media_router(message):
            chat_id = int(message.chat.id)
            state = self._get_state(chat_id)
            action = str((state or {}).get("action") or "")
            handler = self._media_handlers.get(action)
            if handler is not None:
                try:
                    handler(chat_id, message)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Plugin media handler failed: %s", exc)
                    self._safe_send(chat_id, "❌ Ошибка обработки файла: <code>" + self._esc(exc) + "</code>")

    # -------------------------------------------------------------- app menu
    def prepare_owner_menu(self) -> None:
        commands = [
            types.BotCommand("menu", "Главное меню"),
            types.BotCommand("plugins", "Управление плагинами"),
            types.BotCommand("cancel", "Отменить текущий ввод"),
        ]
        try:
            self.bot.set_my_commands(commands)
            self.bot.set_chat_menu_button(
                chat_id=self.owner_id,
                menu_button=types.MenuButtonCommands(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Unable to configure Telegram menu button: %s", exc)

    def send_navigation_keyboard(self, chat_id: int) -> None:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, row_width=2)
        kb.row(
            types.KeyboardButton("🏠 Главное меню"),
            types.KeyboardButton("🧩 Управление плагинами"),
        )
        self._safe_send(
            chat_id,
            "✅ <b>Быстрое меню включено.</b> Главная панель показывает установленные плагины автоматически.",
            reply_markup=kb,
        )

    def send_main_menu(self, chat_id: int, prefix: str = "") -> None:
        self.core.plugins.refresh()
        infos = [
            info for info in self.core.plugins.plugins
            if info.enabled and info.loaded
        ]
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for info in infos:
            label = self._plugin_button_label(info)
            keyboard.add(
                types.InlineKeyboardButton(label, callback_data=f"app:open:{info.uuid}")
            )
        keyboard.add(
            types.InlineKeyboardButton(
                "🧩 Управление плагинами",
                callback_data="app:plugins",
            )
        )

        if infos:
            body = (
                "Выберите нужный раздел. Список на этой панели обновляется "
                "автоматически после установки или удаления плагина."
            )
        else:
            body = (
                "Пока не установлен ни один плагин. Откройте управление "
                "плагинами и загрузите совместимый файл <code>.py</code> или пакет <code>.zip</code>."
            )
        text = (
            (prefix + "\n\n" if prefix else "")
            + "🤖 <b>GGSEL Control</b>\n"
            + "<i>Личная панель управления магазином</i>\n\n"
            + body
        )
        self._safe_send(chat_id, text, reply_markup=keyboard)

    def _plugin_button_label(self, info) -> str:
        name = str(info.name or info.filename or "Плагин").strip()
        clean = name[:46]
        known = clean.casefold()
        if any(token in known for token in ("fazercards", "steam gift")):
            icon = "🎮"
        elif "kosell" in known or "rent" in known or "аренд" in known:
            icon = "🔑"
        elif "price" in known or "цен" in known:
            icon = "💹"
        elif "smm" in known:
            icon = "📣"
        else:
            icon = "🧩"
        return f"{icon} {clean}"

    def send_plugins(self, chat_id: int) -> None:
        self.core.plugins.refresh()
        infos = list(self.core.plugins.plugins)
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton(
                "➕ Добавить или обновить плагин",
                callback_data="app:add_plugin",
            )
        )
        lines = [
            "🧩 <b>Управление плагинами</b>",
            "",
            "Здесь можно открыть, включить, выключить, обновить или удалить плагин.",
        ]
        if infos:
            lines.extend(["", f"Установлено: <b>{len(infos)}</b>"])
            for info in infos:
                if not info.enabled:
                    icon = "⚪️"
                    status = "выключен"
                elif info.loaded:
                    icon = "🟢"
                    status = "работает"
                else:
                    icon = "🔴"
                    status = "ошибка"
                keyboard.add(
                    types.InlineKeyboardButton(
                        f"{icon} {(info.name or info.filename)[:46]}",
                        callback_data=f"app:manage:{info.uuid}",
                    )
                )
                lines.append(
                    f"{icon} <b>{self._esc(info.name or info.filename)}</b> — {status}"
                )
        else:
            lines.extend(["", "Плагины ещё не установлены."])
        lines.extend([
            "",
            "Файлы, загруженные через Telegram, сохраняются в "
            "<code>/app/data/plugins</code> и не пропадают после пересборки Bothost.",
        ])
        keyboard.add(
            types.InlineKeyboardButton("🏠 Главное меню", callback_data="app:home")
        )
        self._safe_send(chat_id, "\n".join(lines), reply_markup=keyboard)

    def open_plugin(self, chat_id: int, identity: Any) -> None:
        info = self._resolve_plugin(identity)
        if not info:
            self._safe_send(chat_id, "❌ Плагин не найден.")
            self.send_plugins(chat_id)
            return
        if not info.enabled:
            self._safe_send(chat_id, "⚫️ Плагин выключен. Откройте ⚙️ управление и включите его.")
            self.send_plugin_card(chat_id, info)
            return
        if not info.loaded:
            self._safe_send(
                chat_id,
                "❌ Плагин не загрузился:\n<code>" + self._esc(info.error or "неизвестная ошибка") + "</code>",
            )
            self.send_plugin_card(chat_id, info)
            return
        try:
            result = self.core.plugins.open_settings(info, chat_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Plugin settings failed %s: %s", info.filename, exc)
            self._safe_send(chat_id, "❌ Ошибка настроек: <code>" + self._esc(exc) + "</code>")
            return
        if result is None:
            self.send_plugin_card(chat_id, info)
            return
        if isinstance(result, tuple):
            values = list(result) + [None]
            self._safe_send(chat_id, str(values[0]), reply_markup=values[1])
        else:
            self._safe_send(chat_id, str(result))

    def send_plugin_card(self, chat_id: int, info) -> None:
        status = "🟢 работает" if info.loaded and info.enabled else (
            "⚫️ выключен" if not info.enabled else "🔴 ошибка"
        )
        lines = [
            "⚙️ <b>" + self._esc(info.name or info.filename) + "</b>",
            "",
            "Статус: <b>" + status + "</b>",
            "Версия: <code>" + self._esc(info.version or "—") + "</code>",
            "Файл: <code>" + self._esc(info.filename) + "</code>",
            "Источник: <b>" + ("/app/data/plugins" if info.source == "user" else "репозиторий") + "</b>",
        ]
        if info.author:
            lines.append("Автор: " + self._esc(info.author))
        if info.description:
            lines.extend(["", self._esc(info.description)])
        if info.error:
            lines.extend(["", "⚠️ <code>" + self._esc(info.error) + "</code>"])
        kb = types.InlineKeyboardMarkup(row_width=1)
        if info.loaded and info.has_settings:
            kb.add(types.InlineKeyboardButton("⚙️ Открыть настройки", callback_data=f"app:open:{info.uuid}"))
        kb.add(
            types.InlineKeyboardButton(
                "⏸ Выключить" if info.enabled else "▶️ Включить",
                callback_data=f"app:toggle:{info.uuid}",
            )
        )
        kb.add(types.InlineKeyboardButton("🗑 Удалить плагин", callback_data=f"app:delete:{info.uuid}"))
        kb.add(types.InlineKeyboardButton("⬅️ Управление плагинами", callback_data="app:plugins"))
        self._safe_send(chat_id, "\n".join(lines), reply_markup=kb)

    def begin_plugin_upload(self, chat_id: int) -> None:
        self.set_state(chat_id, APP_UPLOAD_STATE, {})
        self._safe_send(
            chat_id,
            "➕ <b>Установка или обновление плагина</b>\n\n"
            "Отправьте плагин документом:\n"
            "• <code>.py</code> — один файл плагина;\n"
            "• <code>.zip</code> — плагин вместе с изображениями и другими файлами.\n\n"
            "Файлы проверятся и сохранятся в <code>/app/data/plugins</code>. "
            "После успешной установки бот применит изменения автоматически.\n\n"
            "Команда /cancel отменяет загрузку.",
        )

    def send_status(self, chat_id: int) -> None:
        ggsel = "✅ подключён" if self.core.ggsel_ready else "⚠️ ошибка подключения"
        plugins = sum(1 for item in self.core.plugins.plugins if item.loaded)
        errors = [item for item in self.core.plugins.plugins if item.enabled and not item.loaded]
        text = (
            "📊 <b>Статус личного бота</b>\n\n"
            f"GGSEL: <b>{ggsel}</b>\n"
            f"Плагинов загружено: <b>{plugins}/{len(self.core.plugins.plugins)}</b>\n"
            f"Ошибок плагинов: <b>{len(errors)}</b>\n"
            f"Хранилище: <code>{self.config.data_dir}</code>\n"
            f"Пользовательские плагины: <code>{self.config.user_plugins_dir}</code>\n"
            f"Опрос заказов: каждые <b>{self.config.poll_interval:g} сек.</b>"
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🧩 Плагины", callback_data="app:plugins"))
        kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="app:home"))
        self._safe_send(chat_id, text, reply_markup=kb)

    def send_migration_help(self, chat_id: int) -> None:
        self._safe_send(
            chat_id,
            "📥 Данные FazerCards уже должны находиться в <code>/app/data/configs</code>. "
            "Связки, заказы и шаблоны сохраняются независимо от обновлений репозитория.",
        )

    # ----------------------------------------------------------- app actions
    def _handle_app_callback(self, call, action: str) -> None:
        chat_id = int(call.message.chat.id)
        if action == "home":
            self._clear_state(chat_id)
            self.send_main_menu(chat_id)
        elif action in {"plugins", "plugins_refresh"}:
            self._clear_state(chat_id)
            self.send_plugins(chat_id)
        elif action == "add_plugin":
            self.begin_plugin_upload(chat_id)
        elif action == "status":
            self._clear_state(chat_id)
            self.send_status(chat_id)
        elif action == "migration":
            self.send_migration_help(chat_id)
        elif action == "restart":
            self._safe_send(chat_id, "♻️ Перезапускаю бота…")
            self.core.request_restart()
        elif action.startswith("plugin:"):  # совместимость v1.0.0
            self.open_plugin(chat_id, action.split(":", 1)[1])
        elif action.startswith("open:"):
            self.open_plugin(chat_id, action.split(":", 1)[1])
        elif action.startswith("manage:"):
            info = self._resolve_plugin(action.split(":", 1)[1])
            if info:
                self.send_plugin_card(chat_id, info)
            else:
                self.send_plugins(chat_id)
        elif action.startswith("toggle:"):
            identity = action.split(":", 1)[1]
            info = self._resolve_plugin(identity)
            if not info:
                self.send_plugins(chat_id)
                return
            new_enabled = not info.enabled
            self.core.plugins.set_enabled(info.filename, new_enabled)
            self._safe_send(
                chat_id,
                ("▶️ Плагин включён." if new_enabled else "⏸ Плагин выключен.")
                + " Применяю изменение перезапуском…",
            )
            self.core.request_restart()
        elif action.startswith("delete_yes:"):
            info = self._resolve_plugin(action.split(":", 1)[1])
            if not info:
                self.send_plugins(chat_id)
                return
            ok, message = self.core.plugins.remove_plugin(info.filename)
            self._safe_send(chat_id, ("✅ " if ok else "❌ ") + self._esc(message))
            if ok:
                self._safe_send(chat_id, "♻️ Перезапускаю бота, чтобы полностью отключить обработчики плагина…")
                self.core.request_restart()
            else:
                self.send_plugins(chat_id)
        elif action.startswith("delete:"):
            info = self._resolve_plugin(action.split(":", 1)[1])
            if not info:
                self.send_plugins(chat_id)
                return
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("🗑 Да, удалить", callback_data=f"app:delete_yes:{info.uuid}"))
            kb.add(types.InlineKeyboardButton("⬅️ Отмена", callback_data=f"app:manage:{info.uuid}"))
            note = (
                "Файл из <code>/app/data/plugins</code> будет перемещён в резервную корзину."
                if info.source == "user"
                else "Встроенный файл останется в репозитории, но будет постоянно скрыт и отключён."
            )
            self._safe_send(
                chat_id,
                "⚠️ <b>Удалить плагин?</b>\n\n<b>" + self._esc(info.name) + "</b>\n" + note
                + "\n\nКонфиги плагина в <code>/app/data/configs</code> автоматически не удаляются.",
                reply_markup=kb,
            )

    def _handle_plugin_upload(self, message) -> None:
        chat_id = int(message.chat.id)
        document = getattr(message, "document", None)
        filename = str(getattr(document, "file_name", "") or "")
        extension = filename.casefold().rsplit(".", 1)[-1] if "." in filename else ""
        if extension not in {"py", "zip"}:
            self._safe_send(
                chat_id,
                "❌ Нужен документ <code>.py</code> или <code>.zip</code>. Попробуйте снова.",
            )
            return
        try:
            file_info = self.bot.get_file(document.file_id)
            content = self.bot.download_file(file_info.file_path)
        except Exception as exc:  # noqa: BLE001
            self._safe_send(chat_id, "❌ Не удалось скачать файл: <code>" + self._esc(exc) + "</code>")
            return
        ok, message_text, _info = self.core.plugins.install_package(filename, content)
        self._clear_state(chat_id)
        self._safe_send(chat_id, message_text)
        if ok:
            self.core.request_restart(delay=1.8)
        else:
            self.send_plugins(chat_id)

    # --------------------------------------------------------------- routing
    def _find_plugin_callback(self, data: str) -> Optional[Callable]:
        with self._lock:
            callbacks = list(self._plugin_callbacks)
        for prefix, callback in callbacks:
            if data.startswith(prefix):
                return callback
        return None

    def _state_action(self, chat_id: int) -> str:
        state = self._get_state(chat_id)
        return str((state or {}).get("action") or "")

    def _handle_navigation_text(self, chat_id: int, text: str) -> bool:
        normalized = text.casefold()
        if normalized in {"🏠 меню", "🏠 главное меню", "меню", "/menu"}:
            self._clear_state(chat_id)
            self.send_main_menu(chat_id)
            return True
        if normalized in {
            "🧩 плагины", "🧩 управление плагинами", "плагины", "/plugins"
        }:
            self._clear_state(chat_id)
            self.send_plugins(chat_id)
            return True
        return False

    def _resolve_plugin(self, identity: Any):
        raw = str(identity or "")
        info = self.core.plugins.get_by_uuid(raw)
        if info:
            return info
        info = self.core.plugins.find(raw)
        if info:
            return info
        try:
            index = int(raw)
        except (TypeError, ValueError):
            return None
        return self.core.plugins.plugins[index] if 0 <= index < len(self.core.plugins.plugins) else None

    def _answer(self, call, text: str = "", alert: bool = False) -> None:
        try:
            self.bot.answer_callback_query(call.id, text=text or None, show_alert=alert)
        except Exception:
            pass

    def run_polling(self) -> None:
        self.bot.infinity_polling(
            skip_pending=True,
            timeout=30,
            long_polling_timeout=30,
            allowed_updates=["message", "callback_query"],
        )

    def _deny(self, chat_id: int) -> None:
        try:
            self.bot.send_message(chat_id, "⛔ Этот бот является личным и закрыт для других пользователей.")
        except Exception:
            pass

    @staticmethod
    def _split(text: str, limit: int):
        if len(text) <= limit:
            return [text]
        result = []
        rest = text
        while rest:
            cut = min(limit, len(rest))
            if cut < len(rest):
                pivot = rest.rfind("\n", 0, cut)
                if pivot > limit // 2:
                    cut = pivot
            result.append(rest[:cut])
            rest = rest[cut:].lstrip("\n")
        return result

    @staticmethod
    def _esc(value: Any) -> str:
        return html.escape(str(value or ""), quote=False)
