# GGSEL Clean Plugin Host v1.2.0

Чистый однопользовательский Telegram-бот для Bothost. В репозитории нет готовых плагинов, подписок, тарифов, Platega, профилей продавцов и мультитенантной панели.

## Что умеет ядро

- подключается к GGSEL Seller API;
- отслеживает новые заказы, сообщения, чаты и изменения статусов;
- загружает плагины из папки `plugins/` и постоянной папки `/app/data/plugins`;
- принимает плагины через Telegram документом `.py`;
- принимает ZIP-пакеты с плагином и его изображениями/ресурсами;
- хранит настройки плагинов в `/app/data/configs`;
- автоматически показывает каждый работающий плагин отдельной кнопкой на главной панели;
- позволяет включать, выключать, обновлять и удалять плагины.

## Совместимость

Поддерживаются интерфейсы основной сборки GGSEL Unified / GGSEL Cardinal:

- `register(core)` и `setup(core)`;
- `settings(core, chat_id)` и `settings_page(core, chat_id)`;
- `core.bus`, `core.events`, `core.add_handler(...)`;
- `BIND_TO_PRE_INIT`, `BIND_TO_INIT`, `BIND_TO_NEW_CHAT`, `BIND_TO_NEW_ORDER`, `BIND_TO_NEW_MESSAGE`, `BIND_TO_LAST_CHAT_MESSAGE_CHANGED`, `BIND_TO_ORDER_STATUS_CHANGED`;
- `core.telegram.register_callback`, `register_state`, `register_media_state`, `set_state`;
- `core.api`, `core.account`, `core.send_message`;
- импорты `ggsel_bot.*` и `ggsel_cardinal.*`.

Офлайн-проверка выполнена с четырьмя плагинами исходной сборки: FazerCards, KOSell, SMMPrime и Auto Price Manager. Их регистрация прошла без изменения файлов. Реальные внешние операции требуют рабочих ключей GGSEL и поставщиков.
