from __future__ import annotations

import compileall
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    ok = compileall.compile_dir(str(ROOT), quiet=1)
    required = ["run.py", "main.py", "requirements.txt", "ggsel_bot", "GGSelAPI", "plugins"]
    missing = [name for name in required if not (ROOT / name).exists()]
    if missing:
        print("MISSING:", ", ".join(missing))
        return 1
    print("OK: Python syntax and required project paths")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
