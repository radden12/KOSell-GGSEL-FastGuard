from __future__ import annotations

import hashlib
import io
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid as uuidlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("singlebot.plugins")
_UUID_NS = uuidlib.UUID("00000000-0000-0000-0000-00000000beef")
MAX_PLUGIN_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_UNPACKED_BYTES = 40 * 1024 * 1024
MAX_PACKAGE_FILES = 250


@dataclass
class PluginInfo:
    filename: str
    path: Path
    source: str = "bundled"
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    uuid: str = ""
    enabled: bool = True
    pinned: bool = False
    loaded: bool = False
    has_settings: bool = False
    updated_at: str = ""
    error: str = ""
    module: Any = field(default=None, repr=False)


class PluginManager:
    """Менеджер встроенных и загруженных через Telegram плагинов.

    Встроенные файлы берутся из репозитория ``plugins/``. Пользовательские
    плагины хранятся в ``/app/data/plugins`` и переживают пересборку Bothost.
    Файл из /app/data с тем же именем имеет приоритет над репозиторием.
    """

    def __init__(self, core, bundled_dir: Path, user_dir: Path, state_dir: Path):
        self.core = core
        self.bundled_dir = Path(bundled_dir)
        self.user_dir = Path(user_dir)
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "plugins.json"
        self.trash_dir = self.user_dir / ".trash"
        self.plugins: List[PluginInfo] = []
        self.bundled_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- state
    def _read_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    def _write_state(self, state: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        current = state if state is not None else self._read_state()
        for info in self.plugins:
            item = dict(current.get(info.filename) or {})
            item.update({
                "enabled": bool(info.enabled),
                "pinned": bool(info.pinned),
                "removed": bool(item.get("removed", False)),
            })
            current[info.filename] = item
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(current, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    # --------------------------------------------------------------- scanning
    @staticmethod
    def _is_plugin_file(path: Path) -> bool:
        return (
            path.is_file()
            and path.suffix.casefold() == ".py"
            and path.name != "__init__.py"
            and not path.name.startswith("_")
        )

    def _scan_paths(self) -> Dict[str, Tuple[Path, str]]:
        found: Dict[str, Tuple[Path, str]] = {}
        for path in sorted(self.bundled_dir.glob("*.py")):
            if self._is_plugin_file(path):
                found[path.name] = (path, "bundled")
        for path in sorted(self.user_dir.glob("*.py")):
            if self._is_plugin_file(path):
                found[path.name] = (path, "user")
        return found

    def load_all(self) -> None:
        state = self._read_state()
        candidates: List[PluginInfo] = []
        for filename, (path, source) in self._scan_paths().items():
            st = dict(state.get(filename) or {})
            if bool(st.get("removed", False)) and source == "bundled":
                continue
            info = PluginInfo(
                filename=filename,
                path=path,
                source=source,
                enabled=bool(st.get("enabled", True)),
                pinned=bool(st.get("pinned", False)),
                uuid=str(uuidlib.uuid5(_UUID_NS, filename)),
            )
            self._read_metadata(info)
            candidates.append(info)

        # Защита от нескольких копий одного плагина с одинаковым UUID.
        winners: Dict[str, PluginInfo] = {}
        for info in candidates:
            key = info.uuid or info.filename.casefold()
            previous = winners.get(key)
            if previous is None:
                winners[key] = info
                continue
            preferred = self._prefer(info, previous)
            rejected = previous if preferred is info else info
            winners[key] = preferred
            rejected.enabled = False
            rejected.loaded = False
            rejected.error = "Дубликат плагина: используется файл " + preferred.filename

        self.plugins = sorted(candidates, key=lambda x: (not x.pinned, x.name.casefold(), x.filename.casefold()))
        for info in self.plugins:
            if not info.enabled or info.error.startswith("Дубликат плагина"):
                continue
            self._register(info)

        self._write_state(state)
        log.info(
            "Plugins found=%s loaded=%s bundled=%s user=%s",
            len(self.plugins),
            sum(1 for x in self.plugins if x.loaded),
            sum(1 for x in self.plugins if x.source == "bundled"),
            sum(1 for x in self.plugins if x.source == "user"),
        )

    @staticmethod
    def _prefer(left: PluginInfo, right: PluginInfo) -> PluginInfo:
        if left.source != right.source:
            return left if left.source == "user" else right
        try:
            return left if left.path.stat().st_mtime >= right.path.stat().st_mtime else right
        except OSError:
            return left

    def refresh(self) -> None:
        """Обновляет список файлов без регистрации новых обработчиков."""
        known = {x.filename: x for x in self.plugins}
        state = self._read_state()
        refreshed: List[PluginInfo] = []
        for filename, (path, source) in self._scan_paths().items():
            st = dict(state.get(filename) or {})
            if bool(st.get("removed", False)) and source == "bundled":
                continue
            current = known.get(filename)
            if current and current.path == path:
                refreshed.append(current)
                continue
            info = PluginInfo(
                filename=filename,
                path=path,
                source=source,
                enabled=bool(st.get("enabled", True)),
                pinned=bool(st.get("pinned", False)),
                uuid=str(uuidlib.uuid5(_UUID_NS, filename)),
            )
            self._read_metadata(info)
            refreshed.append(info)
        self.plugins = sorted(refreshed, key=lambda x: (not x.pinned, x.name.casefold(), x.filename.casefold()))

    # -------------------------------------------------------------- importing
    def _install_aliases(self) -> None:
        sys.modules.setdefault("ggsel_cardinal", importlib.import_module("ggsel_bot"))
        aliases = {
            "ggsel_cardinal.events": "ggsel_bot.events",
            "ggsel_cardinal.api": "ggsel_bot.api",
            "ggsel_cardinal.storage": "ggsel_bot.storage",
            "ggsel_cardinal.core": "ggsel_bot.core",
            "ggsel_cardinal.plugins": "ggsel_bot.plugin_manager",
            "ggsel_cardinal.ui": "ggsel_bot.panel",
            "ggsel_cardinal.telegram": "ggsel_bot.panel",
        }
        for alias, target in aliases.items():
            sys.modules.setdefault(alias, importlib.import_module(target))

    def _import_module(self, info: PluginInfo):
        self._install_aliases()
        try:
            stamp = str(info.path.stat().st_mtime_ns)
        except OSError:
            stamp = str(time.time_ns())
        digest = hashlib.sha1((str(info.path.resolve()) + stamp).encode("utf-8")).hexdigest()[:16]
        module_name = f"singlebot_plugin_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, info.path)
        if spec is None or spec.loader is None:
            raise ImportError("Не удалось создать модуль плагина")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _read_metadata(self, info: PluginInfo) -> None:
        try:
            info.updated_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.path.stat().st_mtime))
            module = self._import_module(info)
            self._apply_metadata(info, module)
        except Exception as exc:  # noqa: BLE001
            info.error = str(exc)
            info.loaded = False
            log.exception("Plugin metadata failed: %s", info.filename)

    def _apply_metadata(self, info: PluginInfo, module: Any) -> None:
        info.module = module
        info.name = str(getattr(module, "NAME", info.path.stem) or info.path.stem)
        info.version = str(getattr(module, "VERSION", "1.0.0") or "1.0.0")
        info.description = str(getattr(module, "DESCRIPTION", "") or "")
        info.author = str(getattr(module, "AUTHOR", getattr(module, "CREDITS", "")) or "")
        custom_uuid = str(getattr(module, "UUID", "") or "").strip()
        if custom_uuid:
            info.uuid = custom_uuid
        info.has_settings = callable(getattr(module, "settings", None)) or callable(
            getattr(module, "settings_page", None)
        )

        # Старые плагины с относительными *_PATH должны писать в /app/data/configs.
        for attr in list(vars(module)):
            if not attr.endswith("_PATH"):
                continue
            value = getattr(module, attr, None)
            if isinstance(value, str) and value and not os.path.isabs(value):
                setattr(module, attr, str(self.state_dir / os.path.basename(value)))

    def _register(self, info: PluginInfo) -> None:
        try:
            module = info.module or self._import_module(info)
            self._apply_metadata(info, module)
            entry = getattr(module, "register", None)
            if not callable(entry):
                entry = getattr(module, "setup", None)
            if callable(entry):
                entry(self.core)
            elif not self._register_legacy_bindings(module):
                raise RuntimeError(
                    "У плагина нет register(core), setup(core) или legacy BIND_TO_* обработчиков"
                )
            info.loaded = True
            info.error = ""
            log.info("Plugin loaded: %s v%s (%s)", info.name, info.version, info.filename)
        except Exception as exc:  # noqa: BLE001
            info.loaded = False
            info.error = str(exc)
            log.exception("Plugin %s failed to load", info.filename)

    def _register_legacy_bindings(self, module: Any) -> bool:
        """Поддержка старых плагинов, экспортирующих списки BIND_TO_* без setup."""
        mapping = {
            "BIND_TO_PRE_INIT": self.core.BIND_TO_PRE_INIT,
            "BIND_TO_NEW_ORDER": self.core.BIND_TO_NEW_ORDER,
            "BIND_TO_NEW_MESSAGE": self.core.BIND_TO_NEW_MESSAGE,
            "BIND_TO_LAST_CHAT_MESSAGE_CHANGED": self.core.BIND_TO_LAST_CHAT_MESSAGE_CHANGED,
            "BIND_TO_ORDER_STATUS_CHANGED": self.core.BIND_TO_ORDER_STATUS_CHANGED,
        }
        registered = False
        for attr, bind in mapping.items():
            value = getattr(module, attr, None)
            handlers: Iterable[Any]
            if callable(value):
                handlers = [value]
            elif isinstance(value, (list, tuple, set)):
                handlers = value
            else:
                continue
            for handler in handlers:
                if not callable(handler):
                    continue
                registered = True
                if attr == "BIND_TO_PRE_INIT":
                    self._call_legacy_handler(handler, None)
                    continue

                def wrapped(core, event, fn=handler):
                    return self._call_legacy_handler(fn, event)

                self.core.add_handler(bind, wrapped)
        return registered

    def _call_legacy_handler(self, handler: Any, event: Any) -> Any:
        try:
            return handler(self.core, event)
        except TypeError as first:
            try:
                return handler(event)
            except TypeError:
                if event is None:
                    return handler(self.core)
                raise first

    # --------------------------------------------------------------- public API
    def list_infos(self) -> List[PluginInfo]:
        self.refresh()
        return list(self.plugins)

    def loaded(self) -> List[PluginInfo]:
        return [item for item in self.plugins if item.loaded]

    def find(self, filename: str) -> Optional[PluginInfo]:
        return next((item for item in self.plugins if item.filename == filename), None)

    def get(self, filename: str) -> Optional[PluginInfo]:
        return self.find(filename)

    def get_by_uuid(self, plugin_uuid: str) -> Optional[PluginInfo]:
        return next((item for item in self.plugins if item.uuid == str(plugin_uuid)), None)

    def pinned_plugins(self) -> List[PluginInfo]:
        return [item for item in self.plugins if item.pinned]

    def open_settings(self, info_or_filename: Any, chat_id: int):
        info = info_or_filename if isinstance(info_or_filename, PluginInfo) else self.find(str(info_or_filename))
        if not info or not info.loaded or info.module is None:
            return None
        fn = getattr(info.module, "settings", None)
        if not callable(fn):
            fn = getattr(info.module, "settings_page", None)
        return fn(self.core, chat_id) if callable(fn) else None

    @staticmethod
    def _safe_plugin_name(filename: str) -> str:
        base = os.path.basename(str(filename or "")).strip()
        base = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_.()\- ]+", "_", base)
        if not base.casefold().endswith(".py"):
            base = (base or "plugin") + ".py"
        if base.startswith("_") or base == "__init__.py":
            base = "p_" + base.lstrip("_")
        return base[:180]

    def install_package(self, filename: str, content: Any) -> Tuple[bool, str, Optional[PluginInfo]]:
        """Install either a single .py plugin or a safe .zip plugin package."""
        name = str(filename or "").strip()
        if name.casefold().endswith(".py"):
            return self.install_plugin(name, content)
        if name.casefold().endswith(".zip"):
            return self._install_zip_package(name, content)
        return False, "❌ Поддерживаются только файлы <code>.py</code> и <code>.zip</code>.", None

    def _install_zip_package(self, filename: str, content: Any) -> Tuple[bool, str, Optional[PluginInfo]]:
        data = content if isinstance(content, (bytes, bytearray)) else bytes(content or b"")
        data = bytes(data)
        if not data or len(data) > MAX_PACKAGE_BYTES:
            return False, "❌ ZIP-пакет должен быть не больше 20 МБ.", None
        try:
            archive = zipfile.ZipFile(io.BytesIO(data), "r")
        except (zipfile.BadZipFile, OSError) as exc:
            return False, "❌ Архив повреждён: " + str(exc), None

        members = [item for item in archive.infolist() if not item.is_dir()]
        if not members or len(members) > MAX_PACKAGE_FILES:
            return False, "❌ В архиве должно быть от 1 до 250 файлов.", None
        total = sum(max(0, int(item.file_size or 0)) for item in members)
        if total > MAX_PACKAGE_UNPACKED_BYTES:
            return False, "❌ Распакованный пакет больше 40 МБ.", None

        raw_names = [Path(item.filename.replace("\\", "/")) for item in members]
        for item, rel in zip(members, raw_names):
            parts = [part for part in rel.parts if part not in {"", "."}]
            if rel.is_absolute() or ".." in parts:
                return False, "❌ В ZIP найден небезопасный путь: " + item.filename, None
            # Unix symlink bits in external_attr.
            mode = (item.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                return False, "❌ Символические ссылки в пакетах не поддерживаются.", None

        # Strip one common wrapper directory, as GitHub ZIPs usually contain it.
        common_prefix = ""
        first_parts = [list(path.parts) for path in raw_names if path.parts]
        if first_parts and all(len(parts) > 1 for parts in first_parts):
            candidate = first_parts[0][0]
            if all(parts[0] == candidate for parts in first_parts):
                common_prefix = candidate

        prepared: List[Tuple[zipfile.ZipInfo, Path, bytes]] = []
        plugin_files: List[str] = []
        try:
            for item, rel in zip(members, raw_names):
                parts = list(rel.parts)
                if common_prefix and parts and parts[0] == common_prefix:
                    parts = parts[1:]
                if not parts:
                    continue
                target_rel = Path(*parts)
                if target_rel.name in {".DS_Store", "Thumbs.db"} or "__MACOSX" in target_rel.parts:
                    continue
                payload = archive.read(item)
                if target_rel.suffix.casefold() == ".py":
                    try:
                        source = payload.decode("utf-8-sig")
                        compile(source, str(target_rel), "exec")
                    except (UnicodeDecodeError, SyntaxError) as exc:
                        return False, "❌ Ошибка Python-файла <code>" + str(target_rel) + "</code>: " + str(exc), None
                    if len(target_rel.parts) == 1 and target_rel.name != "__init__.py" and not target_rel.name.startswith("_"):
                        plugin_files.append(target_rel.name)
                prepared.append((item, target_rel, payload))
        finally:
            archive.close()

        if not plugin_files:
            return False, "❌ В корне ZIP-пакета не найден запускаемый файл плагина <code>.py</code>.", None

        stage = self.user_dir / (".install_" + uuidlib.uuid4().hex)
        stage.mkdir(parents=True, exist_ok=False)
        try:
            for _item, rel, payload in prepared:
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            # Copy package contents atomically per file. Existing assets are replaced,
            # unrelated plugins remain untouched.
            for source in sorted(stage.rglob("*")):
                if not source.is_file():
                    continue
                rel = source.relative_to(stage)
                target = self.user_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                shutil.copyfile(source, tmp)
                os.replace(tmp, target)
        except OSError as exc:
            return False, "❌ Не удалось установить ZIP-пакет: " + str(exc), None
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        state = self._read_state()
        for plugin_file in plugin_files:
            state[plugin_file] = {
                **dict(state.get(plugin_file) or {}),
                "enabled": True,
                "removed": False,
            }
        self._write_state(state)
        self.refresh()
        first = self.find(plugin_files[0])
        names = ", ".join(plugin_files[:6])
        return (
            True,
            "✅ ZIP-пакет установлен. Плагины: <code>" + names + "</code>. Бот перезапускается.",
            first,
        )

    def install_plugin(self, filename: str, content: Any) -> Tuple[bool, str, Optional[PluginInfo]]:
        if not str(filename or "").casefold().endswith(".py"):
            return False, "❌ Нужен файл с расширением <code>.py</code>.", None
        data = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
        data = bytes(data)
        if not data or len(data) > MAX_PLUGIN_BYTES:
            return False, "❌ Размер плагина должен быть от 1 байта до 8 МБ.", None
        try:
            source = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return False, "❌ Плагин должен быть сохранён в UTF-8: " + str(exc), None
        safe = self._safe_plugin_name(filename)
        try:
            compile(source, safe, "exec")
        except SyntaxError as exc:
            return False, "❌ Синтаксическая ошибка: " + str(exc), None

        path = self.user_dir / safe
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(source)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            return False, "❌ Не удалось сохранить плагин: " + str(exc), None

        state = self._read_state()
        state[safe] = {
            **dict(state.get(safe) or {}),
            "enabled": True,
            "removed": False,
        }
        self._write_state(state)
        info = PluginInfo(filename=safe, path=path, source="user", enabled=True)
        info.uuid = str(uuidlib.uuid5(_UUID_NS, safe))
        self._read_metadata(info)
        self.refresh()
        if info.error:
            return (
                False,
                "⚠️ Файл сохранён, но при проверке возникла ошибка: <code>" + info.error + "</code>",
                info,
            )
        return True, "✅ Плагин сохранён. Бот перезапускается и подключит его автоматически.", info

    def set_enabled(self, filename: str, enabled: bool) -> bool:
        info = self.find(filename)
        if not info:
            return False
        state = self._read_state()
        item = dict(state.get(filename) or {})
        item["enabled"] = bool(enabled)
        item["removed"] = False
        state[filename] = item
        info.enabled = bool(enabled)
        self._write_state(state)
        return True

    def set_pinned(self, filename: str, pinned: bool) -> bool:
        info = self.find(filename)
        if not info:
            return False
        state = self._read_state()
        item = dict(state.get(filename) or {})
        item["pinned"] = bool(pinned)
        state[filename] = item
        info.pinned = bool(pinned)
        self._write_state(state)
        return True

    def remove_plugin(self, filename: str) -> Tuple[bool, str]:
        info = self.find(filename)
        if not info:
            return False, "Плагин не найден."

        # Плагин из /app/data удаляется физически, встроенный скрывается через
        # постоянное состояние, чтобы не вернуться после очередного деплоя.
        if info.source == "user" and info.path.exists():
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            target = self.trash_dir / (
                time.strftime("%Y%m%d_%H%M%S_") + info.path.name
            )
            try:
                shutil.move(str(info.path), str(target))
            except OSError as exc:
                return False, "Не удалось удалить файл: " + str(exc)

        state = self._read_state()
        item = dict(state.get(filename) or {})
        item.update({"enabled": False, "removed": True})
        state[filename] = item
        self._write_state(state)
        self.plugins = [x for x in self.plugins if x.filename != filename]
        return True, "Плагин удалён. После перезапуска его обработчики полностью отключатся."
