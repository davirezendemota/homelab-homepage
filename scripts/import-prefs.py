#!/usr/bin/env python3
"""Importa preferências exportadas do localStorage para o SQLite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DB_PATH", str(ROOT / "data" / "homepage.db"))
sys.path.insert(0, str(ROOT))

from app import init_db, update_prefs  # noqa: E402


def load_payload(path: str | None) -> dict:
    source = sys.stdin if path in (None, "-") else open(path, encoding="utf-8")
    try:
        data = json.load(source)
    finally:
        if source is not sys.stdin:
            source.close()

    if "prefs" in data:
        return data["prefs"]
    if all(key in data for key in ("favorites", "hiddenContainers", "hiddenStacks", "collapsedStacks", "settings")):
        return data
    raise SystemExit("JSON inválido: esperado objeto prefs ou { prefs: ... }")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    prefs = load_payload(path)
    init_db()
    result = update_prefs(prefs)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
