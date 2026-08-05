from __future__ import annotations

import logging
import os

from ggsel_bot.config import Settings
from ggsel_bot.core import SingleBotCore


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> int:
    configure_logging()
    try:
        settings = Settings.from_env()
        SingleBotCore(settings).run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.getLogger("singlebot").exception("Fatal startup error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
